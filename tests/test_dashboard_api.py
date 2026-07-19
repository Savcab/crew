"""Live HTTP tests for the crew dashboard API (crew/server/app.py).

The module owns a unique loopback port, dashboard process, and throwaway
MorphDB app. It never restarts or authenticates against the operator's 8788
dashboard. It drives its isolated server using ONLY
test_dashapi_-prefixed agents that are ALWAYS created with launch:false (never
boots a real claude) and ALWAYS cleaned up, even on failure.

The suite provisions every graph row it asserts against, so it passes on a
brand-new empty installation as well as an established one. NEVER touch:
operator-owned agents, edges, homes, or tmux sessions. Every helper that can
mutate state asserts the target name is test_dashapi_-prefixed and not in the
legacy protected-name set before doing anything. The namespaced prefix (not a
bare "test_") matters: this app can be shared by concurrently-running suites.

    python3 tests/test_dashboard_api.py -v   (from the repo root)
"""
import contextlib
import http.cookiejar
import json
import os
import shutil
import subprocess
import sys
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from crew import config as _config, graphstore as gs, schema  # noqa: E402


PORT = str(24000 + (os.getpid() % 1000))
BASE = "http://127.0.0.1:" + PORT
TEST_APP = f"crewtest-dashboard-api-{os.getpid()}"
CAPFILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "var", f"dashboard-{PORT}.cap")
_COOKIE_JAR = http.cookiejar.CookieJar()
_AUTH_OPENER = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(_COOKIE_JAR))


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
RUN_ID = f"{int(time.time() * 1000)}_{os.getpid()}"
_SERVER_STARTED = False


def _server_env():
    env = dict(os.environ)
    for key in (
            "CREW_APP", "CREW_PROJECT", "CREW_ROOT", "CREW_PORT",
            "CREW_EXPAND_CMD", "EXPAND_STUB_MODE", "CREW_EXPAND_TIMEOUT"):
        env.pop(key, None)
    env.update({
        "CREW_APP": TEST_APP,
        "CREW_PROJECT": "default",
        "CREW_PORT": PORT,
        "MORPHDB_HOST": "127.0.0.1:18787",
    })
    return env


def _dashboard(action, check=False):
    return subprocess.run(
        [sys.executable, os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "bin", "crew"), "dashboard", action],
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        env=_server_env(), capture_output=True, text=True, timeout=30,
        check=check)


def _wait_dashboard(timeout=15):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            status, body = _req("GET", "/api/health")
            if (status == 200 and isinstance(body, dict)
                    and body.get("ok") is True
                    and body.get("service") == "crew-dashboard"
                    and body.get("port") == int(PORT)
                    and body.get("app") == TEST_APP):
                return
            last = (status, body)
        except (OSError, urllib.error.URLError) as error:
            last = error
        time.sleep(0.1)
    raise RuntimeError(f"isolated dashboard did not become healthy: {last!r}")


