"""crew.mail — the ONE messaging path between agents, with the gate built in.

The rule "you can only message agents you're connected to" is enforced HERE, at
delivery time, not as UI advice. There is no separate ungated bus in this
product: every agent message goes through `deliver`, which refuses unless the
edge graph authorizes sender→target (crew.graphstore.can_message). So a hard
wall — an agent literally cannot type into a peer it has no edge to.

Delivery is RELIABLE and OBSERVABLE, not fire-and-forget:
  * every message is recorded in the MorphDB message log (queued→delivered);
  * we don't blast Enter into a pane blindly — we wait for the target's claude to
    look idle and hold still (tmuxio.pane_ready) before typing, so a message won't
    interleave with a mid-turn generation or get swallowed by a permission dialog.
    (The one case pane_ready can't detect from outside is a very long inter-chunk
    pause that looks identical to idle; there Claude Code's own input layer is the
    backstop — it buffers text typed mid-turn and submits it when the turn ends, so
    the message still reaches the agent intact rather than corrupting the stream.)
  * if the target is busy past a short window the message stays QUEUED; queues are
    flushed both by the dashboard's background flusher AND inline at the start of
    every deliver()/say_to_agent() call, so a headless box (no dashboard running)
    still drains its queues whenever any crew CLI runs;
  * a message queued past MAX_QUEUE_AGE expires to `failed`, and the SENDER's pane
    gets a best-effort one-line bounce notice — expiry is loud, not silent;
  * a multi-line body (diff/code/JSON handoff) is written in FULL to a file in the
    target's home (.crew-inbox/) and a single-line pointer is delivered in its
    place, so the pane wire format stays one forge-proof line while nothing is
    lost (the message log also keeps the full body);
  * an edge's `max_turns` rate-limits how often sender→target may fire, so a tight
    loop can't run away;
  * an edge's `token_cap`/`cost_cap` budget the TARGET's hourly claude spend (read
    from its transcripts — crew.usage): once over budget, new sends are refused.
    Like max_turns this is enforced in deliver() only, NOT on the queued-flush
    path — a message that already passed the gate still flushes.

The wire format types the text into the target's claude pane with `tmux send-keys
-l`, then Enter, so it lands in that agent's prompt as if a human typed it. The
target pane is resolved LIVE to the pane actually running claude (robust to window
splits), so a restarted/rearranged claude is still reachable.
"""
import os
import re
import subprocess
import sys
import time

from . import config, graphstore as gs, usage
from .notify import notify
from .server import tmuxio

# How long deliver() waits for a busy target to become idle before giving up and
# leaving the message queued for the background flusher.
READY_WAIT_SECS = 6.0

# A queued message whose target never frees up (busy forever / down) ages out to
# `failed` instead of being re-scanned every few seconds for eternity — with a
# bounce notice typed back to the sender's pane (see _bounce).
MAX_QUEUE_AGE = 3600  # 1 hour

# Where a multi-line body lands in the TARGET's home; the pane gets a one-line
# pointer to the file instead (see _inbox_drop).
INBOX_DIR = ".crew-inbox"


def whoami():
    """This caller's agent name.

    The AUTHORITATIVE source is the live tmux session the call runs in ($TMUX_PANE →
    #S): that's the pane crew launched, and an in-pane shell cannot rewrite it. We
    check it FIRST so an agent can't set $CREW_AGENT=<peer> to send as another agent
    and ride that peer's edges past the gate. Only when there's no agent-owned pane
    (e.g. the user running `crew` from their own terminal) do we fall back to the
    pinned $CREW_AGENT / $AGENT_MAIL_NAME hints."""
    sess = ""
    pane = os.environ.get("TMUX_PANE")
    if pane:
        ok, s = tmuxio.tmux("display-message", "-t", pane, "-p", "#S")
        if ok and s.strip():
            sess = s.strip()
            a = gs.get_agent_by_name(sess)
            if a:
                return a["name"]
    for var in ("CREW_AGENT", "AGENT_MAIL_NAME"):
        v = os.environ.get(var)
        if v and gs.get_agent_by_name(v):
            return v
    return sess or os.environ.get("CREW_AGENT") or os.environ.get("AGENT_MAIL_NAME") or "unknown"


