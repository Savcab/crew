"""crew.harness — the Harness base class and its first three clients.

Crew operates one level above coding harnesses (Claude Code, Codex CLI,
Hermes...), and those harnesses could not be more different on disk: JSON
session registries, sqlite goal stores, kanban boards.  The base class's
responsibility is deliberately narrow: capture only what Crew wants to SHOW
the user — the goals an agent is working toward — not to wrap everything a
harness can do.  These tests pin that contract:

  (1) the interface is primitive: a reader turns one agent home into a list
      of open goals plus a reason, and the base class normalizes the rest
      (bounds, cleaning, the wire dict) identically for every client;
  (2) ``probe`` never raises and never lies: an unreadable harness reports
      "unknown" with a reason, which is a different claim from "no goals";
  (3) each client reads its harness's OWN durable state — Claude Code's task
      files, Codex's thread-goal sqlite, Hermes's kanban — with no
      cooperation from the agent and no driving of the TUI.

Every test runs against a synthetic state directory; nothing here touches a
real harness install (tests/test_harness_live.py does that, opt-in).

    python3 -m unittest tests.test_harness   (from the repo root)
"""
import argparse
import contextlib
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from crew import cli, harness  # noqa: E402
from crew import runtime as runtimes  # noqa: E402
from crew.harness import base  # noqa: E402
from crew.harness import claude as claude_harness  # noqa: E402


SID = "11111111-2222-3333-4444-555555555555"
DAY_MS = 24 * 60 * 60 * 1000


def now_ms():
    return int(time.time() * 1000)


def write_json(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)


