"""Opt-in verification of the harness readers against REAL harness installs.

`tests/test_harness.py` pins the logic against synthetic state directories.
This file pins the part a fake can never cover: that Claude Code's, Codex
CLI's, and Hermes's actual on-disk layouts still are what `crew.harness`
believes they are.  Crew reads those layouts without the harness's
cooperation, so these are the canaries that fire when a release moves one —
the failure mode the design deliberately accepted.

They only read.  Nothing under any harness's own state directory is written,
and no goal text is asserted on or printed: a goal is operator content.

    CREW_RUN_HARNESS_LIVE=1 python3 -W error tests/test_harness_live.py -v
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from crew import harness  # noqa: E402
from crew.harness import claude as claude_harness  # noqa: E402
from crew.harness import codex as codex_harness  # noqa: E402
from crew.harness import hermes as hermes_harness  # noqa: E402

_LIVE_ENABLED = os.environ.get("CREW_RUN_HARNESS_LIVE") == "1"

_WIRE_KEYS = {"runtime", "supported", "goal", "goal_count", "goals", "reason",
              "subagents"}


def _shape_ok(case, state, runtime):
    """One reading has the wire shape, whatever the install contains."""
    case.assertEqual(state.runtime, runtime)
    case.assertIsInstance(state.supported, bool)
    case.assertIsInstance(state.goals, tuple)
    for goal in state.goals:
        case.assertIsInstance(goal, str)
        case.assertLessEqual(len(goal), harness.MAX_TEXT)
    case.assertIsInstance(state.reason, str)
    case.assertTrue(state.subagents is None
                    or (isinstance(state.subagents, int)
                        and state.subagents >= 0))
    case.assertEqual(set(state.as_dict()), _WIRE_KEYS)


def _count_ok(case, runtime, home):
    """The reader itself counts subagents against the real install.

    ``state`` swallows a failing count by design, so the raw reader is what
    has to be exercised here: this is the canary for the store moving.
    """
    count = harness.HARNESSES[runtime].read_subagents(home)
    case.assertTrue(count is None or (isinstance(count, int) and count >= 0))


@unittest.skipUnless(_LIVE_ENABLED, "set CREW_RUN_HARNESS_LIVE=1 to run")
class LiveClaudeLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not os.path.isdir(claude_harness.state_dir()):
            raise unittest.SkipTest("no Claude Code state dir on this machine")

    def setUp(self):
        claude_harness.reset_caches()

    def test_the_session_registry_still_parses(self):
        rows = claude_harness._sessions(claude_harness.state_dir())
        for row in rows:
            self.assertIsInstance(row, dict)
        # A machine that has ever run Claude Code has registry entries.
        self.assertGreater(len(rows), 0)

    def test_a_live_session_yields_a_wire_shaped_reading(self):
        running = claude_harness._running_claude_pids()
        if not running:
            raise unittest.SkipTest("no running Claude Code to read")
        rows = [row for row in
                claude_harness._sessions(claude_harness.state_dir())
                if row.get("pid") in running and row.get("cwd")]
        if not rows:
            raise unittest.SkipTest("no registered live session")
        for row in rows:
            state = harness.probe(
                {"name": "live", "runtime": "claude", "home": row["cwd"]})
            _shape_ok(self, state, "claude")
            self.assertTrue(state.supported)

    def test_the_session_registry_still_yields_a_subagent_count(self):
        # Claude Code counts from the same registry the goals come from, so
        # this asks only that the kind/liveness reading survives the real
        # file layout — never how many subagents happen to be running.
        _count_ok(self, "claude", os.getcwd())

    def test_a_dead_home_reports_a_reason_not_goals(self):
        state = harness.probe({"name": "live", "runtime": "claude",
                               "home": os.path.join(ROOT, "var")})
        _shape_ok(self, state, "claude")
        if not state.goals:
            self.assertNotEqual(state.reason, "")


@unittest.skipUnless(_LIVE_ENABLED, "set CREW_RUN_HARNESS_LIVE=1 to run")
class LiveCodexLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not os.path.isdir(codex_harness.state_dir()):
            raise unittest.SkipTest("no Codex state dir on this machine")

    def test_the_goal_store_is_where_crew_looks(self):
        found = codex_harness._stores(codex_harness.state_dir())
        if not found:
            raise unittest.SkipTest("this Codex install predates goal stores")
        goals_db, threads_db = found
        self.assertTrue(os.path.isfile(goals_db))
        self.assertTrue(os.path.isfile(threads_db))

    def test_probing_a_real_home_returns_a_wire_shaped_reading(self):
        state = harness.probe({"name": "live", "runtime": "codex",
                               "home": os.path.expanduser("~")})
        _shape_ok(self, state, "codex")
        self.assertTrue(state.supported)

    def test_the_spawn_edge_query_survives_the_real_store(self):
        # None here is a legitimate answer: an install that predates
        # thread_spawn_edges has no reading to give.
        _count_ok(self, "codex", os.getcwd())

    def test_probing_never_raises_even_on_a_locked_store(self):
        # Codex may be running and mid-write; the probe must still answer.
        for _ in range(3):
            _shape_ok(self, harness.probe(
                {"name": "live", "runtime": "codex",
                 "home": os.getcwd()}), "codex")


@unittest.skipUnless(_LIVE_ENABLED, "set CREW_RUN_HARNESS_LIVE=1 to run")
class LiveHermesLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not os.path.isdir(hermes_harness.state_dir()):
            raise unittest.SkipTest("no Hermes state dir on this machine")

    def test_the_kanban_is_where_crew_looks(self):
        if not os.path.isfile(os.path.join(hermes_harness.state_dir(),
                                           "kanban.db")):
            raise unittest.SkipTest("this Hermes install has no kanban yet")
        state = harness.probe({"name": "live", "runtime": "hermes",
                               "home": os.getcwd()})
        _shape_ok(self, state, "hermes")
        self.assertTrue(state.supported)
        # A readable kanban answers with goals or with no reason to give.
        if state.reason:
            self.assertEqual(state.goals, ())

    def test_the_delegation_store_is_where_crew_looks(self):
        # state.db is a different file from the kanban; a Hermes that has
        # never delegated has none, which reads as None.
        _count_ok(self, "hermes", os.getcwd())

    def test_the_reading_is_home_independent(self):
        first = harness.probe({"name": "live", "runtime": "hermes",
                               "home": os.getcwd()})
        second = harness.probe({"name": "live", "runtime": "hermes",
                                "home": os.path.expanduser("~")})
        self.assertEqual(first.goals, second.goals)


if __name__ == "__main__":
    unittest.main(verbosity=2)
