"""Live HTTP tests for the crew dashboard API (crew/server/app.py), run against
the REAL dashboard that's already listening on 127.0.0.1:8788 (MorphDB app
'crew').

This intentionally does NOT start its own dashboard process — per
.claude/skills/feature-development's ground rules, `crew dashboard start/stop`
and `crew init` are unsafe to live-test (var/dashboard.pid is process-global,
not port-scoped), and the port is already bound by the live instance anyway.
Instead this drives the dashboard that's already running, using ONLY
test_dashapi_-prefixed agents that are ALWAYS created with launch:false (never
boots a real claude) and ALWAYS cleaned up, even on failure.

NEVER touch: the real agents (leads, builder, sales, AgentA, AgentB) or their
edges/homes/tmux sessions. Every helper that can mutate state asserts the
target name is test_dashapi_-prefixed and not in REAL_AGENTS before doing
anything. The namespaced prefix (not a bare "test_") matters: this app is
shared live by OTHER concurrently-running test suites (e.g. a CLI test writer
seeds its own test_cli_* agents in the same app) — a blanket "test_" sweep on
setUpModule would delete a sibling suite's in-flight fixtures.

    python3 -m unittest tests.test_dashboard_api   (from the repo root)
"""
import contextlib
import json
import os
import shutil
import sys
import time
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BASE = "http://127.0.0.1:" + os.environ.get("CREW_PORT", "8788")


@contextlib.contextmanager
def _pinned_app(app):
    """Pin $CREW_APP for the duration of a direct crew.graphstore call, then
    restore whatever was there before — see MalformedPendingRow._direct."""
    prev = os.environ.get("CREW_APP")
    os.environ["CREW_APP"] = app
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop("CREW_APP", None)
        else:
            os.environ["CREW_APP"] = prev
HOME_ROOT = "/tmp/crew_tests"
REAL_AGENTS = {"leads", "builder", "sales", "AgentA", "AgentB"}
# Namespaced (not a bare "test_"): the live app is shared by other concurrently
# running test suites, so cleanup must only ever touch OUR OWN fixtures.
NAME_PREFIX = "test_dashapi_"
RUN_ID = str(int(time.time()))