def _sanitize(body):
    """Neutralize a message body so it can't FORGE provenance. Delivery prefixes a
    `[crew msg from <sender>]` line; a malicious body could otherwise embed its own
    fake prefix (or a newline that submits early). Collapse newlines to spaces and
    defang any literal crew-prefix token so the real prefix is unambiguous.
    (deliver() reroutes genuinely multi-line bodies to an inbox file BEFORE this —
    see _deliverable — so the collapse only ever hits one-line text.)"""
    b = " ".join((body or "").splitlines()).strip()
    return b.replace("[crew msg from", "[crew-msg-from")


# Typing serialization: the dashboard's 4s flusher and the inline flush in
# deliver()/say_to_agent() run in DIFFERENT processes, so both can pass
# pane_ready for the same target and interleave their send-keys into one pane.
# Cheapest cross-process guard: an O_CREAT|O_EXCL lockfile per target under
# var/, held across the ready-check + type. A lock older than LOCK_STALE is
# broken (its holder crashed mid-type).
# ponytail: advisory, not airtight — two processes can break the SAME stale lock
# in the same instant and both win, and a holder typing longer than 30s loses
# exclusivity. The ceiling is "a crashed holder stalls one target's mail ≤30s,
# and a photo-finish stale-break can interleave once"; real fcntl locks only if
# that ever bites.
LOCK_STALE = 30.0
_VAR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "var")


def _acquire_lock(target):
    """The per-target typing lock, or None if another live process holds it."""
    path = os.path.join(_VAR, f"typing-{target}.lock")
    try:
        os.makedirs(_VAR, exist_ok=True)
        try:
            if time.time() - os.stat(path).st_mtime > LOCK_STALE:
                os.unlink(path)
        except OSError:
            pass
        os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        return path
    except OSError:
        return None


def _release_lock(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def _log_refusal(sender, target, body, status):
    """Best-effort audit row for a send the gate turned away (status one of
    gs.REFUSAL_STATUSES). These rows are excluded from recent_message_count, so
    a retrying sender can't pin its own rate window full, and flush_queued only
    scans status='queued', so they are never delivered."""
    try:
        gs.create_message(sender, target, body, status=status)
    except gs.GraphError:
        pass


def _type_into_pane(pane, text):
    """Type `text` + Enter into a pane that is ALREADY known idle, then confirm it
    submitted (don't fire Enter blind). Returns True if we believe it landed."""
    try:
        subprocess.run(["tmux", "send-keys", "-t", pane, "-l", "--", text],
                       check=True, timeout=5)
    except (subprocess.SubprocessError, OSError):
        return False
    # Snapshot the pane WITH our text in the input box but BEFORE Enter. A successful
    # submit changes the frame (input clears / claude starts working), so we confirm
    # consumption by comparing against THIS frame — not by searching for the text,
    # which is also echoed into the transcript and would make us fire a spurious
    # second Enter (that could pick a permission-menu default).
    before = tmuxio.capture_frame(pane)
    try:
        subprocess.run(["tmux", "send-keys", "-t", pane, "Enter"], check=True, timeout=5)
    except (subprocess.SubprocessError, OSError):
        return False
    time.sleep(0.4)
    # still idle AND the frame is unchanged → the Enter didn't take (rare race); nudge
    # once more. If anything changed (input cleared, working, transcript grew) it
    # submitted, so we do NOT re-send.
    if tmuxio.pane_ready(pane) and tmuxio.capture_frame(pane) == before:
        try:
            subprocess.run(["tmux", "send-keys", "-t", pane, "Enter"], check=True, timeout=5)
        except (subprocess.SubprocessError, OSError):
            return False
    return True


def _deliver_when_ready(pane, text, wait_secs, lock_name, on_typed=None):
    """Wait up to `wait_secs` for the pane to be idle, then type — with the
    per-target typing lock held across the ready-check + type, so this and a
    concurrent flusher can't interleave keystrokes into one pane. `on_typed`
    (if given) runs after a successful type while the lock is STILL HELD, so a
    caller can mark its message delivered before any other process can list it
    as queued and type it again. Returns True if delivered, False if the pane
    never became ready (or stayed locked) in time (→ leave it queued)."""
    deadline = time.monotonic() + wait_secs
    while True:
        lock = _acquire_lock(lock_name)
        if lock:
            try:
                if tmuxio.pane_ready(pane):
                    typed = _type_into_pane(pane, text)
                    if typed and on_typed:
                        on_typed()
                    return typed
            finally:
                _release_lock(lock)
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.5)


