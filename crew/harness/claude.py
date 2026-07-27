"""Claude Code's goal reader.

Claude Code records everything Crew needs in its own state directory — no
cooperation from the agent, nothing for it to keep up to date:

    sessions/<pid>.json          live session registry, keyed by cwd
    tasks/session-<sid8>/*.json  the durable task list  -> open goals

A goal belongs to a LIVE session: the registry keeps a file per session ever
started and the OS recycles pids, so liveness is confirmed against processes
actually running Claude Code (one cached ps sweep), never bare pid liveness.
"""
import glob
import json
import os
import re
import subprocess
import time

from .. import runtime as runtimes
from .base import Harness

_STATE_ENV = ("CREW_CLAUDE_STATE_DIR", "CLAUDE_CONFIG_DIR")
_DEFAULT_STATE = "~/.claude"
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

# One ps sweep / registry read serves a whole dashboard poll (ps ~180ms).
_CACHE_TTL = 3.0
_PROCESS_CACHE = {"at": -1.0, "pids": None}
_REGISTRY_CACHE = {"at": -1.0, "dir": None, "rows": None}


def state_dir():
    for name in _STATE_ENV:
        value = os.environ.get(name)
        if value:
            return os.path.expanduser(value)
    return os.path.expanduser(_DEFAULT_STATE)


def reset_caches():
    """Drop cached sweeps (tests, and after a state-dir change)."""
    _PROCESS_CACHE.update(at=-1.0, pids=None)
    _REGISTRY_CACHE.update(at=-1.0, dir=None, rows=None)


def _pid_alive(pid):
    try:
        os.kill(int(pid), 0)
    except PermissionError:
        return True         # exists, owned by another user
    except (OSError, TypeError, ValueError):
        return False
    return True


def _running_claude_pids():
    """PIDs currently running Claude Code, from one short-lived ps sweep.

    Returns None when ps is unavailable, so callers degrade to plain pid
    liveness instead of declaring every session dead.
    """
    now = time.monotonic()
    if (_PROCESS_CACHE["pids"] is not None
            and now - _PROCESS_CACHE["at"] < _CACHE_TTL):
        return _PROCESS_CACHE["pids"]
    try:
        out = subprocess.run(["ps", "-Ao", "pid=,comm="], capture_output=True,
                             text=True, timeout=5).stdout
    except Exception:
        return None
    pids = set()
    for line in (out or "").splitlines():
        parts = line.split(None, 1)
        if len(parts) != 2 or not parts[0].isdigit():
            continue
        if runtimes.process_matches("claude", parts[1], parts[1]):
            pids.add(int(parts[0]))
    _PROCESS_CACHE.update(at=now, pids=pids)
    return pids


def _sessions(state):
    """Every readable entry in Claude Code's live session registry."""
    rows = []
    for path in glob.glob(os.path.join(state, "sessions", "*.json")):
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                row = json.load(fh)
        except (OSError, ValueError):
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _registry(state):
    """The session registry, cached so one poll reads it once."""
    now = time.monotonic()
    if (_REGISTRY_CACHE["rows"] is not None
            and _REGISTRY_CACHE["dir"] == state
            and now - _REGISTRY_CACHE["at"] < _CACHE_TTL):
        return _REGISTRY_CACHE["rows"]
    rows = _sessions(state)
    _REGISTRY_CACHE.update(at=now, dir=state, rows=rows)
    return rows


def _live_session(home, registry):
    """The newest live Claude Code session whose cwd is this agent's home."""
    try:
        target = os.path.realpath(home)
    except (OSError, TypeError, ValueError):
        return None
    running = _running_claude_pids()
    best = None
    for row in registry:
        cwd = row.get("cwd")
        if not cwd:
            continue
        try:
            if os.path.realpath(cwd) != target:
                continue
        except (OSError, TypeError, ValueError):
            continue
        pid = row.get("pid")
        if running is None:
            if not _pid_alive(pid):
                continue
        elif pid not in running:
            continue
        try:
            started = float(row.get("startedAt") or 0)
        except (TypeError, ValueError):
            started = 0.0
        if best is None or started > best[0]:
            best = (started, row)
    return best[1] if best else None


def _open_tasks(state, session_id):
    """Open tasks from the session's durable task store, active first."""
    directory = os.path.join(state, "tasks", f"session-{session_id[:8]}")
    open_tasks = []
    for path in glob.glob(os.path.join(directory, "*.json")):
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                task = json.load(fh)
        except (OSError, ValueError):
            continue
        if not isinstance(task, dict):
            continue
        if task.get("status") not in ("pending", "in_progress"):
            continue
        try:
            order = int(task.get("id"))
        except (TypeError, ValueError):
            order = 1 << 30
        open_tasks.append((task.get("status") != "in_progress", order, task))
    open_tasks.sort(key=lambda item: item[:2])
    return [str(task.get("subject") or task.get("activeForm") or "")
            for _, _, task in open_tasks]


class ClaudeCodeHarness(Harness):
    key = "claude"

    def read_goals(self, home):
        state = state_dir()
        session = _live_session(home, _registry(state))
        if not session:
            return [], "no live Claude Code session in this home"
        session_id = str(session.get("sessionId") or "")
        if not _SESSION_ID_RE.match(session_id):
            return [], "live session has an unusable id"
        return _open_tasks(state, session_id), ""
