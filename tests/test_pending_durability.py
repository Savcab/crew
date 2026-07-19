"""Durability and exactly-once regressions for pending graph requests.

These tests use a dedicated MorphDB tenant and exercise the real persistence
boundary.  Resolution races are synchronized around the replay mutation so
they prove the request itself is claimed, rather than relying on an eventual
edge uniqueness error to hide a double approval.
"""
import multiprocessing
import os
import sys
import threading
import time
import unittest
import uuid
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_APP = "crewtest-pending-durability"

from crew import graphstore as gs, guard, schema  # noqa: E402

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


def tearDownModule():
    try:
        try:
            gs._req("DELETE", f"/app/{TEST_APP}", app=None)
        except gs.GraphError:
            pass
    finally:
        _CREW_APP_PATCHER.stop()


def _name(stem):
    return f"pd_{stem}_{uuid.uuid4().hex[:10]}"


def _pending_rows(actor):
    result = gs.list_objects(
        "graph_edit", actor=actor, result="pending", limit=50,
        sort="created_at", order="desc")
    return (result or {}).get("objects", [])


def _connect_request(stem):
    actor = _name(f"{stem}_f")
    target = _name(f"{stem}_h")
    for current in gs.list_agents():
        if current.get("can_edit_graph"):
            gs.set_foreman(current["_guid"], revoke=True, actor="human")
    source_row = gs.create_agent(
        actor, home=f"/tmp/crew_pending_durability/{actor}",
        can_edit_graph=True)
    target_row = gs.create_agent(
        target, home=f"/tmp/crew_pending_durability/{target}")
    try:
        gs.create_edge(
            source_row["_guid"], target_row["_guid"], actor=actor,
            max_turns=5, token_cap=1000, cost_cap=1.0)
    except gs.GraphError:
        pass
    else:
        raise AssertionError("out-of-envelope connect was not queued")
    row = _pending_rows(actor)[0]
    return actor, source_row, target_row, row


def _cap_raise_request(stem):
    source = _name(f"{stem}_a")
    target = _name(f"{stem}_b")
    source_row = gs.create_agent(
        source, home=f"/tmp/crew_pending_durability/{source}")
    target_row = gs.create_agent(
        target, home=f"/tmp/crew_pending_durability/{target}")
    edge = gs.create_edge(
        source_row["_guid"], target_row["_guid"], max_turns=5)
    try:
        gs.update_edge(edge["_guid"], {"max_turns": 10}, actor=source)
    except gs.GraphError:
        pass
    else:
        raise AssertionError("cap raise was not queued")
    row = _pending_rows(source)[0]
    return source, edge, row


def _configure_process(host, app):
    from crew import config as worker_config
    worker_config.MORPHDB_HOST = host
    os.environ["CREW_APP"] = app
    os.environ.pop("CREW_PROJECT", None)


def _approve_process(host, app, guid, entered, release, outcomes):
    _configure_process(host, app)
    original_create = gs.create_edge

    def blocked_create(*args, **kwargs):
        entered.set()
        if not release.wait(10):
            raise AssertionError("timed out waiting to release process replay")
        return original_create(*args, **kwargs)

    gs.create_edge = blocked_create
    try:
        guard.approve_pending(guid, actor="human")
    except Exception as error:
        outcomes.put(("approve", "error", f"{type(error).__name__}: {error}"))
    else:
        outcomes.put(("approve", "ok", ""))


def _reject_process(host, app, guid, outcomes):
    _configure_process(host, app)
    try:
        guard.reject_pending(guid, reason="process race", actor="human")
    except Exception as error:
        outcomes.put(("reject", "error", f"{type(error).__name__}: {error}"))
    else:
        outcomes.put(("reject", "ok", ""))


