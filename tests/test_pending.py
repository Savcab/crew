"""WAVE 4 tests: the pending-approval queue — guard.py routes exactly two
op-cases to PENDING instead of refusing (foreman connect to a human-created
node; a cap raise by any agent on an edge it's an endpoint of), plus
approve_pending/reject_pending, the `crew pending|approve|reject` CLI verbs,
and the dashboard's /api/pending surface.

Three layers, per SKILL.md:
  * unit — a throwaway MorphDB app (`crewtest-pending-unit`), registered in
    setUpModule and cascade-deleted in tearDownModule.
  * live — a throwaway project ("w4test", its own MorphDB app "crew-w4test"),
    driving the real CLI as an operator shell (never touching the real
    5-agent "crew" app). The requesting foreman acts via $CREW_AGENT (no
    live tmux pane needed outside a tmux session — crew.mail.whoami() falls
    back to $CREW_AGENT when there's no agent-owned pane, same trick
    test_foreman.py's live CLI tests rely on).
  * browser — tests/browser/pending-tray.md, executed separately via
    playwright tools against the real "crew" app (the dashboard only ever
    serves the default project) with test_w4ui_* fixtures.

    python3 -m unittest tests.test_pending          (from the repo root)
    python3 -m unittest discover tests                (full suite)
"""
import contextlib
import os
import subprocess
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_APP = "crewtest-pending-unit"
os.environ["CREW_APP"] = TEST_APP

from crew import cli, config, graphstore as gs, guard, schema  # noqa: E402

_orig_max_agents = None
_orig_spawn_rate = None


def setUpModule():
    os.environ["CREW_APP"] = TEST_APP
    try:
        gs._req("DELETE", f"/app/{TEST_APP}", app=None)
    except gs.GraphError:
        pass
    schema.ensure_schema(TEST_APP)
    global _orig_max_agents, _orig_spawn_rate
    _orig_max_agents, _orig_spawn_rate = config.MAX_AGENTS, config.SPAWN_RATE
    config.MAX_AGENTS = 10_000
    config.SPAWN_RATE = 10_000


def tearDownModule():
    config.MAX_AGENTS, config.SPAWN_RATE = _orig_max_agents, _orig_spawn_rate
    try:
        gs._req("DELETE", f"/app/{TEST_APP}", app=None)
    except gs.GraphError:
        pass


def _audit_rows(actor=None, op=None, result=None):
    res = gs.list_objects("graph_edit", limit=1000, sort="created_at", order="desc")
    rows = (res or {}).get("objects", [])
    if actor is not None:
        rows = [r for r in rows if r.get("actor") == actor]
    if op is not None:
        rows = [r for r in rows if r.get("op") == op]
    if result is not None:
        rows = [r for r in rows if r.get("result") == result]
    return rows


def _pending_rows(actor=None):
    return _audit_rows(actor=actor, result="pending")


def _foreman(name):
    return gs.create_agent(name, home=f"/tmp/crew_pendingtest/{name}",
                           can_edit_graph=True)


def _messages_to(target):
    res = gs.list_objects("message", target=target, sort="created_at", order="desc",
                          limit=200)
    return (res or {}).get("objects", [])