class HarnessCase(unittest.TestCase):
    """Shared synthetic state dirs + cache hygiene for every suite here."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="crew-harness-test-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.claude_dir = os.path.join(self.root, "claude-state")
        self.codex_dir = os.path.join(self.root, "codex-state")
        self.hermes_dir = os.path.join(self.root, "hermes-state")
        self.home = os.path.join(self.root, "agent-home")
        for path in (self.claude_dir, self.codex_dir, self.hermes_dir,
                     self.home):
            os.makedirs(path, exist_ok=True)
        env = {"CREW_CLAUDE_STATE_DIR": self.claude_dir,
               "CREW_CODEX_STATE_DIR": self.codex_dir,
               "CREW_HERMES_STATE_DIR": self.hermes_dir}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        claude_harness.reset_caches()
        self.addCleanup(claude_harness.reset_caches)
        # Sessions of this test process count as live Claude Code unless a
        # test opts into the real ps sweep.
        self._running = mock.patch.object(
            claude_harness, "_running_claude_pids",
            lambda: {os.getpid(), os.getppid()})
        self._running.start()
        self.addCleanup(self._stop_running_patch)

    def _stop_running_patch(self):
        if self._running is not None:
            self._running.stop()

    def use_real_process_sweep(self):
        self._running.stop()
        self._running = None

    # -- fixture builders ---------------------------------------------------
    def claude_session(self, home=None, pid=None, sid=SID, started=100.0,
                       kind=None):
        pid = os.getpid() if pid is None else pid
        row = {"pid": pid, "sessionId": sid, "cwd": home or self.home,
               "startedAt": started, "status": "running"}
        if kind:
            # Claude Code omits the key entirely for interactive sessions it
            # started before the field existed; absence must read the same.
            row["kind"] = kind
        write_json(os.path.join(self.claude_dir, "sessions", f"{pid}.json"),
                   row)
        return sid

    def claude_task(self, number, subject, status="pending", sid=SID):
        write_json(
            os.path.join(self.claude_dir, "tasks", f"session-{sid[:8]}",
                         f"{number}.json"),
            {"id": str(number), "subject": subject, "status": status})

    def claude_scheduled(self, rows, home=None):
        """Write a home's scheduled-task store verbatim (shape included)."""
        write_json(os.path.join(home or self.home, ".claude",
                                "scheduled_tasks.json"), rows)

    def scheduled_task(self, task_id="s1", cron="0 9 * * *", age_days=0,
                       **fields):
        """One scheduled-task row, created ``age_days`` ago."""
        row = {"id": task_id, "cron": cron, "prompt": "post the digest",
               "createdAt": now_ms() - age_days * DAY_MS}
        row.update(fields)
        return row

    def codex_state(self, generation=1):
        goals = os.path.join(self.codex_dir, f"goals_{generation}.sqlite")
        threads = os.path.join(self.codex_dir, f"state_{generation}.sqlite")
        with sqlite3.connect(goals) as db:
            db.execute("CREATE TABLE IF NOT EXISTS thread_goals ("
                       "thread_id TEXT PRIMARY KEY, goal_id TEXT, "
                       "objective TEXT, status TEXT, updated_at_ms INTEGER)")
        with sqlite3.connect(threads) as db:
            db.execute("CREATE TABLE IF NOT EXISTS threads ("
                       "id TEXT PRIMARY KEY, cwd TEXT, "
                       "archived INTEGER DEFAULT 0, updated_at INTEGER, "
                       "updated_at_ms INTEGER)")
        return goals, threads

    def codex_goal(self, thread_id, objective, status="active", cwd=None,
                   updated_ms=1000, archived=0, generation=1):
        goals, threads = self.codex_state(generation)
        with sqlite3.connect(threads) as db:
            db.execute("INSERT OR REPLACE INTO threads VALUES (?,?,?,?,?)",
                       (thread_id, cwd or self.home, archived,
                        updated_ms // 1000, updated_ms))
        with sqlite3.connect(goals) as db:
            db.execute("INSERT OR REPLACE INTO thread_goals VALUES (?,?,?,?,?)",
                       (thread_id, f"g-{thread_id}", objective, status,
                        updated_ms))

    def codex_spawn_edge(self, parent_id, child_id, status="running",
                         cwd=None, archived=0, generation=1):
        # Deliberately NOT part of codex_state: an install that predates
        # spawn edges has the table missing, which is the common case.
        _, threads = self.codex_state(generation)
        with sqlite3.connect(threads) as db:
            db.execute("CREATE TABLE IF NOT EXISTS thread_spawn_edges ("
                       "parent_thread_id TEXT, child_thread_id TEXT, "
                       "status TEXT)")
            db.execute("INSERT OR REPLACE INTO threads VALUES (?,?,?,?,?)",
                       (parent_id, cwd or self.home, archived, 1, 1000))
            db.execute("INSERT INTO thread_spawn_edges VALUES (?,?,?)",
                       (parent_id, child_id, status))

    def hermes_state(self):
        path = os.path.join(self.hermes_dir, "kanban.db")
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS tasks ("
                       "id TEXT PRIMARY KEY, title TEXT, body TEXT, "
                       "status TEXT, priority INTEGER DEFAULT 0, "
                       "created_at INTEGER)")
        return path

    def hermes_task(self, task_id, title, status="todo", priority=0,
                    created=1000):
        path = self.hermes_state()
        with sqlite3.connect(path) as db:
            db.execute("INSERT OR REPLACE INTO tasks VALUES (?,?,?,?,?,?)",
                       (task_id, title, "", status, priority, created))

    def hermes_delegations(self):
        # A different file from the kanban: the board can be readable while
        # the delegation store is absent, and vice versa.
        path = os.path.join(self.hermes_dir, "state.db")
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS async_delegations ("
                       "delegation_id TEXT PRIMARY KEY, state TEXT)")
        return path

    def hermes_delegation(self, delegation_id, state="running"):
        path = self.hermes_delegations()
        with sqlite3.connect(path) as db:
            db.execute("INSERT OR REPLACE INTO async_delegations "
                       "VALUES (?,?)", (delegation_id, state))

    def hermes_cron(self, payload):
        """Write Hermes's cron job file verbatim (shape included)."""
        write_json(os.path.join(self.hermes_dir, "cron", "jobs.json"), payload)

    def cron_job(self, job_id="c1", workdir=None, **fields):
        """One Hermes cron job; ``workdir`` defaults to this agent's home."""
        job = {"id": job_id, "schedule": "0 * * * *", "prompt": "poll inbox",
               "workdir": self.home if workdir is None else workdir}
        job.update(fields)
        return job

    def agent(self, runtime="claude", home=None, name="worker"):
        return {"name": name, "runtime": runtime,
                "home": self.home if home is None else home}


# --------------------------------------------------------------------------- #
# The base class and the probe seam
# --------------------------------------------------------------------------- #
class _CannedHarness(harness.Harness):
    key = "claude"

    def __init__(self, goals=(), reason="", boom=None):
        self._goals, self._reason, self._boom = list(goals), reason, boom

    def read_goals(self, home):
        if self._boom:
            raise self._boom
        return list(self._goals), self._reason


class _CountingHarness(_CannedHarness):
    """A canned reader that also counts subagents and cron loops — or fails
    trying, one count at a time."""

    def __init__(self, count=0, count_boom=None, cron=None, cron_boom=None,
                 **kwargs):
        super().__init__(**kwargs)
        self._count, self._count_boom = count, count_boom
        self._cron, self._cron_boom = cron, cron_boom

    def read_subagents(self, home):
        if self._count_boom:
            raise self._count_boom
        return self._count

    def read_cron_loops(self, home):
        if self._cron_boom:
            raise self._cron_boom
        return self._cron


class BaseContractTests(HarnessCase):
    def test_every_reader_is_a_registered_runtime(self):
        for key, reader in harness.HARNESSES.items():
            self.assertEqual(key, reader.key)
            self.assertIn(key, runtimes.ADAPTERS)
            self.assertIsInstance(reader, harness.Harness)

    def test_first_clients_are_claude_codex_and_hermes(self):
        self.assertEqual(set(harness.HARNESSES),
                         {"claude", "codex", "hermes"})

    def test_probe_reports_a_runtime_without_reader_as_unsupported(self):
        state = harness.probe(
            {"name": "x", "home": self.home, "runtime": "custom",
             "launch_cmd": "./mytool"})
        self.assertFalse(state.supported)
        self.assertEqual(state.goal_count, 0)
        self.assertIn("no goal state", state.reason)

    def test_probe_never_raises_when_a_reader_explodes(self):
        bomb = _CannedHarness(boom=RuntimeError("layout changed"))
        with mock.patch.dict(harness.HARNESSES, {"claude": bomb}):
            state = harness.probe(self.agent())
        self.assertTrue(state.supported)
        self.assertEqual(state.goals, ())
        self.assertIn("layout changed", state.reason)

    def test_probe_without_home_degrades_with_a_reason(self):
        for key in harness.HARNESSES:
            state = harness.probe({"name": "x", "runtime": key})
            self.assertTrue(state.supported, key)
            self.assertEqual(state.goals, (), key)
            self.assertIn("home", state.reason, key)

    def test_probe_many_preserves_order_and_length(self):
        self.claude_session()
        self.claude_task(1, "claude goal", status="in_progress")
        agents = [self.agent(name="a"),
                  {"name": "b", "runtime": "custom", "home": self.home,
                   "launch_cmd": "./mytool"},
                  self.agent(runtime="hermes", name="c")]
        states = harness.probe_many(agents)
        self.assertEqual([s.runtime for s in states],
                         ["claude", "custom", "hermes"])
        self.assertEqual(states[0].goal, "claude goal")
        self.assertEqual(harness.probe_many([]), [])
        self.assertEqual(harness.probe_many(None), [])

    def test_state_bounds_and_cleans_every_reading(self):
        reader = _CannedHarness(
            goals=["  spaced \n out  ", "", "g" * 500], reason="  why \n not ")
        state = reader.state(self.home)
        self.assertEqual(state.goals[0], "spaced out")
        self.assertEqual(len(state.goals), 2)          # empty reading dropped
        self.assertEqual(len(state.goals[1]), base.MAX_TEXT)
        self.assertEqual(state.reason, "why not")

    def test_state_caps_a_runaway_goal_list(self):
        reader = _CannedHarness(goals=[f"goal {i}" for i in range(300)])
        state = reader.state(self.home)
        self.assertEqual(state.goal_count, base.MAX_GOALS)

    def test_goal_is_the_first_open_goal(self):
        state = harness.HarnessState("claude", True, ("first", "second"))
        self.assertEqual(state.goal, "first")
        self.assertEqual(state.goal_count, 2)
        self.assertEqual(harness.HarnessState("claude", True).goal, "")

    def test_as_dict_is_the_wire_contract(self):
        state = harness.HarnessState("codex", True, ("a", "b"), "note", 3, 2)
        self.assertEqual(state.as_dict(), {
            "runtime": "codex", "supported": True, "goal": "a",
            "goal_count": 2, "goals": ["a", "b"], "reason": "note",
            "subagents": 3, "cron_loops": 2})

    def test_a_reader_that_cannot_count_subagents_reads_none(self):
        # None is the base default: "no reading", which is a different claim
        # from an honest 0.
        state = _CannedHarness(goals=["work"]).state(self.home)
        self.assertIsNone(state.subagents)
        self.assertIsNone(state.as_dict()["subagents"])

    def test_a_broken_subagent_count_does_not_cost_the_goals(self):
        reader = _CountingHarness(goals=["still the goal"],
                                  count_boom=RuntimeError("table dropped"))
        state = reader.state(self.home)
        self.assertEqual(state.goals, ("still the goal",))
        self.assertIsNone(state.subagents)

    def test_a_nonsense_subagent_count_clamps_to_zero(self):
        state = _CountingHarness(count=-4).state(self.home)
        self.assertEqual(state.subagents, 0)

    def test_an_unsupported_runtime_counts_no_subagents(self):
        state = harness.probe(
            {"name": "x", "home": self.home, "runtime": "custom",
             "launch_cmd": "./mytool"})
        self.assertFalse(state.supported)
        self.assertIsNone(state.subagents)

    def test_a_reader_that_cannot_count_cron_loops_reads_none(self):
        # Same honesty as subagents: a harness with no cron concept has no
        # reading to give, which is not a claim that it schedules nothing.
        state = _CannedHarness(goals=["work"]).state(self.home)
        self.assertIsNone(state.cron_loops)
        self.assertIsNone(state.as_dict()["cron_loops"])

    def test_a_broken_cron_count_costs_neither_goals_nor_subagents(self):
        reader = _CountingHarness(goals=["still the goal"], count=2,
                                  cron_boom=RuntimeError("jobs file moved"))
        state = reader.state(self.home)
        self.assertEqual(state.goals, ("still the goal",))
        self.assertEqual(state.subagents, 2)
        self.assertIsNone(state.cron_loops)

    def test_a_nonsense_cron_count_clamps_to_zero(self):
        state = _CountingHarness(cron=-4).state(self.home)
        self.assertEqual(state.cron_loops, 0)

    def test_an_unsupported_runtime_counts_no_cron_loops(self):
        state = harness.probe(
            {"name": "x", "home": self.home, "runtime": "custom",
             "launch_cmd": "./mytool"})
        self.assertFalse(state.supported)
        self.assertIsNone(state.cron_loops)


# --------------------------------------------------------------------------- #
# Claude Code
# --------------------------------------------------------------------------- #
class ClaudeGoalTests(HarnessCase):
    def test_reads_open_tasks_from_the_live_session(self):
        self.claude_session()
        self.claude_task(1, "ship the migration", status="in_progress")
        self.claude_task(2, "write the docs")
        state = harness.probe(self.agent())
        self.assertTrue(state.supported)
        self.assertEqual(state.goal, "ship the migration")
        self.assertEqual(state.goal_count, 2)
        self.assertEqual(state.reason, "")

    def test_in_progress_tasks_outrank_pending(self):
        self.claude_session()
        self.claude_task(1, "queued early")
        self.claude_task(9, "being done now", status="in_progress")
        state = harness.probe(self.agent())
        self.assertEqual(state.goals, ("being done now", "queued early"))

    def test_finished_tasks_are_not_goals(self):
        self.claude_session()
        self.claude_task(1, "already done", status="completed")
        self.claude_task(2, "cancelled", status="deleted")
        state = harness.probe(self.agent())
        self.assertEqual(state.goals, ())

    def test_malformed_task_files_are_skipped(self):
        self.claude_session()
        directory = os.path.join(self.claude_dir, "tasks",
                                 f"session-{SID[:8]}")
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, "junk.json"), "w") as fh:
            fh.write("{not json")
        write_json(os.path.join(directory, "list.json"), ["not", "a", "dict"])
        self.claude_task(3, "the real goal", status="in_progress")
        state = harness.probe(self.agent())
        self.assertEqual(state.goals, ("the real goal",))

    def test_no_live_session_is_a_reason_not_an_empty_claim(self):
        state = harness.probe(self.agent())
        self.assertTrue(state.supported)
        self.assertEqual(state.goals, ())
        self.assertIn("no live Claude Code session", state.reason)

    def test_newest_live_session_wins(self):
        old = "aaaaaaaa-0000-0000-0000-000000000000"
        self.claude_session(pid=os.getppid(), sid=old, started=50.0)
        self.claude_session(started=200.0)
        self.claude_task(1, "stale", status="in_progress", sid=old)
        self.claude_task(1, "current", status="in_progress")
        state = harness.probe(self.agent())
        self.assertEqual(state.goal, "current")

    def test_unusable_session_id_is_refused(self):
        self.claude_session(sid="../../etc/passwd")
        state = harness.probe(self.agent())
        self.assertEqual(state.goals, ())
        self.assertIn("unusable id", state.reason)

    def test_state_dir_env_fallback_order(self):
        with mock.patch.dict(os.environ,
                             {"CREW_CLAUDE_STATE_DIR": "",
                              "CLAUDE_CONFIG_DIR": self.claude_dir}):
            self.assertEqual(claude_harness.state_dir(), self.claude_dir)