# --------------------------------------------------------------------------- #
# tiny HTTP helper (mirrors graphstore._req's shape: no external deps)
# --------------------------------------------------------------------------- #
def _req(method, path, body=None, opener=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if method == "POST":
        req.add_header("X-Crew-CSRF", "1")
    client = opener or _AUTH_OPENER
    try:
        with client.open(req, timeout=10) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as error:
        try:
            raw = error.read()
            try:
                return error.code, json.loads(raw)
            except Exception:
                return error.code, raw.decode(errors="replace")
        finally:
            error.close()


def get(path):
    return _req("GET", path)


def post(path, body=None):
    return _req("POST", path, body if body is not None else {})


def _bootstrap_operator_cookie():
    try:
        with open(CAPFILE) as fh:
            capability = fh.read().strip()
    except OSError as error:
        raise RuntimeError(
            f"dashboard capability not found at {CAPFILE}; start this port "
            "with `crew dashboard start`") from error
    if not capability:
        raise RuntimeError(f"dashboard capability file is empty: {CAPFILE}")
    status, body = _req(
        "POST", "/api/auth/bootstrap", {"capability": capability},
        opener=_AUTH_OPENER)
    if status != 200 or not body or not body.get("ok"):
        raise RuntimeError(f"dashboard capability bootstrap failed: {status} {body!r}")


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


def _cleanup_test_homes():
    """Remove only this module's namespaced homes, never HOME_ROOT itself."""
    try:
        entries = os.listdir(HOME_ROOT)
    except OSError:
        return
    for entry in entries:
        if not entry.startswith(NAME_PREFIX):
            continue
        path = os.path.join(HOME_ROOT, entry)
        if os.path.islink(path) or not os.path.isdir(path):
            try:
                os.unlink(path)
            except OSError:
                pass
        else:
            shutil.rmtree(path, ignore_errors=True)


def setUpModule():
    global _SERVER_STARTED
    schema.ensure_schema(TEST_APP)
    unittest.addModuleCleanup(_cleanup_module_resources)
    started = _dashboard("start")
    if started.returncode != 0:
        raise RuntimeError(
            f"isolated dashboard start failed: {started.stdout!r} "
            f"{started.stderr!r}")
    _SERVER_STARTED = True
    _wait_dashboard()
    os.makedirs(HOME_ROOT, exist_ok=True)
    _bootstrap_operator_cookie()
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
    _cleanup_module_resources()


def _cleanup_module_resources():
    global _SERVER_STARTED
    if _SERVER_STARTED:
        try:
            status, snap = get("/api/graph/snapshot")
            if status == 200 and snap and snap.get("ok"):
                for agent in snap.get("agents", []):
                    name = agent.get("name") or ""
                    if name.startswith(NAME_PREFIX):
                        _remove_agent(name)
        except Exception:
            pass
        stopped = _dashboard("stop")
        if stopped.returncode == 0:
            _SERVER_STARTED = False
        else:
            # Never delete an app while a test dashboard may still be serving
            # it. The registered module cleanup will make one more stop attempt
            # after tearDownModule and surface the leak if it remains live.
            raise RuntimeError(
                f"isolated dashboard stop failed: {stopped.stdout!r} "
                f"{stopped.stderr!r}")
    try:
        gs._req("DELETE", f"/app/{TEST_APP}", app=None)
    except gs.GraphError:
        pass
    _cleanup_test_homes()


# --------------------------------------------------------------------------- #
# GET /api/graph/snapshot — shape
# --------------------------------------------------------------------------- #
class SnapshotShape(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src_name = _assert_test_name(f"{NAME_PREFIX}snapshot_src_{RUN_ID}")
        cls.tgt_name = _assert_test_name(f"{NAME_PREFIX}snapshot_tgt_{RUN_ID}")
        created = []
        try:
            for name in (cls.src_name, cls.tgt_name):
                status, body = post("/api/agent/create", {
                    "name": name, "home": _home(name), "launch": False,
                    "launch_cmd": "true",
                })
                if status != 200 or not body.get("ok"):
                    raise RuntimeError(f"snapshot fixture create failed for {name}: {body}")
                created.append(name)
            status, body = post("/api/edge/create", {
                "source": cls.src_name, "target": cls.tgt_name,
                "label": "snapshot fixture", "directed": True,
            })
            if status != 200 or not body.get("ok"):
                raise RuntimeError(f"snapshot edge fixture create failed: {body}")
            cls.edge_guid = body["edge"]["_guid"]
        except Exception:
            for name in reversed(created):
                _remove_agent(name)
            raise

    @classmethod
    def tearDownClass(cls):
        # Agent deletion cascades the fixture edge.
        _remove_agent(cls.src_name)
        _remove_agent(cls.tgt_name)

    def test_ok_and_top_level_shape(self):
        status, body = get("/api/graph/snapshot")
        self.assertEqual(status, 200)
        self.assertTrue(body.get("ok"), body)
        self.assertIsInstance(body.get("agents"), list)
        self.assertIsInstance(body.get("edges"), list)
        self.assertIsInstance(body.get("pending_count"), int)
        self.assertIsInstance(body.get("workspace_key"), str)
        self.assertTrue(body.get("workspace_key"))

    def test_owned_fixture_agents_present(self):
        _, body = get("/api/graph/snapshot")
        names = {a.get("name") for a in body["agents"]}
        self.assertTrue({self.src_name, self.tgt_name}.issubset(names), names)

    def test_agent_fields_present(self):
        _, body = get("/api/graph/snapshot")
        by_name = {a["name"]: a for a in body["agents"]}
        a = by_name[self.src_name]
        for key in ("_guid", "name", "role", "home", "session", "status",
                   "alive", "live_status", "out_edges", "in_edges"):
            self.assertIn(key, a, f"agent missing field {key!r}: {a}")
        self.assertIsInstance(a["alive"], bool)
        self.assertIn(
            a["live_status"],
            ("idle", "working", "needs_input", "not_started", "unknown", "down"),
        )

    def test_edge_fields_resolved(self):
        _, body = get("/api/graph/snapshot")
        edge = next((e for e in body["edges"] if e.get("_guid") == self.edge_guid), None)
        self.assertIsNotNone(edge, "owned fixture edge missing from snapshot")
        for key in ("_guid", "source", "target", "directed", "source_name", "target_name"):
            self.assertIn(key, edge, f"edge missing field {key!r}: {edge}")
        self.assertEqual(edge["source_name"], self.src_name)
        self.assertEqual(edge["target_name"], self.tgt_name)


class EmptySnapshotShape(unittest.TestCase):
    def test_brand_new_empty_install_is_a_valid_snapshot(self):
        """The snapshot builder must treat an app with no rows as healthy.

        The live dashboard may contain operator-owned data, so exercise the
        empty boundary in-process instead of deleting anything to manufacture
        an empty live graph.
        """
        from crew.server import app as dashboard_app

        with mock.patch.object(dashboard_app.gs, "list_agents", return_value=[]), \
             mock.patch.object(dashboard_app.gs, "list_edges", return_value=[]), \
             mock.patch.object(dashboard_app.config, "current_app",
                               return_value="crew-empty-test"), \
             mock.patch.object(dashboard_app.tmuxio, "_session_pane_map", return_value={}), \
             mock.patch.object(dashboard_app, "_pending_rows", return_value=[]), \
             mock.patch.object(dashboard_app, "_status_transitions"):
            body = dashboard_app._graph_snapshot()

        self.assertEqual(body, {
            "ok": True, "workspace_key": "crew-empty-test",
            "agents": [], "edges": [], "pending_count": 0,
        })


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
        self.assertEqual(agent["status"], "not_started")
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
            "max_turns": 3, "directed": False,
        })
        self.assertEqual(status, 200)
        self.assertTrue(body.get("ok"), body)
        edge = body["edge"]
        guid = edge["_guid"]
        self.assertEqual(edge.get("label"), "t-label")
        self.assertEqual(edge.get("target_action"), "do the thing")
        self.assertTrue(edge.get("reply_expected"))
        self.assertEqual(int(edge.get("max_turns")), 3)
        self.assertFalse(edge.get("directed"))

        _, snap = get("/api/graph/snapshot")
        found = next((e for e in snap["edges"] if e["_guid"] == guid), None)
        self.assertIsNotNone(found, "created edge not present in snapshot")
        self.assertEqual(found.get("source_name"), self.src_name)
        self.assertEqual(found.get("target_name"), self.tgt_name)

        # --- UPDATE --- #
        status, body = post("/api/edge/update", {
            "guid": guid, "label": "t-label-2", "max_turns": 7,
            "reply_expected": False, "directed": True,
        })
        self.assertEqual(status, 200)
        self.assertTrue(body.get("ok"), body)
        edge2 = body["edge"]
        self.assertEqual(edge2.get("label"), "t-label-2")
        self.assertEqual(int(edge2.get("max_turns")), 7)
        self.assertFalse(edge2.get("reply_expected"))
        self.assertTrue(edge2.get("directed"))

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

    def test_create_rejects_non_finite_cost_cap_without_persisting_an_edge(self):
        status, body = post("/api/edge/create", {
            "source": self.src_name, "target": self.tgt_name,
            "cost_cap": "nan", "directed": True,
        })
        self.assertEqual(status, 200)
        self.assertFalse(body.get("ok"), body)
        self.assertIn("finite", body.get("error", "").lower())
        _, snapshot = get("/api/graph/snapshot")
        matching = [e for e in snapshot.get("edges", [])
                    if e.get("source_name") == self.src_name
                    and e.get("target_name") == self.tgt_name]
        self.assertEqual(matching, [])

    def test_update_rejects_non_finite_cost_cap_without_changing_the_edge(self):
        _, created = post("/api/edge/create", {
            "source": self.src_name, "target": self.tgt_name,
            "cost_cap": 1.0, "directed": True,
        })
        self.assertTrue(created.get("ok"), created)
        guid = created["edge"]["_guid"]
        status, body = post("/api/edge/update", {
            "guid": guid, "cost_cap": "inf",
        })
        self.assertEqual(status, 200)
        self.assertFalse(body.get("ok"), body)
        self.assertIn("finite", body.get("error", "").lower())
        _, snapshot = get("/api/graph/snapshot")
        edge = next(e for e in snapshot.get("edges", []) if e.get("_guid") == guid)
        self.assertEqual(float(edge.get("cost_cap")), 1.0)

    def test_create_rejects_negative_cap_without_persisting_an_edge(self):
        status, body = post("/api/edge/create", {
            "source": self.src_name, "target": self.tgt_name,
            "token_cap": -1, "directed": True,
        })
        self.assertEqual(status, 200)
        self.assertFalse(body.get("ok"), body)
        self.assertRegex(body.get("error", "").lower(), "zero|positive")
        _, snapshot = get("/api/graph/snapshot")
        matching = [e for e in snapshot.get("edges", [])
                    if e.get("source_name") == self.src_name
                    and e.get("target_name") == self.tgt_name]
        self.assertEqual(matching, [])

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

    def test_bless_bad_guid_does_not_upsert_a_phantom_edge(self):
        fake_guid = f"edge_{NAME_PREFIX}fake_bless_guid_{RUN_ID}"
        try:
            status, body = post("/api/edge/bless", {"guid": fake_guid})
            self.assertEqual(status, 200)
            self.assertFalse(body.get("ok"), body)
            _, snapshot = get("/api/graph/snapshot")
            self.assertFalse(any(
                edge.get("_guid") == fake_guid
                for edge in snapshot.get("edges", [])))
        finally:
            # Keep this safe against the old PATCH-upsert bug during a RED run.
            post("/api/edge/delete", {"guid": fake_guid})