# --------------------------------------------------------------------------- #
# unit — (a) foreman connect -> human-created node => pending, no edge
# --------------------------------------------------------------------------- #
class ConnectPendingTests(unittest.TestCase):
    def test_foreman_connect_to_human_node_yields_pending_no_edge(self):
        f = _foreman("cp_f1")
        human_node = gs.create_agent("cp_human1", home="/tmp/crew_pendingtest/cp_human1")
        with self.assertRaises(gs.GraphError) as ctx:
            gs.create_edge(f["_guid"], human_node["_guid"], actor="cp_f1",
                          max_turns=5, token_cap=1000, cost_cap=1.0)
        msg = str(ctx.exception)
        self.assertIn("queued", msg.lower())
        self.assertIn("crew pending", msg)
        self.assertIn("Nothing was created yet", msg)
        # no edge exists
        self.assertEqual(gs.edges_from_to(f["_guid"], human_node["_guid"]), [])
        # a pending row, NOT a refused row
        rows = _pending_rows(actor="cp_f1")
        self.assertTrue(any(r.get("op") == "connect" for r in rows), rows)
        refused = _audit_rows(actor="cp_f1", op="connect", result="refused")
        self.assertEqual(refused, [])

    def test_pending_row_captures_full_connect_args(self):
        f = _foreman("cp_f2")
        human_node = gs.create_agent("cp_human2", home="/tmp/crew_pendingtest/cp_human2")
        with self.assertRaises(gs.GraphError):
            gs.create_edge(f["_guid"], human_node["_guid"], actor="cp_f2",
                          label="l", description="d", conditions=["when x"],
                          target_action="do it", max_turns=5, token_cap=1000,
                          cost_cap=1.0)
        rows = _pending_rows(actor="cp_f2")
        row = next(r for r in rows if r.get("op") == "connect")
        args = row.get("args") or {}
        self.assertEqual(args.get("source"), f["_guid"])
        self.assertEqual(args.get("target"), human_node["_guid"])
        self.assertEqual(args.get("label"), "l")
        self.assertEqual(args.get("max_turns"), 5)
        self.assertEqual(args.get("token_cap"), 1000)
        self.assertEqual(args.get("cost_cap"), 1.0)

    def test_connect_to_agent_owned_by_someone_else_still_hard_refused(self):
        # NOT the human-created case: an out-of-envelope endpoint created by a
        # THIRD agent (not human, not this foreman) stays a hard refusal.
        f1 = _foreman("cp_f3")
        f2 = _foreman("cp_f4")
        third_owned = gs.create_agent("cp_owned3", home="/tmp/crew_pendingtest/cp_owned3",
                                      actor="cp_f4")
        with self.assertRaises(gs.GraphError) as ctx:
            gs.create_edge(f1["_guid"], third_owned["_guid"], actor="cp_f3",
                          max_turns=5, token_cap=1000, cost_cap=1.0)
        self.assertIn("drawn by the user", str(ctx.exception))
        rows = _audit_rows(actor="cp_f3", op="connect", result="refused")
        self.assertTrue(rows)


# --------------------------------------------------------------------------- #
# unit — (b) cap raise by any agent on an edge it's an endpoint of => pending
# --------------------------------------------------------------------------- #
class CapRaisePendingTests(unittest.TestCase):
    def test_plain_agent_cap_raise_yields_pending_cap_unchanged(self):
        a = gs.create_agent("cr_a1", home="/tmp/crew_pendingtest/cr_a1")
        b = gs.create_agent("cr_b1", home="/tmp/crew_pendingtest/cr_b1")
        e = gs.create_edge(a["_guid"], b["_guid"], max_turns=10, token_cap=1000)
        with self.assertRaises(gs.GraphError) as ctx:
            gs.update_edge(e["_guid"], {"max_turns": 50}, actor="cr_a1")
        msg = str(ctx.exception)
        self.assertIn("cap raise", msg.lower())
        self.assertIn("queued", msg.lower())
        refreshed = gs.get_object(e["_guid"])
        self.assertEqual(refreshed.get("max_turns"), 10)  # unchanged
        rows = _pending_rows(actor="cr_a1")
        self.assertTrue(any(r.get("op") == "update_edge" for r in rows), rows)

    def test_raise_to_zero_also_pending(self):
        a = gs.create_agent("cr_a2", home="/tmp/crew_pendingtest/cr_a2")
        b = gs.create_agent("cr_b2", home="/tmp/crew_pendingtest/cr_b2")
        e = gs.create_edge(a["_guid"], b["_guid"], max_turns=10)
        with self.assertRaises(gs.GraphError):
            gs.update_edge(e["_guid"], {"max_turns": 0}, actor="cr_a2")
        rows = _pending_rows(actor="cr_a2")
        self.assertTrue(rows)

    def test_foreman_cap_raise_on_own_inbound_edge_also_pending(self):
        f = _foreman("cr_f3")
        boss = gs.create_agent("cr_boss3", home="/tmp/crew_pendingtest/cr_boss3")  # human-made
        e = gs.create_edge(boss["_guid"], f["_guid"], actor="human", max_turns=5)
        with self.assertRaises(gs.GraphError) as ctx:
            gs.update_edge(e["_guid"], {"max_turns": 50}, actor="cr_f3")
        self.assertIn("cap raise", str(ctx.exception).lower())
        rows = _pending_rows(actor="cr_f3")
        self.assertTrue(any(r.get("op") == "update_edge" for r in rows), rows)

    def test_cap_lower_still_applies_directly_not_pending(self):
        a = gs.create_agent("cr_a4", home="/tmp/crew_pendingtest/cr_a4")
        b = gs.create_agent("cr_b4", home="/tmp/crew_pendingtest/cr_b4")
        e = gs.create_edge(a["_guid"], b["_guid"], max_turns=10)
        out = gs.update_edge(e["_guid"], {"max_turns": 2}, actor="cr_a4")
        self.assertEqual(out.get("max_turns"), 2)
        self.assertEqual(_pending_rows(actor="cr_a4"), [])


