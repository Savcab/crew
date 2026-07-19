"""crew.config — one place for the knobs every other module reads.

Two servers are in play and they MUST NOT collide:
  * MorphDB  — the data backend (agents + edges live here). Default 127.0.0.1:8787.
  * crew     — this project's dashboard + API.            Default 127.0.0.1:8788.

Everything is overridable by env so a second instance / a test run can move off
the live ports without touching code.
"""
import json
import os
import re

# --- paths ------------------------------------------------------------------ #
# The repo root (two dirs up from this file: crew/config.py -> crew/ -> repo/)
# and its var/ scratch dir (dashboard pid/log, project registry). Defined here
# (not cli.py) so any module can reach them without importing the CLI.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VAR = os.path.join(ROOT, "var")

# --- MorphDB (the data backend) ------------------------------------------- #
# MORPHDB_HOST may be a full URL or a bare host[:port] (http:// assumed) — same
# rule the morphdb skill's own client uses, so a hosted MorphDB just works.
MORPHDB_HOST = os.environ.get("MORPHDB_HOST", "127.0.0.1:8787").strip()
DEFAULT_APP = "crew"

# --- projects --------------------------------------------------------------- #
# A "project" is an isolated slice of crew: its own MorphDB app (tenant) and
# (by default) its own subtree under crew_root(). The unnamed default project
# maps onto the original single-tenant "crew" app so existing installs/tests
# are unaffected.
DEFAULT_PROJECT = "default"


def current_project():
    """The project we operate within. Read LIVE from $CREW_PROJECT on every call
    (not frozen at import), same rule as current_app()."""
    return (os.environ.get("CREW_PROJECT") or DEFAULT_PROJECT).strip() or DEFAULT_PROJECT


def project_app(project):
    """The MorphDB app key a project's data lives in."""
    return DEFAULT_APP if project == DEFAULT_PROJECT else f"crew-{project}"


def morphdb_base():
    return MORPHDB_HOST if "://" in MORPHDB_HOST else "http://" + MORPHDB_HOST


def current_app():
    """The MorphDB app key (the tenant) we read/write. Read LIVE from the env on
    every call (not frozen at import) so a test can point the whole stack at a
    throwaway app by setting $CREW_APP before exercising graphstore.

    Precedence: an explicit $CREW_APP always wins (tests rely on this pinning);
    otherwise the app is derived from the current project."""
    override = os.environ.get("CREW_APP", "").strip()
    if override:
        return override
    return project_app(current_project())


def session_name(project, name):
    """The tmux session name for an agent: the plain name in the default
    project (unchanged behavior for existing installs), else prefixed with the
    project so two projects' agents can never collide on a session name."""
    return name if project == DEFAULT_PROJECT else f"{project}__{name}"


def crew_root():
    """Base directory new agents' homes are planned under by default. Read LIVE
    from $CREW_ROOT on every call, default ~/crew."""
    return os.path.expanduser(os.environ.get("CREW_ROOT", "~/crew"))


def _projects_file():
    return os.path.join(VAR, "projects.json")


def list_known_projects():
    """Every registered project name, "default" always implicitly first even if
    it was never explicitly registered. Tolerates a missing/corrupt file."""
    names = []
    try:
        with open(_projects_file()) as f:
            data = json.load(f)
        if isinstance(data, list):
            names = [n for n in data if isinstance(n, str)]
    except (OSError, ValueError):
        pass
    if DEFAULT_PROJECT in names:
        names.remove(DEFAULT_PROJECT)
    return [DEFAULT_PROJECT] + names


def register_project(name):
    """Add `name` to the project registry (var/projects.json), idempotent."""
    names = [n for n in list_known_projects() if n != DEFAULT_PROJECT]
    if name != DEFAULT_PROJECT and name not in names:
        names.append(name)
    os.makedirs(VAR, exist_ok=True)
    tmp = _projects_file() + ".tmp"
    with open(tmp, "w") as f:
        json.dump(names, f)
    os.replace(tmp, _projects_file())


# --- crew dashboard ------------------------------------------------------- #
DASHBOARD_HOST = "127.0.0.1"
try:
    DASHBOARD_PORT = int(os.environ.get("CREW_PORT", "8788"))
except ValueError:
    DASHBOARD_PORT = 8788

# How a new agent's claude is launched into its tmux pane. An agent runs
# unattended (it must call `crew message` without a human clicking "allow" each
# time), so the default bypasses permission prompts. Override globally with
# $CREW_LAUNCH_CMD, or per-agent via the dashboard's "Launch command" field /
# `crew spawn-agent --launch-cmd`.
#
# `--dangerously-skip-permissions` only skips the prompts; it does NOT sandbox, so
# the agent's `crew message` can still reach the tmux socket. (If a user turns on
# Claude's bash sandbox via settings — CLAUDE_CODE_SANDBOXED=1 — delivery breaks;
# crew.mail detects that and prints the fix.)
LAUNCH_CMD = os.environ.get("CREW_LAUNCH_CMD",
                            "claude --dangerously-skip-permissions")

