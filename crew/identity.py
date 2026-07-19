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

from . import config, guard


# The caller (spawn) attaches a resolved per-direction view onto each edge dict:
# `_conditions` (this agent's outgoing trigger list), `_action` (what this agent does
# when messaged), `_reply` (reply flag for this direction). These helpers read that
# view but fall back to the raw edge fields so the renderers stay pure and work in
# tests that pass plain edge dicts.
def _edge_conditions(edge):
    c = edge.get("_conditions")
    if isinstance(c, list) and c:
        return [s for s in c if isinstance(s, str) and s.strip()]
    single = (edge.get("condition") or "").strip()
    return [single] if single else []


def _edge_action(edge):
    return (edge.get("_action") or edge.get("target_action") or "").strip()


def _edge_reply(edge):
    v = edge.get("_reply")
    return edge.get("reply_expected") if v is None else v


def edge_budget(edge):
    """Human summary of an edge's token/cost budget caps — '2,000,000 tok/hr',
    '$1.50/hr', both joined with ' + ', or '' when unbudgeted. Shared by the
    identity renderers and `crew edges`."""
    parts = []
    if int(edge.get("token_cap") or 0):
        parts.append(f"{int(edge['token_cap']):,} tok/hr")
    if float(edge.get("cost_cap") or 0):
        parts.append(f"${float(edge['cost_cap']):g}/hr")
    return " + ".join(parts)


def render_graph_powers(agent, quota=None):
    """The '## Graph powers' block for a foreman (can_edit_graph=true) agent —
    identical content is spliced into both identity.md and the CLAUDE.md
    managed block, so the two never disagree about what the agent may do.

    `quota` is computed LIVE by the caller (crew.spawn.rewrite_identity, via
    crew.guard.quota_state — the one place this module reaches for graphstore
    I/O) so the numbers here are correct at render time; this function itself
    stays pure (a missing/None quota just renders zeros, never raises)."""
    q = quota or {}
    return [
        "## Graph powers",
        "You have the **foreman** flag (`can_edit_graph=true`) — you may edit "
        "the agent graph, confined to your own envelope:",
        "",
        "- `crew spawn-agent <name> ...` — create new agents (they land inside "
        "your envelope)",
        "- `crew connect <A> <B> --when \"...\"` / `crew disconnect <A> <B>` — "
        "wire agents inside your envelope",
        "- `crew cap <A> <B> --max-turns N --token-cap N --cost-cap X` — LOWER "
        "(never raise) a rate/budget cap on your own edges",
        "- `crew note agent <name> \"...\"` / `crew note edge <A> <B> \"...\"` — "
        "leave a freeform note (always allowed, any agent)",
        "- `crew up <name>` / `crew down <name>` — bring up/down agents YOU created",
        "",
        f"**Envelope rule:** {guard.FOREMAN_ENVELOPE_SENTENCE}.",
        "",
        f"**Quota (live):** {q.get('agents_used', 0)}/{q.get('max_agents', 0)} "
        f"agents used · {q.get('spawns_this_hour', 0)}/{q.get('spawn_rate', 0)} "
        "agent-spawns this hour",
        f"**Edge-cap ceilings:** max_turns ≤ {q.get('max_turns_ceiling', 0):g} · "
        f"token_cap ≤ {q.get('token_cap_ceiling', 0):g} · "
        f"cost_cap ≤ ${q.get('cost_cap_ceiling', 0):g}",
        "",
        "Edits you make apply immediately but render **unblessed** until the user "
        "reviews and blesses them (`crew bless <agent>` / `crew bless --edge <A> "
        "<B>` / `crew bless --all`).",
        "",
    ]


def render_file_grants(grants):
    """The '## File grants' block — spliced into both identity.md and the
    CLAUDE.md managed block (symmetric with "who you may message"), only
    when `grants` is non-empty. Each line names the refs/ symlink the grant
    lives at, the real path it points to, and the recorded mode.

    HONESTY LABEL (load-bearing, asserted verbatim-ish by tests): `mode` is
    recorded intent, not filesystem enforcement — nothing here sandboxes the
    agent's shell. A grant just marks the sanctioned, audited exception to
    the default workspace boundary; it doesn't add any technical restriction
    beyond what the agent's shell already could or couldn't reach."""
    grants = grants or []
    if not grants:
        return []
    lines = ["## File grants", ""]
    for g in grants:
        name = g.get("name", "?")
        path = g.get("path", "?")
        mode = g.get("mode", "ro")
        lines.append(f"- `refs/{name}` → `{path}` ({mode})")
    lines += [
        "",
        "**Honesty label:** `mode` above is recorded intent, not filesystem "
        "enforcement — nothing sandboxes your shell, so this is not a "
        "technical guarantee. Each grant marks the sanctioned, audited "
        "exception to your workspace boundary; treat these paths (and only "
        "these) as authorized outside your own home.",
        "",
    ]
    return lines