# --------------------------------------------------------------------------- #
# unit — approve_pending
# --------------------------------------------------------------------------- #
class ApprovePendingTests(unittest.TestCase):
    def test_approve_connect_creates_edge_stored_args_foreman_unblessed(self):
        f = _foreman("ap_f1")
        human_node = gs.create_agent("ap_human1", home="/tmp/crew_pendingtest/ap_human1")
        with self.assertRaises(gs.GraphError):
            gs.create_edge(f["_guid"], human_node["_guid"], actor="ap_f1",
                          max_turns=5, token_cap=1000, cost_cap=1.0)
        row = _pending_rows(actor="ap_f1")[0]
        guard.approve_pending(row["_guid"], actor="human")
        edges = gs.edges_from_to(f["_guid"], human_node["_guid"])
        self.assertEqual(len(edges), 1)
        edge = edges[0]
        self.assertEqual(edge.get("created_by"), "ap_f1")
        self.assertFalse(edge.get("blessed"))
        refreshed = gs.get_object(row["_guid"])
        self.assertEqual(refreshed.get("result"), "approved")

    def test_approve_cap_raise_applies_new_cap(self):
        a = gs.create_agent("ap_a2", home="/tmp/crew_pendingtest/ap_a2")
        b = gs.create_agent("ap_b2", home="/tmp/crew_pendingtest/ap_b2")
        e = gs.create_edge(a["_guid"], b["_guid"], max_turns=10)
        with self.assertRaises(gs.GraphError):
            gs.update_edge(e["_guid"], {"max_turns": 50}, actor="ap_a2")
        row = _pending_rows(actor="ap_a2")[0]
        guard.approve_pending(row["_guid"], actor="human")
        refreshed = gs.get_object(e["_guid"])
        self.assertEqual(refreshed.get("max_turns"), 50)

    def test_approve_queues_notice_to_requester(self):
        f = _foreman("ap_f3")
        human_node = gs.create_agent("ap_human3", home="/tmp/crew_pendingtest/ap_human3")
        with self.assertRaises(gs.GraphError):
            gs.create_edge(f["_guid"], human_node["_guid"], actor="ap_f3",
                          max_turns=5, token_cap=1000, cost_cap=1.0)
        row = _pending_rows(actor="ap_f3")[0]
        guard.approve_pending(row["_guid"], actor="human")
        msgs = _messages_to("ap_f3")
        self.assertTrue(any(m.get("sender") == "crew" and "approved" in (m.get("body") or "")
                            for m in msgs), msgs)

    def test_approve_already_approved_refused(self):
        f = _foreman("ap_f4")
        human_node = gs.create_agent("ap_human4", home="/tmp/crew_pendingtest/ap_human4")
        with self.assertRaises(gs.GraphError):
            gs.create_edge(f["_guid"], human_node["_guid"], actor="ap_f4",
                          max_turns=5, token_cap=1000, cost_cap=1.0)
        row = _pending_rows(actor="ap_f4")[0]
        guard.approve_pending(row["_guid"], actor="human")
        with self.assertRaises(gs.GraphError):
            guard.approve_pending(row["_guid"], actor="human")

    def test_approve_of_rejected_refused(self):
        f = _foreman("ap_f5")
        human_node = gs.create_agent("ap_human5", home="/tmp/crew_pendingtest/ap_human5")
        with self.assertRaises(gs.GraphError):
            gs.create_edge(f["_guid"], human_node["_guid"], actor="ap_f5",
                          max_turns=5, token_cap=1000, cost_cap=1.0)
        row = _pending_rows(actor="ap_f5")[0]
        guard.reject_pending(row["_guid"], reason="no", actor="human")
        with self.assertRaises(gs.GraphError):
            guard.approve_pending(row["_guid"], actor="human")

    def test_non_human_approve_refused(self):
        f = _foreman("ap_f6")
        human_node = gs.create_agent("ap_human6", home="/tmp/crew_pendingtest/ap_human6")
        with self.assertRaises(gs.GraphError):
            gs.create_edge(f["_guid"], human_node["_guid"], actor="ap_f6",
                          max_turns=5, token_cap=1000, cost_cap=1.0)
        row = _pending_rows(actor="ap_f6")[0]
        with self.assertRaises(gs.GraphError) as ctx:
            guard.approve_pending(row["_guid"], actor="ap_f6")
        self.assertIn("human", str(ctx.exception).lower())
        # still pending
        refreshed = gs.get_object(row["_guid"])
        self.assertEqual(refreshed.get("result"), "pending")


