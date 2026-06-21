"""crew.identity — render an agent's durable identity.

Claude sessions are ephemeral; a crew agent is not. The agent's identity lives
in three durable places: its MorphDB record, its tmux session, and an
`identity.md` file written into its home directory. When a session dies and a new
`claude` starts in that home, re-reading identity.md is how it resumes BEING that
agent — its role, its workspace boundary, and exactly who it may talk to.

This module is pure string rendering (no I/O, no MorphDB) so it is trivially
testable and so the dashboard, the CLI, and the spawn path all produce the same
text. `write_identity` (the one side-effecting helper) just drops the rendered
string onto disk.
"""
import os

from . import config


def render_identity_md(agent, neighbors):
    """The full identity.md body for `agent`.

    `neighbors` is a list of (neighbor_agent_dict, edge_dict) the agent MAY
    message (already resolved by the caller from the edge graph). The "may
    message" list is load-bearing: delivery is hard-blocked to exactly these.
    """
    name = agent.get("name", "?")
    role = (agent.get("role") or "").strip()
    identity = (agent.get("identity") or "").strip()
    home = agent.get("home") or "(unset)"

    lines = [f"# Identity: {name}", ""]
    if role:
        lines += [f"**Role:** {role}", ""]
    if identity:
        lines += [identity, ""]

    lines += [
        "## You are a long-running agent",
        f"You are `{name}`, a persistent member of a crew — not a throwaway "
        "session. If this terminal restarts, re-read this file to resume who you "
        "are and pick your work back up.",
        "",
        "## Your workspace",
        f"Your home is `{home}`. You are the ONLY agent here. Do all of your work "
        "inside it and never reach into another agent's directory — homes are "
        "non-overlapping on purpose so crew members don't collide.",
        "",
        "## Who you may talk to",
    ]

    if neighbors:
        lines.append(
            "You may message ONLY these agents (delivery to anyone else is "
            "blocked). Message with: `crew message <name> \"...\"`")
        lines.append("")
        for nb, edge in neighbors:
            nb_name = nb.get("name", "?")
            nb_role = (nb.get("role") or "").strip()
            cond = (edge.get("condition") or "").strip()
            desc = (edge.get("description") or "").strip()
            head = f"- **{nb_name}**" + (f" — {nb_role}" if nb_role else "")
            lines.append(head)
            if desc:
                lines.append(f"  - relationship: {desc}")
            lines.append(f"  - message them when: {cond or 'whenever it helps the work'}")
        lines.append("")
        lines.append(
            "When a message arrives prefixed `[crew msg from <name>]`, it is from "
            "that peer agent (not the user) — act on it if it fits your role, then "
            "reply with the `crew message <name>` command shown after the `↩`.")
    else:
        lines.append(
            "You currently have no connections, so you cannot message anyone yet. "
            "The user connects agents on the crew dashboard; this file updates when "
            "they do.")
    lines.append("")
    return "\n".join(lines)


def render_spawn_context(agent, neighbors):
    """The short message typed into a freshly-spawned agent's prompt, pointing it
    at identity.md (which carries the detail). Kept brief so it doesn't dominate
    the agent's first turn."""
    name = agent.get("name", "?")
    home = agent.get("home") or "."
    path = os.path.join(home, config.IDENTITY_FILE)
    n = len(neighbors)
    who = (f"You may message {n} connected agent(s)." if n
           else "You have no connections yet.")
    return (f"You are crew agent '{name}'. Read {path} to load your identity, "
            f"workspace rules, and who you may talk to. {who} "
            f"To message a connected agent: crew message <name> \"...\".")


def write_identity(home, text):
    """Write identity.md into the agent's home dir. Returns the path. Best-effort
    creation of the dir (it should already exist from spawn)."""
    home = os.path.realpath(os.path.expanduser(str(home)))
    os.makedirs(home, exist_ok=True)
    path = os.path.join(home, config.IDENTITY_FILE)
    with open(path, "w") as f:
        f.write(text)
    return path