def _format(sender, body, no_prefix):
    # The prefix names the sender; HOW to reply (crew message <sender>) is already in
    # every agent's identity.md, so we don't tack a reply hint onto each message.
    if no_prefix:
        return body
    if sender == "crew":   # reserved system sender (e.g. the connections-changed notice)
        return f"[crew] {body}"
    return f"[crew msg from {sender}] {body}"


def _sandbox_hint():
    return ("delivery failed — this claude is sandboxed (CLAUDE_CODE_SANDBOXED=1) so "
            "it can't reach the tmux socket. Set \"sandbox\": false in "
            "~/.claude/settings.json so crew messaging works.")


def _clip(s, n=80):
    return s if len(s) <= n else s[:n].rstrip() + "…"


def _inbox_drop(t_agent, sender, body, created_at=None):
    """Write a multi-line `body` in FULL to <target home>/.crew-inbox/ and return
    the single-line pointer body to deliver instead, or None if the target has no
    usable home (caller falls back to the collapsed body). The filename derives
    from the MESSAGE's created_at, not the wall clock, so a queued message flushed
    later reuses the SAME file (matched by content) instead of dropping a
    duplicate — while a DIFFERENT same-second message from the same sender gets a
    numbered variant rather than overwriting it."""
    home = os.path.expanduser(t_agent.get("home") or "")
    if not home or not os.path.isdir(home):
        return None
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(created_at or time.time()))
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", str(sender or "")) or "unknown"
    base = f"{ts}-from-{safe}"
    data = body if body.endswith("\n") else body + "\n"
    ibox = os.path.join(home, INBOX_DIR)
    try:
        os.makedirs(ibox, exist_ok=True)
        fname, n = base + ".md", 2
        while os.path.exists(os.path.join(ibox, fname)):
            try:
                with open(os.path.join(ibox, fname), encoding="utf-8") as fh:
                    if fh.read() == data:
                        break  # this exact message, re-dropped — reuse the file
            except OSError:
                pass
            fname = f"{base}-{n}.md"
            n += 1
        with open(os.path.join(ibox, fname), "w", encoding="utf-8") as fh:
            fh.write(data)
    except OSError:
        return None
    first = _clip((body.splitlines() or [""])[0].strip())
    return f"(full message in {INBOX_DIR}/{fname}) {first}"


def _deliverable(t_agent, sender, body, created_at=None):
    """The single line actually typed into the pane for `body`. A multi-line body
    (diff/code/JSON handoff) is written whole to the target's .crew-inbox/ and
    replaced by a pointer; a single-line body passes straight through. Either way
    the result goes through _sanitize, so the anti-forgery guarantee holds for
    pointers too."""
    if "\n" in body:
        pointer = _inbox_drop(t_agent, sender, body, created_at)
        if pointer:
            return _sanitize(pointer)
    return _sanitize(body)


def _bounce(m):
    """Dead-letter notice: best-effort tell the SENDER's pane that its queued
    message aged out undelivered. One line, only if the sender has a live pane
    that is idle RIGHT NOW — never waits and never raises, so an unreachable
    sender can't stall or break a flush pass."""
    try:
        sender = m.get("sender") or ""
        s = gs.get_agent_by_name(sender)
        if not s:
            return
        session = s.get("session") or sender
        if not tmuxio.tmux("has-session", "-t", session)[0]:
            return
        pane = tmuxio.claude_pane(session)
        if not tmuxio.pane_ready(pane):
            return
        gist = _clip(_sanitize(m.get("body") or ""))
        _type_into_pane(pane, f'[crew] your message to {m.get("target")} expired '
                              f'undelivered after {MAX_QUEUE_AGE // 3600 or 1}h: "{gist}"')
    except Exception:
        pass