# --------------------------------------------------------------------------- #
# unit — reject_pending
# --------------------------------------------------------------------------- #
class RejectPendingTests(unittest.TestCase):
    def test_reject_leaves_no_edge_marks_rejected_with_reason(self):
        f = _foreman("rj_f1")
        human_node = gs.create_agent("rj_human1", home="/tmp/crew_pendingtest/rj_human1")
        with self.assertRaises(gs.GraphError):
            gs.create_edge(f["_guid"], human_node["_guid"], actor="rj_f1",
                          max_turns=5, token_cap=1000, cost_cap=1.0)
        row = _pending_rows(actor="rj_f1")[0]
        guard.reject_pending(row["_guid"], reason="not needed", actor="human")
        self.assertEqual(gs.edges_from_to(f["_guid"], human_node["_guid"]), [])
        refreshed = gs.get_object(row["_guid"])
        self.assertEqual(refreshed.get("result"), "rejected")
        self.assertEqual(refreshed.get("reason"), "not needed")

    def test_reject_queues_notice_with_reason(self):
        f = _foreman("rj_f2")
        human_node = gs.create_agent("rj_human2", home="/tmp/crew_pendingtest/rj_human2")
        with self.assertRaises(gs.GraphError):
            gs.create_edge(f["_guid"], human_node["_guid"], actor="rj_f2",
                          max_turns=5, token_cap=1000, cost_cap=1.0)
        row = _pending_rows(actor="rj_f2")[0]
        guard.reject_pending(row["_guid"], reason="nope", actor="human")
        msgs = _messages_to("rj_f2")
        self.assertTrue(any(m.get("sender") == "crew" and "rejected" in (m.get("body") or "")
                            and "nope" in (m.get("body") or "") for m in msgs), msgs)

    def test_non_human_reject_refused(self):
        f = _foreman("rj_f3")
        human_node = gs.create_agent("rj_human3", home="/tmp/crew_pendingtest/rj_human3")
        with self.assertRaises(gs.GraphError):
            gs.create_edge(f["_guid"], human_node["_guid"], actor="rj_f3",
                          max_turns=5, token_cap=1000, cost_cap=1.0)
        row = _pending_rows(actor="rj_f3")[0]
        with self.assertRaises(gs.GraphError):
            guard.reject_pending(row["_guid"], actor="rj_f3")