# --------------------------------------------------------------------------- #
# Error shapes: unknown routes + missing-param terminal endpoints
# --------------------------------------------------------------------------- #
class ErrorShapes(unittest.TestCase):
    def test_control_post_without_operator_cookie_is_forbidden(self):
        unauthenticated = urllib.request.build_opener()
        status, body = _req(
            "POST", "/api/agent/remove", {"name": NAME_PREFIX + "ghost"},
            opener=unauthenticated)
        self.assertEqual(status, 403)
        self.assertFalse(body.get("ok"))
        self.assertIn("operator capability", body.get("error", ""))

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
        self.assertEqual(status, 400)
        self.assertFalse(body.get("ok"))
        self.assertIn("id", body.get("error", "").lower())

    def test_pty_resize_missing_id(self):
        status, body = post("/api/pty/resize", {})
        self.assertEqual(status, 400)
        self.assertFalse(body.get("ok"))
        self.assertIn("id", body.get("error", "").lower())

    def test_pty_resize_rejects_invalid_dimensions(self):
        cases = (
            ({"id": "not-live", "cols": True, "rows": 24}, "cols"),
            ({"id": "not-live", "cols": 80.5, "rows": 24}, "cols"),
            ({"id": "not-live", "cols": 80, "rows": "24"}, "rows"),
            ({"id": "not-live", "cols": 501, "rows": 24}, "cols"),
            ({"id": "not-live", "cols": 80, "rows": 301}, "rows"),
        )
        for payload, field in cases:
            with self.subTest(payload=payload):
                status, body = post("/api/pty/resize", payload)
                self.assertEqual(status, 400, body)
                self.assertFalse(body.get("ok"), body)
                self.assertIn(field, body.get("error", "").lower())

    def test_known_api_paths_reject_wrong_methods(self):
        for method, path in (
            ("GET", "/api/agent/remove"),
            ("POST", "/api/health"),
            ("PUT", "/api/graph/snapshot"),
        ):
            with self.subTest(method=method, path=path):
                status, body = _req(method, path, {})
                self.assertEqual(status, 405, body)
                self.assertFalse(body.get("ok"), body)
                self.assertIn("method", body.get("error", "").lower())


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
# Direct malformed-row fixtures must target the same isolated app as BASE,
# regardless of another discovered module's temporary process environment.
_DASHBOARD_APP = TEST_APP


