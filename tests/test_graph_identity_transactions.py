"""Graph mutations publish persistence and runtime identity as one outcome.

These regressions use a real throwaway MorphDB tenant and real identity files.
The injected failure occurs after the first endpoint was rewritten, which is the
dangerous partial-success window: persistence and every identity must return to
the prior graph, the original error must remain visible, and the audit log must
not claim that the mutation applied.
"""
import os
import sys
import tempfile
import threading
import unittest
from unittest import mock


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from crew import cli, config, graphstore as gs, guard, schema, spawn  # noqa: E402
from crew.server import app as dashboard_app  # noqa: E402


TEST_APP = f"crewtest-graph-identity-tx-{os.getpid()}"
INJECTED = "injected identity rewrite failure"


class GraphIdentityTransactionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._previous_app = os.environ.get("CREW_APP")
        cls._previous_project = os.environ.get("CREW_PROJECT")
        os.environ["CREW_APP"] = TEST_APP
        os.environ["CREW_PROJECT"] = config.DEFAULT_PROJECT
        try:
            gs._req("DELETE", f"/app/{TEST_APP}", app=None)
        except gs.GraphError:
            pass
        schema.ensure_schema(TEST_APP)
        cls._homes = tempfile.TemporaryDirectory(
            prefix="crew-graph-identity-tx-")

    @classmethod
    def tearDownClass(cls):
        cls._homes.cleanup()
        try:
            gs._req("DELETE", f"/app/{TEST_APP}", app=None)
        except gs.GraphError:
            pass
        if cls._previous_app is None:
            os.environ.pop("CREW_APP", None)
        else:
            os.environ["CREW_APP"] = cls._previous_app
        if cls._previous_project is None:
            os.environ.pop("CREW_PROJECT", None)
        else:
            os.environ["CREW_PROJECT"] = cls._previous_project

    def _agent(self, name, *, foreman=False):
        home = os.path.join(self._homes.name, name)
        os.makedirs(home)
        agent = gs.create_agent(
            name, home=home, runtime="custom", launch_cmd="exec sh",
            can_edit_graph=foreman)
        spawn.rewrite_identity(agent)
        return agent

    @staticmethod
    def _identity(agent):
        with open(os.path.join(agent["home"], config.IDENTITY_FILE)) as stream:
            return stream.read()

    @staticmethod
    def _fail_on_call(number):
        calls = []

        def rewrite(agent, notify=False):
            calls.append(agent["_guid"])
            if len(calls) == number:
                raise gs.GraphError(INJECTED)
            return spawn.rewrite_identity(agent, notify=notify)

        rewrite.calls = calls
        return rewrite

    @staticmethod
    def _audit(op, *, guid=None, source=None, target=None):
        rows = (gs.list_objects(
            "graph_edit", sort="created_at", order="desc", limit=1000
        ) or {}).get("objects", [])
        rows = [row for row in rows if row.get("op") == op]
        if guid is not None:
            rows = [row for row in rows if (row.get("args") or {}).get("guid") == guid]
        if source is not None:
            rows = [row for row in rows
                    if (row.get("args") or {}).get("source") == source
                    and (row.get("args") or {}).get("target") == target]
        return rows

    def test_ambiguous_delete_requires_confirmed_404_before_counting_as_rollback(self):
        outage = gs.GraphError("cannot reach MorphDB")
        with mock.patch.object(gs, "delete_object", side_effect=outage), \
             mock.patch.object(gs, "get_object", side_effect=outage):
            with self.assertRaisesRegex(gs.GraphError, "cannot reach"):
                gs._delete_object_verified("edge", "edge-uncertain")

        with mock.patch.object(gs, "delete_object", side_effect=outage), \
             mock.patch.object(
                 gs, "get_object", side_effect=gs.GraphError("404: no object")):
            self.assertIsNone(
                gs._delete_object_verified("edge", "edge-confirmed-gone"))

    def test_remove_agent_runs_its_authorization_gate_once(self):
        target = self._agent("tx_remove_single_gate")

        with mock.patch.object(
                guard, "check", wraps=guard.check) as check:
            gs.delete_agent(target["_guid"], actor="human")

        remove_calls = [
            call for call in check.call_args_list
            if len(call.args) >= 2 and call.args[1] == "remove"
        ]
        self.assertEqual(len(remove_calls), 1)

    def test_create_rolls_back_graph_and_both_identities_on_second_rewrite_failure(self):
        source = self._agent("tx_create_source")
        target = self._agent("tx_create_target")
        before = (self._identity(source), self._identity(target))
        rewrite = self._fail_on_call(2)

        with self.assertRaisesRegex(gs.GraphError, INJECTED):
            gs.create_edge(
                source["_guid"], target["_guid"], max_turns=5,
                actor="human", _identity_rewriter=rewrite)

        self.assertEqual(gs.edges_from_to(source["_guid"], target["_guid"]), [])
        self.assertEqual((self._identity(source), self._identity(target)), before)
        self.assertGreaterEqual(len(rewrite.calls), 4)
        rows = self._audit(
            "connect", source=source["_guid"], target=target["_guid"])
        self.assertEqual([row.get("result") for row in rows], ["failed"])
        self.assertIn(INJECTED, rows[0].get("reason") or "")

    def test_create_lost_post_response_is_reconciled_then_identities_publish(self):
        source = self._agent("tx_create_amb_source")
        target = self._agent("tx_create_amb_target")
        real_create = gs.create_object
        injected = False

        def commit_then_raise(otype, body):
            nonlocal injected
            result = real_create(otype, body)
            if otype == "edge" and not injected:
                injected = True
                raise gs.GraphError("lost create response")
            return result

        with mock.patch.object(gs, "create_object", side_effect=commit_then_raise):
            edge = gs.create_edge(
                source["_guid"], target["_guid"], actor="human",
                _identity_rewriter=spawn.rewrite_identity)

        self.assertEqual(edge["source"], source["_guid"])
        self.assertEqual(edge["target"], target["_guid"])
        self.assertIn(target["name"], self._identity(source))
        rows = self._audit(
            "connect", source=source["_guid"], target=target["_guid"])
        self.assertEqual([row.get("result") for row in rows], ["applied"])

    def test_update_restores_edge_and_identities_on_rewrite_failure(self):
        source = self._agent("tx_update_source")
        target = self._agent("tx_update_target")
        edge = gs.create_edge(
            source["_guid"], target["_guid"], max_turns=5, actor="human")
        spawn.rewrite_identity(source)
        spawn.rewrite_identity(target)
        before = (self._identity(source), self._identity(target))

        with self.assertRaisesRegex(gs.GraphError, INJECTED):
            gs.update_edge(
                edge["_guid"], {"max_turns": 50}, actor="human",
                _identity_rewriter=self._fail_on_call(2))

        self.assertEqual(gs.get_object(edge["_guid"])["max_turns"], 5)
        self.assertEqual((self._identity(source), self._identity(target)), before)
        rows = self._audit("update_edge", guid=edge["_guid"])
        self.assertEqual([row.get("result") for row in rows], ["failed"])

    def test_update_lost_patch_response_is_verified_then_identities_publish(self):
        source = self._agent("tx_update_amb_source")
        target = self._agent("tx_update_amb_target")
        edge = gs.create_edge(
            source["_guid"], target["_guid"], max_turns=5, actor="human")
        real_patch = gs.patch_object
        injected = False

        def commit_then_raise(otype, guid, body):
            nonlocal injected
            result = real_patch(otype, guid, body)
            if (otype == "edge" and guid == edge["_guid"]
                    and body.get("max_turns") == 50 and not injected):
                injected = True
                raise gs.GraphError("lost patch response")
            return result

        with mock.patch.object(gs, "patch_object", side_effect=commit_then_raise):
            updated = gs.update_edge(
                edge["_guid"], {"max_turns": 50}, actor="human",
                _identity_rewriter=spawn.rewrite_identity)

        self.assertEqual(updated["max_turns"], 50)
        self.assertIn("at most 50 message(s) per hour", self._identity(source))
        rows = self._audit("update_edge", guid=edge["_guid"])
        self.assertEqual([row.get("result") for row in rows], ["applied"])

    def test_delete_restores_same_edge_and_identities_on_rewrite_failure(self):
        source = self._agent("tx_delete_source")
        target = self._agent("tx_delete_target")
        edge = gs.create_edge(
            source["_guid"], target["_guid"], label="keep me", actor="human")
        spawn.rewrite_identity(source)
        spawn.rewrite_identity(target)
        before = (self._identity(source), self._identity(target))

        with self.assertRaisesRegex(gs.GraphError, INJECTED):
            gs.delete_edge(
                edge["_guid"], actor="human",
                _identity_rewriter=self._fail_on_call(2))

        restored = gs.get_object(edge["_guid"])
        self.assertEqual(restored["_guid"], edge["_guid"])
        self.assertEqual(restored["label"], "keep me")
        self.assertEqual((self._identity(source), self._identity(target)), before)
        rows = self._audit("disconnect", guid=edge["_guid"])
        self.assertEqual([row.get("result") for row in rows], ["failed"])

    def test_delete_lost_response_is_verified_then_identities_publish(self):
        source = self._agent("tx_delete_amb_source")
        target = self._agent("tx_delete_amb_target")
        edge = gs.create_edge(source["_guid"], target["_guid"], actor="human")
        spawn.rewrite_identity(source)
        spawn.rewrite_identity(target)
        real_delete = gs.delete_object
        injected = False

        def commit_then_raise(otype, guid):
            nonlocal injected
            result = real_delete(otype, guid)
            if otype == "edge" and guid == edge["_guid"] and not injected:
                injected = True
                raise gs.GraphError("lost delete response")
            return result

        with mock.patch.object(gs, "delete_object", side_effect=commit_then_raise):
            gs.delete_edge(
                edge["_guid"], actor="human",
                _identity_rewriter=spawn.rewrite_identity)

        self.assertEqual(gs.edges_from_to(source["_guid"], target["_guid"]), [])
        self.assertNotIn(target["name"], self._identity(source))
        self.assertNotIn(source["name"], self._identity(target))
        rows = self._audit("disconnect", guid=edge["_guid"])
        self.assertEqual([row.get("result") for row in rows], ["applied"])

    def test_batch_disconnect_restores_every_edge_before_rewriting_old_truth(self):
        source = self._agent("tx_batch_source")
        target = self._agent("tx_batch_target")
        forward = gs.create_edge(source["_guid"], target["_guid"], actor="human")
        backward = gs.create_edge(target["_guid"], source["_guid"], actor="human")
        spawn.rewrite_identity(source)
        spawn.rewrite_identity(target)
        before = (self._identity(source), self._identity(target))

        with self.assertRaisesRegex(gs.GraphError, INJECTED):
            gs.disconnect_between(
                source["_guid"], target["_guid"], actor="human",
                _identity_rewriter=self._fail_on_call(2))

        self.assertEqual(
            {edge["_guid"] for edge in gs.edges_from_to(source["_guid"], target["_guid"])
             + gs.edges_from_to(target["_guid"], source["_guid"])},
            {forward["_guid"], backward["_guid"]})
        self.assertEqual((self._identity(source), self._identity(target)), before)
        for guid in (forward["_guid"], backward["_guid"]):
            rows = self._audit("disconnect", guid=guid)
            self.assertEqual([row.get("result") for row in rows], ["failed"])

    def test_batch_disconnect_restores_first_edge_when_second_delete_fails(self):
        source = self._agent("tx_batch_delete_source")
        target = self._agent("tx_batch_delete_target")
        forward = gs.create_edge(source["_guid"], target["_guid"], actor="human")
        backward = gs.create_edge(target["_guid"], source["_guid"], actor="human")
        spawn.rewrite_identity(source)
        spawn.rewrite_identity(target)
        before = (self._identity(source), self._identity(target))
        real_delete = gs.delete_object
        calls = []

        def fail_second(otype, guid):
            if otype == "edge":
                calls.append(guid)
                if len(calls) == 2:
                    raise gs.GraphError("injected second delete failure")
            return real_delete(otype, guid)

        with mock.patch.object(gs, "delete_object", side_effect=fail_second):
            with self.assertRaisesRegex(gs.GraphError, "second delete"):
                gs.disconnect_between(
                    source["_guid"], target["_guid"], actor="human",
                    _identity_rewriter=spawn.rewrite_identity)

        remaining = {
            edge["_guid"] for edge in
            gs.edges_from_to(source["_guid"], target["_guid"])
            + gs.edges_from_to(target["_guid"], source["_guid"])
        }
        self.assertEqual(remaining, {forward["_guid"], backward["_guid"]})
        self.assertEqual((self._identity(source), self._identity(target)), before)
        for guid in remaining:
            rows = self._audit("disconnect", guid=guid)
            self.assertEqual([row.get("result") for row in rows], ["failed"])

    def test_foreman_flag_rolls_back_when_its_identity_cannot_be_published(self):
        agent = self._agent("tx_foreman")
        before = self._identity(agent)

        with self.assertRaisesRegex(gs.GraphError, INJECTED):
            gs.set_foreman(
                agent["_guid"], revoke=False, actor="human",
                _identity_rewriter=self._fail_on_call(1))

        self.assertFalse(gs.get_object(agent["_guid"])["can_edit_graph"])
        self.assertEqual(self._identity(agent), before)
        rows = [row for row in self._audit("foreman")
                if (row.get("args") or {}).get("name") == agent["name"]]
        self.assertEqual([row.get("result") for row in rows], ["failed"])

    def test_foreman_lost_patch_response_is_verified_then_identity_publishes(self):
        agent = self._agent("tx_foreman_ambiguous")
        real_patch = gs.patch_object
        injected = False

        def commit_then_raise(otype, guid, body):
            nonlocal injected
            result = real_patch(otype, guid, body)
            if (otype == "agent" and guid == agent["_guid"]
                    and body.get("can_edit_graph") is True and not injected):
                injected = True
                raise gs.GraphError("lost foreman response")
            return result

        with mock.patch.object(gs, "patch_object", side_effect=commit_then_raise):
            updated = gs.set_foreman(
                agent["_guid"], actor="human",
                _identity_rewriter=spawn.rewrite_identity)
        self.addCleanup(
            gs.patch_object, "agent", agent["_guid"],
            {"can_edit_graph": False})
        self.assertTrue(updated["can_edit_graph"])
        self.assertIn("## Graph powers", self._identity(agent))

    def test_grant_and_foreman_cannot_publish_stale_agent_snapshots(self):
        agent = self._agent("tx_grant_foreman_race")
        self.addCleanup(
            gs.patch_object, "agent", agent["_guid"],
            {"can_edit_graph": False})
        grant_target = os.path.join(self._homes.name, "tx_grant_source")
        os.makedirs(grant_target)
        original_rewrite = spawn.rewrite_identity
        grant_paused = threading.Event()
        foreman_attempted = threading.Event()
        foreman_finished = threading.Event()
        gate_used = threading.Event()
        errors = []

        def controlled_rewrite(snapshot, notify=False):
            if (threading.current_thread().name == "tx-grant"
                    and not gate_used.is_set()):
                gate_used.set()
                grant_paused.set()
                if not foreman_attempted.wait(2):
                    raise AssertionError("foreman never attempted concurrent mutation")
                # On the vulnerable implementation foreman completes here,
                # then this stale pre-foreman grant snapshot overwrites it.
                foreman_finished.wait(0.5)
            return original_rewrite(snapshot, notify=notify)

        def run_grant():
            try:
                spawn.grant_path(agent["name"], grant_target, actor="human")
            except Exception as error:
                errors.append(error)

        def run_foreman():
            try:
                if not grant_paused.wait(2):
                    raise AssertionError("grant never reached identity publication")
                foreman_attempted.set()
                gs.set_foreman(
                    agent["_guid"], actor="human",
                    _identity_rewriter=spawn.rewrite_identity)
            except Exception as error:
                errors.append(error)
            finally:
                foreman_finished.set()

        with mock.patch.object(spawn, "rewrite_identity", controlled_rewrite):
            grant_thread = threading.Thread(target=run_grant, name="tx-grant")
            foreman_thread = threading.Thread(target=run_foreman, name="tx-foreman")
            grant_thread.start()
            foreman_thread.start()
            grant_thread.join(5)
            foreman_thread.join(5)

        self.assertFalse(grant_thread.is_alive())
        self.assertFalse(foreman_thread.is_alive())
        self.assertEqual(errors, [])
        refreshed = gs.get_object(agent["_guid"])
        self.assertTrue(refreshed["can_edit_graph"])
        self.assertEqual(len(refreshed.get("grants") or []), 1)
        text = self._identity(agent)
        self.assertIn("## Graph powers", text)
        self.assertIn("## File grants", text)

    def test_pending_approval_records_failure_and_does_not_apply_stale_graph(self):
        foreman = self._agent("tx_pending_foreman", foreman=True)
        self.addCleanup(
            gs.patch_object, "agent", foreman["_guid"],
            {"can_edit_graph": False})
        target = self._agent("tx_pending_target")
        with self.assertRaisesRegex(gs.GraphError, "queued"):
            gs.create_edge(
                foreman["_guid"], target["_guid"], actor=foreman["name"],
                max_turns=5, token_cap=1000, cost_cap=1.0)
        pending = (gs.list_objects(
            "graph_edit", result="pending", actor=foreman["name"], limit=20
        ) or {}).get("objects", [])[0]

        with mock.patch.object(
                spawn, "rewrite_identity", side_effect=gs.GraphError(INJECTED)):
            with self.assertRaisesRegex(gs.GraphError, INJECTED):
                guard.approve_pending(pending["_guid"], actor="human")

        self.assertEqual(
            gs.get_object(pending["_guid"])["result"], "approval_failed")
        self.assertEqual(gs.edges_from_to(foreman["_guid"], target["_guid"]), [])

    def test_cli_cap_uses_transaction_instead_of_leaving_identity_stale(self):
        source = self._agent("tx_cap_source")
        target = self._agent("tx_cap_target")
        edge = gs.create_edge(
            source["_guid"], target["_guid"], max_turns=5, actor="human")
        parser = cli.build_parser()
        args = parser.parse_args([
            "cap", source["name"], target["name"], "--max-turns", "50"])

        with mock.patch.object(cli, "_ACTOR", "human"), \
             mock.patch.object(
                 spawn, "rewrite_identity", side_effect=gs.GraphError(INJECTED)):
            with self.assertRaisesRegex(gs.GraphError, INJECTED):
                args.fn(args)

        self.assertEqual(gs.get_object(edge["_guid"])["max_turns"], 5)

    def test_cli_connect_and_disconnect_surface_rewrite_failures(self):
        source = self._agent("tx_cli_edge_source")
        target = self._agent("tx_cli_edge_target")
        parser = cli.build_parser()
        connect = parser.parse_args(["connect", source["name"], target["name"]])
        with mock.patch.object(cli, "_ACTOR", "human"), \
             mock.patch.object(
                 spawn, "rewrite_identity", side_effect=gs.GraphError(INJECTED)):
            with self.assertRaisesRegex(gs.GraphError, INJECTED):
                connect.fn(connect)
        self.assertEqual(gs.edges_from_to(source["_guid"], target["_guid"]), [])

        edge = gs.create_edge(source["_guid"], target["_guid"], actor="human")
        disconnect = parser.parse_args([
            "disconnect", source["name"], target["name"]])
        with mock.patch.object(cli, "_ACTOR", "human"), \
             mock.patch.object(
                 spawn, "rewrite_identity", side_effect=gs.GraphError(INJECTED)):
            with self.assertRaisesRegex(gs.GraphError, INJECTED):
                disconnect.fn(disconnect)
        self.assertEqual(gs.get_object(edge["_guid"])["_guid"], edge["_guid"])

    def test_cli_foreman_surfaces_rewrite_failure_and_restores_flag(self):
        agent = self._agent("tx_cli_foreman")
        parser = cli.build_parser()
        args = parser.parse_args(["foreman", agent["name"]])
        with mock.patch.object(cli, "_ACTOR", "human"), \
             mock.patch.object(
                 spawn, "rewrite_identity", side_effect=gs.GraphError(INJECTED)):
            with self.assertRaisesRegex(gs.GraphError, INJECTED):
                args.fn(args)
        self.assertFalse(gs.get_object(agent["_guid"])["can_edit_graph"])

    def test_dashboard_create_returns_error_and_leaves_no_edge(self):
        source = self._agent("tx_dashboard_source")
        target = self._agent("tx_dashboard_target")
        handler = object.__new__(dashboard_app.Handler)
        responses = []
        handler._json = lambda body, *args: responses.append(body)

        with mock.patch.object(
                dashboard_app.spawn, "rewrite_identity",
                side_effect=gs.GraphError(INJECTED)):
            handler._edge_create({
                "source": source["name"], "target": target["name"],
                "directed": True})

        self.assertEqual(responses, [{"ok": False, "error": mock.ANY}])
        self.assertIn(INJECTED, responses[0]["error"])
        self.assertEqual(gs.edges_from_to(source["_guid"], target["_guid"]), [])

    def test_dashboard_update_and_delete_return_error_with_prior_edge_intact(self):
        source = self._agent("tx_dashboard_mut_source")
        target = self._agent("tx_dashboard_mut_target")
        edge = gs.create_edge(
            source["_guid"], target["_guid"], max_turns=5, actor="human")
        handler = object.__new__(dashboard_app.Handler)
        responses = []
        handler._json = lambda body, *args: responses.append(body)

        with mock.patch.object(
                dashboard_app.spawn, "rewrite_identity",
                side_effect=gs.GraphError(INJECTED)):
            handler._edge_update({"guid": edge["_guid"], "max_turns": 50})
        self.assertFalse(responses[-1]["ok"])
        self.assertIn(INJECTED, responses[-1]["error"])
        self.assertEqual(gs.get_object(edge["_guid"])["max_turns"], 5)

        with mock.patch.object(
                dashboard_app.spawn, "rewrite_identity",
                side_effect=gs.GraphError(INJECTED)):
            handler._edge_delete({"guid": edge["_guid"]})
        self.assertFalse(responses[-1]["ok"])
        self.assertIn(INJECTED, responses[-1]["error"])
        self.assertEqual(gs.get_object(edge["_guid"])["_guid"], edge["_guid"])

    def test_dashboard_foreman_returns_error_and_restores_flag(self):
        agent = self._agent("tx_dashboard_foreman")
        handler = object.__new__(dashboard_app.Handler)
        responses = []
        handler._json = lambda body, *args: responses.append(body)
        with mock.patch.object(
                dashboard_app.spawn, "rewrite_identity",
                side_effect=gs.GraphError(INJECTED)):
            handler._agent_foreman({"name": agent["name"]})
        self.assertFalse(responses[-1]["ok"])
        self.assertIn(INJECTED, responses[-1]["error"])
        self.assertFalse(gs.get_object(agent["_guid"])["can_edit_graph"])

    def test_dashboard_pending_approval_returns_error_and_marks_failed(self):
        foreman = self._agent("tx_dashboard_pending_f", foreman=True)
        self.addCleanup(
            gs.patch_object, "agent", foreman["_guid"],
            {"can_edit_graph": False})
        target = self._agent("tx_dashboard_pending_h")
        with self.assertRaisesRegex(gs.GraphError, "queued"):
            gs.create_edge(
                foreman["_guid"], target["_guid"], actor=foreman["name"],
                max_turns=5, token_cap=1000, cost_cap=1.0)
        pending = (gs.list_objects(
            "graph_edit", result="pending", actor=foreman["name"], limit=20
        ) or {}).get("objects", [])[0]
        handler = object.__new__(dashboard_app.Handler)
        responses = []
        handler._json = lambda body, *args: responses.append(body)
        with mock.patch.object(
                dashboard_app.spawn, "rewrite_identity",
                side_effect=gs.GraphError(INJECTED)):
            handler._pending_approve({"guid": pending["_guid"]})
        self.assertFalse(responses[-1]["ok"])
        self.assertIn(INJECTED, responses[-1]["error"])
        self.assertEqual(
            gs.get_object(pending["_guid"])["result"], "approval_failed")
        self.assertEqual(gs.edges_from_to(foreman["_guid"], target["_guid"]), [])


