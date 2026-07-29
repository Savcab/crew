"""Hermes's goal reader.

Hermes (Nous Research's hermes-agent) runs as one per-user install whose
durable task board is a sqlite kanban:

    ~/.hermes/kanban.db   tasks(id, title, status, priority, created_at, ...)

Open board cards are the goals Hermes is working toward.  Two properties
differ from the other readers and are deliberate:

  * The board is machine-wide — Hermes is a singleton gateway, not a
    per-project process — so the agent's home does not filter the reading.
  * Cards are durable; no live Hermes process is required.
"""
import contextlib
import json
import os

from .base import MAX_GOALS, Harness, read_only_db

_STATE_ENV = ("CREW_HERMES_STATE_DIR", "HERMES_HOME")
_DEFAULT_STATE = "~/.hermes"
# Hermes's own vocabulary (kanban_db.VALID_STATUSES) minus its two terminal
# states, 'done' and 'archived'.
_OPEN = ("triage", "todo", "scheduled", "ready", "running", "blocked",
         "review")
# The in-flight delegation states, exactly the predicate Hermes itself uses
# to find live async delegations in state.db.
_LIVE_DELEGATIONS = ("running", "finalizing")


def state_dir():
    for name in _STATE_ENV:
        value = os.environ.get(name)
        if value:
            return os.path.expanduser(value)
    return os.path.expanduser(_DEFAULT_STATE)


def _fires_in(job, target):
    """One cron job that is enabled and fires in ``target``.

    Hermes stores a job's directory as ``workdir``; a job without one is
    bound to no agent's home and must never be attributed to the agent that
    happens to be probed.
    """
    if not isinstance(job, dict) or not job.get("enabled", True):
        return False
    workdir = job.get("workdir")
    if not isinstance(workdir, str) or not workdir:
        return False
    try:
        return os.path.realpath(workdir) == target
    except (OSError, TypeError, ValueError):
        return False


class HermesHarness(Harness):
    key = "hermes"

    def read_goals(self, home):
        # ponytail: default board only, no workspace_path filter — revisit if
        # anyone runs several Hermes agents with per-project boards.
        path = os.path.join(state_dir(), "kanban.db")
        if not os.path.isfile(path):
            return [], "no Hermes kanban on this machine"
        marks = ",".join("?" * len(_OPEN))
        with contextlib.closing(read_only_db(path)) as db:
            rows = db.execute(
                f"SELECT title FROM tasks WHERE status IN ({marks}) "
                "ORDER BY (status != 'running'), priority DESC, "
                "created_at ASC LIMIT ?", (*_OPEN, MAX_GOALS)).fetchall()
        return [row[0] for row in rows], ""

    def read_cron_loops(self, home):
        """Enabled cron jobs whose workdir is this agent's home.

        The one reading Hermes does NOT share machine-wide: a cron job names
        the directory it fires in, so it belongs to exactly one home.  An
        install with no profile at all has no reading to give; a profile
        that simply has no jobs file has an honest zero.
        """
        state = state_dir()
        if not os.path.isdir(state):
            return None
        path = os.path.join(state, "cron", "jobs.json")
        if not os.path.isfile(path):
            return 0
        with open(path, encoding="utf-8", errors="replace") as fh:
            jobs = json.load(fh).get("jobs")
        if not isinstance(jobs, list):
            return None
        target = os.path.realpath(home)
        return sum(_fires_in(job, target) for job in jobs)

    def read_subagents(self, home):
        """Async delegations in flight — machine-wide, like the board."""
        path = os.path.join(state_dir(), "state.db")
        if not os.path.isfile(path):
            return None
        marks = ",".join("?" * len(_LIVE_DELEGATIONS))
        with contextlib.closing(read_only_db(path)) as db:
            row = db.execute(
                "SELECT COUNT(*) FROM async_delegations "
                f"WHERE state IN ({marks})", _LIVE_DELEGATIONS).fetchone()
        return row[0]