def deliver(target, body, sender=None, no_prefix=False):
    """Send `body` to agent `target` as `sender`. Returns (ok, message).

    Refuses (ok=False) when the edge graph does not authorize sender→target (the
    hard block), when the edge's max_turns is exhausted, or when the edge's
    token/cost budget is spent (budgets are checked HERE only, not in
    flush_queued — mirroring max_turns, a queued message that already passed the
    gate is never re-vetted). On an authorized send
    the target's queued backlog is flushed FIRST (so ordering holds and a headless
    box drains its queues without the dashboard); THEN, if the authorizing edge
    has a `transform` (WAVE 5 — see crew.mail._transform_edge/_run_transform),
    it runs ONCE, right here, at accept time: nonempty stdout REPLACES the body
    for the rest of this call (queued/logged/typed transformed); empty stdout,
    a nonzero exit, or a timeout DROPS the message instead — logged with status
    "filtered" and the ORIGINAL body, the operator notified, and this call
    returns (False, reason). flush_queued() NEVER re-runs a transform — a
    message that was queued busy already carries its (already-transformed)
    final body. After that, the message is logged with its FULL body, and
    delivered to target's live claude pane once it's idle — as a
    one-line .crew-inbox/ pointer when the body is multi-line. If the target stays
    busy — or older messages are still queued ahead of this one — it is left
    QUEUED (ok=True) for the flusher (which delivers oldest-first), expiring
    after MAX_QUEUE_AGE with a bounce notice back to the sender."""
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
        _log_refusal(sender, target, body, "blocked")
        return False, (
            f"BLOCKED: '{sender}' has no relationship to '{target}', so you cannot "
            f"message them. Connect the agents first (crew connect {sender} {target} "
            f"--when \"<condition>\"), or ask the user to add the edge on the dashboard.")

    # max_turns: if the authorizing edge caps exchanges, enforce it against the log.
    cap, window = _turn_cap(sender, target)
    if cap and gs.recent_message_count(sender, target, int(time.time()) - window) >= cap:
        _log_refusal(sender, target, body, "ratelimited")
        return False, (
            f"rate limit reached: the {sender}→{target} edge allows {cap} message(s) "
            f"per {window // 3600 or 1}h. Wait, or raise the limit on the edge.")

    # token/cost budget: if the authorizing edge budgets the target's hourly
    # spend, meter its transcripts (crew.usage — fail-open zeros) and refuse
    # once over. Enforced at deliver() time only, like max_turns.
    tok_cap, cost_cap = _budget_caps(sender, target)
    if tok_cap or cost_cap:
        spend = usage.hourly_spend(t.get("home") or "", time.time() - 3600)
        if tok_cap and spend["tokens"] >= tok_cap:
            _log_refusal(sender, target, body, "budget")
            return False, (
                f"budget reached: the {sender}→{target} edge caps '{target}' at "
                f"{tok_cap:,} tokens/hr and it has spent {spend['tokens']:,} in the "
                f"last hour. Wait, or raise the cap on the edge.")
        if cost_cap and spend["cost"] >= cost_cap:
            _log_refusal(sender, target, body, "budget")
            return False, (
                f"budget reached: the {sender}→{target} edge caps '{target}' at "
                f"${cost_cap:.2f}/hr and it has spent ${spend['cost']:.2f} in the "
                f"last hour. Wait, or raise the cap on the edge.")

    # Self-serve flush: drain the target's queued backlog BEFORE this message, so a
    # headless operator's queue moves whenever any crew CLI runs (no dashboard
    # needed) and this new message can't jump ahead of older queued ones. Runs
    # before this message is recorded, so it can never double-deliver it.
    try:
        flush_queued(target=target)
    except gs.GraphError:
        pass

    # WAVE 5: run the authorizing edge's transform, ONCE, right here, BEFORE
    # the message is recorded — so a queued (busy-target) message already
    # carries its final (transformed) body and flush_queued never re-runs it.
    tr_edge = _transform_edge(sender, target)
    if tr_edge:
        ok, result, short_reason = _run_transform(
            tr_edge["transform"], body, sender, target, tr_edge.get("label") or "")
        if not ok:
            try:
                gs.create_message(sender, target, body, status="filtered")
            except gs.GraphError:
                pass
            notify("message_filtered", sender,
                  f"{tr_edge.get('label') or os.path.basename(tr_edge['transform'])}: "
                  f"{short_reason}")
            return False, result
        body = result

    # record first (queued) with the FULL body, so a crash mid-send never loses the
    # message and the log keeps multi-line content intact.
    try:
        msg = gs.create_message(sender, target, body, status="queued")
    except gs.GraphError:
        msg = None

    expiry = f"{MAX_QUEUE_AGE // 3600 or 1}h"
    if not tmuxio.tmux("has-session", "-t", t.get("session") or target)[0]:
        return True, (f"queued for '{target}' — its session isn't running yet; will "
                      f"deliver when it comes up, or expire undelivered after {expiry}.")

    # Ordering guard: the flush above only preserves order when it actually DRAINED
    # the backlog. If the target was busy, older messages are still queued — direct-
    # delivering this one the instant the pane frees up would jump the queue, so
    # leave it queued too and let the flusher deliver strictly oldest-first.
    if msg:
        try:
            backlog = gs.list_messages(status="queued", target=target, limit=2)
        except gs.GraphError:
            backlog = []
        if any(q.get("_guid") != msg["_guid"] for q in backlog):
            return True, (f"queued for '{target}' behind older queued messages; will "
                          f"be delivered in order as soon as it's idle, but expires "
                          f"undelivered after {expiry} if the target stays busy "
                          f"(you'll get a bounce notice).")

    pane = tmuxio.claude_pane(t.get("session") or target)
    text = _format(sender, _deliverable(t, sender, body,
                                        (msg or {}).get("created_at")), no_prefix)
    def _mark_delivered():
        # runs inside the typing lock, so the flusher's under-lock re-vet can
        # never see this message as still queued after it was typed
        if msg:
            try: gs.mark_message(msg["_guid"], "delivered", delivered=True)
            except gs.GraphError: pass
    try:
        delivered = _deliver_when_ready(pane, text, READY_WAIT_SECS, target,
                                        on_typed=_mark_delivered)
    except (subprocess.SubprocessError, OSError) as e:
        if os.environ.get("CLAUDE_CODE_SANDBOXED"):
            return False, _sandbox_hint()
        return False, f"delivery failed: {e}"

    if delivered:
        return True, f"delivered to '{target}' ({pane})"
    return True, (f"queued for '{target}' — it's busy right now; will be delivered as "
                  f"soon as it's idle, but expires undelivered after {expiry} if the "
                  f"target stays busy (you'll get a bounce notice).")


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


