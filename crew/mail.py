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
  * an edge's `token_cap`/`cost_cap` budget the TARGET runtime's hourly usage
    (crew.usage): once over budget, new sends are refused; if a configured
    dimension cannot be measured, sends fail closed as `budget_unavailable`.
    Like max_turns this is enforced in deliver() only, NOT on the queued-flush
    path — a message that already passed the gate still flushes.

The wire format types the text into the target's claude pane with `tmux send-keys
-l`, then Enter, so it lands in that agent's prompt as if a human typed it. The
target pane is resolved LIVE to the pane actually running claude (robust to window
splits), so a restarted/rearranged claude is still reachable.
"""
import fcntl
import hashlib
import math
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
import unicodedata

from . import config, graphstore as gs, runtime as runtimes, usage
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

# Keep one tmux send-keys argument comfortably below shell/PTY implementation
# limits.  Larger bodies are retained in the durable log and, when possible,
# the target's private inbox; the pane receives only a bounded pointer.
MAX_WIRE_CHARS = 4000


def _run_tmux(*args, **kwargs):
    """Run one pane command against the endpoint carried by its target."""
    endpoint = config.tmux_target_endpoint(*args)
    return subprocess.run(
        config.tmux_command(*args, endpoint=endpoint),
        env=config.tmux_environment(endpoint=endpoint), **kwargs)


def _tty_name(value):
    return (value or "").replace("/dev/", "")


def _controlling_tty():
    """The process' real controlling tty, independent of mutable env hints."""
    for stream_fd in (0, 1, 2):
        try:
            if os.isatty(stream_fd):
                tty = _tty_name(os.ttyname(stream_fd))
                if tty and tty != "tty":
                    return tty
        except OSError:
            pass
    # Tool subprocesses may redirect every standard stream while retaining the
    # pane as their controlling terminal. `/dev/tty` only reports the generic
    # alias on macOS, so ask the kernel process table for the concrete ttysNNN.
    try:
        result = subprocess.run(
            ["/bin/ps", "-o", "tty=", "-p", str(os.getpid())],
            capture_output=True, text=True, timeout=2)
        tty = _tty_name((result.stdout or "").strip())
        if result.returncode == 0 and tty not in ("", "?", "??", "tty"):
            return tty
    except (OSError, subprocess.SubprocessError):
        pass
    return ""


def _environment_identifies_crew_tmux():
    """Whether inherited tmux state names Crew's exact private socket."""
    raw = os.environ.get("TMUX", "")
    socket, separator, _rest = raw.partition(",")
    if not separator or not socket:
        return False
    try:
        expected = config.crew_tmux_socket_path()
    except OSError:
        return False
    # Do not resolve the untrusted candidate through symlinks. Crew's validated
    # endpoint has one canonical absolute spelling and tmux exports it verbatim.
    return socket == expected


def _actual_tmux_context():
    """Return (pane, tty_was_inventoried) for this process' controlling tty.

    `TMUX_PANE` is intentionally not consulted: a shell can unset it or point it
    at a peer.  The pane inventory binds the kernel-owned controlling tty to the
    session tmux actually assigned to this process.
    """
    tty = _controlling_tty()
    lister = getattr(tmuxio, "_list_tmux_panes", None)
    if not tty or not callable(lister):
        return None, False
    try:
        panes = []
        for endpoint in (
                config.TMUX_ENDPOINT_CREW, config.TMUX_ENDPOINT_LEGACY):
            try:
                rows = lister(endpoint=endpoint)
            except TypeError:
                # Compatibility with minimal embedders/tests predating routed
                # pane inventory. They describe Crew's dedicated endpoint only.
                rows = lister() if endpoint == config.TMUX_ENDPOINT_CREW else []
            panes.extend(rows or [])
    except Exception as error:
        raise gs.GraphError(
            f"could not inventory tmux panes for caller identity: {error}") from error
    matches = [p for p in panes if _tty_name(p.get("tty")) == tty]
    if len(matches) > 1:
        raise gs.GraphError(
            f"controlling tty '{tty}' maps to multiple tmux panes; refusing to "
            "guess the caller identity")
    if matches:
        return matches[0], True
    if _environment_identifies_crew_tmux() and not panes:
        raise gs.GraphError(
            "this process is inside tmux but its pane inventory is unavailable; "
            "refusing to assume human authority")
    return None, True


def _candidate_apps():
    current_app = config.current_app()
    apps = [current_app]
    for project in config.list_known_projects():
        candidate_app = config.project_app(project)
        if candidate_app not in apps:
            apps.append(candidate_app)
    return current_app, apps


def _validated_owner_name(owner, session):
    name = (owner or {}).get("name") or ""
    if config.reserved_agent_name(name):
        raise gs.GraphError(
            f"tmux session '{session}' belongs to legacy agent {name!r}, whose "
            "name is reserved for Crew authority/system state; rename or remove "
            "that row before running commands from this pane")
    return name


def _owner_for_session(session, pane_id=""):
    """Resolve one stored Crew owner for an actual tmux pane across projects.

    The stored pane id is a second durable binding for a managed pane: tmux lets
    a process rename its session, so the current session name alone cannot be an
    authority boundary.  A caller without an inventoried controlling tty passes
    no pane id and retains the exact-session compatibility behavior.
    """
    current_app, apps = _candidate_apps()
    owners = []
    seen = set()
    for app in apps:
        try:
            agents = gs.list_agents(app=app)
        except gs.GraphError as error:
            if "Unknown app" in str(error) or str(error).startswith("404"):
                continue
            raise
        for candidate in agents:
            if pane_id:
                verifier = getattr(tmuxio, "agent_owns_live_target", None)
                if callable(verifier):
                    matches = verifier(candidate, session, pane_id)
                else:
                    matches = candidate.get("pane") == str(pane_id)
            else:
                ownership = getattr(tmuxio, "owned_agent_session", None)
                if callable(ownership):
                    matches = config.same_tmux_target(
                        ownership(candidate), session)
                else:
                    matches = candidate.get("session") == str(session)
            if not matches:
                continue
            identity = (app, candidate.get("_guid") or candidate.get("name"))
            if identity in seen:
                continue
            seen.add(identity)
            owners.append((app, candidate))
    if len(owners) > 1:
        raise gs.GraphError(
            f"tmux pane '{pane_id or '?'}' / session '{session}' is registered "
            "to multiple crew agents or apps; refusing to guess the caller "
            "identity")
    if not owners:
        return None
    owner_app, owner = owners[0]
    name = _validated_owner_name(owner, session)
    if owner_app != current_app:
        raise gs.GraphError(
            f"agent pane '{session}' belongs to app '{owner_app}', but this "
            f"command selected app '{current_app}'; switch back to the owning "
            "project instead of overriding --project/CREW_APP")
    return name


def _session_has_crew_context(session):
    """Whether tmux still carries Crew's pinned ownership markers."""
    endpoint = config.tmux_target_endpoint(session)
    exact = config.tmux_target(f"={session}", endpoint)
    found = 0
    for key in ("CREW_AGENT", "CREW_APP"):
        ok, raw = tmuxio.tmux(
            "show-environment", "-t", exact, key)
        if ok and (raw or "").rstrip("\n").startswith(key + "="):
            found += 1
    return found == 2


def whoami():
    """This caller's agent name.

    The authoritative source is the real controlling tty mapped through tmux's
    pane inventory to the exact stored session owner.  Environment variables are
    only compatibility hints when no controlling tty can be inventoried: an
    in-pane shell can freely unset or forge TMUX_PANE/CREW_AGENT, so none of them
    may override a real pane.  A legacy owner whose name collides with Crew's
    operator/system identities fails closed.
    """
    sess = ""
    actual_pane, tty_inventoried = _actual_tmux_context()
    if actual_pane:
        session_value = actual_pane.get("session") or ""
        session_endpoint = config.tmux_target_endpoint(session_value)
        sess = str(session_value).strip()
        session_target = config.tmux_target(sess, session_endpoint)
        pane_value = actual_pane.get("pane_id") or ""
        pane_id = str(pane_value).strip()
        pane_target = config.tmux_target(pane_id, session_endpoint)
        owner = _owner_for_session(session_target, pane_target)
        if owner:
            return owner
        if session_endpoint == config.TMUX_ENDPOINT_CREW:
            # The dedicated server is Crew's control plane, never an operator's
            # personal tmux namespace. If a pane there loses its durable row or
            # any ownership marker, treating its unregistered session text as a
            # human shell would turn `unset`/rename into operator authority.
            raise gs.GraphError(
                f"tmux pane '{pane_id}' is running on Crew's private endpoint "
                "but no exact registered owner matches its stored pane and "
                "ownership context; refusing to assume human authority")
        if sess and _session_has_crew_context(session_target):
            raise gs.GraphError(
                f"tmux session '{sess}' carries Crew ownership context but has "
                "no registered owner in the selected/known projects; refusing "
                "to assume human authority")
    elif not tty_inventoried:
        # Compatibility for non-interactive test/automation processes that have
        # no controlling tty. A real tty, when present, always wins above.
        pane = os.environ.get("TMUX_PANE")
        if pane and _environment_identifies_crew_tmux():
            ok, raw_session = tmuxio.tmux(
                "display-message", "-t", pane, "-p", "#S")
            if ok and raw_session.strip():
                sess = raw_session.strip()
                session_target = config.tmux_target(
                    sess, config.TMUX_ENDPOINT_CREW)
                pane_target = config.tmux_target(
                    pane, config.TMUX_ENDPOINT_CREW)
                owner = _owner_for_session(session_target, pane_target)
                if owner:
                    return owner
    if tty_inventoried:
        # A real personal/default-server pane is an operator shell, not an
        # agent identity hint. Neither its freely chosen session name nor its
        # shell environment may impersonate a same-named registered agent.
        # Returning unknown lets the CLI retain normal human/operator behavior
        # when no forged agent marker exists; cli.main separately fails closed
        # if an inherited CREW_AGENT/AGENT_MAIL_NAME marker is present.
        return "unknown"
    for var in ("CREW_AGENT", "AGENT_MAIL_NAME"):
        v = os.environ.get(var)
        candidate = gs.get_agent_by_name(v) if v else None
        if candidate:
            return _validated_owner_name(candidate, candidate.get("session") or v)
    return sess or os.environ.get("CREW_AGENT") or os.environ.get("AGENT_MAIL_NAME") or "unknown"


def _sanitize(body):
    """Neutralize a message body so it can't FORGE provenance. Delivery prefixes a
    `[crew msg from <sender>]` line; a malicious body could otherwise embed its own
    fake prefix (or a newline that submits early). Collapse newlines to spaces and
    defang any literal crew-prefix token so the real prefix is unambiguous.
    (deliver() reroutes genuinely multi-line bodies to an inbox file BEFORE this —
    see _deliverable — so the collapse only ever hits one-line text.)"""
    safe = []
    for char in body or "":
        codepoint = ord(char)
        # Whitespace controls become visible separation.  Every other C0/C1
        # control (including ESC, BEL, NUL, backspace, CSI, and DEL) is removed
        # so stored mail cannot operate the receiver's terminal.
        if (char in "\t\n\v\f\r" or codepoint in range(0x1C, 0x20)
                or codepoint == 0x85
                or unicodedata.category(char) in ("Zl", "Zp")):
            safe.append(" ")
        elif codepoint < 0x20 or 0x7F <= codepoint <= 0x9F:
            continue
        else:
            safe.append(char)
    b = " ".join("".join(safe).split()).strip()
    return b.replace("[crew msg from", "[crew-msg-from")


# Typing serialization: dashboard and CLI flushers run in different processes.
# A stable lock inode plus kernel-owned flock avoids pathname stale-break races;
# closing the descriptor (including on process exit) releases ownership.
# Tests may override this with an isolated directory. Production resolves a
# private per-UID runtime directory that remains writable inside agent sandboxes
# without granting write access to the source checkout.
_VAR = None


class _FileLock:
    __slots__ = ("fd", "path")

    def __init__(self, fd, path):
        self.fd = fd
        self.path = path


def _lock_path(identity, kind="typing"):
    app = config.current_app() or "crew"
    scope = f"{app}\0{kind}\0{identity}".encode("utf-8", errors="surrogatepass")
    digest = hashlib.sha256(scope).hexdigest()
    directory = _VAR or config.runtime_state_dir("mail-locks")
    return os.path.join(directory, f"{kind}-{digest}.lock")


def _acquire_lock(identity, *, kind="typing", blocking=False):
    """Acquire an app+kind+immutable-identity flock, or None if contended."""
    path = _lock_path(identity, kind)
    directory = os.path.dirname(path)
    fd = None
    try:
        os.makedirs(directory, mode=0o700, exist_ok=True)
        flags = os.O_CREAT | os.O_RDWR
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags, 0o600)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise PermissionError(f"unsafe mail lock file: {path}")
        os.fchmod(fd, 0o600)
        operation = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        fcntl.flock(fd, operation)
        return _FileLock(fd, path)
    except BlockingIOError:
        if fd is not None:
            os.close(fd)
        return None
    except OSError:
        if fd is not None:
            os.close(fd)
        return None


def _release_lock(lock):
    if not lock:
        return
    try:
        fcntl.flock(lock.fd, fcntl.LOCK_UN)
    except OSError:
        pass
    finally:
        try:
            os.close(lock.fd)
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


def _live_owned_session(agent):
    """Return a verified live session without dropping its tmux endpoint."""
    ownership = getattr(tmuxio, "owned_agent_session", None)
    if callable(ownership):
        session = ownership(agent)
        if not session:
            return None
        endpoint = config.tmux_target_endpoint(session)
        exact = config.tmux_target(f"={session}", endpoint)
        return session if tmuxio.tmux("has-session", "-t", exact)[0] else None
    session = agent.get("session") or agent.get("name")
    return session if (session and tmuxio.tmux(
        "has-session", "-t", session)[0]) else None


def _pane_for_agent(agent):
    runtime_key = runtimes.resolve_agent_runtime(agent)
    ownership = getattr(tmuxio, "owned_agent_session", None)
    if ownership:
        session = ownership(agent)
        if not session:
            return None, runtime_key
    else:
        session = agent.get("session") or agent.get("name")
    exact_resolver = getattr(tmuxio, "exact_runtime_pane", None)
    if callable(exact_resolver):
        pane = exact_resolver(agent, session)
        if (pane and ownership
                and not config.same_tmux_target(ownership(agent), session)):
            return None, runtime_key
        return pane, runtime_key
    resolver = getattr(tmuxio, "runtime_pane", None)
    if callable(resolver):
        # Never fall back to the session's first shell pane. If the managed
        # runtime died but tmux stayed up, typing message text + Enter into that
        # shell would execute the message as a command. A missing runtime pane is
        # an ordinary unavailable/queued state, handled explicitly by every
        # caller below.
        pane = resolver(
            session, runtime_key, agent.get("launch_cmd"), fallback=False)
        if not pane:
            stored_resolver = getattr(tmuxio, "stored_runtime_pane", None)
            if callable(stored_resolver):
                pane = stored_resolver(agent, session)
        if (pane and ownership
                and not config.same_tmux_target(ownership(agent), session)):
            return None, runtime_key
        return pane, runtime_key
    return tmuxio.claude_pane(session), runtime_key


def _runtime_state(pane, runtime_key):
    detector = getattr(tmuxio, "detect_status", None)
    if detector:
        return detector(tmuxio.capture_frame(pane), runtime_key)
    return "idle" if tmuxio.pane_ready(pane) else "working"


def _pane_ready(pane, runtime_key):
    try:
        return tmuxio.pane_ready(pane, runtime_key)
    except TypeError:  # compatibility with small test/third-party fakes
        return tmuxio.pane_ready(pane)


class _SubmitOutcome(str):
    """String-like submission result whose truth value preserves old callers."""

    def __new__(cls, value, accepted=False):
        obj = super().__new__(cls, value)
        obj.accepted = accepted
        return obj

    def __bool__(self):
        return self.accepted


_SUBMITTED = _SubmitOutcome("submitted", accepted=True)
_NOT_STARTED = _SubmitOutcome("not_started")
_DELIVERY_UNCERTAIN = _SubmitOutcome("delivery_uncertain")


class _DeliveryIdentityError(Exception):
    """A claimed row no longer binds to the identities accepted for delivery."""


def _type_into_pane(pane, text, runtime_key="claude", submit_key="Enter"):
    """Attempt one tmux submission and classify its at-most-once boundary.

    ``not_started`` is reserved for an OS-level failure before tmux launched.
    Once an external command may have acted, any failure is ``delivery_uncertain``
    and must never be automatically retried.  ``submitted`` means Crew observed
    the complete tmux command sequence succeed.
    """
    try:
        _run_tmux("send-keys", "-t", pane, "-l", "--", text,
                  check=True, timeout=5)
    except OSError:
        return _NOT_STARTED
    except subprocess.SubprocessError:
        return _DELIVERY_UNCERTAIN
    # Snapshot the pane WITH our text in the input box but BEFORE Enter. A successful
    # submit changes the frame (input clears / claude starts working), so we confirm
    # consumption by comparing against THIS frame — not by searching for the text,
    # which is also echoed into the transcript and would make us fire a spurious
    # second Enter (that could pick a permission-menu default).
    try:
        before = tmuxio.capture_frame(pane)
    except Exception:
        return _DELIVERY_UNCERTAIN
    try:
        _run_tmux("send-keys", "-t", pane, submit_key,
                  check=True, timeout=5)
    except (subprocess.SubprocessError, OSError):
        return _DELIVERY_UNCERTAIN
    if submit_key != "Enter":
        # Codex documents Tab as "queue for next turn" while it is working.
        # The TUI owns that queue; one successful key submission is acceptance.
        return _SUBMITTED
    time.sleep(0.4)
    # still idle AND the frame is unchanged → the Enter didn't take (rare race); nudge
    # once more. If anything changed (input cleared, working, transcript grew) it
    # submitted, so we do NOT re-send.
    try:
        needs_nudge = (_pane_ready(pane, runtime_key)
                       and tmuxio.capture_frame(pane) == before)
    except Exception:
        return _DELIVERY_UNCERTAIN
    if needs_nudge:
        try:
            _run_tmux("send-keys", "-t", pane, "Enter", check=True, timeout=5)
        except (subprocess.SubprocessError, OSError):
            return _DELIVERY_UNCERTAIN
    return _SUBMITTED


def _deliver_when_ready(pane, text, wait_secs, lock_name, on_typed=None,
                        runtime_key="claude", on_claim=None, on_result=None,
                        pane_resolver=None):
    """Wait up to `wait_secs` for the pane to be idle, then type — with the
    per-target typing lock held across the ready-check + type, so this and a
    concurrent flusher can't interleave keystrokes into one pane. `on_typed`
    (if given) runs after a successful type while the lock is STILL HELD, so a
    caller can mark its message delivered before any other process can list it
    as queued and type it again. When supplied, ``pane_resolver`` re-resolves
    the owned runtime pane only after this lock is held, so a restart between
    discovery and submission cannot leave a stale pane cached. Returns True if
    delivered, False if the pane never became ready (or stayed locked) in time
    (→ leave it queued)."""
    deadline = time.monotonic() + wait_secs
    while True:
        lock = _acquire_lock(lock_name)
        if lock:
            try:
                active_pane = pane
                active_runtime = runtime_key
                if pane_resolver is not None:
                    resolved = pane_resolver()
                    if isinstance(resolved, tuple):
                        active_pane, active_runtime = resolved
                    else:
                        active_pane = resolved
                    if not active_pane:
                        return "not_started"
                state = _runtime_state(active_pane, active_runtime)
                if active_runtime == "codex" and state == "working":
                    if on_claim:
                        on_claim()
                    payload = text() if callable(text) else text
                    typed = _type_into_pane(
                        active_pane, payload,
                        runtime_key="codex", submit_key="Tab")
                    if typed:
                        if on_typed:
                            on_typed()
                        if on_result:
                            on_result("runtime_queued")
                        return "runtime_queued"
                    outcome = ("delivery_uncertain"
                               if typed == _DELIVERY_UNCERTAIN
                               else "not_started")
                    if on_result:
                        on_result(outcome)
                    return outcome
                if active_runtime == "custom" and state == "unknown":
                    return False
                if _pane_ready(active_pane, active_runtime):
                    if on_claim:
                        on_claim()
                    payload = text() if callable(text) else text
                    typed = _type_into_pane(
                        active_pane, payload,
                        runtime_key=active_runtime, submit_key="Enter")
                    if typed:
                        if on_typed:
                            on_typed()
                        if on_result:
                            on_result("delivered")
                        return "delivered"
                    outcome = ("delivery_uncertain"
                               if typed == _DELIVERY_UNCERTAIN
                               else "not_started")
                    if on_result:
                        on_result(outcome)
                    return outcome
            finally:
                _release_lock(lock)
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.5)


def _limit_wire(text, marker="… [truncated]"):
    """Bound one already-sanitized pane payload while keeping truncation loud."""
    if len(text) <= MAX_WIRE_CHARS:
        return text
    if len(marker) >= MAX_WIRE_CHARS:
        return marker[:MAX_WIRE_CHARS]
    return text[:MAX_WIRE_CHARS - len(marker)].rstrip() + marker


def _format(sender, body, no_prefix):
    # The prefix names the sender; HOW to reply (crew message <sender>) is already in
    # every agent's identity.md, so we don't tack a reply hint onto each message.
    safe_body = _sanitize(body)
    if no_prefix:
        return _limit_wire(safe_body)
    if sender == "crew":   # reserved system sender (e.g. the connections-changed notice)
        return _limit_wire(f"[crew] {safe_body}")
    safe_sender = _sanitize(sender) or "unknown"
    return _limit_wire(f"[crew msg from {safe_sender}] {safe_body}")


def _sandbox_hint():
    return ("delivery failed — this agent runtime is in the Claude Code sandbox "
            "(CLAUDE_CODE_SANDBOXED=1), so it can't reach the tmux socket. "
            "Set \"sandbox\": false in "
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
    # The stored home is graph data, so canonicalizing it BEFORE checking it
    # would hand write authority to whatever a symlinked home points at — the
    # O_NOFOLLOW work below only protects paths under the home once the home
    # itself is trusted.  spawn._validated_agent_home is the same fail-closed
    # resolver identity.md writes already use; on refusal the caller keeps the
    # durable message and falls back to the truncated terminal delivery.
    from . import spawn  # local: crew.spawn imports the mail-free graph layer
    try:
        home = spawn._validated_agent_home(t_agent)
    except gs.GraphError:
        return None
    if not os.path.isdir(home):
        return None
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(created_at or time.time()))
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", str(sender or "")) or "unknown"
    base = f"{ts}-from-{safe}"
    data = body if body.endswith("\n") else body + "\n"
    home_fd = None
    dir_fd = None
    try:
        # This folder carries full handoffs, not just the single-line terminal
        # pointer. Keep it private even under a permissive user umask, and never
        # follow an agent-created symlink that redirects Crew's write outside the
        # managed home.  Anchor every step to the HOME descriptor, so a home
        # swapped for a symlink after the check above cannot move the write
        # either.
        dir_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        dir_flags |= getattr(os, "O_NOFOLLOW", 0)
        home_fd = os.open(home, dir_flags)
        try:
            os.mkdir(INBOX_DIR, 0o700, dir_fd=home_fd)
        except FileExistsError:
            pass  # existing entry: the O_NOFOLLOW open below vets it
        dir_fd = os.open(INBOX_DIR, dir_flags, dir_fd=home_fd)
        if not stat.S_ISDIR(os.fstat(dir_fd).st_mode):
            return None
        os.fchmod(dir_fd, 0o700)

        fname, n = base + ".md", 2
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        while True:
            try:
                read_fd = os.open(fname, os.O_RDONLY | nofollow, dir_fd=dir_fd)
            except FileNotFoundError:
                try:
                    write_fd = os.open(
                        fname, os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow,
                        0o600, dir_fd=dir_fd)
                except FileExistsError:
                    continue  # raced another writer; inspect it on the next pass
                try:
                    os.fchmod(write_fd, 0o600)
                    with os.fdopen(write_fd, "w", encoding="utf-8") as fh:
                        write_fd = None
                        fh.write(data)
                finally:
                    if write_fd is not None:
                        os.close(write_fd)
                break
            except OSError:
                # Symlink, directory, device, or another unsafe existing entry:
                # never follow/overwrite it; choose the numbered sibling.
                fname = f"{base}-{n}.md"
                n += 1
                continue
            else:
                try:
                    if not stat.S_ISREG(os.fstat(read_fd).st_mode):
                        same = False
                    else:
                        os.fchmod(read_fd, 0o600)
                        with os.fdopen(read_fd, encoding="utf-8") as fh:
                            read_fd = None
                            same = fh.read() == data
                    if same:
                        break  # this exact message, re-dropped — reuse the file
                finally:
                    if read_fd is not None:
                        os.close(read_fd)
                fname = f"{base}-{n}.md"
                n += 1
    except OSError:
        return None
    finally:
        for fd in (dir_fd, home_fd):
            if fd is not None:
                os.close(fd)
    first = _clip((body.splitlines() or [""])[0].strip())
    return f"(full message in {INBOX_DIR}/{fname}) {first}"


def _deliverable(t_agent, sender, body, created_at=None):
    """The single line actually typed into the pane for `body`. A multi-line body
    (diff/code/JSON handoff) is written whole to the target's .crew-inbox/ and
    replaced by a pointer; a single-line body passes straight through. Either way
    the result goes through _sanitize, so the anti-forgery guarantee holds for
    pointers too."""
    safe_body = _sanitize(body)
    has_line_break = bool(re.search(
        r"[\n\r\v\f\x1c-\x1e\x85\u2028\u2029]", body))
    if has_line_break or len(safe_body) > MAX_WIRE_CHARS:
        pointer = _inbox_drop(t_agent, sender, body, created_at)
        if pointer:
            return _sanitize(pointer)
    return _limit_wire(
        safe_body,
        marker="… [truncated; full body remains in Crew message log]")


def _bound_target(message):
    """Resolve the immutable target identity for a durable message.

    The display name is deliberately not authoritative: it can be reused after
    deletion.  Legacy rows without a GUID cannot be proven safe and fail
    honestly instead of being routed to whoever currently owns the old name.
    """
    guid = (message.get("target_guid") or "").strip()
    snapshot = message.get("target") or "unknown"
    if not guid:
        return None, (
            f"legacy message lacks a target identity binding for '{snapshot}'")
    try:
        agent = gs.get_object(guid)
    except gs.GraphError as error:
        # Only a concrete 404 proves this immutable identity is gone. A timeout,
        # connection error, or server failure is retryable queue uncertainty.
        if not str(error).lstrip().startswith("404:"):
            raise
        return None, (
            f"target identity for '{snapshot}' no longer exists or was replaced")
    name = (agent or {}).get("name")
    current = gs.get_agent_by_name(name) if name else None
    if not current or current.get("_guid") != guid:
        return None, (
            f"target identity for '{snapshot}' is no longer an active agent")
    return agent, ""


def _sender_identity_error(message):
    """Return why immutable sender provenance is unsafe, or an empty string."""
    snapshot = message.get("sender") or "unknown"
    if snapshot == "crew":
        return ""  # reserved system provenance has no agent row
    guid = (message.get("sender_guid") or "").strip()
    if not guid:
        return f"legacy message lacks a sender identity binding for '{snapshot}'"
    try:
        agent = gs.get_object(guid)
    except gs.GraphError as error:
        if not str(error).lstrip().startswith("404:"):
            raise
        # Historical mail from a removed sender can retain truthful immutable
        # provenance.  Name reuse is the unsafe case: it would make the row look
        # attributable to the replacement.
        replacement = gs.get_node_by_name(snapshot)
        if replacement:
            return f"sender identity for '{snapshot}' was deleted and replaced"
        return ""
    name = (agent or {}).get("name")
    current = gs.get_node_by_name(name) if name else None
    if not current or current.get("_guid") != guid:
        return f"sender identity for '{snapshot}' is no longer an active agent"
    replacement = gs.get_node_by_name(snapshot)
    if replacement and replacement.get("_guid") != guid:
        return f"sender identity for '{snapshot}' was replaced"
    return ""


def _bound_sender_agent(message):
    """Return the live sender identity for private bounce delivery, or None."""
    snapshot = message.get("sender") or ""
    guid = (message.get("sender_guid") or "").strip()
    if snapshot == "crew" or not guid:
        return None
    try:
        agent = gs.get_object(guid)
    except gs.GraphError:
        return None
    if (agent or {}).get("kind") == gs.WEBHOOK_KIND:
        return None
    current = gs.get_node_by_name((agent or {}).get("name"))
    if not current or current.get("_guid") != guid:
        return None
    snapshot_owner = gs.get_node_by_name(snapshot)
    if snapshot_owner and snapshot_owner.get("_guid") != guid:
        return None
    return agent


def _bounce(m):
    """Dead-letter notice: best-effort tell the SENDER's pane that its queued
    message aged out undelivered. One line, only if the sender has a live pane
    that is idle RIGHT NOW — never waits and never raises, so an unreachable
    sender can't stall or break a flush pass."""
    try:
        sender = m.get("sender") or ""
        s = _bound_sender_agent(m)
        if not s:
            return
        if not _live_owned_session(s):
            return
        pane, runtime_key = _pane_for_agent(s)
        if not pane:
            return
        lock = _acquire_lock(s["_guid"])
        if not lock:
            return
        try:
            if not _pane_ready(pane, runtime_key):
                return
            gist = _clip(_sanitize(m.get("body") or ""))
            _type_into_pane(
                pane, f'[crew] your message to {m.get("target")} expired '
                      f'undelivered after {MAX_QUEUE_AGE // 3600 or 1}h: "{gist}"',
                runtime_key=runtime_key)
        finally:
            _release_lock(lock)
    except Exception:
        pass


def _message_for_request_id(request_id, sender, target, sender_agent,
                            target_agent, edge):
    """Reconcile one caller-owned logical send before re-running any gate.

    Webhook retries derive a stable request ID per invocation edge. Returning
    an already-created row here reuses its rate reservation and transformed
    result when the original HTTP response or final invocation PATCH was lost.
    """
    if request_id is None:
        return None
    request_id = str(request_id).strip()
    if not request_id:
        raise gs.GraphError("message request_id must be a nonempty string")
    rows = (gs.list_objects(
        "message", request_id=request_id, limit=2) or {}).get("objects", [])
    if not rows:
        return None
    expected = {
        "sender": sender, "target": target,
        "sender_guid": sender_agent.get("_guid") or "",
        "target_guid": target_agent.get("_guid") or "",
        "edge_guid": edge.get("_guid") or "",
        "request_id": request_id,
    }
    if len(rows) != 1 or any(
            rows[0].get(key) != value for key, value in expected.items()):
        raise gs.GraphError(
            "message request_id already exists with different routing content")
    return rows[0]


def _reconcile_webhook_message(
        request_id, sender, target, expected_edge_guid,
        expected_sender_guid, expected_target_guid):
    """Resolve a webhook's durable send before consulting mutable graph state.

    The webhook receipt freezes exact endpoint and edge GUIDs and derives one
    stable message request ID per route. Once that exact message row exists, a
    later edge/agent deletion must not turn a retry into a rejection. Conversely,
    a colliding request ID or malformed durable status fails closed.
    """
    expected = (
        request_id, expected_edge_guid,
        expected_sender_guid, expected_target_guid,
    )
    if not all(value is not None for value in expected):
        return None
    request_id = str(request_id).strip()
    if not request_id:
        raise gs.GraphError("message request_id must be a nonempty string")
    rows = (gs.list_objects(
        "message", request_id=request_id, limit=2) or {}).get("objects", [])
    if not rows:
        return None
    expected_fields = {
        "sender": sender,
        "target": target,
        "sender_guid": expected_sender_guid,
        "target_guid": expected_target_guid,
        "edge_guid": expected_edge_guid,
        "request_id": request_id,
    }
    if len(rows) != 1 or any(
            rows[0].get(key) != value
            for key, value in expected_fields.items()):
        raise gs.GraphError(
            "message request_id already exists with different routing content")
    message = rows[0]
    status = message.get("status")
    if status == "filtered":
        return (
            False,
            message.get("status_detail")
            or "message was filtered by the edge transform",
        )
    if status not in {
        "queued",
        "submitting",
        "delivered",
        "runtime_queued",
        "delivery_uncertain",
        "failed",
    }:
        raise gs.GraphError(
            "message request_id has an invalid durable status")
    return True, message


def _reserve_accepted_message(sender, target, body, no_prefix,
                              sender_agent, target_agent, edge, limits,
                              request_id=None, raise_graph_errors=False):
    """Apply acceptance gates and create the durable row atomically for caps.

    When ``max_turns`` is configured, the app+edge+direction rate lock spans the
    count read through the message create.  The created row is the reservation;
    another process cannot observe the old count until that reservation exists.
    """
    existing = _message_for_request_id(
        request_id, sender, target, sender_agent, target_agent, edge)
    if existing:
        if existing.get("status") == "filtered":
            return None, body, (
                False, existing.get("status_detail")
                or "message was filtered by the edge transform")
        return existing, existing.get("body") or body, None

    cap, window = limits["max_turns"], 3600
    rate_lock = None
    if cap:
        direction = (f"{edge.get('_guid')}:{sender_agent.get('_guid')}:"
                     f"{target_agent.get('_guid')}")
        rate_lock = _acquire_lock(direction, kind="rate", blocking=True)
        if not rate_lock:
            if raise_graph_errors:
                raise gs.GraphError(
                    "rate reservation unavailable before durable acceptance")
            _log_refusal(sender, target, body, "blocked")
            return None, body, (
                False,
                "rate reservation unavailable: Crew could not acquire the "
                "durable rate-limit lock; no message was accepted")
    try:
        if (cap and gs.recent_message_count(
                sender, target, int(time.time()) - window,
                edge_guid=edge.get("_guid"),
                sender_guid=sender_agent.get("_guid"),
                target_guid=target_agent.get("_guid"),
                ceiling=cap) >= cap):
            _log_refusal(sender, target, body, "ratelimited")
            return None, body, (
                False,
                f"rate limit reached: the {sender}→{target} edge allows "
                f"{cap} message(s) per {window // 3600 or 1}h. Wait, or "
                "raise the limit on the edge.")

        # Token/cost budgets are evaluated while a capped acceptance owns its
        # reservation lock, so no later send can pass the same rate snapshot.
        tok_cap = limits["token_cap"]
        cost_cap = limits["cost_cap"]
        if tok_cap or cost_cap:
            runtime_key = runtimes.resolve_agent_runtime(target_agent)
            spend = usage.hourly_usage(
                target_agent.get("home") or "", time.time() - 3600,
                runtime_key=runtime_key)
            configured = (("tokens", tok_cap, f"{tok_cap:,} tokens/hr"),
                          ("cost", cost_cap, f"${cost_cap:.2f}/hr"))
            for dimension, cap_value, cap_label in configured:
                if not cap_value:
                    continue
                metric = spend[dimension]
                if not metric["available"]:
                    _log_refusal(
                        sender, target, body, "budget_unavailable")
                    return None, body, (
                        False,
                        f"budget unavailable: cannot verify the {cap_label} "
                        f"{dimension} cap for '{target}' because "
                        f"{metric['reason']}. Delivery is blocked so an "
                        "unavailable meter cannot bypass the cap.")
            if tok_cap and spend["tokens"]["value"] >= tok_cap:
                _log_refusal(sender, target, body, "budget")
                return None, body, (
                    False,
                    f"budget reached: the {sender}→{target} edge caps "
                    f"'{target}' at {tok_cap:,} tokens/hr and it has spent "
                    f"{spend['tokens']['value']:,} in the last hour. Wait, "
                    "or raise the cap on the edge.")
            if cost_cap and spend["cost"]["value"] >= cost_cap:
                _log_refusal(sender, target, body, "budget")
                return None, body, (
                    False,
                    f"budget reached: the {sender}→{target} edge caps "
                    f"'{target}' at ${cost_cap:.2f}/hr and it has spent "
                    f"${spend['cost']['value']:.2f} in the last hour. Wait, "
                    "or raise the cap on the edge.")

        # Transform during acceptance before the final body is durably
        # reserved. A durable request-ID row prevents later retries from
        # re-running it; a process crash inside this pre-row window cannot.
        tr_edge = edge if (edge.get("transform") or "").strip() else None
        if tr_edge:
            ok, result, short_reason = _run_transform(
                tr_edge["transform"], body, sender, target,
                tr_edge.get("label") or "")
            if not ok:
                try:
                    gs.create_message(
                        sender, target, body, status="filtered",
                        sender_guid=sender_agent["_guid"],
                        target_guid=target_agent["_guid"],
                        edge_guid=edge["_guid"], no_prefix=no_prefix,
                        status_detail=result, request_id=request_id)
                except gs.GraphError:
                    if raise_graph_errors:
                        raise
                    pass
                notify(
                    "message_filtered", sender,
                    f"{tr_edge.get('label') or os.path.basename(tr_edge['transform'])}: "
                    f"{short_reason}")
                return None, body, (False, result)
            body = result

        try:
            message = gs.create_message(
                sender, target, body, status="queued",
                sender_guid=sender_agent["_guid"],
                target_guid=target_agent["_guid"],
                edge_guid=edge["_guid"], no_prefix=no_prefix,
                request_id=request_id)
        except gs.GraphError as error:
            if raise_graph_errors:
                raise
            return None, body, (
                False,
                "delivery refused before submission: could not create the "
                f"durable message row ({error})")
        return message, body, None
    finally:
        _release_lock(rate_lock)


def _accept_message(
        target, body, sender=None, no_prefix=False, *,
        request_id=None, flush_existing=True,
        expected_edge_guid=None, expected_sender_guid=None,
        expected_target_guid=None, raise_graph_errors=False):
    """Run the graph/budget/transform gate and durably reserve one message."""
    sender = sender or whoami()
    body = (body or "").strip()
    if not body:
        return None, (False, "empty message")
    if sender == target:
        return None, (False, "can't message yourself")

    target_agent = gs.get_agent_by_name(target)
    if not target_agent:
        return None, (False, f"no agent named '{target}'")

    # Interactive sends opportunistically drain older accepted work. Webhook
    # ingress disables this potentially slow step and lets the dashboard's
    # background flusher deliver the newly-reserved FIFO rows.
    if flush_existing:
        try:
            flush_queued(target=target)
        except gs.GraphError:
            pass

    try:
        with gs._invariant_lock("edge-authorization"):
            target_agent = gs.get_agent_by_name(target)
            if not target_agent:
                return None, (False, f"no agent named '{target}'")
            edge = gs.authorizing_edge(sender, target)
            if not edge:
                _log_refusal(sender, target, body, "blocked")
                return None, (False, (
                    f"BLOCKED: '{sender}' has no relationship to '{target}', so you "
                    f"cannot message them. Connect the agents first (crew connect "
                    f"{sender} {target} --when \"<condition>\"), or ask the user to "
                    "add the edge on the dashboard."))
            if (
                (expected_edge_guid
                 and edge.get("_guid") != expected_edge_guid)
                or (
                    expected_target_guid
                    and target_agent.get("_guid") != expected_target_guid
                )
            ):
                _log_refusal(sender, target, body, "blocked")
                return None, (False, (
                    "BLOCKED: webhook route identity changed after the "
                    "invocation snapshot; send a new invocation"))

            # Bind provenance to the exact endpoint GUID from the edge that
            # authorized this send. Names are display snapshots only.
            if edge.get("target") == target_agent.get("_guid"):
                sender_guid = edge.get("source")
            elif (not edge.get("directed", True)
                  and edge.get("source") == target_agent.get("_guid")):
                sender_guid = edge.get("target")
            else:
                sender_guid = ""
            if expected_sender_guid and sender_guid != expected_sender_guid:
                _log_refusal(sender, target, body, "blocked")
                return None, (False, (
                    "BLOCKED: webhook route identity changed after the "
                    "invocation snapshot; send a new invocation"))
            sender_agent = gs.get_object(sender_guid) if sender_guid else None
            current_sender = gs.get_node_by_name(sender)
            if (not sender_agent or sender_agent.get("name") != sender
                    or not current_sender
                    or current_sender.get("_guid") != sender_guid):
                _log_refusal(sender, target, body, "blocked")
                return None, (False, (
                    f"BLOCKED: sender identity for '{sender}' changed during "
                    "authorization; retry from the currently registered node"))

            try:
                limits = gs.normalize_edge_numeric_fields({
                    "max_turns": edge.get("max_turns") or 0,
                    "token_cap": edge.get("token_cap") or 0,
                    "cost_cap": edge.get("cost_cap") or 0,
                })
            except gs.GraphError as error:
                _log_refusal(sender, target, body, "blocked")
                return None, (False, (
                    f"BLOCKED: invalid edge limits for {sender}→{target}: "
                    f"{error}. Ask the user to repair the edge before messaging."))
            message, accepted_body, rejection = _reserve_accepted_message(
                sender, target, body, no_prefix, sender_agent, target_agent,
                edge, limits, request_id=request_id,
                raise_graph_errors=raise_graph_errors)
    except gs.GraphError as error:
        if raise_graph_errors:
            raise
        _log_refusal(sender, target, body, "blocked")
        return None, (False, f"BLOCKED: {error}")
    if rejection:
        return None, rejection
    return {
        "sender": sender, "target": target, "body": accepted_body,
        "sender_agent": sender_agent, "target_agent": target_agent,
        "edge": edge, "message": message,
    }, None


def enqueue(
        target, body, sender=None, no_prefix=False, *, request_id=None,
        expected_edge_guid=None, expected_sender_guid=None,
        expected_target_guid=None, raise_graph_errors=False):
    """Durably accept one graph-authorized message without waiting on tmux.

    Used by public webhook fan-out so the HTTP response time is independent of
    agent runtime readiness. The regular background flusher owns delivery.
    Returns ``(True, message_row)`` or ``(False, reason)``.
    """
    sender = sender or whoami()
    reconciled = _reconcile_webhook_message(
        request_id, sender, target, expected_edge_guid,
        expected_sender_guid, expected_target_guid)
    if reconciled is not None:
        return reconciled
    accepted, rejection = _accept_message(
        target, body, sender=sender, no_prefix=no_prefix,
        request_id=request_id, flush_existing=False,
        expected_edge_guid=expected_edge_guid,
        expected_sender_guid=expected_sender_guid,
        expected_target_guid=expected_target_guid,
        raise_graph_errors=raise_graph_errors)
    if rejection:
        return rejection
    return True, accepted["message"]


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
    accepted, rejection = _accept_message(
        target, body, sender=sender, no_prefix=no_prefix,
        flush_existing=True)
    if rejection:
        return rejection
    sender = accepted["sender"]
    body = accepted["body"]
    t = accepted["target_agent"]
    msg = accepted["message"]

    expiry = f"{MAX_QUEUE_AGE // 3600 or 1}h"
    if not _live_owned_session(t):
        return True, (f"queued for '{target}' — its session isn't running yet; will "
                      f"deliver when it comes up, or expire undelivered after {expiry}.")

    # Ordering guard: the flush above only preserves order when it actually DRAINED
    # the backlog. If the target was busy, older messages are still queued — direct-
    # delivering this one the instant the pane frees up would jump the queue, so
    # leave it queued too and let the flusher deliver strictly oldest-first.
    if msg:
        try:
            backlog = gs.list_messages(status="queued", target=target, limit=2)
        except gs.GraphError as error:
            return True, (
                f"queued for '{target}' — Crew could not verify the older queue "
                f"order ({error}); no pane input was attempted, and a later "
                "flusher will retry safely.")
        if any(q.get("_guid") != msg["_guid"] for q in backlog):
            return True, (f"queued for '{target}' behind older queued messages; will "
                          f"be delivered in order as soon as it's idle, but expires "
                          f"undelivered after {expiry} if the target stays busy "
                          f"(you'll get a bounce notice).")

    pane, runtime_key = _pane_for_agent(t)
    if not pane:
        return True, (
            f"queued for '{target}' — its {runtime_key} runtime isn't running; "
            f"will deliver after it starts, or expire undelivered after {expiry}.")
    resolved_pane = {"value": pane}

    def _resolve_delivery_pane():
        # Runtime restarts update the durable agent row and may replace its pane
        # after the optimistic availability check above. Re-read the immutable
        # target and resolve its owned pane only once the typing lock is held.
        current_target = gs.get_object(msg["target_guid"])
        current_pane, current_runtime = _pane_for_agent(current_target)
        resolved_pane["value"] = current_pane
        return current_pane, current_runtime

    def _delivery_text():
        # Materialize multiline inbox content only after `_claim_submitting`
        # runs under the typing lock.  This keeps every delivery side effect on
        # the claimed side of the durable state boundary.
        return _format(
            sender,
            _deliverable(t, sender, body, msg.get("created_at")),
            no_prefix)
    def _claim_submitting():
        # Claim the durable work while the per-target lock is held and before
        # the first external command.  Queue scanners never retry claimed rows.
        gs.mark_message(msg["_guid"], "submitting", detail="")
        current = gs.get_object(msg["_guid"])
        _, identity_error = _bound_target(current)
        identity_error = identity_error or _sender_identity_error(current)
        if identity_error:
            gs.mark_message(
                msg["_guid"], "failed", detail=identity_error)
            raise _DeliveryIdentityError(identity_error)
    submission_state = {"outcome": None}

    def _record_submission(outcome):
        # Runs inside the typing lock.  Failure outcomes are durable too: only a
        # proven pre-launch failure returns to queued; anything that may have
        # touched the pane is terminally uncertain and is never auto-retried.
        submission_state["outcome"] = outcome
        if outcome in ("delivered", "runtime_queued"):
            gs.mark_message(
                msg["_guid"], outcome, delivered=True, detail="")
        elif outcome == "delivery_uncertain":
            gs.mark_message(
                msg["_guid"], "delivery_uncertain",
                detail=("tmux input may have acted, but submission was not "
                        "confirmed; automatic retry is disabled"))
        else:
            gs.mark_message(
                msg["_guid"], "queued",
                detail="tmux did not start; no input was sent and retry is safe")
    try:
        delivered = _deliver_when_ready(
            pane, _delivery_text, READY_WAIT_SECS, msg["target_guid"],
            runtime_key=runtime_key, on_claim=_claim_submitting,
            on_result=_record_submission,
            pane_resolver=_resolve_delivery_pane)
    except _DeliveryIdentityError as error:
        return False, (
            f"delivery refused after identity revalidation: {error}; no tmux "
            "input was sent.")
    except gs.GraphError as error:
        if submission_state["outcome"] is not None:
            # The row was already claimed and an external attempt occurred.
            # Best-effort make that ambiguity explicit; if persistence is still
            # unavailable it remains `submitting`, which queue scanners also
            # never retry.
            try:
                gs.mark_message(
                    msg["_guid"], "delivery_uncertain",
                    detail=("tmux submission completed, but the final status "
                            f"write failed: {error}"))
            except gs.GraphError:
                pass
            return False, (
                f"delivery outcome is uncertain for '{target}' ({error}); "
                "Crew will not retry this message automatically.")
        # No tmux command ran, so retry is safe only after we durably prove the
        # row is back in `queued`.  A failed claim PATCH can have an unknown
        # server-side outcome; never promise retryability from the exception
        # alone.
        try:
            gs.mark_message(
                msg["_guid"], "queued",
                detail=f"submission did not start: {error}")
        except gs.GraphError as rollback_error:
            return False, (
                f"submission did not start for '{target}', but Crew could not "
                f"confirm the durable queue state ({rollback_error}); inspect "
                "the message before retrying.")
        return True, (
            f"queued for '{target}' — submission did not start ({error}); "
            "the durable row was rolled back and a later flusher may retry it.")
    except (subprocess.SubprocessError, OSError) as e:
        if os.environ.get("CLAUDE_CODE_SANDBOXED"):
            return False, _sandbox_hint()
        return False, f"delivery failed: {e}"

    pane = resolved_pane["value"] or pane
    if delivered == "runtime_queued":
        return True, f"queued in Codex for '{target}' next turn ({pane})"
    if delivered == "delivered":
        return True, f"delivered to '{target}' ({pane})"
    if delivered == "delivery_uncertain":
        return False, (
            f"delivery outcome is uncertain for '{target}'; Crew will not "
            "retry this message automatically.")
    if delivered == "not_started":
        return True, (
            f"queued for '{target}' — tmux could not be started, so no input "
            "was sent and a later flusher may retry it.")
    return True, (f"queued for '{target}' — it's busy right now; will be delivered as "
                  f"soon as it's idle, but expires undelivered after {expiry} if the "
                  f"target stays busy (you'll get a bounce notice).")


def _turn_cap(sender, target):
    """(max_turns, window_secs) for the edge that authorizes sender→target, or
    (0, _) if uncapped. Window is fixed at 1h — a simple, predictable budget."""
    edge = gs.authorizing_edge(sender, target)
    return (int(edge.get("max_turns") or 0) if edge else 0), 3600


def _budget_caps(sender, target):
    """(token_cap, cost_cap) for the edge that authorizes sender→target, or
    (0, 0.0) if unbudgeted. Mirrors _turn_cap's lookup: forward edges first, then
    undirected reverse edges (their caps bind too)."""
    edge = gs.authorizing_edge(sender, target)
    if not edge:
        return 0, 0.0
    return int(edge.get("token_cap") or 0), float(edge.get("cost_cap") or 0)


def _transform_edge(sender, target):
    """The edge (dict) that authorizes sender→target AND carries a nonempty
    `transform`, or None. Same first-edge convention as _turn_cap/_budget_caps:
    a forward edge wins if it has one; else an UNDIRECTED reverse edge's
    transform binds too. (If a forward edge exists but has no transform, we do
    NOT fall through to a reverse edge's transform — the forward edge is
    already the sole authorizing edge in that case, same as _turn_cap.)"""
    edge = gs.authorizing_edge(sender, target)
    return edge if edge and (edge.get("transform") or "").strip() else None


def _open_transform_fd(path):
    """Open one regular transform beneath the trusted root without symlinks.

    Every directory component is walked from a pinned root descriptor and the
    final file uses O_NOFOLLOW.  The returned descriptor therefore remains the
    selected inode even if an attacker renames the pathname immediately after
    this function returns.
    """
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    if not nofollow or not directory:
        raise OSError("secure transform opening is unavailable")
    root = os.path.realpath(os.path.expanduser(config.TRANSFORMS_DIR))
    candidate = os.path.abspath(os.path.expanduser(str(path)))
    try:
        contained = os.path.commonpath((root, candidate)) == root
    except ValueError:
        contained = False
    if not contained or candidate == root:
        raise OSError("transform is outside the trusted root")

    dir_flags = os.O_RDONLY | directory | nofollow
    dir_flags |= getattr(os, "O_CLOEXEC", 0)
    current_fd = os.open(os.sep, dir_flags)
    try:
        root_parts = [part for part in root.split(os.sep) if part]
        relative_parts = os.path.relpath(candidate, root).split(os.sep)
        if not relative_parts or any(
                part in ("", ".", os.pardir) for part in relative_parts):
            raise OSError("invalid transform path")
        for component in root_parts + relative_parts[:-1]:
            next_fd = os.open(component, dir_flags, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        file_flags = os.O_RDONLY | nofollow | getattr(os, "O_NONBLOCK", 0)
        file_flags |= getattr(os, "O_CLOEXEC", 0)
        file_fd = os.open(
            relative_parts[-1], file_flags, dir_fd=current_fd)
        try:
            if not stat.S_ISREG(os.fstat(file_fd).st_mode):
                raise OSError("transform is not a regular file")
            return file_fd
        except Exception:
            os.close(file_fd)
            raise
    finally:
        os.close(current_fd)


def _snapshot_transform(path):
    """Return an anonymous, stable snapshot of a securely opened transform."""
    source_fd = _open_transform_fd(path)
    try:
        before = os.fstat(source_fd)
        chunks = []
        while True:
            chunk = os.read(source_fd, 64 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(source_fd)
        fingerprint = lambda info: (
            info.st_dev, info.st_ino, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)
        data = b"".join(chunks)
        if fingerprint(before) != fingerprint(after) or len(data) != before.st_size:
            raise OSError("transform changed while it was being read")
    finally:
        os.close(source_fd)

    snapshot = tempfile.TemporaryFile(prefix="crew-transform-")
    try:
        snapshot.write(data)
        snapshot.flush()
        snapshot.seek(0)
        return snapshot
    except Exception:
        snapshot.close()
        raise


def _run_transform(path, body, sender, target, label):
    """Run an edge transform during one acceptance attempt.

    Queue flush never re-runs transforms, and a durable request-ID row lets an
    HTTP retry reuse the prior result. A process crash after this call but
    before persistence can repeat it, so transform scripts must make external
    side effects idempotent. CREW_SENDER/CREW_TARGET/CREW_EDGE_LABEL are added
    to its environment.

    Returns (ok, result, short_reason):
      * exit 0 + nonempty stdout  -> (True, <decoded stripped stdout>, None)
      * exit 0 + empty stdout, nonzero exit, or a timeout -> (False, <public
        reason string for the sender>, <bare reason for the operator notify>)
    """
    env = dict(os.environ)
    env["CREW_SENDER"] = sender
    env["CREW_TARGET"] = target
    env["CREW_EDGE_LABEL"] = label or ""
    script = _sanitize(os.path.basename(str(path or ""))) or "transform"
    snapshot = None
    try:
        snapshot = _snapshot_transform(path)
    except (OSError, TypeError, ValueError):
        reason = "unavailable"
        return False, f"transform {script} {reason}", reason
    try:
        fd = snapshot.fileno()
        proc = subprocess.run(
            [sys.executable, f"/dev/fd/{fd}"],
            input=body.encode("utf-8"), capture_output=True,
            timeout=config.TRANSFORM_TIMEOUT, env=env, pass_fds=(fd,))
    except subprocess.TimeoutExpired:
        reason = f"timed out after {config.TRANSFORM_TIMEOUT:g}s"
        return False, f"transform {reason}", reason
    except (OSError, TypeError, ValueError):
        reason = "unavailable"
        return False, f"transform {script} {reason}", reason
    finally:
        snapshot.close()
    out = (proc.stdout or b"").decode("utf-8", errors="replace").strip()
    if proc.returncode == 0 and out:
        return True, out, None
    if proc.returncode == 0:
        reason = "empty output"
    else:
        # stderr is transform-controlled and may contain secrets, terminal
        # controls, absolute paths, or tracebacks. Keep it captured for process
        # hygiene but never echo it into sender/operator-facing surfaces.
        reason = f"exit {proc.returncode}"
    return False, f"filtered by {script}: {reason}", reason


def say_to_agent(name, text, *, actor):
    """Human operator → agent, bypassing peer edges but not caller identity.

    ``actor`` is mandatory so every entry point must propagate its independently
    resolved caller.  Only the literal operator actor (``human``) may use this
    path; managed agents must use :func:`deliver`, where graph edges and budgets
    are enforced.  Delivery remains readiness-gated so we never fire Enter blind.
    Returns ``(ok, message)``.
    """
    if actor != "human":
        reason = ("operator kickoff is human-only; agents must use `crew message "
                  "<target> <text>` over an authorized edge")
        _log_refusal(actor or "unknown", name, text or "", "blocked")
        return False, reason
    text = _sanitize(text)
    if not text:
        return False, "empty message"
    a = gs.get_agent_by_name(name)
    if not a:
        return False, f"no agent named '{name}'"
    if not _live_owned_session(a):
        return False, f"'{name}' has no running session"
    # Drain the agent's queued backlog first (same self-serve flush as deliver) so
    # a kickoff from a headless CLI also delivers waiting mail, oldest first.
    try:
        flush_queued(target=name)
    except gs.GraphError:
        pass
    pane, runtime_key = _pane_for_agent(a)
    if not pane:
        return False, (
            f"'{name}' has a tmux session but no running {runtime_key} runtime — "
            "start its runtime before sending a kickoff")
    body = _limit_wire(f"[crew · from you] {text}")

    def _resolve_kickoff_pane():
        try:
            current = gs.get_object(a["_guid"])
        except gs.GraphError:
            return None, runtime_key
        return _pane_for_agent(current)

    try:
        ok = _deliver_when_ready(
            pane, body, READY_WAIT_SECS, a["_guid"],
            runtime_key=runtime_key, pane_resolver=_resolve_kickoff_pane)
    except (subprocess.SubprocessError, OSError) as e:
        return False, f"send failed: {e}"
    if ok in ("delivered", "runtime_queued"):
        return True, f"sent to '{name}'"
    if ok == "delivery_uncertain":
        return False, (
            f"send outcome for '{name}' is uncertain; inspect the target "
            "before trying again")
    return False, f"'{name}' is busy — try again in a moment"