def render_identity_md(agent, neighbors, incoming=None, quota=None):
    """The full identity.md body for `agent`.

    `neighbors` is a list of (neighbor_agent_dict, edge_dict) the agent MAY message
    (OUTGOING edges — the "when should I message them" trigger). `incoming` is a
    list of (peer_agent_dict, edge_dict) of agents that may message THIS agent — the
    receiver's half of the contract (what to do when they message you). Both lists
    are load-bearing: delivery is hard-blocked to exactly the messageable set.
    `quota` (see render_graph_powers) is only consulted when `agent.can_edit_graph`.
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
        "non-overlapping on purpose so crew members don't collide, unless a "
        "grant below explicitly authorizes a specific path.",
        "",
        "Keep a running `progress.md` in your home with what you're doing and what's "
        "in flight, and update it as you work — your identity survives a restart, but "
        "your in-progress work only survives if you write it down here.",
        "",
    ]
    lines += render_file_grants(agent.get("grants"))
    if agent.get("can_edit_graph"):
        lines += render_graph_powers(agent, quota)
    lines += ["## Who you may message"]

    if neighbors:
        lines.append(
            "You may message ONLY these agents (delivery to anyone else is "
            "blocked). Message with: `crew message <name> \"...\"`")
        lines.append("")
        for nb, edge in neighbors:
            nb_name = nb.get("name", "?")
            nb_role = (nb.get("role") or "").strip()
            conds = _edge_conditions(edge)
            head = f"- **{nb_name}**" + (f" — {nb_role}" if nb_role else "")
            lines.append(head)
            if len(conds) <= 1:
                lines.append(f"  - message them when: {conds[0] if conds else 'whenever it helps the work'}")
            else:
                lines.append("  - message them when any of these:")
                for c in conds:
                    lines.append(f"    - {c}")
            if _edge_reply(edge):
                lines.append("  - they will reply — wait for and use their reply")
            cap = int(edge.get("max_turns") or 0)
            if cap:
                lines.append(f"  - limit: at most {cap} message(s) per hour on this link")
            budget = edge_budget(edge)
            if budget:
                lines.append(f"  - budget: {budget} — messages to them are refused "
                             "once they've spent it")
        lines.append("")
    else:
        lines.append(
            "You currently have no one to message. The user connects agents on the "
            "crew dashboard; this file updates when they do.")
        lines.append("")

    # The RECEIVER's half: what to do when a peer messages YOU.
    if incoming:
        lines.append("## When these agents message you")
        lines.append("")
        for pr, edge in incoming:
            pr_name = pr.get("name", "?")
            action = _edge_action(edge)
            lines.append(f"- **{pr_name}** may message you.")
            if action:
                lines.append(f"  - when they do: {action}")
            if _edge_reply(edge):
                lines.append(f"  - reply to them with `crew message {pr_name} \"...\"`")
        lines.append("")

    lines.append(
        "A message prefixed `[crew msg from <name>]` is from that peer agent (not the "
        "user) — act on it if it fits your role, then reply with the `crew message "
        "<name>` command shown after the `↩`. A `[crew · from you]` line is the user "
        "seeding or steering you directly.")
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


# --------------------------------------------------------------------------- #
# CLAUDE.md — the NATIVE identity hand-off
# --------------------------------------------------------------------------- #
# Claude Code auto-loads CLAUDE.md from its working dir at the start of EVERY
# session (however it was launched — by crew or by a human `claude` restart). So
# the durable way to make a fresh session BE the agent is to put the essentials in
# CLAUDE.md, not to type them in after boot (the old timer/send-keys injection
# raced claude's startup and, if claude hadn't launched yet, dumped the text into a
# bare shell). The full record still lives in identity.md; CLAUDE.md carries the
# load-bearing core (who you are, your workspace boundary, exactly who you may
# message) plus a pointer to read identity.md.
CREW_BLOCK_BEGIN = "<!-- BEGIN crew identity (managed by crew — do not edit) -->"
CREW_BLOCK_END = "<!-- END crew identity -->"


def render_claude_md(agent, neighbors, incoming=None, quota=None):
    """The managed crew block for the home's CLAUDE.md (no markers — the writer adds
    them). Mirrors identity.md's facts but tuned to sit in the system context: terse,
    imperative, and front-loading the messaging rule that the delivery gate enforces.
    Renders BOTH sides of each relationship — who you message (and when), and who
    messages you (and what they expect)."""
    name = agent.get("name", "?")
    role = (agent.get("role") or "").strip()
    identity = (agent.get("identity") or "").strip()
    home = agent.get("home") or "(unset)"

    lines = [
        f"# Crew agent: {name}",
        "",
        f"You are **{name}**, a long-running member of a crew — a durable agent, not "
        "a throwaway session. This file is loaded automatically every time Claude "
        f"starts in this directory; it tells you who you are. Read `{config.IDENTITY_FILE}` "
        "here for the full record (and re-read it if this session was restarted).",
        "",
        "Your crew role, workspace boundary, and messaging rules below are your real "
        "job and take precedence over any global persona, output style, or skill that "
        f"conflicts with them — act as {name} first.",
        "",
    ]
    if role:
        lines += [f"**Role:** {role}", ""]
    if identity:
        lines += [identity, ""]
    lines += [
        f"**Workspace:** your home is `{home}`. You are the ONLY agent here — do all "
        "your work inside it and never reach into another agent's directory, unless "
        "a grant below explicitly authorizes a specific path. Keep a `progress.md` "
        "here with your in-flight work so you can resume after a restart.",
        "",
    ]
    lines += render_file_grants(agent.get("grants"))
    if agent.get("can_edit_graph"):
        lines += render_graph_powers(agent, quota)
    lines += ["## Who you may message"]
    if neighbors:
        lines.append(
            "You may message ONLY these agents — delivery to anyone else is "
            "hard-blocked at send time:")
        lines.append("")
        for nb, edge in neighbors:
            nb_name = nb.get("name", "?")
            nb_role = (nb.get("role") or "").strip()
            conds = _edge_conditions(edge)
            cond = " / ".join(conds) if conds else "whenever it helps the work"
            head = f"- **{nb_name}**" + (f" — {nb_role}" if nb_role else "")
            extra = ""
            if _edge_reply(edge):
                extra += " · they'll reply"
            cap = int(edge.get("max_turns") or 0)
            if cap:
                extra += f" · max {cap}/hr"
            budget = edge_budget(edge)
            if budget:
                extra += f" · budget {budget}"
            lines.append(head + f" · message them when: {cond}" + extra)
        lines += [
            "",
            'Message a peer with: `crew message <name> "..."`.',
        ]
    else:
        lines.append(
            "You have no one to message yet. The user connects agents on the crew "
            "dashboard; this file updates when they do.")
    if incoming:
        lines += ["", "## When these agents message you"]
        for pr, edge in incoming:
            pr_name = pr.get("name", "?")
            action = _edge_action(edge)
            tail = f" → {action}" if action else ""
            reply = f" · reply with `crew message {pr_name}`" if _edge_reply(edge) else ""
            lines.append(f"- **{pr_name}** may message you{tail}{reply}")
    lines += [
        "",
        "A line prefixed `[crew msg from <name>]` is from that peer agent (not the "
        "user) — act on it if it fits your role, then reply with the `crew message` "
        "command shown after the `↩`. A `[crew · from you]` line is the user steering "
        "you directly.",
        "",
    ]
    return "\n".join(lines)


def _merge_managed_block(existing, block):
    """Splice the crew-managed `block` into `existing` CLAUDE.md text, replacing any
    prior crew block (between the markers) and preserving everything else. If there's
    no existing crew block, the managed block goes FIRST (identity should lead), with
    the user's content kept below."""
    # Defang any marker tokens that leaked in via agent free-text (role/identity):
    # an embedded END marker would forge the block boundary and make the NEXT merge's
    # partition() cut in the wrong place, leaking the managed block into user content.
    block = block.replace(CREW_BLOCK_BEGIN, "").replace(CREW_BLOCK_END, "")
    wrapped = f"{CREW_BLOCK_BEGIN}\n{block.rstrip()}\n{CREW_BLOCK_END}\n"
    if existing and CREW_BLOCK_BEGIN in existing and CREW_BLOCK_END in existing:
        pre, _, rest = existing.partition(CREW_BLOCK_BEGIN)
        _, _, post = rest.partition(CREW_BLOCK_END)
        return f"{pre.rstrip()}\n\n{wrapped}{post.lstrip()}".lstrip() \
            if pre.strip() else f"{wrapped}{post.lstrip()}"
    if existing and existing.strip():
        return f"{wrapped}\n{existing.lstrip()}"
    return wrapped


def write_claude_md(home, block):
    """Write/update the home's CLAUDE.md so a fresh claude auto-loads the agent's
    identity. Idempotent and non-destructive: only the crew-managed block is
    (re)written; any other content in an existing CLAUDE.md is kept. Returns the path."""
    home = os.path.realpath(os.path.expanduser(str(home)))
    os.makedirs(home, exist_ok=True)
    path = os.path.join(home, "CLAUDE.md")
    existing = ""
    try:
        with open(path) as f:
            existing = f.read()
    except OSError:
        pass
    with open(path, "w") as f:
        f.write(_merge_managed_block(existing, block))
    return path
