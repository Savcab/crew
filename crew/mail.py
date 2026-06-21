"""crew.mail — the ONE messaging path between agents, with the gate built in.

The rule "you can only message agents you're connected to" is enforced HERE, at
delivery time, not as UI advice. There is no separate ungated bus in this
product: every agent message goes through `deliver`, which refuses unless the
edge graph authorizes sender→target (crew.graphstore.can_message). So a hard
wall — an agent literally cannot type into a peer it has no edge to.

Delivery itself is the proven agent-mail mechanic: type the text into the
target's claude pane with `tmux send-keys -l`, then Enter, so it lands in that
agent's prompt exactly as if a human typed it. The target pane is resolved LIVE
(session name → the pane actually running claude) so a restarted claude is still
reachable.
"""
import os
import subprocess

from . import config, graphstore as gs
from .server import tmuxio

CREW_CMD = os.environ.get("CREW_CMD", "crew")


def whoami():
    """This caller's agent name. Pinned at spawn via $CREW_AGENT / $AGENT_MAIL_NAME
    (set in the tmux session env), with a fall back to the tmux session name."""
    for var in ("CREW_AGENT", "AGENT_MAIL_NAME"):
        v = os.environ.get(var)
        if v and gs.get_agent_by_name(v):
            return v
    # fall back to the current tmux session name, if we're in one
    pane = os.environ.get("TMUX_PANE")
    if pane:
        ok, sess = tmuxio.tmux("display-message", "-t", pane, "-p", "#S")
        if ok and sess.strip():
            a = gs.get_agent_by_name(sess.strip())
            if a:
                return a["name"]
            return sess.strip()
    return os.environ.get("CREW_AGENT") or os.environ.get("AGENT_MAIL_NAME") or "unknown"


def deliver(target, body, sender=None, no_prefix=False):
    """Send `body` to agent `target` as `sender`. Returns (ok, message).

    Refuses (ok=False) when the edge graph does not authorize sender→target — the
    hard block. On success the text is typed into target's live claude pane."""
    sender = sender or whoami()
    body = (body or "").strip()
    if not body:
        return False, "empty message"
    if sender == target:
        return False, "can't message yourself"

    t = gs.get_agent_by_name(target)
    if not t:
        return False, f"no agent named '{target}'"

    if not gs.can_message(sender, target):
        return False, (
            f"BLOCKED: '{sender}' has no relationship to '{target}', so you cannot "
            f"message them. Connect the agents first (crew connect {sender} {target} "
            f"--when \"<condition>\"), or ask the user to add the edge on the dashboard.")

    pane = tmuxio.resolve_target(t.get("session") or target)

    if no_prefix:
        text = body
    else:
        reply = f'↩ reply: {CREW_CMD} message {sender} "..."'
        text = f"[crew msg from {sender}] {body}  {reply}"

    try:
        subprocess.run(["tmux", "send-keys", "-t", pane, "-l", "--", text],
                       check=True, timeout=5)
        subprocess.run(["tmux", "send-keys", "-t", pane, "Enter"], check=True, timeout=5)
    except (subprocess.SubprocessError, OSError) as e:
        if os.environ.get("CLAUDE_CODE_SANDBOXED"):
            return False, (
                "delivery failed — this claude is sandboxed "
                "(CLAUDE_CODE_SANDBOXED=1) so it can't reach the tmux socket. Set "
                '"sandbox": false in ~/.claude/settings.json (or run the message '
                "with dangerouslyDisableSandbox) so crew messaging works.")
        return False, f"delivery failed: {e}"
    return True, f"sent to '{target}' ({pane})"
