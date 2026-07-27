"""Codex CLI's goal reader.

Codex keeps a durable, first-class goal store in its state directory:

    goals_<N>.sqlite   thread_goals(thread_id, objective, status, ...)
    state_<N>.sqlite   threads(id, cwd, archived, ...)

A goal belongs to a thread, and the thread's ``cwd`` is the same
home-directory join key Crew uses everywhere else.  Codex bumps the ``<N>``
suffix when it migrates a store's schema, so the newest generation of each
file is read.  Goals are durable — they survive the TUI exiting — so unlike
Claude Code there is no liveness requirement here.
"""
import contextlib
import os
import re

from .base import Harness, read_only_db

_STATE_ENV = ("CREW_CODEX_STATE_DIR", "CODEX_HOME")
_DEFAULT_STATE = "~/.codex"
# Everything but 'complete': a paused or usage-limited goal is still the
# thing the agent is working toward.
_OPEN = ("active", "paused", "blocked", "usage_limited", "budget_limited")
_GENERATION_RE = re.compile(r"_(\d+)\.sqlite$")
# ponytail: bound the thread->goal join; a home with 400+ open threads gets
# its most recent 400 considered.
_MAX_THREADS = 400


def state_dir():
    for name in _STATE_ENV:
        value = os.environ.get(name)
        if value:
            return os.path.expanduser(value)
    return os.path.expanduser(_DEFAULT_STATE)


def _latest(state, prefix):
    """The newest schema generation of one Codex store, or None."""
    best = None
    for path in os.listdir(state) if os.path.isdir(state) else []:
        if not path.startswith(f"{prefix}_"):
            continue
        found = _GENERATION_RE.search(path)
        if not found:
            continue
        generation = int(found.group(1))
        if best is None or generation > best[0]:
            best = (generation, os.path.join(state, path))
    return best[1] if best else None


def _stores(state):
    """(goals_db, threads_db) for this install, or None if either is absent."""
    goals = _latest(state, "goals")
    threads = _latest(state, "state")
    return (goals, threads) if goals and threads else None


class CodexHarness(Harness):
    key = "codex"

    def read_goals(self, home):
        found = _stores(state_dir())
        if not found:
            return [], "no Codex goal store on this machine"
        goals_db, threads_db = found
        with contextlib.closing(read_only_db(threads_db)) as db:
            rows = db.execute(
                "SELECT id FROM threads WHERE archived = 0 "
                "AND cwd IN (?, ?) "
                "ORDER BY COALESCE(updated_at_ms, updated_at) DESC LIMIT ?",
                (str(home), os.path.realpath(home), _MAX_THREADS)).fetchall()
        thread_ids = [row[0] for row in rows]
        if not thread_ids:
            return [], ""
        id_marks = ",".join("?" * len(thread_ids))
        status_marks = ",".join("?" * len(_OPEN))
        with contextlib.closing(read_only_db(goals_db)) as db:
            rows = db.execute(
                f"SELECT objective FROM thread_goals "
                f"WHERE thread_id IN ({id_marks}) "
                f"AND status IN ({status_marks}) "
                "ORDER BY (status != 'active'), updated_at_ms DESC",
                (*thread_ids, *_OPEN)).fetchall()
        return [row[0] for row in rows], ""
