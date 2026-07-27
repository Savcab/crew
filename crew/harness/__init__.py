"""crew.harness — what each agent is working toward, read from its harness.

Crew organizes agents that live inside somebody else's coding harness
(Claude Code, Codex CLI, Hermes...).  Each harness already holds the fact an
operator wants at a glance — the goals its agent is currently pursuing — and
each records it completely differently: Claude Code in per-session task
files, Codex in a thread-goal sqlite, Hermes on a kanban board.

This package is the single seam that normalizes that.  ``Harness`` is the
base class each client implements (see base.py: its responsibility is only
what Crew SHOWS the user, never a full model of a harness), and

    probe(agent)        -> HarnessState
    probe_many(agents)  -> [HarnessState, ...]

is what the CLI and the dashboard call.  Two properties every caller can
rely on, because this runs inside the dashboard's poll loop:

  * It never raises.  Every unreadable, malformed, or absent input degrades
    to an empty reading with a stated reason.  A harness that cannot be
    read says "unknown", never "no goals" — the latter is a claim.
  * It never writes.  Harness state belongs to the harness; every reader
    opens it read-only and briefly.

Adding a harness = one ``Harness`` subclass + one entry in ``HARNESSES``
(plus its runtime adapter in crew.runtime).  Nothing else changes.
"""
from .. import runtime as runtimes
from .base import MAX_TEXT, Harness, HarnessState, clean_text  # noqa: F401
from .claude import ClaudeCodeHarness
from .codex import CodexHarness
from .hermes import HermesHarness

HARNESSES = {
    reader.key: reader
    for reader in (ClaudeCodeHarness(), CodexHarness(), HermesHarness())
}


def probe(agent):
    """One agent's goals, normalized across harnesses. Never raises."""
    try:
        key = runtimes.resolve_agent_runtime(agent)
    except Exception:
        key = "custom"
    reader = HARNESSES.get(key)
    if reader is None:
        try:
            label = runtimes.adapter(key).label
        except Exception:
            label = key or "unknown"
        return HarnessState(
            key, False, reason=f"{label} has no goal state Crew can read")
    home = agent.get("home") if isinstance(agent, dict) else None
    if not home:
        return HarnessState(key, True, reason="agent has no home directory")
    try:
        return reader.state(home)
    except Exception as error:
        # A harness that changed its private layout under us degrades to
        # "unknown"; it does not take the graph down.
        return HarnessState(
            key, True, reason=f"harness state unreadable: {error}")


def probe_many(agents):
    """Probe a list of agents, one reading per agent, order preserved."""
    return [probe(agent) for agent in agents or []]