def _budget_caps(sender, target):
    """(token_cap, cost_cap) for the edge that authorizes sender→target, or
    (0, 0.0) if unbudgeted. Mirrors _turn_cap's lookup: forward edges first, then
    undirected reverse edges (their caps bind too)."""
    s = gs.get_agent_by_name(sender)
    t = gs.get_agent_by_name(target)
    if not s or not t:
        return 0, 0.0
    for e in gs.edges_from_to(s["_guid"], t["_guid"]):
        tc, cc = int(e.get("token_cap") or 0), float(e.get("cost_cap") or 0)
        if tc > 0 or cc > 0:
            return tc, cc
    for e in gs.edges_from_to(t["_guid"], s["_guid"]):
        if e.get("directed", True):
            continue
        tc, cc = int(e.get("token_cap") or 0), float(e.get("cost_cap") or 0)
        if tc > 0 or cc > 0:
            return tc, cc
    return 0, 0.0


def _transform_edge(sender, target):
    """The edge (dict) that authorizes sender→target AND carries a nonempty
    `transform`, or None. Same first-edge convention as _turn_cap/_budget_caps:
    a forward edge wins if it has one; else an UNDIRECTED reverse edge's
    transform binds too. (If a forward edge exists but has no transform, we do
    NOT fall through to a reverse edge's transform — the forward edge is
    already the sole authorizing edge in that case, same as _turn_cap.)"""
    s = gs.get_agent_by_name(sender)
    t = gs.get_agent_by_name(target)
    if not s or not t:
        return None
    fwd = gs.edges_from_to(s["_guid"], t["_guid"])
    if fwd:
        for e in fwd:
            if (e.get("transform") or "").strip():
                return e
        return None
    for e in gs.edges_from_to(t["_guid"], s["_guid"]):
        if not e.get("directed", True) and (e.get("transform") or "").strip():
            return e
    return None