class ClaudeLivenessTests(HarnessCase):
    def test_a_recycled_pid_is_not_a_live_session(self):
        # The registry row exists, but no process with that pid runs Claude
        # Code any more — the reader must not resurrect the dead session.
        self.claude_session(pid=os.getpid())
        self.claude_task(1, "ghost goal", status="in_progress")
        with mock.patch.object(claude_harness, "_running_claude_pids",
                               lambda: set()):
            claude_harness.reset_caches()
            state = harness.probe(self.agent())
        self.assertEqual(state.goals, ())
        self.assertIn("no live Claude Code session", state.reason)

    def test_ps_failure_falls_back_to_plain_pid_liveness(self):
        self.claude_session(pid=os.getpid())
        self.claude_task(1, "still mine", status="in_progress")
        with mock.patch.object(claude_harness, "_running_claude_pids",
                               lambda: None):
            claude_harness.reset_caches()
            state = harness.probe(self.agent())
        self.assertEqual(state.goal, "still mine")

    def test_process_sweep_reads_ps_output(self):
        self.use_real_process_sweep()
        claude_harness.reset_caches()
        fake_ps = mock.Mock()
        fake_ps.stdout = (f"  {os.getpid()} claude\n  999999 vim\n"
                          "garbage line\n")
        with mock.patch.object(claude_harness.subprocess, "run",
                               return_value=fake_ps):
            pids = claude_harness._running_claude_pids()
        self.assertEqual(pids, {os.getpid()})

    def test_process_sweep_failure_returns_none(self):
        self.use_real_process_sweep()
        claude_harness.reset_caches()
        with mock.patch.object(claude_harness.subprocess, "run",
                               side_effect=OSError("no ps")):
            self.assertIsNone(claude_harness._running_claude_pids())


