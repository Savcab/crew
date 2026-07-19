"""Dashboard-owned status monitoring and transition notification contracts."""

import os
from pathlib import Path
import sys
import unittest
from unittest import mock


sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from crew.server import app  # noqa: E402


class _ImmediateThread:
    def __init__(self, target=None, **_kwargs):
        self.target = target

    def start(self):
        if self.target:
            self.target()


class StatusTransitionTests(unittest.TestCase):
    def setUp(self):
        app._prev_status.clear()
        if hasattr(app, "_last_notify"):
            app._last_notify.clear()

    def _transition(self, status, calls):
        with mock.patch.object(app, "notify", side_effect=lambda *a: calls.append(a)), \
             mock.patch.object(app.threading, "Thread", _ImmediateThread):
            app._status_transitions([{
                "name": "builder",
                "session": "crew_builder",
                "live_status": status,
            }])

    def test_first_observation_seeds_without_announcing_existing_state(self):
        calls = []
        self._transition("down", calls)
        self.assertEqual(calls, [])

    def test_steady_state_does_not_repeat_an_alert(self):
        calls = []
        self._transition("idle", calls)
        self._transition("down", calls)
        self._transition("down", calls)
        self.assertEqual([call[0] for call in calls], ["agent_down"])

    def test_each_real_down_transition_notifies_even_within_one_minute(self):
        """Transition de-duplication is enough; time suppression loses events."""
        calls = []
        self._transition("idle", calls)
        self._transition("down", calls)
        self._transition("idle", calls)
        self._transition("down", calls)
        self.assertEqual([call[0] for call in calls], [
            "agent_down", "agent_down",
        ])

    def test_needs_input_detail_is_runtime_neutral(self):
        calls = []
        self._transition("idle", calls)
        self._transition("needs_input", calls)
        self.assertEqual(calls[0][0], "needs_input")
        self.assertIn("waiting for input", calls[0][2])
        self.assertNotIn("permission prompt", calls[0][2])

    def test_same_name_replacement_seeds_a_new_identity_without_false_alert(self):
        calls = []
        with mock.patch.object(app, "notify", side_effect=lambda *a: calls.append(a)), \
             mock.patch.object(app.threading, "Thread", _ImmediateThread):
            app._status_transitions([{
                "_guid": "agent-old",
                "name": "builder",
                "session": "crew_builder",
                "live_status": "idle",
            }])
            # The graph can replace an agent between monitor cycles without
            # ever presenting an empty name set. Its GUID, not its reusable
            # display name, defines whether this is a transition.
            app._status_transitions([{
                "_guid": "agent-new",
                "name": "builder",
                "session": "crew_builder",
                "live_status": "down",
            }])

        self.assertEqual(calls, [])
        self.assertEqual(app._prev_status, {"agent-new": "down"})


class BackgroundMonitorTests(unittest.TestCase):
    def setUp(self):
        app._prev_status.clear()

    def test_monitor_cycle_derives_status_without_a_browser_snapshot(self):
        agents = [{"_guid": "agent-builder", "name": "builder",
                   "session": "crew_builder"}]

        live = {"session": "crew_builder", "pane": "%7"}

        def enrich(agent, *, live):
            self.assertEqual(live, {
                "session": "crew_builder", "pane": "%7"})
            return {"live_status": "idle", "runtime_alive": True}

        with mock.patch.object(app.gs, "list_agents", return_value=agents), \
             mock.patch.object(
                 app.tmuxio, "live_agent_inventory",
                 return_value={"agent-builder": live}), \
             mock.patch.object(app.tmuxio, "agent_snapshot_fields", side_effect=enrich), \
             mock.patch.object(app, "_status_transitions") as transitions:
            self.assertTrue(app._status_monitor_once())

        transitions.assert_called_once()
        observed = transitions.call_args.args[0]
        self.assertEqual(observed[0]["live_status"], "idle")

    def test_monitor_cycle_quarantines_malformed_identities_and_keeps_sparse_valid(self):
        sparse_valid = {"_guid": "agent-builder", "name": "builder"}
        persisted = [
            {"_guid": "agent-null-name", "name": None},
            {"_guid": "agent-invalid-name", "name": "not a valid name"},
            {"name": "missing_guid"},
            sparse_valid,
        ]
        live = {"session": "builder", "pane": "%7"}

        def enrich(agent, *, live):
            self.assertIs(agent, sparse_valid)
            self.assertEqual(live, {"session": "builder", "pane": "%7"})
            return {"live_status": "idle", "runtime_alive": True}

        with mock.patch.object(app.gs, "list_agents", return_value=persisted), \
             mock.patch.object(
                 app.tmuxio, "live_agent_inventory",
                 return_value={"agent-builder": live}) as inventory, \
             mock.patch.object(
                 app.tmuxio, "agent_snapshot_fields", side_effect=enrich
             ) as snapshot_fields, \
             mock.patch.object(app, "_status_transitions") as transitions:
            self.assertTrue(app._status_monitor_once())

        inventory.assert_called_once_with([sparse_valid])
        snapshot_fields.assert_called_once()
        transitions.assert_called_once()
        self.assertEqual(transitions.call_args.args[0], [{
            "_guid": "agent-builder",
            "name": "builder",
            "live_status": "idle",
            "runtime_alive": True,
        }])

    def test_monitor_cycle_recovers_from_backend_failure(self):
        with mock.patch.object(app.gs, "list_agents", side_effect=RuntimeError("offline")):
            self.assertFalse(app._status_monitor_once())

    def test_dashboard_main_starts_the_monitor_thread(self):
        source = Path(app.__file__).read_text(encoding="utf-8")
        self.assertIn("target=_status_monitor_loop", source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
