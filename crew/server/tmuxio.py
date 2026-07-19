#!/usr/bin/env python3
"""tmuxio — tmux primitives + session→pane targeting for the crew dashboard.

This turns a dashboard "target" (a bare tmux session name like 'crew-worker-1')
into the EXACT pane running `claude`, and owns the few tmux shell-outs the live
stack still needs: process discovery, pane resolution, status detection, and the
readiness gate that messaging waits on before typing into a pane.

The terminal transport itself is a real `tmux attach` client in a PTY (see
ptyio) — xterm.js owns rendering — so the OLD scrape/render machinery
(capture_live / ansi_to_html / xterm256 / the shell-tab windows / send-keys
transport) is gone. `capture_frame` survives because `detect_status` still reads
a visible frame to infer worker state for the crew graph.

No third-party deps. Pure stdlib.
"""
import re
import shutil
import subprocess
import time

from .. import config, runtime as runtimes

# Self-locating tmux binary (same resolution the OLD dashboard used). Falls back
# to the common Homebrew path so a stripped PATH (e.g. a launchd context) still
# finds it.
TMUX = shutil.which("tmux") or "/opt/homebrew/bin/tmux"


def tmux(*args, timeout=5, endpoint=None):
    """Run a tmux command, decoding stdout as text. Returns (ok, output) where
    `output` is stdout on success or stderr on failure (handy for surfacing the
    tmux error straight back to the caller)."""
    try:
        endpoint = endpoint or config.tmux_target_endpoint(*args)
        out = subprocess.run(
            config.tmux_command(*args, endpoint=endpoint, executable=TMUX),
            capture_output=True, text=True, timeout=timeout,
            env=config.tmux_environment(endpoint=endpoint))
        return out.returncode == 0, (out.stdout if out.returncode == 0 else out.stderr)
    except Exception as e:
        return False, str(e)


def _tty_name(value):
    return (value or "").replace("/dev/", "")


def _parse_process_inventory(raw):
    """ps text -> tty -> process rows. Kept pure for deterministic tests."""
    by_tty = {}
    for line in (raw or "").splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 2:
            continue
        tty = _tty_name(parts[0])
        if tty in ("??", "?", ""):
            continue
        comm = parts[1]
        command = parts[2] if len(parts) > 2 else comm
        by_tty.setdefault(tty, []).append({"comm": comm, "command": command})
    return by_tty


def process_inventory():
    """All controlling-terminal processes, collected in one ps pass."""
    try:
        out = subprocess.run(["ps", "-axo", "tty=,comm=,command="],
                             capture_output=True, text=True, timeout=5)
        return _parse_process_inventory(out.stdout)
    except Exception:
        return {}


def claude_ttys():
    """Backward-compatible view of ttys running Claude Code."""
    return {
        tty for tty, rows in process_inventory().items()
        if any(runtimes.process_matches("claude", p["comm"], p["command"])
               for p in rows)
    }


# Spinner glyphs Claude Code cycles through while generating. v2.x rotates a set
# much wider than any single capture shows (✻✶✢✽✳✦✧∗·◐◓◑◒ …), and the word after
# it isn't always an "-ing" gerund ("Booping…", "Churned…"), and the hint is no
# longer always "esc to interrupt" (now "(2s · thinking with high effort)"). So we
# match on the STABLE shape — a spinner glyph + a word ending in the … ellipsis, or
# an elapsed-time status — rather than a fixed word list. The delivery gate
# (pane_ready) additionally compares two frames over time, which catches generation
# regardless of glyph/word, so this only needs to be good enough for the status dot.
_SPINNERS = "✻✶✢✽✳✦✧∗·◐◓◑◒◇✦"