class ClaudeSubagentTests(HarnessCase):
    def test_a_live_background_session_is_a_subagent(self):
        # A background session is its own registered process; the agent that
        # spawned it need not have a single open task for it to be counted.
        self.claude_session(kind="bg")
        state = harness.probe(self.agent())
        self.assertEqual(state.subagents, 1)
        self.assertEqual(state.goals, ())

    def test_every_live_background_session_counts(self):
        self.claude_session(pid=os.getpid(), sid="bg-one", kind="bg")
        self.claude_session(pid=os.getppid(), sid="bg-two", kind="bg")
        state = harness.probe(self.agent())
        self.assertEqual(state.subagents, 2)

    def test_background_sessions_in_another_home_are_not_ours(self):
        elsewhere = os.path.join(self.root, "other-home")
        os.makedirs(elsewhere, exist_ok=True)
        self.claude_session(home=elsewhere, kind="bg")
        state = harness.probe(self.agent())
        self.assertEqual(state.subagents, 0)

    def test_a_background_session_whose_process_is_gone_does_not_count(self):
        # The registry keeps a file per session ever started; a subagent
        # count that trusted it would only ever climb.
        self.claude_session(pid=424242, kind="bg")
        state = harness.probe(self.agent())
        self.assertEqual(state.subagents, 0)

    def test_interactive_sessions_are_agents_not_subagents(self):
        self.claude_session(pid=os.getpid(), sid="plain")
        self.claude_session(pid=os.getppid(), sid="typed", kind="interactive")
        state = harness.probe(self.agent())
        self.assertEqual(state.subagents, 0)

    def test_a_home_reached_through_a_symlink_still_counts(self):
        link = os.path.join(self.root, "home-link")
        os.symlink(self.home, link)
        self.claude_session(home=os.path.realpath(self.home), kind="bg")
        state = harness.probe(self.agent(home=link))
        self.assertEqual(state.subagents, 1)


