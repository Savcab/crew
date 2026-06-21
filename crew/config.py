"""crew.config — one place for the knobs every other module reads.

Two servers are in play and they MUST NOT collide:
  * MorphDB  — the data backend (agents + edges live here). Default 127.0.0.1:8787.
  * crew     — this project's dashboard + API.            Default 127.0.0.1:8788.

Everything is overridable by env so a second instance / a test run can move off
the live ports without touching code.
"""
import os
import re

# --- MorphDB (the data backend) ------------------------------------------- #
# MORPHDB_HOST may be a full URL or a bare host[:port] (http:// assumed) — same
# rule the morphdb skill's own client uses, so a hosted MorphDB just works.
MORPHDB_HOST = os.environ.get("MORPHDB_HOST", "127.0.0.1:8787").strip()
DEFAULT_APP = "crew"


def morphdb_base():
    return MORPHDB_HOST if "://" in MORPHDB_HOST else "http://" + MORPHDB_HOST


def current_app():
    """The MorphDB app key (the tenant) we read/write. Read LIVE from the env on
    every call (not frozen at import) so a test can point the whole stack at a
    throwaway app by setting $CREW_APP before exercising graphstore."""
    return (os.environ.get("CREW_APP") or DEFAULT_APP).strip() or DEFAULT_APP


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

# The identity file written into every agent's home dir (see crew.identity).
IDENTITY_FILE = "identity.md"

# An agent name becomes a tmux session name, an agent-mail identity, and (often)
# a directory basename, so it must be a safe slug: no slashes, dots (tmux parses
# '.' as window.pane), spaces, or shell metacharacters. Max 64 chars.
_AGENT_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def valid_agent_name(name):
    return isinstance(name, str) and _AGENT_NAME_RE.match(name) is not None