def detect_status(text, runtime_key="claude"):
    """Infer the configured runtime's state from its visible screen.

    Order matters: 'working' is the strongest signal, so check it first.
    'needs_input' only fires on the STRUCTURED permission UI — a numbered selection
    menu — not on prose like 'what do you want done?'.
    """
    low = (text or "").lower()

    # 1. working — the interrupt hint, an elapsed-time status line "(Ns · …)", or a
    #    spinner glyph followed by a word + the … ellipsis ("✽ Booping…").
    if ("esc to interrupt" in low
            or re.search(r"\(\d+s\s*·", text)
            or re.search(r"(?m)^[ \t]*[" + _SPINNERS + r"]\s+\S+…", text)):
        return "working"

    # 2. needs_input — the permission/选择 dialog renders a numbered menu with a
    #    selection arrow on the active row. Require the arrow + a numbered option,
    #    not just the words "do you want" (which appears in normal Claude prose).
    # only ❯ is Claude's selection arrow; plain '>' is a markdown blockquote
    has_menu = re.search(r"^\s*❯\s*\d+\.\s", text, re.M) is not None
    # The prose phrase alone is not enough: agents commonly ask an ordinary
    # question such as "do you want to proceed with this plan?". The stable UI
    # boundary is the numbered selection cursor.
    if has_menu:
        return "needs_input"

    if runtime_key == "claude":
        return "idle"
    if runtime_key == "codex":
        # Codex's stable idle affordance is its › composer. Stay conservative:
        # an unrecognized frame is unknown, never a false idle signal.
        if re.search(r"(?m)^\s*›\s*", text) or "enter to send" in low:
            return "idle"
        return "unknown"
    return "unknown"


def list_claude_panes(endpoint=config.TMUX_ENDPOINT_CREW):
    """(session, pane_id) for every pane with a claude process on its tty. Listed in
    tmux's natural -a order (session→window→pane ascending) so the first row per
    session is its lowest-index claude pane — which is what _session_pane_map picks."""
    ok, raw = tmux(
        "list-panes", "-a", "-F",
        "#{session_name}\t#{pane_id}\t#{pane_tty}", endpoint=endpoint)
    if not ok:
        return []
    ctty = claude_ttys()
    panes = []
    for line in raw.strip().splitlines():
        p = line.split("\t")
        if len(p) < 3:
            continue
        sess, pane_id, pane_tty = p[:3]
        if pane_tty.replace("/dev/", "") in ctty:
            panes.append({
                "session": config.tmux_target(sess, endpoint),
                "pane_id": config.tmux_target(pane_id, endpoint),
            })
    return panes


def _list_tmux_panes(session=None, endpoint=None):
    # Prefix matching is useful interactively but unsafe for managed routing: a
    # stale stored session "crew-a" must never resolve to somebody's "crew-abc".
    endpoint = endpoint or config.tmux_target_endpoint(session)
    args = (["list-panes", "-s", "-t", f"={session}"] if session else
            ["list-panes", "-a"])
    ok, raw = tmux(
        *args, "-F", "#{session_name}\t#{pane_id}\t#{pane_tty}",
        endpoint=endpoint)
    if not ok:
        return []
    out = []
    for line in raw.strip().splitlines():
        parts = line.split("\t")
        if len(parts) >= 3:
            out.append({"session": config.tmux_target(parts[0], endpoint),
                        "pane_id": config.tmux_target(parts[1], endpoint),
                        "tty": _tty_name(parts[2])})
    return out


def _match_runtime_panes(panes, processes, agents=None):
    """Select one matching runtime pane per configured agent session."""
    by_session = {}
    for agent in agents or []:
        session = agent.get("session") or agent.get("name")
        if session:
            by_session[session] = agent
    found = {}
    for pane in panes:
        session = pane.get("session")
        if not session or session in found:
            continue
        rows = processes.get(_tty_name(pane.get("tty")), [])
        agent = by_session.get(session)
        if agent is not None:
            key = runtimes.resolve_agent_runtime(agent)
            launch = agent.get("launch_cmd")
            matches = any(runtimes.process_matches(
                key, row.get("comm"), row.get("command"), launch) for row in rows)
        elif agents is not None:
            matches = False
        else:
            matches = any(
                runtimes.process_matches(key, row.get("comm"), row.get("command"))
                for key in ("claude", "codex") for row in rows)
        if matches:
            found[session] = pane.get("pane_id")
    return found