class ClaudeCronTests(HarnessCase):
    def test_a_scheduled_task_in_this_home_is_a_cron_loop(self):
        self.claude_scheduled([self.scheduled_task()])
        state = harness.probe(self.agent())
        self.assertEqual(state.cron_loops, 1)

    def test_a_scheduled_task_needs_no_live_session(self):
        # The store is durable and the schedule fires on its own, so a home
        # with no session open still has a loop running under it.
        self.claude_scheduled([self.scheduled_task()])
        state = harness.probe(self.agent())
        self.assertEqual(state.goals, ())
        self.assertEqual(state.cron_loops, 1)

    def test_a_home_with_no_store_is_an_honest_zero(self):
        # Unlike Hermes, absence here is not ambiguity: the durable-cron
        # flag ships off, so a home without the file schedules nothing.
        self.assertEqual(harness.probe(self.agent()).cron_loops, 0)

    def test_another_homes_store_is_not_ours(self):
        elsewhere = os.path.join(self.root, "other-home")
        os.makedirs(elsewhere, exist_ok=True)
        self.claude_scheduled([self.scheduled_task()], home=elsewhere)
        self.assertEqual(harness.probe(self.agent()).cron_loops, 0)
        self.assertEqual(harness.probe(self.agent(home=elsewhere)).cron_loops,
                         1)

    def test_a_recurring_task_expires_after_a_week(self):
        # The binary stops firing a non-permanent recurring task 7 days
        # after it was created; counting it would badge a dead loop.
        self.claude_scheduled([self.scheduled_task(age_days=8)])
        self.assertEqual(harness.probe(self.agent()).cron_loops, 0)

    def test_a_recurring_task_inside_the_week_still_counts(self):
        self.claude_scheduled([self.scheduled_task(age_days=6)])
        self.assertEqual(harness.probe(self.agent()).cron_loops, 1)

    def test_a_permanent_task_never_expires(self):
        self.claude_scheduled([self.scheduled_task(age_days=400,
                                                   permanent=True)])
        self.assertEqual(harness.probe(self.agent()).cron_loops, 1)

    def test_malformed_rows_do_not_count(self):
        self.claude_scheduled([
            self.scheduled_task("s1", cron="0 9 * *"),          # 4 fields
            self.scheduled_task("s2", cron="0 9 * * * *"),      # 6 fields
            dict(self.scheduled_task("s3"), prompt=None),
            dict(self.scheduled_task("s4"), createdAt="yesterday"),
            {"cron": "0 9 * * *", "prompt": "no id at all",
             "createdAt": now_ms()},
            "not a row in any sense",
            self.scheduled_task("s7"),
        ])
        self.assertEqual(harness.probe(self.agent()).cron_loops, 1)

    def test_a_corrupt_store_reads_none_not_zero(self):
        path = os.path.join(self.home, ".claude", "scheduled_tasks.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{ this was half-written")
        self.assertIsNone(harness.probe(self.agent()).cron_loops)

    def test_a_store_of_the_wrong_shape_reads_none(self):
        self.claude_scheduled({"tasks": [self.scheduled_task()]})
        self.assertIsNone(harness.probe(self.agent()).cron_loops)

    def test_a_broken_store_leaves_the_goals_alone(self):
        self.claude_session()
        self.claude_task(1, "ship the migration", status="in_progress")
        self.claude_scheduled({"tasks": []})
        state = harness.probe(self.agent())
        self.assertEqual(state.goal, "ship the migration")
        self.assertIsNone(state.cron_loops)


# --------------------------------------------------------------------------- #
# Codex CLI
# --------------------------------------------------------------------------- #
class CodexGoalTests(HarnessCase):
    def test_reads_the_active_goal_for_the_agents_home(self):
        self.codex_goal("t1", "refactor the parser")
        state = harness.probe(self.agent(runtime="codex"))
        self.assertTrue(state.supported)
        self.assertEqual(state.goal, "refactor the parser")
        self.assertEqual(state.reason, "")

    def test_completed_goals_are_excluded(self):
        self.codex_goal("t1", "old work", status="complete")
        state = harness.probe(self.agent(runtime="codex"))
        self.assertEqual(state.goals, ())

    def test_paused_and_limited_goals_still_count_as_open(self):
        self.codex_goal("t1", "paused work", status="paused", updated_ms=1000)
        self.codex_goal("t2", "limited work", status="usage_limited",
                        updated_ms=2000)
        state = harness.probe(self.agent(runtime="codex"))
        self.assertEqual(state.goal_count, 2)

    def test_active_goals_outrank_the_rest_then_newest_first(self):
        self.codex_goal("t1", "paused new", status="paused", updated_ms=9000)
        self.codex_goal("t2", "active old", status="active", updated_ms=1000)
        self.codex_goal("t3", "active new", status="active", updated_ms=5000)
        state = harness.probe(self.agent(runtime="codex"))
        self.assertEqual(state.goals,
                         ("active new", "active old", "paused new"))

    def test_goals_in_other_homes_are_excluded(self):
        elsewhere = os.path.join(self.root, "other-home")
        os.makedirs(elsewhere, exist_ok=True)
        self.codex_goal("t1", "someone else's goal", cwd=elsewhere)
        state = harness.probe(self.agent(runtime="codex"))
        self.assertEqual(state.goals, ())

    def test_archived_threads_are_excluded(self):
        self.codex_goal("t1", "archived thread goal", archived=1)
        state = harness.probe(self.agent(runtime="codex"))
        self.assertEqual(state.goals, ())

    def test_a_home_reached_through_a_symlink_still_matches(self):
        link = os.path.join(self.root, "home-link")
        os.symlink(self.home, link)
        self.codex_goal("t1", "real goal", cwd=os.path.realpath(self.home))
        state = harness.probe(self.agent(runtime="codex", home=link))
        self.assertEqual(state.goal, "real goal")

    def test_missing_goal_store_is_a_reason(self):
        state = harness.probe(self.agent(runtime="codex"))
        self.assertTrue(state.supported)
        self.assertEqual(state.goals, ())
        self.assertIn("no Codex goal store", state.reason)

    def test_newest_schema_generation_wins(self):
        self.codex_goal("t1", "old generation", generation=1)
        self.codex_state(generation=2)      # empty but newer store
        state = harness.probe(self.agent(runtime="codex"))
        self.assertEqual(state.goals, ())

    def test_corrupt_database_degrades_to_a_reason(self):
        # A thread for this home must exist, or the reader never even opens
        # the goal store; corruption has to hit a file that gets read.
        self.codex_goal("t1", "reachable goal")
        with open(os.path.join(self.codex_dir, "goals_1.sqlite"), "wb") as fh:
            fh.write(b"this is not sqlite at all")
        state = harness.probe(self.agent(runtime="codex"))
        self.assertTrue(state.supported)
        self.assertEqual(state.goals, ())
        self.assertNotEqual(state.reason, "")