def _run_transform(path, body, sender, target, label):
    """Run an edge's transform script ONCE against `body` (crew.mail.deliver
    calls this a single time per message, at accept time — flush_queued never
    re-runs it, see both docstrings). CREW_SENDER/CREW_TARGET/CREW_EDGE_LABEL
    are added to its environment.

    Returns (ok, result, short_reason):
      * exit 0 + nonempty stdout  -> (True, <decoded stripped stdout>, None)
      * exit 0 + empty stdout, nonzero exit, or a timeout -> (False, <public
        reason string for the sender>, <bare reason for the operator notify>)
    """
    env = dict(os.environ)
    env["CREW_SENDER"] = sender
    env["CREW_TARGET"] = target
    env["CREW_EDGE_LABEL"] = label or ""
    script = os.path.basename(path)
    try:
        proc = subprocess.run([sys.executable, path], input=body.encode("utf-8"),
                              capture_output=True, timeout=config.TRANSFORM_TIMEOUT,
                              env=env)
    except subprocess.TimeoutExpired:
        reason = f"timed out after {config.TRANSFORM_TIMEOUT:g}s"
        return False, f"transform {reason}", reason
    out = (proc.stdout or b"").decode("utf-8", errors="replace").strip()
    if proc.returncode == 0 and out:
        return True, out, None
    if proc.returncode == 0:
        reason = "empty output"
    else:
        err_line = (proc.stderr or b"").decode("utf-8", errors="replace").splitlines()
        reason = (err_line[0].strip() if err_line and err_line[0].strip()
                 else f"exit {proc.returncode}")
    return False, f"filtered by {script}: {reason}", reason


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
    # Drain the agent's queued backlog first (same self-serve flush as deliver) so
    # a kickoff from a headless CLI also delivers waiting mail, oldest first.
    try:
        flush_queued(target=name)
    except gs.GraphError:
        pass
    pane = tmuxio.claude_pane(session)
    body = f"[crew · from you] {text}"
    try:
        ok = _deliver_when_ready(pane, body, READY_WAIT_SECS, name)
    except (subprocess.SubprocessError, OSError) as e:
        return False, f"send failed: {e}"
    return (True, f"sent to '{name}'") if ok else (False, f"'{name}' is busy — try again in a moment")


def flush_queued(limit=50, target=None):
    """Deliver queued messages whose target is now idle (all targets, or just
    `target`). Called periodically by the dashboard server AND inline at the start
    of deliver()/say_to_agent(), so queues drain even with no dashboard running.
    Messages older than MAX_QUEUE_AGE are expired to `failed` and their sender's
    pane gets a best-effort bounce notice (_bounce). Returns the count delivered.

    WAVE 5: never re-runs a message's transform — deliver() already ran it ONCE
    at accept time (before the message was even queued), so a queued row's
    `body` is already final; this function just types it out verbatim."""
    delivered = 0
    now = int(time.time())
    # failure notices are BATCHED into one webhook POST after the loop — with a
    # slow/unreachable webhook, per-message notify() (3s timeout each) would stall
    # an inline flush by 3s × N expiries before the caller's own message sends
    failed = []
    for m in gs.list_messages(status="queued", target=target, limit=limit):
        if now - int(m.get("created_at") or now) > MAX_QUEUE_AGE:
            try: gs.mark_message(m["_guid"], "failed")
            except gs.GraphError: pass
            _bounce(m)
            # the bounce only reaches a sender pane that's alive AND idle — the
            # webhook (crew.notify, never raises) reaches the OPERATOR regardless
            failed.append((m.get("sender") or "unknown",
                           f'message to {m.get("target")} expired undelivered: '
                           f'{_clip(_sanitize(m.get("body") or ""), 60)}'))
            continue
        tname = m.get("target")
        t = gs.get_agent_by_name(tname)
        if not t:
            try: gs.mark_message(m["_guid"], "failed")
            except gs.GraphError: pass
            failed.append((m.get("sender") or "unknown",
                           f"message to {tname} failed: that agent no longer exists"))
            continue
        session = t.get("session") or tname
        if not tmuxio.tmux("has-session", "-t", session)[0]:
            continue  # session not up yet — keep queued
        pane = tmuxio.claude_pane(session)
        lock = _acquire_lock(tname)
        if not lock:
            continue  # another process is typing into this target — keep queued
        try:
            # re-vet under the lock: our queued-list snapshot predates it, and a
            # concurrent deliver()/flusher may have typed this message meanwhile
            try:
                cur = gs.get_object(m["_guid"])
            except gs.GraphError:
                cur = None
            if not cur or cur.get("status") != "queued":
                continue
            if not tmuxio.pane_ready(pane):
                continue  # still busy — keep queued
            sender = m.get("sender") or "crew"
            # _deliverable: the log stores the FULL body — multi-line ones become
            # an inbox-file pointer here, exactly as on the direct path (same
            # filename).
            text = _format(sender, _deliverable(t, sender, m.get("body") or "",
                                                m.get("created_at")), False)
            if _type_into_pane(pane, text):
                try: gs.mark_message(m["_guid"], "delivered", delivered=True)
                except gs.GraphError: pass
                delivered += 1
        finally:
            _release_lock(lock)
    if len(failed) == 1:
        notify("message_expired", failed[0][0], failed[0][1])
    elif failed:
        senders = ", ".join(sorted({s for s, _ in failed}))
        notify("message_expired", "crew",
               f"{len(failed)} queued messages failed (senders: {senders}); "
               f"see `crew mail --status failed`")
    return delivered