def list_runtime_panes(agents=None, endpoint=config.TMUX_ENDPOINT_CREW):
    """Runtime panes for Crew agents, using one tmux and one ps inventory pass."""
    return _match_runtime_panes(
        _list_tmux_panes(endpoint=endpoint), process_inventory(), agents)


# session-name → claude pane_id map, cached briefly. list_claude_panes() shells
# out to `ps -axo` (~180ms!), so resolving it fresh on every graph-snapshot poll
# would be heavy. The map only changes when a worker's claude restarts, so a short
# TTL is safe and keeps the poll cheap.
_PANE_CACHE = {"at": 0.0, "map": {}, "key": None}
_PANE_TTL = 3.0  # seconds


def _session_pane_map(force=False, agents=None):
    now = time.monotonic()
    cache_key = None if agents is None else tuple(sorted(
        ((a.get("session") or a.get("name") or ""),
         runtimes.resolve_agent_runtime(a), a.get("launch_cmd") or "")
        for a in agents))
    if (not force and _PANE_CACHE.get("key") == cache_key
            and (now - _PANE_CACHE["at"]) < _PANE_TTL):
        return _PANE_CACHE["map"]
    m = list_runtime_panes(agents)
    _PANE_CACHE["map"] = m
    _PANE_CACHE["at"] = now
    _PANE_CACHE["key"] = cache_key
    return m


def agent_inventory_key(agent):
    """Stable per-row key; never a tmux session name."""
    return ((agent or {}).get("_guid") or (agent or {}).get("name") or "")


def live_agent_inventory(agents):
    """Exact endpoint-tagged live session/pane state for current agent rows.

    The dictionary is keyed by durable agent identity, not session text, so a
    same-named Crew/default-server pair can never collapse or cross-route.
    """
    result = {
        agent_inventory_key(agent): {"session": None, "pane": None}
        for agent in agents or []
    }
    owned = []
    for agent in agents or []:
        session = owned_agent_session(agent)
        if session is not None:
            owned.append((agent, session))
            result[agent_inventory_key(agent)]["session"] = session
    if not owned:
        return result

    processes = process_inventory()
    for endpoint in (config.TMUX_ENDPOINT_CREW, config.TMUX_ENDPOINT_LEGACY):
        group = []
        for agent, session in owned:
            if config.tmux_target_endpoint(session) != endpoint:
                continue
            routed = dict(agent)
            routed["session"] = session
            group.append(routed)
        if not group:
            continue
        matched = _match_runtime_panes(
            _list_tmux_panes(endpoint=endpoint), processes, group)
        for routed in group:
            session = routed["session"]
            pane = matched.get(session)
            if pane is not None:
                result[agent_inventory_key(routed)]["pane"] = config.tmux_target(
                    pane, endpoint)
    return result


def runtime_pane(session, runtime_key, launch_cmd=None, fallback=True):
    """Pane actually running one configured runtime inside a session."""
    if not session:
        return None
    endpoint = config.tmux_target_endpoint(session)
    exists, _ = tmux(
        "has-session", "-t", f"={session}", endpoint=endpoint)
    if not exists:
        return None
    agent = {"name": session, "session": session, "runtime": runtime_key,
             "launch_cmd": launch_cmd or ""}
    panes = _list_tmux_panes(session)
    matched = _match_runtime_panes(panes, process_inventory(), [agent])
    if session in matched:
        return matched[session]
    if not fallback:
        return None
    # A no-launch session intentionally has only a shell. Prefer the runtime's
    # window, then Claude's legacy window, then tmux's active pane.
    for window in (runtimes.window_name(runtime_key), "claude"):
        ok, pane = tmux(
            "list-panes", "-t", f"={session}:{window}", "-F", "#{pane_id}",
            endpoint=endpoint)
        if ok and pane.strip():
            return config.tmux_target(
                pane.strip().splitlines()[0], endpoint)
    return config.tmux_target(session, endpoint)


