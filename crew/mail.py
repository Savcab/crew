"""crew.mail — the ONE messaging path between agents, with the gate built in.

The rule "you can only message agents you're connected to" is enforced HERE, at
delivery time, not as UI advice. There is no separate ungated bus in this
product: every agent message goes through `deliver`, which refuses unless the
edge graph authorizes sender→target (crew.graphstore.can_message). So a hard
wall — an agent literally cannot type into a peer it has no edge to.

Delivery is RELIABLE and OBSERVABLE, not fire-and-forget:
  * every message is recorded in the MorphDB message log (queued→delivered);
  * we NEVER blast Enter into a pane blindly — we wait for the target's claude to
    be idle (ready for a prompt) before typing, so a message can't interleave with
    a mid-turn generation or get swallowed by a permission dialog;
  * if the target is busy past a short window the message stays QUEUED and the
    dashboard's background flusher retries it when the target frees up;
  * an edge's `max_turns` caps how many times sender→target may fire, so two
    agents can't ping-pong forever.

The wire format types the text into the target's claude pane with `tmux send-keys
-l`, then Enter, so it lands in that agent's prompt as if a human typed it. The
target pane is resolved LIVE to the pane actually running claude (robust to window
splits), so a restarted/rearranged claude is still reachable.
"""
import os
import subprocess
import time

from . import config, graphstore as gs
from .server import tmuxio

CREW_CMD = os.environ.get("CREW_CMD", "crew")

# How long deliver() waits for a busy target to become idle before giving up and
# leaving the message queued for the background flusher.
READY_WAIT_SECS = 6.0


def whoami():
    """This caller's agent name. Pinned at spawn via $CREW_AGENT / $AGENT_MAIL_NAME
    (set in the tmux session env), with a fall back to the tmux session name."""
    for var in ("CREW_AGENT", "AGENT_MAIL_NAME"):
        v = os.environ.get(var)
        if v and gs.get_agent_by_name(v):
            return v
    pane = os.environ.get("TMUX_PANE")
    if pane:
        ok, sess = tmuxio.tmux("display-message", "-t", pane, "-p", "#S")
        if ok and sess.strip():
            a = gs.get_agent_by_name(sess.strip())
            if a:
                return a["name"]
            return sess.strip()
    return os.environ.get("CREW_AGENT") or os.environ.get("AGENT_MAIL_NAME") or "unknown"


def _sanitize(body):
    """Neutralize a message body so it can't FORGE provenance. Delivery prefixes a
    `[crew msg from <sender>]` line; a malicious body could otherwise embed its own
    fake prefix (or a newline that submits early). Collapse newlines to spaces and
    defang any literal crew-prefix token so the real prefix is unambiguous."""
    b = " ".join((body or "").splitlines()).strip()
    return b.replace("[crew msg from", "[crew-msg-from")


def _type_into_pane(pane, text):
    """Type `text` + Enter into a pane that is ALREADY known idle, then confirm it
    submitted (don't fire Enter blind). Returns True if we believe it landed."""
    try:
        subprocess.run(["tmux", "send-keys", "-t", pane, "-l", "--", text],
                       check=True, timeout=5)
        subprocess.run(["tmux", "send-keys", "-t", pane, "Enter"], check=True, timeout=5)
    except (subprocess.SubprocessError, OSError):
        return False
    # confirm consumption: a submitted prompt makes claude go `working` (or at least
    # clears the input). If it's still idle with our text on screen, the Enter
    # didn't take (rare race) — nudge Enter once more.
    time.sleep(0.4)
    if tmuxio.pane_ready(pane):
        frame = tmuxio.capture_frame(pane)
        if text[:40] in frame:
            try:
                subprocess.run(["tmux", "send-keys", "-t", pane, "Enter"],
                               check=True, timeout=5)
            except (subprocess.SubprocessError, OSError):
                return False
    return True


def _deliver_when_ready(pane, text, wait_secs):
    """Wait up to `wait_secs` for the pane to be idle, then type. Returns True if
    delivered, False if the pane never became ready in time (→ leave it queued)."""
    deadline = time.monotonic() + wait_secs
    while True:
        if tmuxio.pane_ready(pane):
            return _type_into_pane(pane, text)
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.5)


def _format(sender, body, no_prefix):
    if no_prefix:
        return body
    if sender == "crew":   # reserved system sender (e.g. the connections-changed notice)
        return f"[crew] {body}"
    reply = f'↩ reply: {CREW_CMD} message {sender} "..."'
    return f"[crew msg from {sender}] {body}  {reply}"


def _sandbox_hint():
    return ("delivery failed — this claude is sandboxed (CLAUDE_CODE_SANDBOXED=1) so "
            "it can't reach the tmux socket. Set \"sandbox\": false in "
            "~/.claude/settings.json so crew messaging works.")