class EdgeIdentityLockPlanningTests(unittest.TestCase):
    """Endpoint changes observed after lock planning must widen via retry."""

    @staticmethod
    def _recording_transaction(plans):
        def transaction(agent_guids):
            plans.append(tuple(guid for guid in agent_guids if guid))
            return gs.contextlib.nullcontext()
        return transaction

    def test_update_retries_when_locked_edge_has_an_unplanned_endpoint(self):
        first = {"_guid": "edge", "source": "a", "target": "b",
                 "directed": True}
        moved = {"_guid": "edge", "source": "a", "target": "c",
                 "directed": True}
        updated = dict(moved, target="d")
        plans = []

        with mock.patch.object(
                gs, "get_object",
                side_effect=(first, moved, moved, moved)), \
             mock.patch.object(
                 gs, "_edge_identity_transaction",
                 side_effect=self._recording_transaction(plans)), \
             mock.patch.object(gs.guard, "check"), \
             mock.patch.object(gs.guard, "audit"), \
             mock.patch.object(gs, "_validate_edge_contract"), \
             mock.patch.object(
                 gs, "_patch_object_verified", return_value=updated) as patcher:
            result = gs.update_edge("edge", {"target": "d"})

        self.assertEqual(result, updated)
        self.assertEqual(plans, [("a", "b", "d"), ("a", "c", "d")])
        patcher.assert_called_once_with("edge", "edge", {"target": "d"})

    def test_delete_retries_when_locked_edge_has_an_unplanned_endpoint(self):
        first = {"_guid": "edge", "source": "a", "target": "b",
                 "directed": True}
        moved = {"_guid": "edge", "source": "a", "target": "c",
                 "directed": True}
        plans = []

        with mock.patch.object(
                gs, "get_object",
                side_effect=(first, moved, moved, moved)), \
             mock.patch.object(
                 gs, "_edge_identity_transaction",
                 side_effect=self._recording_transaction(plans)), \
             mock.patch.object(gs.guard, "check"), \
             mock.patch.object(gs.guard, "audit"), \
             mock.patch.object(
                 gs, "_delete_object_verified", return_value=None) as deleter:
            self.assertIsNone(gs.delete_edge("edge"))

        self.assertEqual(plans, [("a", "b"), ("a", "c")])
        deleter.assert_called_once_with("edge", "edge")

    def test_create_revalidates_nonhuman_actor_after_identity_lock_wait(self):
        foreman = {"_guid": "foreman-guid", "name": "foreman",
                   "can_edit_graph": True}
        creator = mock.Mock()
        with mock.patch.object(
                gs, "get_agent_by_name", side_effect=(foreman, None)), \
             mock.patch.object(gs.guard, "check"), \
             mock.patch.object(
                 gs, "_edge_identity_transaction",
                 return_value=gs.contextlib.nullcontext()), \
             mock.patch.object(gs, "_create_edge_verified", creator):
            with self.assertRaisesRegex(
                    gs.GraphError, "actor identity.*changed|no longer exists"):
                gs.create_edge(
                    "source", "target", actor="foreman", max_turns=1,
                    token_cap=1, cost_cap=1)
        creator.assert_not_called()

    def test_preapproved_create_revalidates_pinned_requester_after_lock_wait(self):
        creator = mock.Mock()
        with mock.patch.object(gs, "get_agent_by_name", return_value=None), \
             mock.patch.object(
                 gs, "_edge_identity_transaction",
                 return_value=gs.contextlib.nullcontext()), \
             mock.patch.object(gs, "_create_edge_verified", creator):
            with self.assertRaisesRegex(
                    gs.GraphError, "actor identity.*changed|no longer exists"):
                gs.create_edge(
                    "source", "target", actor="foreman", max_turns=1,
                    token_cap=1, cost_cap=1, _pre_approved=True,
                    _actor_guid="foreman-guid")
        creator.assert_not_called()

    def test_create_refuses_endpoint_deleted_before_identity_lock_acquisition(self):
        creator = mock.Mock()
        with mock.patch.object(
                gs, "get_object",
                side_effect=({"_guid": "source"},
                             gs.GraphError("404: target deleted"))), \
             mock.patch.object(gs.guard, "check"), \
             mock.patch.object(
                 gs, "_edge_identity_transaction",
                 return_value=gs.contextlib.nullcontext()), \
             mock.patch.object(gs, "_create_edge_verified", creator):
            with self.assertRaisesRegex(gs.GraphError, "404: target deleted"):
                gs.create_edge("source", "target")
        creator.assert_not_called()

    def test_update_revalidates_nonhuman_actor_when_edge_is_unchanged(self):
        edge = {"_guid": "edge", "source": "actor-guid", "target": "peer",
                "directed": True, "max_turns": 5}
        actor = {"_guid": "actor-guid", "name": "actor"}
        patcher = mock.Mock()

        with mock.patch.object(gs, "get_object", side_effect=(edge, edge)), \
             mock.patch.object(
                 gs, "get_agent_by_name", side_effect=(actor, None)), \
             mock.patch.object(gs.guard, "check"), \
             mock.patch.object(gs.guard, "audit"), \
             mock.patch.object(
                 gs, "_edge_identity_transaction",
                 return_value=gs.contextlib.nullcontext()), \
             mock.patch.object(gs, "_validate_edge_contract"), \
             mock.patch.object(gs, "_patch_object_verified", patcher):
            with self.assertRaisesRegex(
                    gs.GraphError, "actor identity.*changed|no longer exists"):
                gs.update_edge(
                    "edge", {"max_turns": 4}, actor="actor")

        patcher.assert_not_called()

    def test_update_cannot_rewrite_immutable_edge_provenance(self):
        edge = {
            "_guid": "edge", "source": "source", "target": "target",
            "directed": True, "created_by": "original",
            "created_by_guid": "original-guid", "created_at": 123,
            "blessed": False,
        }
        patcher = mock.Mock()

        with mock.patch.object(gs, "get_object", return_value=edge), \
             mock.patch.object(gs.guard, "check"), \
             mock.patch.object(gs.guard, "audit"), \
             mock.patch.object(gs, "_patch_object_verified", patcher):
            with self.assertRaisesRegex(
                    gs.GraphError, "immutable|cannot be updated"):
                gs.update_edge("edge", {
                    "created_by": "replacement",
                    "created_by_guid": "replacement-guid",
                    "created_at": 0,
                    "blessed": True,
                }, actor="human")

        patcher.assert_not_called()

    def test_applied_edge_audit_uses_the_transaction_pinned_actor_guid(self):
        edge = {
            "_guid": "edge", "source": "actor-guid", "target": "peer",
            "directed": True, "max_turns": 5,
        }
        updated = dict(edge, max_turns=4)
        actor = {"_guid": "actor-guid", "name": "actor"}

        with mock.patch.object(gs, "get_object", side_effect=(edge, edge)), \
             mock.patch.object(gs, "get_agent_by_name", return_value=actor), \
             mock.patch.object(gs.guard, "check"), \
             mock.patch.object(gs.guard, "audit") as audit, \
             mock.patch.object(
                 gs, "_edge_identity_transaction",
                 return_value=gs.contextlib.nullcontext()), \
             mock.patch.object(gs, "_validate_edge_contract"), \
             mock.patch.object(
                 gs, "_patch_object_verified", return_value=updated):
            gs.update_edge("edge", {"max_turns": 4}, actor="actor")

        self.assertEqual(audit.call_args.kwargs.get("actor_guid"), "actor-guid")

    def test_agent_update_revalidates_foreman_identity_inside_agent_lock(self):
        foreman = {"_guid": "foreman-guid", "name": "foreman"}
        child = {
            "_guid": "child-guid", "name": "child",
            "created_by_guid": "foreman-guid",
        }
        patcher = mock.Mock()

        with mock.patch.object(
                 gs, "get_agent_by_name", side_effect=(foreman, None)), \
             mock.patch.object(
                 gs, "_invariant_lock",
                 return_value=gs.contextlib.nullcontext()), \
             mock.patch.object(gs, "get_object", return_value=child), \
             mock.patch.object(gs.guard, "check"), \
             mock.patch.object(gs.guard, "audit"), \
             mock.patch.object(gs, "patch_object", patcher):
            with self.assertRaisesRegex(
                    gs.GraphError, "actor identity.*changed|no longer exists"):
                gs.update_agent(
                    "child-guid", actor="foreman", notes="reviewed")

        patcher.assert_not_called()

    def test_agent_create_revalidates_foreman_identity_at_commit(self):
        foreman = {"_guid": "foreman-guid", "name": "foreman"}
        actor_lookups = iter((foreman, None))

        def lookup(name, app=gs._CURRENT_APP):
            if name == "foreman":
                return next(actor_lookups)
            return None

        creator = mock.Mock()
        with mock.patch.object(gs, "get_agent_by_name", side_effect=lookup), \
             mock.patch.object(
                 gs, "_invariant_lock",
                 return_value=gs.contextlib.nullcontext()), \
             mock.patch.object(gs.guard, "check"), \
             mock.patch.object(gs.guard, "audit"), \
             mock.patch.object(gs, "_create_agent_verified", creator):
            with self.assertRaisesRegex(
                    gs.GraphError, "actor identity.*changed|no longer exists"):
                gs.create_agent(
                    "child", home="/tmp/child", actor="foreman",
                    runtime="custom", launch_cmd="true")

        creator.assert_not_called()

    def test_delete_rechecks_foreman_authority_under_actor_lock(self):
        edge = {"_guid": "edge", "source": "child-a", "target": "child-b",
                "directed": True}
        foreman = {"_guid": "foreman-guid", "name": "foreman",
                   "can_edit_graph": True}
        plans = []
        deleter = mock.Mock()

        with mock.patch.object(gs, "get_object", side_effect=(edge, edge)), \
             mock.patch.object(gs, "get_agent_by_name", return_value=foreman), \
             mock.patch.object(
                 gs.guard, "check",
                 side_effect=(None, gs.GraphError("foreman was revoked"))), \
             mock.patch.object(gs.guard, "audit"), \
             mock.patch.object(
                 gs, "_edge_identity_transaction",
                 side_effect=self._recording_transaction(plans)), \
             mock.patch.object(gs, "_delete_object_verified", deleter):
            with self.assertRaisesRegex(gs.GraphError, "foreman was revoked"):
                gs.delete_edge("edge", actor="foreman")

        self.assertEqual(plans, [("child-a", "child-b", "foreman-guid")])
        deleter.assert_not_called()

    def test_disconnect_batch_revalidates_nonhuman_actor_before_delete(self):
        foreman = {"_guid": "foreman-guid", "name": "foreman",
                   "can_edit_graph": True}
        edge = {"_guid": "edge", "source": "child-a", "target": "child-b"}
        plans = []
        deleter = mock.Mock()

        with mock.patch.object(
                 gs, "get_agent_by_name", side_effect=(foreman, None)), \
             mock.patch.object(
                 gs, "_edge_identity_transaction",
                 side_effect=self._recording_transaction(plans)), \
             mock.patch.object(gs, "edges_from_to", side_effect=([edge], [])), \
             mock.patch.object(gs.guard, "check"), \
             mock.patch.object(gs.guard, "audit"), \
             mock.patch.object(gs, "_delete_object_verified", deleter):
            with self.assertRaisesRegex(
                    gs.GraphError, "actor identity.*changed|no longer exists"):
                gs.disconnect_between(
                    "child-a", "child-b", actor="foreman")

        self.assertEqual(plans, [("child-a", "child-b", "foreman-guid")])
        deleter.assert_not_called()

    def test_delete_aborts_when_optimistic_edge_read_is_uncertain(self):
        deleter = mock.Mock()
        with mock.patch.object(
                gs, "get_object", side_effect=gs.GraphError("503: unavailable")), \
             mock.patch.object(gs.guard, "check"), \
             mock.patch.object(gs, "_delete_object_verified", deleter):
            with self.assertRaisesRegex(gs.GraphError, "503: unavailable"):
                gs.delete_edge("edge")
        deleter.assert_not_called()

    def test_delete_aborts_when_locked_edge_read_is_uncertain(self):
        edge = {"_guid": "edge", "source": "a", "target": "b"}
        deleter = mock.Mock()
        with mock.patch.object(
                gs, "get_object",
                side_effect=(edge, gs.GraphError("503: unavailable"))), \
             mock.patch.object(gs.guard, "check"), \
             mock.patch.object(
                 gs, "_edge_identity_transaction",
                 return_value=gs.contextlib.nullcontext()), \
             mock.patch.object(gs, "_delete_object_verified", deleter):
            with self.assertRaisesRegex(gs.GraphError, "503: unavailable"):
                gs.delete_edge("edge")
        deleter.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