def _queued_row_error(message):
    """Return a safe reason when a queued-row snapshot is not processable.

    Queue data is durable input, not trusted Python state.  Validate the fields
    that feed ordering, identity lookup, formatting, and inbox paths before one
    malformed row can raise out of the whole flush pass.
    """
    if not isinstance(message, dict):
        return "corrupt queued message: row is not an object"
    for field in ("_guid", "sender", "target", "body"):
        value = message.get(field)
        if not isinstance(value, str) or (field != "body" and not value.strip()):
            return f"corrupt queued message: invalid {field}"
    created_at = message.get("created_at")
    if (isinstance(created_at, bool)
            or not isinstance(created_at, (int, float))
            or not math.isfinite(created_at)
            or created_at < 0
            or created_at > 253402300799):
        return "corrupt queued message: invalid created_at"
    if message.get("status") != "queued":
        return "corrupt queued message: invalid status"
    if "no_prefix" in message and not isinstance(message.get("no_prefix"), bool):
        return "corrupt queued message: invalid no_prefix"
    for field in ("sender_guid", "target_guid", "edge_guid"):
        # MorphDB returns absent nullable relation fields as None on legacy
        # rows.  Let the identity-binding checks below classify those rows with
        # their precise legacy failure instead of mislabeling them as corrupt.
        if message.get(field) is not None and not isinstance(message.get(field), str):
            return f"corrupt queued message: invalid {field}"
    return ""