class PendingDurabilityTests(unittest.TestCase):
    def test_pending_write_failure_is_reported_and_never_claimed_as_queued(self):
        source = _name("persist_a")
        target = _name("persist_b")
        source_row = gs.create_agent(
            source, home=f"/tmp/crew_pending_durability/{source}")
        target_row = gs.create_agent(
            target, home=f"/tmp/crew_pending_durability/{target}")
        edge = gs.create_edge(
            source_row["_guid"], target_row["_guid"], max_turns=5)
        original_create = gs.create_object

        def fail_pending(otype, body):
            if otype == "graph_edit" and body.get("result") == "pending":
                raise gs.GraphError("pending persistence unavailable")
            return original_create(otype, body)

        with mock.patch.object(gs, "create_object", side_effect=fail_pending):
            with self.assertRaises(gs.GraphError) as raised:
                gs.update_edge(edge["_guid"], {"max_turns": 10}, actor=source)

        self.assertIn("pending persistence unavailable", str(raised.exception))
        self.assertNotIn("queued", str(raised.exception).lower())
        self.assertEqual(gs.get_object(edge["_guid"])["max_turns"], 5)
        self.assertEqual(_pending_rows(source), [])

    def test_lost_pending_create_response_reconciles_exact_committed_request(self):
        source = _name("lost_pending_a")
        target = _name("lost_pending_b")
        source_row = gs.create_agent(
            source, home=f"/tmp/crew_pending_durability/{source}")
        target_row = gs.create_agent(
            target, home=f"/tmp/crew_pending_durability/{target}")
        edge = gs.create_edge(
            source_row["_guid"], target_row["_guid"], max_turns=5)
        original_create = gs.create_object
        injected = False

        def commit_then_lose_response(otype, body):
            nonlocal injected
            result = original_create(otype, body)
            if (otype == "graph_edit" and body.get("result") == "pending"
                    and not injected):
                injected = True
                raise gs.GraphError("lost pending create response")
            return result

        with mock.patch.object(
                gs, "create_object", side_effect=commit_then_lose_response):
            with self.assertRaisesRegex(gs.GraphError, "queued"):
                gs.update_edge(
                    edge["_guid"], {"max_turns": 10}, actor=source)

        rows = _pending_rows(source)
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0].get("request_id"))
        self.assertEqual(gs.get_object(edge["_guid"])["max_turns"], 5)

    def test_claim_write_failure_happens_before_replay_mutation(self):
        _actor, source, target, row = _connect_request("claim")
        original_patch = gs.patch_object

        def fail_claim(otype, guid, body):
            if (otype == "graph_edit" and guid == row["_guid"]
                    and body.get("result") == "applying"):
                raise gs.GraphError("claim persistence unavailable")
            return original_patch(otype, guid, body)

        with mock.patch.object(gs, "patch_object", side_effect=fail_claim), \
             mock.patch.object(gs, "create_edge", wraps=gs.create_edge) as replay:
            with self.assertRaises(gs.GraphError) as raised:
                guard.approve_pending(row["_guid"], actor="human")

        self.assertIn("claim persistence unavailable", str(raised.exception))
        replay.assert_not_called()
        self.assertEqual(gs.get_object(row["_guid"])["result"], "pending")
        self.assertEqual(
            gs.edges_from_to(source["_guid"], target["_guid"]), [])

    def test_concurrent_approve_and_reject_have_exactly_one_winner(self):
        _actor, source, target, row = _connect_request("approve_reject")
        entered = threading.Event()
        release = threading.Event()
        outcomes = []
        outcomes_lock = threading.Lock()
        original_create = gs.create_edge

        def blocked_create(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise AssertionError("timed out waiting to release replay")
            return original_create(*args, **kwargs)

        def resolve(label, fn):
            try:
                fn()
            except Exception as error:  # outcome is asserted below
                result = (label, "error", error)
            else:
                result = (label, "ok", None)
            with outcomes_lock:
                outcomes.append(result)

        with mock.patch.object(gs, "create_edge", side_effect=blocked_create):
            approving = threading.Thread(
                target=resolve,
                args=("approve", lambda: guard.approve_pending(
                    row["_guid"], actor="human")))
            approving.start()
            self.assertTrue(entered.wait(5), "approval never reached replay")
            rejecting = threading.Thread(
                target=resolve,
                args=("reject", lambda: guard.reject_pending(
                    row["_guid"], reason="concurrent rejection", actor="human")))
            rejecting.start()
            # Give the competing resolver a real opportunity to read the same
            # pending row before the first mutation is released.
            time.sleep(0.1)
            release.set()
            approving.join(5)
            rejecting.join(5)

        self.assertFalse(approving.is_alive())
        self.assertFalse(rejecting.is_alive())
        winners = [label for label, state, _ in outcomes if state == "ok"]
        self.assertEqual(winners, ["approve"], outcomes)
        self.assertEqual(gs.get_object(row["_guid"])["result"], "approved")
        self.assertEqual(
            len(gs.edges_from_to(source["_guid"], target["_guid"])), 1)

    def test_separate_processes_share_the_pending_resolution_claim(self):
        from crew import config

        _actor, source, target, row = _connect_request("process_race")
        ctx = multiprocessing.get_context("spawn")
        entered = ctx.Event()
        release = ctx.Event()
        outcomes = ctx.Queue()
        host = config.MORPHDB_HOST
        approving = ctx.Process(
            target=_approve_process,
            args=(host, TEST_APP, row["_guid"], entered, release, outcomes))
        rejecting = ctx.Process(
            target=_reject_process,
            args=(host, TEST_APP, row["_guid"], outcomes))
        try:
            approving.start()
            self.assertTrue(
                entered.wait(10), "approval process never reached replay")
            rejecting.start()
            time.sleep(0.2)
            release.set()
            approving.join(15)
            rejecting.join(15)
            self.assertFalse(approving.is_alive(), "approval process hung")
            self.assertFalse(rejecting.is_alive(), "rejection process hung")
            self.assertEqual(approving.exitcode, 0)
            self.assertEqual(rejecting.exitcode, 0)
            results = [outcomes.get(timeout=3) for _ in range(2)]
        finally:
            release.set()
            for process in (approving, rejecting):
                if process.is_alive():
                    process.terminate()
                    process.join(5)

        winners = [label for label, state, _ in results if state == "ok"]
        self.assertEqual(winners, ["approve"], results)
        self.assertEqual(gs.get_object(row["_guid"])["result"], "approved")
        self.assertEqual(
            len(gs.edges_from_to(source["_guid"], target["_guid"])), 1)

    def test_concurrent_approvals_replay_the_mutation_only_once(self):
        _actor, edge, row = _cap_raise_request("approve_once")
        entered = threading.Event()
        release = threading.Event()
        calls_lock = threading.Lock()
        calls = []
        outcomes = []
        original_update = gs.update_edge

        def blocked_update(*args, **kwargs):
            with calls_lock:
                calls.append(1)
            entered.set()
            if not release.wait(5):
                raise AssertionError("timed out waiting to release replay")
            return original_update(*args, **kwargs)

        def approve():
            try:
                guard.approve_pending(row["_guid"], actor="human")
            except Exception as error:  # outcome is asserted below
                outcomes.append(("error", error))
            else:
                outcomes.append(("ok", None))

        with mock.patch.object(gs, "update_edge", side_effect=blocked_update):
            first = threading.Thread(target=approve)
            first.start()
            self.assertTrue(entered.wait(5), "approval never reached replay")
            second = threading.Thread(target=approve)
            second.start()
            time.sleep(0.1)
            release.set()
            first.join(5)
            second.join(5)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(len(calls), 1, "the stored mutation was replayed twice")
        self.assertEqual(
            [state for state, _ in outcomes].count("ok"), 1, outcomes)
        self.assertEqual(gs.get_object(row["_guid"])["result"], "approved")
        self.assertEqual(gs.get_object(edge["_guid"])["max_turns"], 10)

    def test_mutation_failure_leaves_a_non_replayable_claim(self):
        _actor, source, target, row = _connect_request("mutation_failure")

        with mock.patch.object(
                gs, "create_edge",
                side_effect=gs.GraphError("ambiguous replay failure")):
            with self.assertRaises(gs.GraphError):
                guard.approve_pending(row["_guid"], actor="human")

        failed = gs.get_object(row["_guid"])
        self.assertIn(failed["result"], {"applying", "approval_failed"})
        with self.assertRaises(gs.GraphError):
            guard.approve_pending(row["_guid"], actor="human")
        self.assertEqual(
            gs.edges_from_to(source["_guid"], target["_guid"]), [])

    def test_finalization_failure_cannot_leave_applied_request_replayable(self):
        _actor, source, target, row = _connect_request("finalize_failure")
        original_patch = gs.patch_object

        def fail_finalization(otype, guid, body):
            if (otype == "graph_edit" and guid == row["_guid"]
                    and body.get("result") in {"approved", "approval_failed"}):
                raise gs.GraphError("approval finalization unavailable")
            return original_patch(otype, guid, body)

        with mock.patch.object(
                gs, "patch_object", side_effect=fail_finalization):
            with self.assertRaises(gs.GraphError):
                guard.approve_pending(row["_guid"], actor="human")

        failed = gs.get_object(row["_guid"])
        self.assertEqual(failed["result"], "applying")
        self.assertEqual(
            len(gs.edges_from_to(source["_guid"], target["_guid"])), 1)
        with self.assertRaises(gs.GraphError):
            guard.approve_pending(row["_guid"], actor="human")
        self.assertEqual(
            len(gs.edges_from_to(source["_guid"], target["_guid"])), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