def _claude_wrapper_frame(frame):
    """Whether a foreground Python/Node process is visibly Claude Code.

    Some operator shell wrappers launch Claude through Python, so tmux reports
    ``Python`` instead of ``claude``.  Do not trust that generic process name by
    itself: require both Claude's composer and its permission-mode footer.  A
    returned shell or an arbitrary Python REPL therefore remains unavailable.
    """
    text = frame or ""
    composer = re.search(r"(?m)^\s*❯(?:[\t \u00a0]|$)", text) is not None
    low = text.lower()
    footer = "shift+tab to cycle" in low and "permission" in low
    return composer and footer


def stored_runtime_pane(agent, session):
    """Prove the configured runtime in an exact ownership-bound stored pane.

    Agent sandboxes can intentionally hide peer processes from ``ps -axo``.
    ``runtime_pane`` then cannot discover a healthy target even though the tmux
    server can still report its foreground command.  This fallback is narrower:
    callers must first supply the already verified owned session, and only the
    durable row's exact pane may pass.  It never falls back to a shell or another
    split pane.
    """
    if not isinstance(agent, dict) or not session:
        return None
    pane = agent.get("pane")
    if not isinstance(pane, str) or not pane.startswith("%"):
        return None
    try:
        endpoint = config.tmux_target_endpoint(session)
        target = config.tmux_target(pane, endpoint)
    except OSError:
        return None
    ok, raw = tmux(
        "display-message", "-p", "-t", target,
        "#{session_name}\t#{pane_id}\t#{pane_current_command}")
    if not ok:
        return None
    parts = (raw or "").rstrip("\n").split("\t")
    if (len(parts) != 3 or parts[0] != str(session)
            or parts[1] != pane):
        return None
    runtime_key = runtimes.resolve_agent_runtime(agent)
    launch_cmd = agent.get("launch_cmd") or ""
    foreground = parts[2].strip()
    if runtimes.process_matches(
            runtime_key, foreground, foreground, launch_cmd):
        return target
    # The supported Claude wrapper is generic Python/Node at the process layer.
    # Pair it with a strict live UI signature; never treat a generic interpreter
    # alone as a message input pane.
    wrapper = foreground.rsplit("/", 1)[-1].lower()
    wrapper_like = (
        wrapper in {"node", "claude_wrapper"}
        or re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", wrapper) is not None)
    if (runtime_key == "claude"
            and runtimes.command_executable(launch_cmd) == "claude"
            and wrapper_like
            and _claude_wrapper_frame(capture_frame(target))):
        return target
    return None


def claude_pane(session):
    """The pane_id INSIDE <session> that is actually running claude — robust to the
    user splitting the claude window (Ctrl-b %/") or adding panes. We search EVERY
    pane in the session (`-s`) and pick the one whose tty has a claude process, so
    a crew message always lands in claude's prompt, never a stray shell split.

    Fallbacks, in order: the `claude`-named window's first pane, then the bare
    session name (tmux's active pane) so delivery still attempts something sane."""
    return runtime_pane(session, "claude")


def session_names(endpoint=config.TMUX_ENDPOINT_CREW):
    ok, raw = tmux(
        "list-sessions", "-F", "#{session_name}", endpoint=endpoint)
    return ({config.tmux_target(name, endpoint)
             for name in raw.strip().splitlines()} if ok else set())


def canonical_agent_session(agent, project=None):
    """Canonical stored session identity, without trusting live tmux state."""
    name = agent.get("name") if isinstance(agent, dict) else None
    if not isinstance(name, str) or not config.valid_agent_name(name):
        return None
    try:
        project = config.current_project() if project is None else project
        canonical = config.session_name(project, name)
    except (TypeError, ValueError):
        return None
    stored = agent.get("session")
    if stored not in (None, "") and stored != canonical:
        return None
    return canonical


def _expected_ownership(agent, project=None):
    try:
        expected_project = (
            config.current_project() if project is None else project)
        return {
            "CREW_PROJECT": expected_project,
            "CREW_AGENT": agent["name"],
            "CREW_APP": config.current_app(),
            "MORPHDB_HOST": config.MORPHDB_HOST,
        }
    except (KeyError, TypeError, ValueError):
        return None


def inspect_agent_session(agent, endpoint, project=None, live_session=None,
                          allow_renamed=False):
    """Return one exact owned live target, or ``None``.

    Ownership is one shared boundary for lifecycle, status, mail, identity, and
    dashboard PTY: canonical durable name, four pinned routing markers, and the
    exact stored pane bound uniquely to that live session. Additional split
    panes are allowed; the stored pane itself must occur exactly once.
    """
    canonical = canonical_agent_session(agent, project=project)
    expected = _expected_ownership(agent, project=project)
    pane = agent.get("pane") if isinstance(agent, dict) else None
    if (not canonical or expected is None or not isinstance(pane, str)
            or not pane.startswith("%")):
        return None
    name = str(live_session) if live_session is not None else canonical
    if not allow_renamed and name != canonical:
        return None
    session_target = config.tmux_target(name, endpoint)
    exact = config.tmux_target(f"={name}", endpoint)
    exists, _ = tmux("has-session", "-t", exact)
    if not exists:
        return None
    ok, raw = tmux("show-environment", "-t", exact)
    if not ok:
        return None
    actual = {}
    for line in (raw or "").splitlines():
        if not line or line.startswith("-") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        actual[key] = value
    if any(actual.get(key) != str(value)
           for key, value in expected.items()):
        return None

    pane_target = config.tmux_target(pane, endpoint)
    ok, actual_context = tmux(
        "display-message", "-p", "-t", pane_target,
        "#{session_name}\t#{session_group_list}")
    context_parts = (actual_context or "").strip().split("\t", 1)
    actual_session = context_parts[0] if context_parts else ""
    grouped_sessions = {
        value for value in (context_parts[1].split(",")
                            if len(context_parts) == 2 else [])
        if value
    }
    # A dashboard PTY uses a grouped view so it can select/resize a shared
    # window without moving a user's real client. tmux then reports the newest
    # view as #{session_name} for the shared pane even though list-panes on the
    # exact durable base still contains that same globally unique pane id.
    # Accept only when tmux explicitly lists the canonical base in the pane's
    # session group; an unrelated session or group remains rejected.
    if (not ok or (actual_session != name
                   and name not in grouped_sessions)):
        return None
    ok, raw_panes = tmux(
        "list-panes", "-s", "-t", exact, "-F", "#{pane_id}")
    if not ok:
        return None
    panes = [line.strip() for line in (raw_panes or "").splitlines()
             if line.strip()]
    if panes.count(pane) != 1:
        return None
    return session_target


def agent_owns_live_target(agent, session, pane, project=None):
    """Exact controlling-pane verifier, allowing a tmux session rename."""
    if not isinstance(pane, str) or agent.get("pane") != str(pane):
        return False
    try:
        endpoint = config.tmux_target_endpoint(session, pane)
    except OSError:
        return False
    owned = inspect_agent_session(
        agent, endpoint, project=project, live_session=str(session),
        allow_renamed=True)
    return owned is not None


def owned_agent_session(agent, sessions=None, project=None):
    """Return the exact live session only when its pinned Crew identity matches.

    A durable row reserves a canonical name, but does not prove the current tmux
    session with that name is still Crew's.  Validate all routing dimensions
    before status, PTY, or mail code treats the live session as authoritative.
    """
    session = canonical_agent_session(agent, project=project)
    if not session:
        return None
    if sessions is None:
        endpoints = (config.TMUX_ENDPOINT_CREW, config.TMUX_ENDPOINT_LEGACY)
    else:
        matching = [
            value for value in sessions
            if str(value) == session
        ]
        if not matching:
            return None
        carried = {
            value.endpoint for value in matching
            if isinstance(value, config.TmuxTarget)
        }
        endpoints = tuple(carried or {config.TMUX_ENDPOINT_CREW})

    owned = []
    for endpoint in endpoints:
        target = inspect_agent_session(agent, endpoint, project=project)
        if target is not None:
            owned.append(target)
    # Two servers claiming the same durable identity is ambiguous. Never pick
    # one silently: lifecycle migration must resolve the duplicate explicitly.
    return owned[0] if len(owned) == 1 else None


_LIVE_UNSET = object()


def agent_snapshot_fields(agent, sessions=None, pane_map=None, capture=None,
                          verify_ownership=False, live=_LIVE_UNSET):
    """Normalized liveness/status fields shared by CLI and dashboard."""
    capture = capture or capture_frame
    runtime_key = runtimes.resolve_agent_runtime(agent)
    if live is not _LIVE_UNSET:
        session = (live or {}).get("session")
        pane = (live or {}).get("pane")
        session_alive = bool(session)
    else:
        sessions = sessions or set()
        pane_map = pane_map or {}
        session = agent.get("session") or agent.get("name")
        if verify_ownership:
            session = owned_agent_session(agent, sessions=sessions)
        session_alive = bool(session and session in sessions)
        pane = pane_map.get(session) if session_alive else None
    runtime_alive = bool(pane)
    if runtime_alive:
        live_status = detect_status(capture(pane), runtime_key)
    elif session_alive and agent.get("status") == "not_started":
        live_status = "not_started"
    elif session_alive and runtime_key == "custom":
        # A custom command may intentionally be one-shot. Once launched, a
        # surviving managed session is up, but process state is unknowable.
        live_status = "unknown"
    else:
        live_status = "down"
    endpoint = (config.tmux_target_endpoint(session)
                if isinstance(session, config.TmuxTarget) else None)
    return {
        "runtime": runtime_key,
        "session_alive": session_alive,
        "runtime_alive": runtime_alive,
        # Backward-compatible field: historically this meant a Claude process,
        # not merely a shell session.
        "alive": runtime_alive,
        "live_status": live_status,
        "tmux_endpoint": endpoint,
        "migration_required": endpoint == config.TMUX_ENDPOINT_LEGACY,
    }


# How long the pane must hold completely still (and parse idle) before we believe
# it's genuinely waiting for input. This must exceed Claude's short inter-chunk
# "think" pauses — while streaming a long answer it goes quiet for up to a couple
# seconds between bursts, and during such a pause the captured frame is a blank
# prompt with no spinner, indistinguishable from idle in a single look. Sampling
# across a longer dwell makes a pause reveal itself (output resumes and the frame
# changes). A genuinely long (>READY_DWELL) pause is the one case we can't tell
# apart from outside tmux — and there Claude Code's own input layer is the backstop:
# it buffers text typed mid-turn and submits it when the turn ends, so the message
# still reaches the agent intact (never interleaved into the stream, never lost).
READY_DWELL = 1.6
_READY_STEPS = 4


def _capture_frame_result(target):
    if not target:
        return False, ""
    return tmux("capture-pane", "-t", target, "-p")


def pane_ready(target, runtime_key="claude"):
    """True only when the pane is an IDLE claude prompt ready for a NEW message.

    Robust to Claude Code's ever-changing 'working' UI (v2.1.185 stopped printing
    "esc to interrupt" in the frame and rotates non-"-ing" spinner words, so a
    single-frame text check read 'idle' mid-generation). We instead require the
    frame to parse idle AND stay byte-identical across the whole READY_DWELL window
    — a streaming claude changes the frame within that span; a waiting prompt does
    not. See READY_DWELL for the one residual case (a very long inter-chunk pause)
    and why it's safe (Claude buffers the input)."""
    ok, last = _capture_frame_result(target)
    if not ok:
        return False
    if detect_status(last, runtime_key) != "idle":
        return False
    step = READY_DWELL / _READY_STEPS
    for _ in range(_READY_STEPS):
        time.sleep(step)
        ok, f = _capture_frame_result(target)
        if not ok or f != last or detect_status(f, runtime_key) != "idle":
            return False                   # changed or no longer idle → still working
        last = f
    return True


def capture_frame(target):
    """Just the current visible frame — cheap, used for status detection.

    KEPT (the rest of the OLD capture/render stack is dropped) because
    `detect_status` reads a frame to infer worker state for the crew graph."""
    ok, text = _capture_frame_result(target)
    return text if ok else ""
