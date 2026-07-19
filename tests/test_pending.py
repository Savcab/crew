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
import io
import os
import subprocess
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_APP = "crewtest-pending-unit"

from crew import cli, config, graphstore as gs, guard, schema  # noqa: E402

_orig_max_agents = None
_orig_spawn_rate = None
_CREW_APP_PATCHER = None


def setUpModule():
    global _CREW_APP_PATCHER
    _CREW_APP_PATCHER = mock.patch.dict(os.environ, {"CREW_APP": TEST_APP})
    _CREW_APP_PATCHER.start()
    unittest.addModuleCleanup(_CREW_APP_PATCHER.stop)
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
    try:
        config.MAX_AGENTS, config.SPAWN_RATE = _orig_max_agents, _orig_spawn_rate
        try:
            gs._req("DELETE", f"/app/{TEST_APP}", app=None)
        except gs.GraphError:
            pass
    finally:
        _CREW_APP_PATCHER.stop()


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
    # The product enforces one live foreman. Tests share one module app, so
    # retire the preceding case's holder before constructing the next case.
    for current in gs.list_agents():
        if current.get("can_edit_graph"):
            gs.set_foreman(current["_guid"], revoke=True, actor="human")
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
        former = _foreman("cp_f3")
        third_owned = gs.create_agent("cp_owned3", home="/tmp/crew_pendingtest/cp_owned3",
                                      actor=former["name"])
        current = _foreman("cp_f4")
        with self.assertRaises(gs.GraphError) as ctx:
            gs.create_edge(
                          current["_guid"], third_owned["_guid"],
                          actor=current["name"],
                          max_turns=5, token_cap=1000, cost_cap=1.0)
        self.assertIn("drawn by the user", str(ctx.exception))
        rows = _audit_rows(actor="cp_f4", op="connect", result="refused")
        self.assertTrue(rows)

    def test_invalid_caps_refuse_before_any_pending_row(self):
        valid = {"max_turns": 5, "token_cap": 1000, "cost_cap": 1.0}
        cases = (
            ("max_zero", {"max_turns": 0}),
            ("token_zero", {"token_cap": 0}),
            ("cost_zero", {"cost_cap": 0}),
            ("malformed", {"max_turns": "five"}),
            ("boolean", {"cost_cap": True}),
            ("nonfinite", {"cost_cap": float("inf")}),
            ("max_over", {
                "max_turns": config.AGENT_EDGE_MAX_TURNS_CEILING + 1}),
            ("token_over", {
                "token_cap": config.AGENT_EDGE_TOKEN_CAP_CEILING + 1}),
            ("cost_over", {
                "cost_cap": config.AGENT_EDGE_COST_CAP_CEILING + 0.01}),
        )
        for suffix, override in cases:
            with self.subTest(case=suffix):
                actor = f"cp_bad_{suffix}"
                f = _foreman(actor)
                human_node = gs.create_agent(
                    f"cp_h_{suffix}",
                    home=f"/tmp/crew_pendingtest/cp_h_{suffix}")
                caps = dict(valid)
                caps.update(override)

                with self.assertRaises(gs.GraphError) as ctx:
                    gs.create_edge(
                        f["_guid"], human_node["_guid"], actor=actor, **caps)

                self.assertNotIn("queued", str(ctx.exception).lower())
                self.assertEqual(_pending_rows(actor=actor), [])
                self.assertEqual(
                    gs.edges_from_to(f["_guid"], human_node["_guid"]), [])
                self.assertTrue(
                    _audit_rows(actor=actor, op="connect", result="refused"))


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

    def test_unlimited_to_finite_is_a_lowering_not_a_pending_raise(self):
        a = gs.create_agent("cr_a5", home="/tmp/crew_pendingtest/cr_a5")
        b = gs.create_agent("cr_b5", home="/tmp/crew_pendingtest/cr_b5")
        edge = gs.create_edge(a["_guid"], b["_guid"], max_turns=0)

        updated = gs.update_edge(
            edge["_guid"], {"max_turns": 5}, actor=a["name"])

        self.assertEqual(updated.get("max_turns"), 5)
        self.assertEqual(_pending_rows(actor=a["name"]), [])


