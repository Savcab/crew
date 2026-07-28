"""Harness goal readings riding the graph snapshot onto node cards.

`crew.harness` answers "what is this agent working toward?"; this is the
projection that carries the answer to the browser.  The rules:

  * the snapshot carries a compact per-agent ``harness`` object —
    ``supported``, ``goal``, ``goal_count`` — never the whole HarnessState
    vocabulary and never a webhook (a webhook node runs no harness at all);
  * a slow or broken probe must not delay or break the poll — the snapshot
    degrades to no reading rather than failing;
  * one probe pass serves the whole snapshot, because readers may shell out
    to ps.

    python3 -m unittest tests.test_harness_snapshot   (from the repo root)
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from crew import harness  # noqa: E402
from crew.server import app as dashboard  # noqa: E402


def _state(**fields):
    base = {"runtime": "claude", "supported": True}
    base.update(fields)
    return harness.HarnessState(**base)


class SnapshotProjectionTests(unittest.TestCase):
    """The shape the graph consumes, built from real HarnessState objects."""

    def test_a_goal_reaches_the_agent_row(self):
        agents = [{"name": "AgentA", "_guid": "a"}]

        dashboard._enrich_harness(agents, [
            _state(goals=("rebuild the index", "write the docs"))])

        self.assertEqual(agents[0]["harness"], {
            "supported": True,
            "goal": "rebuild the index",
            "goal_count": 2,
        })

    def test_an_unsupported_runtime_is_marked_not_blank(self):
        agents = [{"name": "toolbox", "_guid": "c"}]

        dashboard._enrich_harness(agents, [
            harness.HarnessState(
                "custom", False,
                reason="Custom command has no goal state Crew can read")])

        self.assertIs(agents[0]["harness"]["supported"], False)
        self.assertEqual(agents[0]["harness"]["goal"], "")

    def test_a_supported_agent_with_nothing_set_still_reports_supported(self):
        agents = [{"name": "AgentB", "_guid": "b"}]

        dashboard._enrich_harness(agents, [_state()])

        self.assertEqual(agents[0]["harness"],
                         {"supported": True, "goal": "", "goal_count": 0})

    def test_the_internal_reason_is_not_shipped_to_the_browser(self):
        agents = [{"name": "AgentA", "_guid": "a"}]

        dashboard._enrich_harness(agents, [
            _state(reason="harness state unreadable: /Users/someone/private")])

        self.assertNotIn("reason", agents[0]["harness"])

    def test_the_full_goal_list_stays_server_side(self):
        # The card renders one goal and a count; the browser never needs the
        # whole list, and goals are operator content.
        agents = [{"name": "AgentA", "_guid": "a"}]

        dashboard._enrich_harness(agents, [
            _state(goals=("first", "second", "third"))])

        self.assertNotIn("goals", agents[0]["harness"])
        self.assertEqual(agents[0]["harness"]["goal_count"], 3)

    def test_a_short_probe_result_never_mislabels_a_later_agent(self):
        agents = [{"name": "AgentA", "_guid": "a"}, {"name": "AgentB",
                                                     "_guid": "b"}]

        dashboard._enrich_harness(agents, [_state(goals=("only mine",))])

        self.assertEqual(agents[0]["harness"]["goal"], "only mine")
        self.assertNotIn("harness", agents[1])


class SnapshotResilienceTests(unittest.TestCase):
    """The poll must survive a harness that misbehaves."""

    def _snapshot(self, agents):
        with mock.patch.object(dashboard.gs, "list_nodes", return_value=agents), \
                mock.patch.object(dashboard.gs, "list_edges", return_value=[]), \
                mock.patch.object(dashboard.config, "current_app",
                                  return_value="crew-harness-test"), \
                mock.patch.object(dashboard.tmuxio, "_session_pane_map",
                                  return_value={}), \
                mock.patch.object(dashboard, "_pending_rows", return_value=[]), \
                mock.patch.object(dashboard, "_status_transitions"):
            return dashboard._graph_snapshot()

    def test_a_raising_probe_leaves_the_snapshot_healthy(self):
        agents = [{"name": "AgentA", "_guid": "a", "home": "/tmp/a",
                   "runtime": "claude"}]

        with mock.patch.object(harness, "probe_many",
                               side_effect=RuntimeError("boom")):
            body = self._snapshot(agents)

        self.assertTrue(body["ok"], body)
        self.assertNotIn("harness", body["agents"][0])

    def test_webhook_nodes_are_never_probed(self):
        nodes = [
            {"name": "AgentA", "_guid": "a", "home": "/tmp/a",
             "runtime": "claude"},
            {"name": "hook", "_guid": "h", "kind": "webhook",
             "webhook_token": "t" * 43},
        ]

        with mock.patch.object(harness, "probe_many",
                               wraps=harness.probe_many) as spy:
            body = self._snapshot(nodes)

        probed = [row.get("name") for row in spy.call_args.args[0]]
        self.assertEqual(probed, ["AgentA"])
        self.assertNotIn("harness", body["webhooks"][0])

    def test_the_whole_snapshot_takes_one_probe_pass(self):
        agents = [{"name": f"Agent{index}", "_guid": str(index),
                   "home": f"/tmp/{index}", "runtime": "claude"}
                  for index in range(4)]

        with mock.patch.object(harness, "probe_many",
                               wraps=harness.probe_many) as spy:
            self._snapshot(agents)

        spy.assert_called_once()

    def test_a_supported_reading_survives_the_round_trip(self):
        agents = [{"name": "AgentA", "_guid": "a", "home": "/tmp/a",
                   "runtime": "claude"}]

        with mock.patch.object(harness, "probe_many",
                               return_value=[_state(goals=("live goal",))]):
            body = self._snapshot(agents)

        self.assertEqual(body["agents"][0]["harness"]["goal"], "live goal")


if __name__ == "__main__":
    unittest.main()