class MalformedPendingRow(unittest.TestCase):
    def setUp(self):
        self.src_name = _assert_test_name(f"{NAME_PREFIX}malformed_src_{RUN_ID}")
        self.tgt_name = _assert_test_name(f"{NAME_PREFIX}malformed_tgt_{RUN_ID}")
        self._created = []
        try:
            for name in (self.src_name, self.tgt_name):
                status, body = post("/api/agent/create", {
                    "name": name, "home": _home(name), "launch": False,
                    "launch_cmd": "true",
                })
                self.assertEqual(status, 200)
                self.assertTrue(body.get("ok"), body)
                self._created.append(name)
            status, body = post("/api/edge/create", {
                "source": self.src_name, "target": self.tgt_name,
                "label": "malformed-row fixture", "directed": True,
            })
            self.assertEqual(status, 200)
            self.assertTrue(body.get("ok"), body)
            self.edge_guid = body["edge"]["_guid"]
        except Exception:
            for name in reversed(self._created):
                _remove_agent(name)
            raise

    def tearDown(self):
        for name in reversed(self._created):
            _remove_agent(name)

    def _direct(self):
        """Every OTHER graph-editing write here goes through the live HTTP
        dashboard (a separate OS process, unaffected by this test process's
        os.environ). This test needs a direct crew.graphstore call instead (to
        hand-craft a malformed row the API has no way to create), and several
        sibling test MODULES pin $CREW_APP to a throwaway app as a top-level
        import-time side effect — which has already run by the time this test
        executes under `discover`. Pin explicitly to the dashboard's configured
        app for direct calls, regardless of import-order pollution; always
        restore whatever was there after."""
        return _pinned_app(_DASHBOARD_APP)

    def test_approve_of_row_with_non_dict_fields_returns_clean_500_not_dropped_connection(self):
        with self._direct():
            row = gs.create_object("graph_edit", {
                # This fixture targets corrupt persisted ``fields``.  Use the
                # trusted human actor so immutable-agent-identity validation
                # does not (correctly) reject the row before that code path.
                "actor": "human", "op": "update_edge",
                "args": {"guid": self.edge_guid, "fields": "max_turns"},  # string, not a dict
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