# --------------------------------------------------------------------------- #
# tiny HTTP helper (mirrors graphstore._req's shape: no external deps)
# --------------------------------------------------------------------------- #
def _req(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw.decode(errors="replace")


def get(path):
    return _req("GET", path)


def post(path, body=None):
    return _req("POST", path, body if body is not None else {})


def _assert_test_name(name):
    """Hard guard: every helper that can mutate state runs its target name
    through this first — never let a bug (or a copy-pasted real name) reach a
    real agent, and never touch a name outside OUR OWN namespace (a sibling
    test suite's test_cli_* fixtures live in this same app concurrently)."""
    if not name or not name.startswith(NAME_PREFIX) or name in REAL_AGENTS:
        raise AssertionError(f"refusing to operate on non-test_dashapi agent name {name!r}")
    return name


def _remove_agent(name):
    """Best-effort cleanup: never raises, never touches a non-test_dashapi_ name."""
    _assert_test_name(name)
    try:
        post("/api/agent/remove", {"name": name})
    except Exception:
        pass


def _home(name):
    _assert_test_name(name)
    return os.path.join(HOME_ROOT, name)


def setUpModule():
    os.makedirs(HOME_ROOT, exist_ok=True)
    # Best-effort sweep of OUR OWN agents orphaned by a prior crashed run. Only
    # ever touches our namespaced prefix — never the real graph, and never a
    # sibling test suite's fixtures that happen to be live in the same app.
    status, snap = get("/api/graph/snapshot")
    if status == 200 and snap and snap.get("ok"):
        for a in snap.get("agents", []):
            n = a.get("name") or ""
            if n.startswith(NAME_PREFIX):
                _remove_agent(n)


def tearDownModule():
    shutil.rmtree(HOME_ROOT, ignore_errors=True)


# --------------------------------------------------------------------------- #
# GET /api/graph/snapshot — shape
# --------------------------------------------------------------------------- #
class SnapshotShape(unittest.TestCase):
    def test_ok_and_top_level_shape(self):
        status, body = get("/api/graph/snapshot")
        self.assertEqual(status, 200)
        self.assertTrue(body.get("ok"), body)
        self.assertIsInstance(body.get("agents"), list)
        self.assertIsInstance(body.get("edges"), list)

    def test_real_agents_present(self):
        _, body = get("/api/graph/snapshot")
        names = {a.get("name") for a in body["agents"]}
        self.assertTrue(REAL_AGENTS.issubset(names),
                        f"expected {REAL_AGENTS} subset of {names}")

    def test_agent_fields_present(self):
        _, body = get("/api/graph/snapshot")
        by_name = {a["name"]: a for a in body["agents"]}
        a = by_name["builder"]
        for key in ("_guid", "name", "role", "home", "session", "status",
                   "alive", "live_status", "out_edges", "in_edges"):
            self.assertIn(key, a, f"agent missing field {key!r}: {a}")
        self.assertIsInstance(a["alive"], bool)
        self.assertIn(a["live_status"], ("idle", "working", "needs_input", "down"))

    def test_edge_fields_resolved(self):
        _, body = get("/api/graph/snapshot")
        edges = body["edges"]
        self.assertGreater(len(edges), 0,
                           "expected at least one pre-existing edge (leads/builder/sales seed data)")
        for e in edges:
            for key in ("_guid", "source", "target", "directed", "source_name", "target_name"):
                self.assertIn(key, e, f"edge missing field {key!r}: {e}")


# --------------------------------------------------------------------------- #
# POST /api/agent/create (launch:false) + /api/agent/remove
# --------------------------------------------------------------------------- #
class AgentCreateRemove(unittest.TestCase):
    def setUp(self):
        self._cleanup = []

    def tearDown(self):
        for n in self._cleanup:
            _remove_agent(n)

    def _mk_name(self, suffix):
        return _assert_test_name(f"{NAME_PREFIX}{suffix}_{RUN_ID}")

    def test_create_minimal_launch_false(self):
        name = self._mk_name("min")
        home = _home(name)
        status, body = post("/api/agent/create", {
            "name": name, "home": home, "launch": False, "launch_cmd": "true",
        })
        self._cleanup.append(name)
        self.assertEqual(status, 200)
        self.assertTrue(body.get("ok"), body)
        agent = body["agent"]
        self.assertEqual(agent["name"], name)
        self.assertEqual(os.path.realpath(agent["home"]), os.path.realpath(home))
        self.assertEqual(agent["status"], "idle")
        _, snap = get("/api/graph/snapshot")
        self.assertIn(name, {a["name"] for a in snap["agents"]})

    def test_create_duplicate_name_rejected(self):
        name = self._mk_name("dup")
        home = _home(name)
        status1, body1 = post("/api/agent/create", {
            "name": name, "home": home, "launch": False, "launch_cmd": "true"})
        self._cleanup.append(name)
        self.assertTrue(body1.get("ok"), body1)
        status2, body2 = post("/api/agent/create", {
            "name": name, "home": home + "2", "launch": False, "launch_cmd": "true"})
        self.assertEqual(status2, 200)
        self.assertFalse(body2.get("ok"))
        self.assertIn("already exists", body2.get("error", ""))

    def test_create_missing_name(self):
        status, body = post("/api/agent/create", {"home": "/tmp/crew_tests/nope", "launch": False})
        self.assertEqual(status, 200)
        self.assertFalse(body.get("ok"))
        self.assertEqual(body.get("error"), "name required")

    def test_create_invalid_name_rejected(self):
        name = NAME_PREFIX + "bad name!"   # namespaced + greppable, but the
        status, body = post("/api/agent/create", {           # regex must reject it
            "name": name, "home": "/tmp/crew_tests/badname", "launch": False})
        self.assertEqual(status, 200)
        self.assertFalse(body.get("ok"))
        self.assertIn("invalid agent name", body.get("error", ""))

    def test_remove_success(self):
        name = self._mk_name("rm")
        home = _home(name)
        _, cbody = post("/api/agent/create", {
            "name": name, "home": home, "launch": False, "launch_cmd": "true"})
        self.assertTrue(cbody.get("ok"), cbody)
        status, body = post("/api/agent/remove", {"name": name})
        self.assertEqual(status, 200)
        self.assertTrue(body.get("ok"), body)
        _, snap = get("/api/graph/snapshot")
        self.assertNotIn(name, {a["name"] for a in snap["agents"]})

    def test_remove_missing_name(self):
        status, body = post("/api/agent/remove", {})
        self.assertEqual(status, 200)
        self.assertFalse(body.get("ok"))
        self.assertEqual(body.get("error"), "name required")

    def test_remove_nonexistent(self):
        name = self._mk_name("ghost")
        status, body = post("/api/agent/remove", {"name": name})
        self.assertEqual(status, 200)
        self.assertFalse(body.get("ok"))
        self.assertIn("no such agent", body.get("error", ""))


# --------------------------------------------------------------------------- #
# POST /api/edge/create|update|delete — round trip + error shapes
# --------------------------------------------------------------------------- #
class EdgeCRUD(unittest.TestCase):
    def setUp(self):
        self.src_name = _assert_test_name(f"{NAME_PREFIX}edge_src_{RUN_ID}")
        self.tgt_name = _assert_test_name(f"{NAME_PREFIX}edge_tgt_{RUN_ID}")
        for n in (self.src_name, self.tgt_name):
            status, body = post("/api/agent/create", {
                "name": n, "home": _home(n), "launch": False, "launch_cmd": "true"})
            self.assertTrue(body.get("ok"), f"setup failed for {n}: {body}")

    def tearDown(self):
        # deleting the agents cascades their edges (graphstore.delete_agent)
        _remove_agent(self.src_name)
        _remove_agent(self.tgt_name)

    def test_create_update_delete_round_trip(self):
        # --- CREATE --- #
        status, body = post("/api/edge/create", {
            "source": self.src_name, "target": self.tgt_name,
            "label": "t-label", "conditions": ["when x happens"],
            "target_action": "do the thing", "reply_expected": True,
            "max_turns": 3, "directed": True,
        })
        self.assertEqual(status, 200)
        self.assertTrue(body.get("ok"), body)
        edge = body["edge"]
        guid = edge["_guid"]
        self.assertEqual(edge.get("label"), "t-label")
        self.assertEqual(edge.get("target_action"), "do the thing")
        self.assertTrue(edge.get("reply_expected"))
        self.assertEqual(int(edge.get("max_turns")), 3)
        self.assertTrue(edge.get("directed"))

        _, snap = get("/api/graph/snapshot")
        found = next((e for e in snap["edges"] if e["_guid"] == guid), None)
        self.assertIsNotNone(found, "created edge not present in snapshot")
        self.assertEqual(found.get("source_name"), self.src_name)
        self.assertEqual(found.get("target_name"), self.tgt_name)

        # --- UPDATE --- #
        status, body = post("/api/edge/update", {
            "guid": guid, "label": "t-label-2", "max_turns": 7, "directed": False,
        })
        self.assertEqual(status, 200)
        self.assertTrue(body.get("ok"), body)
        edge2 = body["edge"]
        self.assertEqual(edge2.get("label"), "t-label-2")
        self.assertEqual(int(edge2.get("max_turns")), 7)
        self.assertFalse(edge2.get("directed"))

        # --- DELETE --- #
        status, body = post("/api/edge/delete", {"guid": guid})
        self.assertEqual(status, 200)
        self.assertTrue(body.get("ok"), body)
        _, snap = get("/api/graph/snapshot")
        self.assertNotIn(guid, {e["_guid"] for e in snap["edges"]})

    def test_create_missing_agents(self):
        status, body = post("/api/edge/create", {
            "source": self.src_name, "target": NAME_PREFIX + "ghost_target_zzz"})
        self.assertEqual(status, 200)
        self.assertFalse(body.get("ok"))
        self.assertEqual(body.get("error"), "source and target must be existing agents")

    def test_create_self_loop_rejected(self):
        status, body = post("/api/edge/create", {
            "source": self.src_name, "target": self.src_name})
        self.assertEqual(status, 200)
        self.assertFalse(body.get("ok"))
        self.assertIn("cannot have an edge to itself", body.get("error", ""))

    def test_update_missing_guid(self):
        status, body = post("/api/edge/update", {"label": "x"})
        self.assertEqual(status, 200)
        self.assertFalse(body.get("ok"))
        self.assertEqual(body.get("error"), "guid required")

    def test_update_bad_guid(self):
        # WARNING (see report): MorphDB's PATCH /objects/edge/<guid> upserts a
        # nonexistent guid instead of 404ing, so this call actually CREATES a
        # real edge object in the live app with this guid. finally: always
        # delete it so the run never leaves that phantom object behind.
        fake_guid = f"edge_{NAME_PREFIX}fake_bad_guid_{RUN_ID}"
        try:
            status, body = post("/api/edge/update", {"guid": fake_guid, "label": "x"})
            self.assertEqual(status, 200)
            self.assertFalse(body.get("ok"))
            self.assertTrue(body.get("error"))   # exact wording not asserted, see report
        finally:
            post("/api/edge/delete", {"guid": fake_guid})

    def test_delete_missing_guid(self):
        status, body = post("/api/edge/delete", {})
        self.assertEqual(status, 200)
        self.assertFalse(body.get("ok"))
        self.assertEqual(body.get("error"), "guid required")

    def test_delete_bad_guid(self):
        fake_guid = f"edge_{NAME_PREFIX}fake_delete_guid_{RUN_ID}"
        status, body = post("/api/edge/delete", {"guid": fake_guid})
        self.assertEqual(status, 200)
        self.assertFalse(body.get("ok"))
        self.assertTrue(body.get("error"))


# --------------------------------------------------------------------------- #
# Error shapes: unknown routes + missing-param terminal endpoints
# --------------------------------------------------------------------------- #
class ErrorShapes(unittest.TestCase):
    def test_unknown_get_path(self):
        status, body = get("/api/nope")
        self.assertEqual(status, 404)
        self.assertEqual(body, {"error": "not found"})

    def test_unknown_post_path(self):
        status, body = post("/api/nope", {})
        self.assertEqual(status, 404)
        self.assertEqual(body, {"error": "not found"})

    def test_pty_stream_missing_target(self):
        status, body = get("/api/pty/stream")
        self.assertEqual(status, 200)
        self.assertFalse(body.get("ok"))
        self.assertEqual(body.get("error"), "t required")

    def test_pty_input_missing_id(self):
        status, body = post("/api/pty/input", {})
        self.assertEqual(status, 200)
        self.assertFalse(body.get("ok"))

    def test_pty_resize_missing_id(self):
        status, body = post("/api/pty/resize", {})
        self.assertEqual(status, 200)
        self.assertFalse(body.get("ok"))


# --------------------------------------------------------------------------- #
# WAVE 4 regression: a malformed pending row must 500 with a clean JSON error,
# never drop the connection. Found during independent verification: a
# hand-written (or otherwise corrupt) `graph_edit` row whose args.fields isn't
# even a dict makes crew.graphstore.update_edge's `dict(fields)` raise a bare
# ValueError deep inside guard.approve_pending's replay — before the fix,
# _pending_approve only caught gs.GraphError, so the request thread died with
# an uncaught exception and the client got a dropped connection (no response
# at all) instead of {"ok": false, "error": ...}. The dashboard process itself
# survived (ThreadingHTTPServer isolates the exception to one thread), but the
# request contract was broken. Uses the graphstore module directly (stdlib-
# only, no server dependency) to hand-craft the row against the SAME live
# "crew" app the dashboard is already serving; always deletes it after.
# --------------------------------------------------------------------------- #
from crew import config as _config, graphstore as gs  # noqa: E402


class MalformedPendingRow(unittest.TestCase):
    def _direct(self):
        """Every OTHER graph-editing write here goes through the live HTTP
        dashboard (a separate OS process, unaffected by this test process's
        os.environ). This test needs a direct crew.graphstore call instead (to
        hand-craft a malformed row the API has no way to create), and several
        sibling test MODULES pin $CREW_APP to a throwaway app as a top-level
        import-time side effect — which has already run by the time this test
        executes under `discover` (all modules are imported before any test
        runs). Pin explicitly to the dashboard's own app (config.DEFAULT_APP,
        "crew") for the duration of the direct calls so this test targets the
        SAME app the live dashboard is serving, regardless of import-order
        pollution; always restore whatever was there after."""
        return _pinned_app(_config.DEFAULT_APP)

    def test_approve_of_row_with_non_dict_fields_returns_clean_500_not_dropped_connection(self):
        with self._direct():
            edges = gs.list_objects("edge", limit=1)["objects"]
            if not edges:
                self.skipTest("no edge in the live app to reference (read-only reference only)")
            real_edge_guid = edges[0]["_guid"]
            row = gs.create_object("graph_edit", {
                "actor": "test_dashapi_malformed_ghost", "op": "update_edge",
                "args": {"guid": real_edge_guid, "fields": "max_turns"},  # string, not a dict
                "result": "pending", "reason": "test_dashapi malformed-row regression",
                "created_at": int(time.time()),
            })
        try:
            status, body = post("/api/pending/approve", {"guid": row["_guid"]})
            self.assertEqual(status, 500, f"expected a clean 500, got {status}: {body!r}")
            self.assertIsInstance(body, dict, f"expected JSON body, got {body!r}")
            self.assertFalse(body.get("ok"))
            self.assertIn("error", body)
            # the row must stay pending — never silently marked resolved by a
            # replay that blew up partway through
            with self._direct():
                refreshed = gs.get_object(row["_guid"])
            self.assertEqual(refreshed.get("result"), "pending")
        finally:
            with self._direct():
                try:
                    gs.delete_object("graph_edit", row["_guid"])
                except gs.GraphError:
                    pass


if __name__ == "__main__":
    unittest.main(verbosity=2)