# --------------------------------------------------------------------------- #
# unit — CLI: `crew pending` / `crew approve` / `crew reject` + prefix matching
# --------------------------------------------------------------------------- #
class PendingCliTests(unittest.TestCase):
    def test_cli_pending_lists_and_approve_reject_dispatch(self):
        f = _foreman("cli_f1")
        human_node = gs.create_agent("cli_human1", home="/tmp/crew_pendingtest/cli_human1")
        with self.assertRaises(gs.GraphError):
            gs.create_edge(f["_guid"], human_node["_guid"], actor="cli_f1",
                          max_turns=5, token_cap=1000, cost_cap=1.0)
        row = _pending_rows(actor="cli_f1")[0]
        p = cli.build_parser()
        with mock.patch.object(cli, "_ACTOR", "human"):
            args = p.parse_args(["pending"])
            self.assertEqual(args.fn(args), 0)
            args = p.parse_args(["approve", row["_guid"]])
            self.assertEqual(args.fn(args), 0)
        edges = gs.edges_from_to(f["_guid"], human_node["_guid"])
        self.assertEqual(len(edges), 1)

    def test_cli_reject_with_why(self):
        f = _foreman("cli_f2")
        human_node = gs.create_agent("cli_human2", home="/tmp/crew_pendingtest/cli_human2")
        with self.assertRaises(gs.GraphError):
            gs.create_edge(f["_guid"], human_node["_guid"], actor="cli_f2",
                          max_turns=5, token_cap=1000, cost_cap=1.0)
        row = _pending_rows(actor="cli_f2")[0]
        p = cli.build_parser()
        with mock.patch.object(cli, "_ACTOR", "human"):
            args = p.parse_args(["reject", row["_guid"], "--why", "bad idea"])
            self.assertEqual(args.fn(args), 0)
        refreshed = gs.get_object(row["_guid"])
        self.assertEqual(refreshed.get("result"), "rejected")
        self.assertEqual(refreshed.get("reason"), "bad idea")

    def test_cli_prefix_unique_resolves(self):
        f = _foreman("cli_f3")
        human_node = gs.create_agent("cli_human3", home="/tmp/crew_pendingtest/cli_human3")
        with self.assertRaises(gs.GraphError):
            gs.create_edge(f["_guid"], human_node["_guid"], actor="cli_f3",
                          max_turns=5, token_cap=1000, cost_cap=1.0)
        row = _pending_rows(actor="cli_f3")[0]
        # NOT row["_guid"][:10] — MorphDB's graph_edit guids all share a
        # constant type prefix ("graphedit_...") that alone is never unique;
        # drop just the last couple chars so this is a real (near-whole, but
        # non-exact) prefix match, not the whole guid.
        prefix = row["_guid"][:-2]
        p = cli.build_parser()
        with mock.patch.object(cli, "_ACTOR", "human"):
            args = p.parse_args(["approve", prefix])
            self.assertEqual(args.fn(args), 0)

    def test_cli_ambiguous_prefix_errors(self):
        with self.assertRaises(gs.GraphError) as ctx:
            cli._resolve_pending("")   # empty prefix matches every pending row
        self.assertIn("ambiguous", str(ctx.exception).lower())


# --------------------------------------------------------------------------- #
# live — throwaway project "w4test", operator-shell round trip
# --------------------------------------------------------------------------- #
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CREW_BIN = os.path.join(ROOT, "bin", "crew")
HOME_BASE = "/tmp/crew_tests_pending"
PROJECT = "w4test"
PROJECT_APP = f"crew-{PROJECT}"