class CodexSubagentTests(HarnessCase):
    def test_a_running_spawn_edge_is_a_live_subagent(self):
        self.codex_spawn_edge("t1", "child-1")
        state = harness.probe(self.agent(runtime="codex"))
        self.assertEqual(state.subagents, 1)

    def test_every_non_terminal_child_is_a_live_subagent(self):
        # A blocked child has not finished, and a status Codex adds later
        # must read as live rather than being silently dropped.
        self.codex_spawn_edge("t1", "child-1", status="blocked")
        self.codex_spawn_edge("t1", "child-2", status="handoff_pending")
        state = harness.probe(self.agent(runtime="codex"))
        self.assertEqual(state.subagents, 2)

    def test_settled_spawn_edges_do_not_count(self):
        for index, status in enumerate(("completed", "failed", "stopped")):
            self.codex_spawn_edge("t1", f"child-{index}", status=status)
        state = harness.probe(self.agent(runtime="codex"))
        self.assertEqual(state.subagents, 0)

    def test_spawn_edges_under_another_home_are_not_ours(self):
        elsewhere = os.path.join(self.root, "other-home")
        os.makedirs(elsewhere, exist_ok=True)
        self.codex_spawn_edge("t1", "child-1", cwd=elsewhere)
        state = harness.probe(self.agent(runtime="codex"))
        self.assertEqual(state.subagents, 0)

    def test_an_archived_parent_thread_has_no_live_children(self):
        self.codex_spawn_edge("t1", "child-1", archived=1)
        state = harness.probe(self.agent(runtime="codex"))
        self.assertEqual(state.subagents, 0)

    def test_a_store_predating_spawn_edges_reads_none_not_zero(self):
        # The goal store is fine; only the newer table is missing, and the
        # goals must survive that untouched.
        self.codex_goal("t1", "refactor the parser")
        state = harness.probe(self.agent(runtime="codex"))
        self.assertEqual(state.goal, "refactor the parser")
        self.assertIsNone(state.subagents)

    def test_a_missing_store_reads_none_not_zero(self):
        state = harness.probe(self.agent(runtime="codex"))
        self.assertIsNone(state.subagents)


class CodexCronTests(HarnessCase):
    def test_codex_has_no_cron_concept_so_there_is_no_reading(self):
        # A readable install with goals still owes no cron answer: Codex
        # schedules nothing, and None says that instead of claiming zero.
        self.codex_goal("t1", "refactor the parser")
        state = harness.probe(self.agent(runtime="codex"))
        self.assertEqual(state.goal, "refactor the parser")
        self.assertIsNone(state.cron_loops)


# --------------------------------------------------------------------------- #
# Hermes
# --------------------------------------------------------------------------- #
class HermesGoalTests(HarnessCase):
    def test_reads_open_kanban_tasks(self):
        self.hermes_task("h1", "triage inbound bugs", status="todo")
        state = harness.probe(self.agent(runtime="hermes"))
        self.assertTrue(state.supported)
        self.assertEqual(state.goal, "triage inbound bugs")
        self.assertEqual(state.reason, "")

    def test_done_and_archived_tasks_are_excluded(self):
        self.hermes_task("h1", "finished", status="done")
        self.hermes_task("h2", "put away", status="archived")
        state = harness.probe(self.agent(runtime="hermes"))
        self.assertEqual(state.goals, ())

    def test_every_open_status_counts(self):
        for index, status in enumerate(
                ("triage", "todo", "scheduled", "ready", "running",
                 "blocked", "review")):
            self.hermes_task(f"h{index}", f"{status} card", status=status)
        state = harness.probe(self.agent(runtime="hermes"))
        self.assertEqual(state.goal_count, 7)

    def test_running_tasks_outrank_the_backlog(self):
        self.hermes_task("h1", "waiting", status="todo", priority=9,
                         created=1)
        self.hermes_task("h2", "being worked", status="running", created=2)
        state = harness.probe(self.agent(runtime="hermes"))
        self.assertEqual(state.goal, "being worked")

    def test_priority_then_age_orders_the_backlog(self):
        self.hermes_task("h1", "old low", status="todo", priority=0,
                         created=1)
        self.hermes_task("h2", "urgent", status="todo", priority=5,
                         created=9)
        self.hermes_task("h3", "old first", status="todo", priority=0,
                         created=0)
        state = harness.probe(self.agent(runtime="hermes"))
        self.assertEqual(state.goals, ("urgent", "old first", "old low"))

    def test_the_board_is_machine_wide_so_home_does_not_filter(self):
        self.hermes_task("h1", "shared card", status="todo")
        elsewhere = os.path.join(self.root, "other-home")
        os.makedirs(elsewhere, exist_ok=True)
        first = harness.probe(self.agent(runtime="hermes"))
        second = harness.probe(self.agent(runtime="hermes", home=elsewhere))
        self.assertEqual(first.goals, second.goals)

    def test_missing_kanban_is_a_reason(self):
        state = harness.probe(self.agent(runtime="hermes"))
        self.assertTrue(state.supported)
        self.assertIn("no Hermes kanban", state.reason)

    def test_corrupt_kanban_degrades_to_a_reason(self):
        with open(os.path.join(self.hermes_dir, "kanban.db"), "wb") as fh:
            fh.write(b"junk")
        state = harness.probe(self.agent(runtime="hermes"))
        self.assertTrue(state.supported)
        self.assertEqual(state.goals, ())
        self.assertNotEqual(state.reason, "")