def deliver(target, body, sender=None, no_prefix=False):
    """Send `body` to agent `target` as `sender`. Returns (ok, message).

    Refuses (ok=False) when the edge graph does not authorize sender→target (the
    hard block) or when the edge's max_turns is exhausted. On an authorized send
    the message is logged and delivered to target's live claude pane once it's
    idle; if the target stays busy it is left QUEUED (ok=True) for the flusher."""
    sender = sender or whoami()
    body = _sanitize(body)
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

    # max_turns: if the authorizing edge caps exchanges, enforce it against the log.
    cap, window = _turn_cap(sender, target)
    if cap and gs.recent_message_count(sender, target, int(time.time()) - window) >= cap:
        return False, (
            f"rate limit reached: the {sender}→{target} edge allows {cap} message(s) "
            f"per {window // 3600 or 1}h. Wait, or raise the limit on the edge.")

    text = _format(sender, body, no_prefix)
    pane = tmuxio.claude_pane(t.get("session") or target)

    # record first (queued), so a crash mid-send never loses the message.
    try:
        msg = gs.create_message(sender, target, body, status="queued")
    except gs.GraphError:
        msg = None

    if not tmuxio.tmux("has-session", "-t", t.get("session") or target)[0]:
        return True, (f"queued for '{target}' — its session isn't running yet; "
                      "will deliver when it comes up.")

    try:
        delivered = _deliver_when_ready(pane, text, READY_WAIT_SECS)
    except (subprocess.SubprocessError, OSError) as e:
        if os.environ.get("CLAUDE_CODE_SANDBOXED"):
            return False, _sandbox_hint()
        return False, f"delivery failed: {e}"

    if delivered:
        if msg:
            try: gs.mark_message(msg["_guid"], "delivered", delivered=True)
            except gs.GraphError: pass
        return True, f"delivered to '{target}' ({pane})"
    return True, (f"queued for '{target}' — it's busy right now; the dashboard will "
                  "deliver this as soon as it's idle.")


def _turn_cap(sender, target):
    """(max_turns, window_secs) for the edge that authorizes sender→target, or
    (0, _) if uncapped. Window is fixed at 1h — a simple, predictable budget."""
    s = gs.get_agent_by_name(sender)
    t = gs.get_agent_by_name(target)
    if not s or not t:
        return 0, 3600
    for e in gs.edges_from_to(s["_guid"], t["_guid"]):
        if int(e.get("max_turns") or 0) > 0:
            return int(e["max_turns"]), 3600
    for e in gs.edges_from_to(t["_guid"], s["_guid"]):
        if not e.get("directed", True) and int(e.get("max_turns") or 0) > 0:
            return int(e["max_turns"]), 3600
    return 0, 3600


def say_to_agent(name, text):
    """Operator → agent (NOT gated). This is the user seeding/kicking an agent from
    the dashboard or `crew kickoff` — it's the human messaging their own agent, not
    peer mail, so the edge gate doesn't apply. Still readiness-gated so we never
    fire Enter blind. Returns (ok, message)."""
    text = _sanitize(text)
    if not text:
        return False, "empty message"
    a = gs.get_agent_by_name(name)
    if not a:
        return False, f"no agent named '{name}'"
    session = a.get("session") or name
    if not tmuxio.tmux("has-session", "-t", session)[0]:
        return False, f"'{name}' has no running session"
    pane = tmuxio.claude_pane(session)
    body = f"[crew · from you] {text}"
    try:
        ok = _deliver_when_ready(pane, body, READY_WAIT_SECS)
    except (subprocess.SubprocessError, OSError) as e:
        return False, f"send failed: {e}"
    return (True, f"sent to '{name}'") if ok else (False, f"'{name}' is busy — try again in a moment")


# A queued message whose target never frees up (busy forever / down) ages out to
# `failed` instead of being re-scanned every few seconds for eternity.
MAX_QUEUE_AGE = 3600  # 1 hour


def flush_queued(limit=50):
    """Deliver queued messages whose target is now idle. Called periodically by the
    dashboard server so a message held back because its target was busy gets
    delivered the moment the target frees up. Messages older than MAX_QUEUE_AGE are
    expired to `failed`. Returns the count delivered."""
    delivered = 0
    now = int(time.time())
    for m in gs.list_messages(status="queued", limit=limit):
        if now - int(m.get("created_at") or now) > MAX_QUEUE_AGE:
            try: gs.mark_message(m["_guid"], "failed")
            except gs.GraphError: pass
            continue
        target = m.get("target")
        t = gs.get_agent_by_name(target)
        if not t:
            try: gs.mark_message(m["_guid"], "failed")
            except gs.GraphError: pass
            continue
        session = t.get("session") or target
        if not tmuxio.tmux("has-session", "-t", session)[0]:
            continue  # session not up yet — keep queued
        pane = tmuxio.claude_pane(session)
        if not tmuxio.pane_ready(pane):
            continue  # still busy — keep queued
        text = _format(m.get("sender") or "crew", m.get("body") or "", False)
        if _type_into_pane(pane, text):
            try: gs.mark_message(m["_guid"], "delivered", delivered=True)
            except gs.GraphError: pass
            delivered += 1
    return delivered