# --------------------------------------------------------------------------- #
# unit — approve_pending
# --------------------------------------------------------------------------- #
class ApprovePendingTests(unittest.TestCase):
    def _connect_request(self, prefix):
        actor = f"{prefix}_f"
        f = _foreman(actor)
        human_node = gs.create_agent(
            f"{prefix}_h", home=f"/tmp/crew_pendingtest/{prefix}_h")
        with self.assertRaises(gs.GraphError):
            gs.create_edge(
                f["_guid"], human_node["_guid"], actor=actor,
                max_turns=5, token_cap=1000, cost_cap=1.0)
        row = _pending_rows(actor=actor)[0]
        return actor, f, human_node, row

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
        self.assertEqual(edge.get("max_turns"), 5)
        self.assertEqual(edge.get("token_cap"), 1000)
        self.assertEqual(edge.get("cost_cap"), 1.0)
        refreshed = gs.get_object(row["_guid"])
        self.assertEqual(refreshed.get("result"), "approved")

    def test_approve_revalidates_tampered_connect_args_before_replay(self):
        cases = (
            ("zero", {"max_turns": 0, "token_cap": 1000, "cost_cap": 1.0}),
            ("malformed", {
                "max_turns": "five", "token_cap": 1000, "cost_cap": 1.0}),
            ("over", {
                "max_turns": 5,
                "token_cap": config.AGENT_EDGE_TOKEN_CAP_CEILING + 1,
                "cost_cap": 1.0}),
            ("boolean", {
                "max_turns": 5, "token_cap": True, "cost_cap": 1.0}),
        )
        for suffix, caps in cases:
            with self.subTest(case=suffix):
                actor, f, human_node, row = self._connect_request(
                    f"ap_bad_{suffix}")
                args = dict(row.get("args") or {})
                args.update(caps)
                gs.patch_object("graph_edit", row["_guid"], {"args": args})

                with mock.patch.object(
                        gs, "create_edge", wraps=gs.create_edge) as replay, \
                     self.assertRaises(gs.GraphError):
                    guard.approve_pending(row["_guid"], actor="human")

                replay.assert_not_called()
                self.assertEqual(
                    gs.edges_from_to(f["_guid"], human_node["_guid"]), [])
                self.assertEqual(gs.get_object(row["_guid"])["result"], "pending")

    def test_approve_rejects_non_mapping_connect_args_before_replay(self):
        actor, f, human_node, row = self._connect_request("ap_bad_shape")
        gs.patch_object("graph_edit", row["_guid"], {"args": "not-a-mapping"})

        with mock.patch.object(gs, "create_edge", wraps=gs.create_edge) as replay, \
             self.assertRaises(gs.GraphError):
            guard.approve_pending(row["_guid"], actor="human")

        replay.assert_not_called()
        self.assertEqual(gs.edges_from_to(f["_guid"], human_node["_guid"]), [])
        self.assertEqual(gs.get_object(row["_guid"])["result"], "pending")

    def test_approve_requires_stored_requester_to_still_be_foreman(self):
        actor, f, human_node, row = self._connect_request("ap_lost_foreman")
        gs.update_agent(f["_guid"], can_edit_graph=False, actor="human")

        with mock.patch.object(gs, "create_edge", wraps=gs.create_edge) as replay, \
             self.assertRaises(gs.GraphError):
            guard.approve_pending(row["_guid"], actor="human")

        replay.assert_not_called()
        self.assertEqual(gs.edges_from_to(f["_guid"], human_node["_guid"]), [])
        self.assertEqual(gs.get_object(row["_guid"])["result"], "pending")

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

    def test_approve_rejects_tampered_cap_request_shape_before_replay(self):
        cases = (
            {"max_turns": 50, "source": "replacement"},
            {"max_turns": 50, "directed": False},
            {"max_turns": 50, "transform": "/tmp/evil.py"},
            {"max_turns": 50, "label": "smuggled"},
            {},
        )
        for index, fields in enumerate(cases):
            with self.subTest(fields=fields):
                a = gs.create_agent(
                    f"ap_shape_a_{index}",
                    home=f"/tmp/crew_pendingtest/ap_shape_a_{index}")
                b = gs.create_agent(
                    f"ap_shape_b_{index}",
                    home=f"/tmp/crew_pendingtest/ap_shape_b_{index}")
                edge = gs.create_edge(
                    a["_guid"], b["_guid"], max_turns=10)
                with self.assertRaises(gs.GraphError):
                    gs.update_edge(
                        edge["_guid"], {"max_turns": 50}, actor=a["name"])
                row = _pending_rows(actor=a["name"])[0]
                args = dict(row.get("args") or {})
                args["fields"] = fields
                gs.patch_object("graph_edit", row["_guid"], {"args": args})

                with mock.patch.object(
                        gs, "update_edge", wraps=gs.update_edge) as replay, \
                     self.assertRaises(gs.GraphError):
                    guard.approve_pending(row["_guid"], actor="human")

                replay.assert_not_called()
                self.assertEqual(
                    gs.get_object(edge["_guid"])["max_turns"], 10)
                self.assertEqual(
                    gs.get_object(row["_guid"])["result"], "pending")

    def test_approve_rejects_nonraising_or_nonendpoint_cap_request(self):
        requester = gs.create_agent(
            "ap_stale_endpoint_a",
            home="/tmp/crew_pendingtest/ap_stale_endpoint_a")
        peer = gs.create_agent(
            "ap_stale_endpoint_b",
            home="/tmp/crew_pendingtest/ap_stale_endpoint_b")
        edge = gs.create_edge(
            requester["_guid"], peer["_guid"], max_turns=10)
        with self.assertRaises(gs.GraphError):
            gs.update_edge(
                edge["_guid"], {"max_turns": 50}, actor=requester["name"])
        row = _pending_rows(actor=requester["name"])[0]

        args = dict(row.get("args") or {})
        args["fields"] = {"max_turns": 2}
        gs.patch_object("graph_edit", row["_guid"], {"args": args})
        with self.assertRaisesRegex(gs.GraphError, "raise"):
            guard.approve_pending(row["_guid"], actor="human")
        self.assertEqual(gs.get_object(row["_guid"])["result"], "pending")

        outsider_a = gs.create_agent(
            "ap_stale_outsider_a",
            home="/tmp/crew_pendingtest/ap_stale_outsider_a")
        outsider_b = gs.create_agent(
            "ap_stale_outsider_b",
            home="/tmp/crew_pendingtest/ap_stale_outsider_b")
        outsider_edge = gs.create_edge(
            outsider_a["_guid"], outsider_b["_guid"], max_turns=10)
        args["guid"] = outsider_edge["_guid"]
        args["fields"] = {"max_turns": 50}
        gs.patch_object("graph_edit", row["_guid"], {"args": args})
        with self.assertRaisesRegex(gs.GraphError, "endpoint"):
            guard.approve_pending(row["_guid"], actor="human")
        self.assertEqual(gs.get_object(row["_guid"])["result"], "pending")

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
    def test_reject_malformed_edge_fields_finishes_without_post_commit_crash(self):
        row = gs.create_object("graph_edit", {
            "actor": "human", "actor_guid": "", "op": "update_edge",
            "args": {"guid": "missing-edge", "fields": "corrupt"},
            "result": "pending", "reason": "", "created_at": int(time.time()),
        })

        rejected = guard.reject_pending(
            row["_guid"], reason="malformed request", actor="human")

        self.assertEqual(rejected["result"], "rejected")
        self.assertEqual(gs.get_object(row["_guid"])["result"], "rejected")

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
    def test_cli_pending_keeps_applying_and_failed_requests_visible(self):
        now = int(time.time())
        applying = gs.create_object("graph_edit", {
            "actor": "ux_actor", "actor_guid": "ux-guid",
            "op": "update_edge", "args": {"guid": "edge", "fields": {
                "max_turns": 50}}, "result": "applying", "reason": "",
            "created_at": now,
        })
        failed = gs.create_object("graph_edit", {
            "actor": "ux_actor", "actor_guid": "ux-guid",
            "op": "update_edge", "args": {"guid": "edge", "fields": {
                "max_turns": 50}}, "result": "approval_failed",
            "reason": "approval mutation failed: backend uncertain",
            "created_at": now + 1,
        })
        self.addCleanup(gs.delete_object, "graph_edit", applying["_guid"])
        self.addCleanup(gs.delete_object, "graph_edit", failed["_guid"])
        parser = cli.build_parser()
        stream = io.StringIO()

        with contextlib.redirect_stdout(stream):
            self.assertEqual(parser.parse_args(["pending"]).fn(
                parser.parse_args(["pending"])), 0)

        output = stream.getvalue()
        self.assertIn("applying", output)
        self.assertIn("approval_failed", output)
        self.assertIn("backend uncertain", output)
        self.assertIn("manual review", output.lower())

    def test_cli_pending_lists_malformed_args_without_crashing(self):
        f = _foreman("cli_malformed_f")
        human_node = gs.create_agent(
            "cli_malformed_h", home="/tmp/crew_pendingtest/cli_malformed_h")
        with self.assertRaises(gs.GraphError):
            gs.create_edge(
                f["_guid"], human_node["_guid"], actor=f["name"],
                max_turns=5, token_cap=1000, cost_cap=1.0)
        row = _pending_rows(actor=f["name"])[0]
        gs.patch_object("graph_edit", row["_guid"], {"args": "broken"})

        parser = cli.build_parser()
        stream = io.StringIO()
        with mock.patch.object(cli, "_ACTOR", "human"), \
             contextlib.redirect_stdout(stream):
            args = parser.parse_args(["pending"])
            self.assertEqual(args.fn(args), 0)
        self.assertIn("malformed stored args", stream.getvalue())

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
        rc, out, err = _run([
            "connect", self.f, self.human_node,
            "--max-turns", "5", "--token-cap", "1000", "--cost-cap", "1.0",
        ], env_extra={"CREW_AGENT": self.f})
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