def _run(args, env_extra=None, timeout=30):
    env = dict(os.environ)
    env.pop("CREW_APP", None)
    env["CREW_PROJECT"] = PROJECT
    if env_extra:
        env.update(env_extra)
    p = subprocess.run([sys.executable, CREW_BIN, *args], cwd=ROOT, env=env,
                       capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


@contextlib.contextmanager
def _pinned_app(app):
    prev = os.environ.get("CREW_APP")
    os.environ["CREW_APP"] = app
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop("CREW_APP", None)
        else:
            os.environ["CREW_APP"] = prev


@unittest.skipUnless(os.environ.get("CREW_LIVE_TESTS", "1") == "1",
                     "set CREW_LIVE_TESTS=0 to skip live pane tests")
class LivePendingCliTests(unittest.TestCase):
    def setUp(self):
        self.f = "test_w4_f"
        self.human_node = "test_w4_humannode"
        self.home_f = os.path.join(HOME_BASE, self.f)
        self.home_human = os.path.join(HOME_BASE, self.human_node)

    def tearDown(self):
        for n in (self.f, self.human_node):
            try:
                _run(["remove-agent", n], timeout=15)
            except Exception:
                pass
        try:
            gs._req("DELETE", f"/app/{PROJECT_APP}", app=None)
        except gs.GraphError:
            pass
        try:
            import json as _json
            names = [n for n in config.list_known_projects() if n != PROJECT]
            os.makedirs(config.VAR, exist_ok=True)
            with open(config._projects_file(), "w") as fh:
                _json.dump([n for n in names if n != config.DEFAULT_PROJECT], fh)
        except OSError:
            pass

    def test_pending_approve_reject_round_trip(self):
        rc, out, err = _run(["project", "create", PROJECT])
        self.assertEqual(rc, 0, f"project create failed: {out!r} {err!r}")

        rc, out, err = _run(["spawn-agent", self.f, "--home", self.home_f,
                             "--launch-cmd", "true", "--no-launch", "--foreman"])
        self.assertEqual(rc, 0, f"spawn foreman failed: {out!r} {err!r}")

        rc, out, err = _run(["spawn-agent", self.human_node, "--home", self.home_human,
                             "--launch-cmd", "true", "--no-launch"])
        self.assertEqual(rc, 0, f"spawn human node failed: {out!r} {err!r}")

        # the foreman, acting via $CREW_AGENT (operator shell has no live pane
        # for it here — mail.whoami() falls back to $CREW_AGENT), requests a
        # connect to the human-made node -> queued, not applied
        rc, out, err = _run(["connect", self.f, self.human_node,
                             "--max-turns", "5", "--token-cap", "1000", "--cost-cap", "1.0"],
                            env_extra={"CREW_AGENT": self.f})
        self.assertEqual(rc, 1, f"expected refusal-as-pending rc: {out!r} {err!r}")
        self.assertIn("queued", (out + err).lower())

        # human sees it in the pending tray
        rc, out, err = _run(["pending"])
        self.assertEqual(rc, 0, f"crew pending failed: {out!r} {err!r}")
        self.assertIn(self.f, out)

        with _pinned_app(PROJECT_APP):
            row = gs.list_objects("graph_edit", result="pending", sort="created_at",
                                  order="desc", limit=10)["objects"][0]
        guid = row["_guid"]

        # approve -> edge exists
        rc, out, err = _run(["approve", guid])
        self.assertEqual(rc, 0, f"crew approve failed: {out!r} {err!r}")

        with _pinned_app(PROJECT_APP):
            f_agent = gs.get_agent_by_name(self.f)
            human_agent = gs.get_agent_by_name(self.human_node)
            edges = gs.edges_from_to(f_agent["_guid"], human_agent["_guid"])
        self.assertTrue(edges, "expected the approved edge to exist")
        self.assertEqual(edges[0].get("created_by"), self.f)
        self.assertFalse(edges[0].get("blessed"))

        # second request, this time rejected
        rc, out, err = _run(["connect", self.f, self.human_node],
                            env_extra={"CREW_AGENT": self.f})
        self.assertEqual(rc, 1)

        with _pinned_app(PROJECT_APP):
            row2 = gs.list_objects("graph_edit", result="pending", sort="created_at",
                                   order="desc", limit=10)["objects"][0]
        guid2 = row2["_guid"]

        rc, out, err = _run(["reject", guid2, "--why", "not now"])
        self.assertEqual(rc, 0, f"crew reject failed: {out!r} {err!r}")

        with _pinned_app(PROJECT_APP):
            refreshed = gs.get_object(guid2)
        self.assertEqual(refreshed.get("result"), "rejected")
        self.assertEqual(refreshed.get("reason"), "not now")


if __name__ == "__main__":
    unittest.main(verbosity=2)