class HermesSubagentTests(HarnessCase):
    def test_running_and_finalizing_delegations_are_in_flight(self):
        self.hermes_delegation("d1", state="running")
        self.hermes_delegation("d2", state="finalizing")
        state = harness.probe(self.agent(runtime="hermes"))
        self.assertEqual(state.subagents, 2)

    def test_delegations_that_are_not_in_flight_do_not_count(self):
        self.hermes_delegation("d1", state="completed")
        self.hermes_delegation("d2", state="pending_delivery")
        self.hermes_delegation("d3", state="something_new")
        state = harness.probe(self.agent(runtime="hermes"))
        self.assertEqual(state.subagents, 0)

    def test_a_missing_delegation_store_leaves_the_board_readable(self):
        self.hermes_task("h1", "triage inbound bugs", status="todo")
        state = harness.probe(self.agent(runtime="hermes"))
        self.assertEqual(state.goal, "triage inbound bugs")
        self.assertIsNone(state.subagents)

    def test_delegations_are_machine_wide_so_home_does_not_filter(self):
        self.hermes_delegation("d1", state="running")
        elsewhere = os.path.join(self.root, "other-home")
        os.makedirs(elsewhere, exist_ok=True)
        first = harness.probe(self.agent(runtime="hermes"))
        second = harness.probe(self.agent(runtime="hermes", home=elsewhere))
        self.assertEqual(first.subagents, second.subagents)
        self.assertEqual(first.subagents, 1)


class HermesCronTests(HarnessCase):
    def test_an_enabled_job_bound_to_this_home_is_a_cron_loop(self):
        self.hermes_cron({"jobs": [self.cron_job()]})
        state = harness.probe(self.agent(runtime="hermes"))
        self.assertEqual(state.cron_loops, 1)

    def test_jobs_are_enabled_unless_they_say_otherwise(self):
        self.hermes_cron({"jobs": [self.cron_job("c1"),
                                   self.cron_job("c2", enabled=True),
                                   self.cron_job("c3", enabled=False)]})
        state = harness.probe(self.agent(runtime="hermes"))
        self.assertEqual(state.cron_loops, 2)

    def test_a_job_bound_to_no_workdir_belongs_to_no_agent(self):
        # Unlike the kanban, cron IS per-home: a job that names no workdir
        # must not be attributed to whichever agent happens to be probed.
        self.hermes_cron({"jobs": [dict(self.cron_job("c1"), workdir=None),
                                   {"id": "c2", "schedule": "0 * * * *"}]})
        state = harness.probe(self.agent(runtime="hermes"))
        self.assertEqual(state.cron_loops, 0)

    def test_jobs_firing_in_another_workdir_are_not_ours(self):
        elsewhere = os.path.join(self.root, "other-home")
        os.makedirs(elsewhere, exist_ok=True)
        self.hermes_cron({"jobs": [self.cron_job("c1", workdir=elsewhere)]})
        state = harness.probe(self.agent(runtime="hermes"))
        self.assertEqual(state.cron_loops, 0)
        elsewhere_state = harness.probe(
            self.agent(runtime="hermes", home=elsewhere))
        self.assertEqual(elsewhere_state.cron_loops, 1)

    def test_a_home_reached_through_a_symlink_still_counts(self):
        link = os.path.join(self.root, "home-link")
        os.symlink(self.home, link)
        self.hermes_cron({"jobs": [
            self.cron_job("c1", workdir=os.path.realpath(self.home))]})
        state = harness.probe(self.agent(runtime="hermes", home=link))
        self.assertEqual(state.cron_loops, 1)

    def test_no_hermes_profile_reads_none_not_zero(self):
        missing = os.path.join(self.root, "no-hermes-here")
        with mock.patch.dict(os.environ,
                             {"CREW_HERMES_STATE_DIR": missing}):
            state = harness.probe(self.agent(runtime="hermes"))
        self.assertIsNone(state.cron_loops)

    def test_a_profile_without_a_jobs_file_is_an_honest_zero(self):
        # The profile is right there and says nothing is scheduled; that is
        # a real zero, not the absence of a reading.
        state = harness.probe(self.agent(runtime="hermes"))
        self.assertEqual(state.cron_loops, 0)

    def test_a_corrupt_jobs_file_reads_none(self):
        path = os.path.join(self.hermes_dir, "cron", "jobs.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{ half a job file")
        state = harness.probe(self.agent(runtime="hermes"))
        self.assertIsNone(state.cron_loops)

    def test_a_jobs_file_of_the_wrong_shape_reads_none(self):
        self.hermes_cron([self.cron_job()])          # a bare list, no "jobs"
        state = harness.probe(self.agent(runtime="hermes"))
        self.assertIsNone(state.cron_loops)

    def test_a_broken_cron_file_leaves_the_board_readable(self):
        self.hermes_task("h1", "triage inbound bugs", status="todo")
        self.hermes_cron({"jobs": "not a list"})
        state = harness.probe(self.agent(runtime="hermes"))
        self.assertEqual(state.goal, "triage inbound bugs")
        self.assertIsNone(state.cron_loops)