def _queued_target_lock_identity(message):
    """Return the stable serialization key available on a queue snapshot.

    Modern rows bind directly to an immutable target GUID. Legacy/corrupt rows
    may not have that relation, but still need a stable quarantine lock so two
    flushers cannot race their terminal status update.
    """
    if not isinstance(message, dict):
        return ""
    target_guid = message.get("target_guid")
    if isinstance(target_guid, str) and target_guid.strip():
        return target_guid.strip()
    target = message.get("target")
    if isinstance(target, str) and target.strip():
        return f"legacy-target:{target.strip()}"
    guid = message.get("_guid")
    if isinstance(guid, str) and guid.strip():
        return f"corrupt-message:{guid.strip()}"
    return ""


def _mark_terminal(message, detail):
    """Persist one terminal queue decision before exposing any side effect.

    ``mark_message`` reconciles a PATCH response lost after commit. False means
    the row may still be queued, so callers must neither bounce nor notify and
    must block later mail for the same target during this pass.
    """
    try:
        gs.mark_message(message["_guid"], "failed", detail=detail)
    except gs.GraphError:
        return False
    return True


def _queued_snapshot(limit, target):
    """Read a stable paged snapshot before mutating any queue statuses.

    ``limit`` remains the useful-work budget for a flush pass, but cannot also
    be the scan horizon: fifty unavailable rows for one target must not starve a
    healthy target in row 51 forever.
    """
    try:
        page_size = max(1, int(limit))
    except (TypeError, ValueError, OverflowError):
        page_size = 50
    rows = []
    offset = 0
    while True:
        page = gs.list_messages(
            status="queued", target=target, limit=page_size, offset=offset)
        rows.extend(page)
        if len(page) < page_size:
            return rows
        offset += len(page)