# --- operator notifications ------------------------------------------------ #
# Where crew.notify POSTs its events (agent died / needs input / handoff
# expired). Set $CREW_WEBHOOK_URL — e.g. https://ntfy.sh/<your-topic> for phone
# push — or hardcode a URL here. Empty → notifications are silently off.
WEBHOOK_URL = os.environ.get("CREW_WEBHOOK_URL", "").strip()

# The identity file written into every agent's home dir (see crew.identity).
IDENTITY_FILE = "identity.md"

# An agent name becomes a tmux session name, an agent-mail identity, and (often)
# a directory basename, so it must be a safe slug: no slashes, dots (tmux parses
# '.' as window.pane), spaces, or shell metacharacters. Max 64 chars.
# Must START alphanumeric (no leading '-'): the name lands in tmux/CLI argv, where a
# leading dash would be parsed as an option flag (e.g. an agent named '-d').
_AGENT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def valid_agent_name(name):
    return isinstance(name, str) and _AGENT_NAME_RE.match(name) is not None


# Same character rules as an agent name (it becomes half of an app key /
# session prefix), capped shorter since it's a prefix, not the whole slug.
_PROJECT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")


def valid_project_name(name):
    return isinstance(name, str) and _PROJECT_NAME_RE.match(name) is not None


# --- containment limits (wave 2) -------------------------------------------- #
# Every knob here bounds what an AGENT actor (never a human) can do to the
# graph — see crew.guard. Frozen at import (same style as DASHBOARD_PORT/
# LAUNCH_CMD above), overridable via env for a second instance / a test run.
try:
    MAX_AGENTS = int(os.environ.get("CREW_MAX_AGENTS", "12"))
except ValueError:
    MAX_AGENTS = 12

# Agent-actor spawns/hr, ALL agent actors combined, per project/app — caps how
# fast a foreman (or a chain of foremen) can grow the crew unsupervised.
try:
    SPAWN_RATE = int(os.environ.get("CREW_SPAWN_RATE", "4"))
except ValueError:
    SPAWN_RATE = 4

# An agent-actor `connect` must set ALL THREE edge caps to a finite value no
# higher than these ceilings (crew.guard's FINITE-CAPS RULE) — an agent can
# never hand out an unlimited/uncapped edge, only a human can.
try:
    AGENT_EDGE_MAX_TURNS_CEILING = int(os.environ.get("AGENT_EDGE_MAX_TURNS_CEILING", "30"))
except ValueError:
    AGENT_EDGE_MAX_TURNS_CEILING = 30

try:
    AGENT_EDGE_TOKEN_CAP_CEILING = int(os.environ.get("AGENT_EDGE_TOKEN_CAP_CEILING", "500000"))
except ValueError:
    AGENT_EDGE_TOKEN_CAP_CEILING = 500000

try:
    AGENT_EDGE_COST_CAP_CEILING = float(os.environ.get("AGENT_EDGE_COST_CAP_CEILING", "5.0"))
except ValueError:
    AGENT_EDGE_COST_CAP_CEILING = 5.0


# --- transform edges (wave 5) ------------------------------------------------ #
# How long crew.mail.deliver() waits for an edge's transform script before
# treating it as a failure (message filtered — see crew.mail's docstring).
try:
    TRANSFORM_TIMEOUT = float(os.environ.get("CREW_TRANSFORM_TIMEOUT", "5.0"))
except ValueError:
    TRANSFORM_TIMEOUT = 5.0

# The ONLY directory a transform script may live in — crew.graphstore refuses
# to attach (create_edge/update_edge) a transform path outside it (realpath
# containment) or that doesn't exist as a file. Human-only to attach (see
# crew.guard), so this is a deliberate narrow surface, not a sandbox.
TRANSFORMS_DIR = os.path.join(VAR, "transforms")


# --- one-blob LLM expansion (UI wave B) ------------------------------------ #
# POST /api/expand turns a human's one-paragraph description into structured
# edge/agent fields by shelling out to a `claude -p --output-format json`-
# shaped command. The default argv; overridable as a single SHELL STRING via
# $CREW_EXPAND_CMD (parsed with shlex, not a list) so a test can point it at
# tests/fixtures/expand_stub.sh without touching code. Read live (like
# CREW_APP/CREW_PROJECT elsewhere in this file) so a dashboard restart with a
# different env picks it up — there's no other way to reconfigure it, since
# the dashboard is a long-running process.
EXPAND_CMD = ["claude", "-p", "--output-format", "json", "--max-turns", "1"]


def expand_cmd():
    """The argv crew/server/app.py's /api/expand shells out to. $CREW_EXPAND_CMD,
    if set, is a shell string (shlex-split) — e.g. the path to a test stub."""
    override = os.environ.get("CREW_EXPAND_CMD", "").strip()
    if override:
        import shlex
        return shlex.split(override)
    return EXPAND_CMD


# How long /api/expand waits for the expander command before giving up and
# returning the verbatim fallback. Overridable so a test can force the
# timeout path in seconds instead of the real 60s default.
try:
    EXPAND_TIMEOUT = float(os.environ.get("CREW_EXPAND_TIMEOUT", "60"))
except ValueError:
    EXPAND_TIMEOUT = 60.0