# --------------------------------------------------------------------------- #
# The hermes runtime adapter (added alongside its harness client)
# --------------------------------------------------------------------------- #
class HermesRuntimeTests(unittest.TestCase):
    def test_hermes_is_a_registered_runtime(self):
        adapter = runtimes.adapter("hermes")
        self.assertEqual(adapter.label, "Hermes")
        self.assertIn("hermes", adapter.process_names)

    def test_launch_command_defaults_to_the_hermes_cli(self):
        self.assertEqual(runtimes.launch_command("hermes", "/tmp"), "hermes")

    def test_infer_runtime_recognizes_a_hermes_launch(self):
        self.assertEqual(runtimes.infer_runtime("hermes"), "hermes")

    def test_process_match_sees_through_the_python_wrapper(self):
        # ~/.local/bin/hermes execs a venv python whose argv[0] basename is
        # macOS's capital-P "Python"; the real CLI path is argv[1].
        self.assertTrue(runtimes.process_matches(
            "hermes", "Python",
            "/opt/venv/bin/Python /Users/x/.hermes/hermes-agent/hermes"))
        self.assertFalse(runtimes.process_matches(
            "hermes", "vim", "vim hermes-notes.txt"))

    def test_identity_publishes_with_no_native_file(self):
        # Hermes reads no CLAUDE.md/AGENTS.md equivalent; publication must
        # produce identity.md alone instead of failing closed.
        from crew import identity
        home = tempfile.mkdtemp(prefix="crew-hermes-identity-")
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        path = identity.write_identity_bundle(home, "I am worker h.",
                                              "hermes", None)
        with open(path, encoding="utf-8") as fh:
            self.assertIn("worker h", fh.read())
        self.assertEqual(
            [name for name in os.listdir(home) if name.endswith(".md")],
            ["identity.md"])


# --------------------------------------------------------------------------- #
# The CLI over the seam
# --------------------------------------------------------------------------- #
class CliTests(HarnessCase):
    def _run(self, agents, states, name=None, as_json=False):
        out = io.StringIO()
        with mock.patch.object(cli, "_operator_agents", return_value=agents), \
                mock.patch.object(cli.harness, "probe_many",
                                  return_value=states), \
                contextlib.redirect_stdout(out):
            code = cli.cmd_harness(argparse.Namespace(name=name,
                                                      json=as_json))
        return code, out.getvalue()

    def test_json_output_carries_the_goal_list(self):
        agents = [self.agent(name="alpha")]
        states = [harness.HarnessState("claude", True, ("first", "second"))]
        code, out = self._run(agents, states, as_json=True)
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload[0]["agent"], "alpha")
        self.assertEqual(payload[0]["goals"], ["first", "second"])
        self.assertNotIn("loop", payload[0])

    def test_table_output_shows_goal_and_remaining_count(self):
        agents = [self.agent(name="alpha")]
        states = [harness.HarnessState("claude", True, ("first", "second"))]
        code, out = self._run(agents, states)
        self.assertEqual(code, 0)
        self.assertIn("first", out)
        self.assertIn("+1 more open", out)

    def test_json_output_carries_the_subagent_count(self):
        agents = [self.agent(name="alpha")]
        states = [harness.HarnessState("claude", True, ("first",),
                                       subagents=2)]
        code, out = self._run(agents, states, as_json=True)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)[0]["subagents"], 2)

    def test_table_output_shows_live_subagents(self):
        agents = [self.agent(name="alpha")]
        states = [harness.HarnessState("claude", True, ("first",),
                                       subagents=2)]
        code, out = self._run(agents, states)
        self.assertEqual(code, 0)
        self.assertIn("subagents: 2 live", out)

    def test_nothing_to_report_prints_no_subagent_line(self):
        # Neither "none running" nor "we could not tell" is worth a line in a
        # table an operator scans.
        agents = [self.agent(name="alpha"), self.agent(name="beta")]
        states = [harness.HarnessState("claude", True, ("first",)),
                  harness.HarnessState("claude", True, ("first",),
                                       subagents=0)]
        code, out = self._run(agents, states)
        self.assertEqual(code, 0)
        self.assertNotIn("subagents", out)

    def test_json_output_carries_the_cron_count(self):
        agents = [self.agent(name="alpha")]
        states = [harness.HarnessState("claude", True, ("first",),
                                       cron_loops=3)]
        code, out = self._run(agents, states, as_json=True)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)[0]["cron_loops"], 3)

    def test_table_output_shows_live_cron_loops(self):
        agents = [self.agent(name="alpha")]
        states = [harness.HarnessState("claude", True, ("first",),
                                       subagents=2, cron_loops=3)]
        code, out = self._run(agents, states)
        self.assertEqual(code, 0)
        self.assertIn("subagents: 2 live", out)
        self.assertIn("cron: 3 live", out)

    def test_nothing_to_report_prints_no_cron_line(self):
        agents = [self.agent(name="alpha"), self.agent(name="beta")]
        states = [harness.HarnessState("claude", True, ("first",)),
                  harness.HarnessState("claude", True, ("first",),
                                       cron_loops=0)]
        code, out = self._run(agents, states)
        self.assertEqual(code, 0)
        self.assertNotIn("cron", out)

    def test_unsupported_runtime_prints_its_reason(self):
        agents = [{"name": "tool", "runtime": "custom", "home": self.home,
                   "launch_cmd": "./mytool"}]
        states = [harness.HarnessState("custom", False,
                                       reason="Custom command has no goal "
                                              "state Crew can read")]
        code, out = self._run(agents, states)
        self.assertEqual(code, 0)
        self.assertIn("no goal state", out)

    def test_unknown_agent_name_fails(self):
        err = io.StringIO()
        with mock.patch.object(cli, "_operator_agents", return_value=[]), \
                contextlib.redirect_stderr(err):
            code = cli.cmd_harness(argparse.Namespace(name="ghost",
                                                      json=False))
        self.assertEqual(code, 1)
        self.assertIn("no such agent", err.getvalue())


if __name__ == "__main__":
    unittest.main()