def flush_queued(limit=50, target=None):
    """Deliver queued messages whose target is now idle (all targets, or just
    `target`). Called periodically by the dashboard server AND inline at the start
    of deliver()/say_to_agent(), so queues drain even with no dashboard running.
    Messages older than MAX_QUEUE_AGE are expired to `failed` and their sender's
    pane gets a best-effort bounce notice (_bounce). Returns the count delivered.

    WAVE 5: never re-runs a message's transform — acceptance already persisted
    the queued row's final `body`; this function just types it out verbatim."""
    delivered = 0
    now = int(time.time())
    # Notices are batched by truthful event family after the loop.  A slow
    # webhook can therefore cost at most one call for expiries and one for other
    # terminal failures, rather than one timeout per queued row.
    expired = []
    expired_bounces = []
    failed = []
    # Strict FIFO is per immutable target, not global: a retryable head blocks
    # later mail to that target for this pass, while other agents keep draining.
    blocked_targets = set()
    handled = 0
    try:
        work_limit = max(1, int(limit))
    except (TypeError, ValueError, OverflowError):
        work_limit = 50
    for m in _queued_snapshot(work_limit, target):
        lock_identity = _queued_target_lock_identity(m)
        target_key = lock_identity
        if target_key in blocked_targets:
            continue
        if handled >= work_limit:
            break
        guid = m.get("_guid") if isinstance(m, dict) else None
        if (not isinstance(guid, str) or not guid.strip()
                or not lock_identity):
            continue
        lock = _acquire_lock(lock_identity)
        if not lock:
            blocked_targets.add(target_key)
            continue  # another process is typing into this target — keep queued
        try:
            # re-vet under the lock: our queued-list snapshot predates it, and a
            # concurrent deliver()/flusher may have typed this message meanwhile
            try:
                cur = gs.get_object(m["_guid"])
            except gs.GraphError as error:
                if not str(error).lstrip().startswith("404:"):
                    blocked_targets.add(target_key)
                continue
            if (not isinstance(cur, dict)
                    or cur.get("_guid") != guid
                    or cur.get("status") != "queued"):
                continue
            # Relation fields are immutable through Crew, but treat MorphDB
            # snapshots as untrusted. Never mutate under a lock derived from a
            # stale/different target binding.
            if _queued_target_lock_identity(cur) != lock_identity:
                blocked_targets.add(target_key)
                continue
            row_error = _queued_row_error(cur)
            if row_error:
                if _mark_terminal(cur, row_error):
                    handled += 1
                    failed.append((
                        cur.get("sender") or "unknown",
                        f"queued message failed validation: {row_error}"))
                else:
                    blocked_targets.add(target_key)
                continue
            if now - int(cur["created_at"]) > MAX_QUEUE_AGE:
                if _mark_terminal(
                        cur, "queued message expired before delivery"):
                    handled += 1
                    expired_bounces.append(cur)
                    # Both bounce and webhook run only after this durable
                    # transition and after the target lock is released.
                    expired.append((
                        cur.get("sender") or "unknown",
                        f'message to {cur.get("target")} expired undelivered: '
                        f'{_clip(_sanitize(cur.get("body") or ""), 60)}'))
                else:
                    blocked_targets.add(target_key)
                continue
            tname = cur.get("target")
            # Re-resolve under the typing lock.  An agent can be deleted after
            # the queue scan; the GUID, not a reusable name, must still identify
            # the pane owner at the exact submission boundary.
            try:
                t, identity_error = _bound_target(cur)
            except gs.GraphError:
                blocked_targets.add(target_key)
                continue
            if identity_error:
                if _mark_terminal(cur, identity_error):
                    handled += 1
                    failed.append((
                        cur.get("sender") or "unknown",
                        f"message to {tname} failed: {identity_error}"))
                else:
                    blocked_targets.add(target_key)
                continue
            try:
                sender_error = _sender_identity_error(cur)
            except gs.GraphError:
                blocked_targets.add(target_key)
                continue
            if sender_error:
                if _mark_terminal(cur, sender_error):
                    handled += 1
                    failed.append((
                        cur.get("sender") or "unknown",
                        f"message to {tname} failed: {sender_error}"))
                else:
                    blocked_targets.add(target_key)
                continue
            if not _live_owned_session(t):
                blocked_targets.add(target_key)
                continue  # session not up yet — keep queued
            # Resolve only inside the target lock. A restart between queue
            # discovery and this boundary must route to the replacement pane,
            # never to a cached pane id.
            pane, runtime_key = _pane_for_agent(t)
            if not pane:
                blocked_targets.add(target_key)
                continue  # shell-only session — never execute message text there
            sender = cur.get("sender") or "crew"
            state = _runtime_state(pane, runtime_key)
            submit_key = ("Tab" if runtime_key == "codex" and state == "working"
                          else "Enter")
            if runtime_key == "custom" and state == "unknown":
                blocked_targets.add(target_key)
                continue
            if submit_key == "Enter" and not _pane_ready(pane, runtime_key):
                blocked_targets.add(target_key)
                continue  # still busy / needs input — keep queued
            try:
                # Claim while the target lock is held, immediately before the
                # first external side effect.  Queue scans select only `queued`,
                # so a crashed or indeterminate claimant is never duplicated.
                gs.mark_message(cur["_guid"], "submitting", detail="")
            except gs.GraphError:
                blocked_targets.add(target_key)
                continue
            handled += 1
            # Materialize multiline inbox content only after the durable claim.
            # The log keeps the full body; the pane receives this stable pointer.
            text = _format(
                sender,
                _deliverable(t, sender, cur.get("body") or "",
                             cur.get("created_at")),
                bool(cur.get("no_prefix", False)))
            outcome = _type_into_pane(
                pane, text, runtime_key=runtime_key, submit_key=submit_key)
            if outcome:
                final_status = ("runtime_queued" if submit_key == "Tab"
                                else "delivered")
                try:
                    gs.mark_message(
                        cur["_guid"], final_status, delivered=True, detail="")
                except gs.GraphError:
                    # External acceptance succeeded but durable finalization
                    # did not.  Keep it terminal; never put it back in queue.
                    try:
                        gs.mark_message(
                            cur["_guid"], "delivery_uncertain",
                            detail=("tmux submission completed, but the final "
                                    "status write failed"))
                    except gs.GraphError:
                        pass  # remains `submitting`, also non-retryable
                else:
                    delivered += 1
            elif outcome == _DELIVERY_UNCERTAIN:
                blocked_targets.add(target_key)
                try:
                    gs.mark_message(
                        cur["_guid"], "delivery_uncertain",
                        detail=("tmux input may have acted, but submission was "
                                "not confirmed; automatic retry is disabled"))
                except gs.GraphError:
                    pass  # remains `submitting`, also non-retryable
            else:
                # An OS-level launch failure proves no tmux command ran, so this
                # is the sole safe automatic retry path.
                blocked_targets.add(target_key)
                try:
                    gs.mark_message(
                        cur["_guid"], "queued",
                        detail=("tmux did not start; no input was sent and "
                                "retry is safe"))
                except gs.GraphError:
                    pass  # conservative: leave claimed rather than duplicate
        finally:
            _release_lock(lock)
    for message in expired_bounces:
        _bounce(message)
    if len(expired) == 1:
        notify("message_expired", expired[0][0], expired[0][1])
    elif expired:
        senders = ", ".join(sorted({s for s, _ in expired}))
        notify("message_expired", "crew",
               f"{len(expired)} queued messages expired (senders: {senders}); "
               f"see `crew mail --status failed`")
    if len(failed) == 1:
        notify("message_failed", failed[0][0], failed[0][1])
    elif failed:
        senders = ", ".join(sorted({s for s, _ in failed}))
        notify("message_failed", "crew",
               f"{len(failed)} queued messages failed (senders: {senders}); "
               f"see `crew mail --status failed`")
    return delivered
