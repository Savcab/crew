"""Agent activity status: a short self-set "what I'm doing" message.

Agents update their OWN activity via `crew activity <text>`; it rides the
graph snapshot onto the node card, and any agent/human reads peers' activity
from the graph without sending mail. Ephemeral presence, not a graph edit —
applied updates are NOT audited (refusals still are, via guard)."""
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from crew import graphstore as gs, schema  # noqa: E402


APP = f"crewtest-activity-{os.getpid()}"
_PATCHER = None


def setUpModule():
    global _PATCHER
    _PATCHER = mock.patch.dict(os.environ, {"CREW_APP": APP})
    _PATCHER.start()
    try:
        gs._req("DELETE", f"/app/{APP}", app=None)
    except gs.GraphError:
        pass
    schema.ensure_schema(APP)


def tearDownModule():
    try:
        gs._req("DELETE", f"/app/{APP}", app=None)
    finally:
        _PATCHER.stop()


class ActivityTests(unittest.TestCase):
    def _agent(self, name):
        return gs.create_agent(name, home=f"/tmp/crew_activitytest/{name}")

    def test_agent_sets_own_activity_and_it_persists_with_timestamp(self):
        a = self._agent("act_self")
        before = time.time()
        row = gs.set_agent_activity(
            a["_guid"], "working on website…", actor="act_self")
        self.assertEqual(row["activity"], "working on website…")
        self.assertGreaterEqual(row["activity_at"], before)
        fresh = gs.get_agent_by_name("act_self")
        self.assertEqual(fresh["activity"], "working on website…")

    def test_agent_cannot_set_another_agents_activity(self):
        a = self._agent("act_victim")
        self._agent("act_attacker")
        with self.assertRaises(gs.GraphError):
            gs.set_agent_activity(a["_guid"], "pwned", actor="act_attacker")
        self.assertFalse(gs.get_agent_by_name("act_victim").get("activity"))

    def test_human_sets_and_clears_any_agents_activity(self):
        a = self._agent("act_human_target")
        gs.set_agent_activity(a["_guid"], "handed off to sales…", actor="human")
        self.assertEqual(
            gs.get_agent_by_name("act_human_target")["activity"],
            "handed off to sales…")
        gs.set_agent_activity(a["_guid"], "", actor="human")
        self.assertEqual(gs.get_agent_by_name("act_human_target")["activity"], "")

    def test_activity_text_is_trimmed_and_capped(self):
        a = self._agent("act_cap")
        row = gs.set_agent_activity(a["_guid"], "  spaced  ", actor="act_cap")
        self.assertEqual(row["activity"], "spaced")
        row = gs.set_agent_activity(a["_guid"], "x" * 500, actor="act_cap")
        self.assertEqual(len(row["activity"]), 200)

    def test_unknown_actor_fails_closed(self):
        a = self._agent("act_target")
        with self.assertRaises(gs.GraphError):
            gs.set_agent_activity(a["_guid"], "ghost", actor="act_ghost")

    def test_applied_activity_updates_are_not_audited(self):
        a = self._agent("act_quiet")
        gs.set_agent_activity(a["_guid"], "tick", actor="act_quiet")
        gs.set_agent_activity(a["_guid"], "tock", actor="act_quiet")
        rows = gs.list_objects(
            "graph_edit", limit=200, sort="created_at", order="desc")["objects"]
        self.assertFalse(
            [r for r in rows if r.get("op") == "activity"
             and r.get("result") == "applied"],
            "ephemeral activity updates must not spam the audit log")


if __name__ == "__main__":
    unittest.main(verbosity=2)
