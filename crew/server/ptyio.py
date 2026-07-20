#!/usr/bin/env python3
"""ptyio — the PTY-attach terminal transport (the correct tmux→browser bridge).

Instead of scraping tmux (capture-pane snapshots + pipe-pane), we run a REAL
`tmux attach` client inside a pseudo-terminal (PTY) per browser stream — exactly
what `tmux attach` in a terminal does, and how ttyd/gotty/wetty work. tmux treats
the PTY as a client: it sizes the window to the PTY (TIOCSWINSZ), renders the pane
with full escapes, and reflows on resize, ALL natively. The HTTP layer just pipes
bytes both ways. This deletes the entire scrape-and-reconstruct machinery and its
whole bug class (scatter, frozen-wide scrollback, letterbox, size races).

Isolation: each stream attaches to a GROUPED session (`tmux new-session -t <base>`)
so viewing a window doesn't yank the user's real client's selected window. The
grouped view gets manual window sizing (the browser grid is authoritative) and
`status off` (pure pane content).

Pure stdlib: os, pty, fcntl, termios, struct, select, signal, threading.
"""
import fcntl
import os
import pty
import select
import signal
import struct
import subprocess
import termios
import threading
import time

from .. import config

# id -> {"fd", "pid", "view", "key"} ; id IS the grouped-view session name (unique).
# `key` = "<session>:<canonical-window-id>" — the window being viewed. Used to enforce ONE live
# view per pane (see open_attach): tmux ties window SIZE to the window object and
# can't show one pane at two sizes, so two concurrent views (e.g. two browser tabs
# on the same worker) would fight over the shared window's size and the panel would
# keep resizing. We evict the prior view for a key when a new one opens → newest
# viewer wins deterministically, no tug-of-war.
_SESS = {}
_LOCK = threading.Lock()
_OPEN_LOCK = threading.Lock()
_N = [0]
_VIEW_MARKER = "@crew_dashboard_view"

# The dashboard PTY uses a DISTINCT TERM ("tmux-256color") so we can scope a
# terminal-override to it WITHOUT touching the user's real terminal (TERM=xterm*).
# The override strips smcup/rmcup → tmux does NOT switch the browser terminal into
# the alternate screen on attach → tmux draws in xterm's MAIN buffer → xterm's own
# scrollback captures the stream and the browser wheel scrolls it NATIVELY.
#
# This is the ONLY scroll model that works here. The two alternatives both fail:
#   - mouse ON → wheel enters tmux COPY-MODE, but copy-mode is a PANE state SHARED
#     across the grouped session, so scrolling freezes the agent's real pane and
#     `crew message` send-keys land in copy-mode (delivery breaks). [verified]
#   - mouse OFF + alt-screen → xterm has no scrollback to scroll, so the wheel emits
#     ARROW keys, which Claude reads as prompt-history recall ("previous message").
# Native xterm scrollback touches neither tmux state nor the app's input. Claude
# renders in the main buffer (alternate_on=0), so its transcript flows into the
# client scrollback on its own.
# ponytail: scoped by TERM string, the only handle terminal-overrides gives us. A
# user running nested tmux (inner client TERM=tmux-256color) would also lose the
# alt-screen on that inner client — rare; upgrade to a custom terminfo if it bites.
_DASH_TERM = "tmux-256color"
_NOALT_OVERRIDE = f"{_DASH_TERM}:smcup@:rmcup@"
_OVERRIDE_DONE = [False]
_OVERRIDE_ENDPOINTS = set()


def _ensure_native_scroll(endpoint=config.TMUX_ENDPOINT_CREW):
    """Append the smcup@/rmcup@ terminal-override for the dashboard TERM once, so
    tmux keeps the browser terminal in its main screen (→ native xterm scrollback).
    Scoped to _DASH_TERM; the user's xterm* clients keep the alternate screen.
    Idempotent across dashboard processes (checks the live value before appending)."""
    if not _OVERRIDE_DONE[0]:
        _OVERRIDE_ENDPOINTS.clear()
    if endpoint in _OVERRIDE_ENDPOINTS:
        return True
    # Lock + re-check so two terminals attaching at once don't both append (the
    # live-value check alone races: both read the empty value before either sets it).
    with _LOCK:
        if endpoint in _OVERRIDE_ENDPOINTS:
            return True
        ok, cur = _tmux(
            "show-options", "-g", "-v", "terminal-overrides",
            endpoint=endpoint)
        if not ok:
            return False
        if _NOALT_OVERRIDE not in (cur or ""):
            ok, _ = _tmux(
                "set-option", "-ga", "terminal-overrides", _NOALT_OVERRIDE,
                endpoint=endpoint)
            if not ok:
                return False
        _OVERRIDE_ENDPOINTS.add(endpoint)
        _OVERRIDE_DONE[0] = True
        return True


def _tmux(*args, timeout=5, endpoint=None):
    try:
        endpoint = endpoint or config.tmux_target_endpoint(*args)
        p = subprocess.run(
            config.tmux_command(*args, endpoint=endpoint),
            capture_output=True, text=True, timeout=timeout,
            env=config.tmux_environment(endpoint=endpoint))
        return p.returncode == 0, p.stdout.strip()
    except Exception:
        return False, ""


def _attach_command(view, endpoint):
    """Exact argv/environment used by the forked tmux attach client."""
    environment = config.tmux_environment(endpoint=endpoint)
    environment["TERM"] = _DASH_TERM
    return (config.tmux_command(
        "attach-session", "-t", view, endpoint=endpoint), environment)


def _exact_session_exists(session, endpoint=None):
    """tmux accepts unique target prefixes; Crew attachments never should."""
    endpoint = endpoint or config.tmux_target_endpoint(session)
    ok, raw = _tmux(
        "list-sessions", "-F", "#{session_name}", endpoint=endpoint)
    return bool(ok and session in (raw or "").splitlines())


def _resolve_window_id(session, window, endpoint=None):
    """Resolve one exact window name/index/id within an exact base session.

    Several tmux commands silently fall back to the selected window when a target
    is missing.  Resolve from the session's inventory first and carry the canonical
    window id forward; aliases such as ``agent`` and ``0`` then share one view key.
    """
    if not isinstance(window, str) or not window:
        return None
    endpoint = endpoint or config.tmux_target_endpoint(session)
    ok, raw = _tmux(
        "list-windows", "-t", session, "-F",
        "#{window_name}\t#{window_index}\t#{window_id}", endpoint=endpoint)
    if not ok:
        return None
    matches = []
    for line in (raw or "").splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        name, index, window_id = parts[:3]
        if window in (name, index, window_id):
            matches.append(window_id)
    return matches[0] if len(matches) == 1 else None


def _drop_view_session(view, endpoint=config.TMUX_ENDPOINT_CREW):
    _tmux("kill-session", "-t", view, endpoint=endpoint)


def open_attach(session, window="claude"):
    """Spawn `tmux attach` to a grouped view of <session>:<window> in a PTY.
    Returns (id, fd) or (None, None) if the base session is gone. `id` is the
    grouped-view session name (also the key for input/resize/close)."""
    if not isinstance(session, str) or not session:
        return None, None
    endpoint = config.tmux_target_endpoint(session)
    # Serialize open preparation + newest-view replacement. Without this, two
    # concurrent reconnects can both observe no prior record and leave competing
    # clients resizing the same shared window.
    with _OPEN_LOCK:
        if not _exact_session_exists(session, endpoint=endpoint):
            return None, None
        window_id = _resolve_window_id(session, window, endpoint=endpoint)
        if not window_id:
            return None, None
        key = f"{endpoint}:{session}:{window_id}"

        with _LOCK:
            _N[0] += 1
            n = _N[0]
        view = f"_ngview_{os.getpid()}_{n}"

        # grouped session: shares <session>'s windows but has its OWN selected
        # window. Every setup step below is part of the transport contract; a
        # partial group must never be presented as a working PTY stream.
        ok, _ = _tmux(
            "new-session", "-d", "-t", session, "-s", view,
            endpoint=endpoint)
        if not ok:
            return None, None
        setup = (
            ("select-window", "-t", f"{view}:{window_id}"),
            ("set-option", "-t", view, "status", "off"),
            ("set-option", "-t", view, "window-size", "manual"),
            ("set-option", "-t", view, "mouse", "off"),
            ("set-option", "-t", view, _VIEW_MARKER, str(os.getpid())),
        )
        for command in setup:
            ok, _ = _tmux(*command, endpoint=endpoint)
            if not ok:
                _drop_view_session(view, endpoint)
                return None, None

        # If the shared pane is already stuck in copy-mode from an earlier scroll,
        # attempt to drop it. tmux returns nonzero when no mode is active, which is
        # the healthy/common case, so this intentionally is not a setup gate.
        _tmux(
            "send-keys", "-t", f"{view}:{window_id}", "-X", "cancel",
            endpoint=endpoint)
        # Install the no-alt-screen override BEFORE the client attaches (it decides
        # alt-screen at attach time), so this attach lands in the main buffer.
        if not _ensure_native_scroll(endpoint):
            _drop_view_session(view, endpoint)
            return None, None

        # Prepare the replacement before evicting the working prior view. A failed
        # setup/fork therefore leaves the existing browser stream intact.
        with _LOCK:
            stale = [vid for vid, rec in _SESS.items()
                     if rec.get("key") == key]
        # ``pty.fork()`` is unsafe here: open_attach runs inside a
        # ThreadingHTTPServer request, and Python 3.14 warns that forkpty from a
        # multithreaded process can deadlock before the child reaches exec.
        # posix_spawn performs the exec transition without running Python in a
        # forked child.  The slave still becomes tmux's stdin/stdout/stderr and
        # setsid gives the attach client an isolated session, matching the old
        # terminal behavior without the at-fork hazard.
        master_fd = None
        slave_fd = None
        try:
            master_fd, slave_fd = pty.openpty()
            command, environment = _attach_command(view, endpoint)
            file_actions = (
                (os.POSIX_SPAWN_DUP2, slave_fd, 0),
                (os.POSIX_SPAWN_DUP2, slave_fd, 1),
                (os.POSIX_SPAWN_DUP2, slave_fd, 2),
                (os.POSIX_SPAWN_CLOSE, master_fd),
                (os.POSIX_SPAWN_CLOSE, slave_fd),
            )
            pid = os.posix_spawnp(
                command[0], command, environment,
                file_actions=file_actions, setsid=True)
        except (OSError, ValueError, NotImplementedError):
            for opened_fd in (master_fd, slave_fd):
                if opened_fd is not None:
                    try:
                        os.close(opened_fd)
                    except OSError:
                        pass
            _drop_view_session(view, endpoint)
            return None, None
        try:
            os.close(slave_fd)
        except OSError:
            pass
        fd = master_fd
        with _LOCK:
            _SESS[view] = {
                "fd": fd, "pid": pid, "view": view, "key": key,
                "endpoint": endpoint,
            }
        for old_view in stale:
            close(old_view)
        return view, fd


def get_fd(pid_id):
    with _LOCK:
        rec = _SESS.get(pid_id)
    return rec["fd"] if rec else None


def _borrow_record(pid_id):
    """Duplicate a tracked fd while holding the registry lock.

    Closing one stream and opening another can immediately reuse an fd number.
    A duplicate remains bound to the original PTY, so an in-flight input/resize
    cannot cross into the newer stream after registry lookup.
    """
    with _LOCK:
        rec = _SESS.get(pid_id)
        if not rec:
            return None, None
        try:
            borrowed_fd = os.dup(rec["fd"])
        except OSError:
            return None, None
        return dict(rec), borrowed_fd


def write_input(pid_id, data_bytes):
    """Write raw bytes (decoded keystrokes / mouse / escapes) to the PTY."""
    try:
        pending = memoryview(data_bytes).cast("B")
    except (TypeError, ValueError):
        return False
    _, fd = _borrow_record(pid_id)
    if fd is None:
        return False
    try:
        while pending:
            try:
                written = os.write(fd, pending)
            except InterruptedError:
                continue
            if written <= 0:
                return False
            pending = pending[written:]
        return True
    except (OSError, ValueError):
        return False
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def set_size(pid_id, cols, rows):
    """Size the view's window to (cols,rows). Two steps because window-size is
    MANUAL: (1) TIOCSWINSZ the PTY so the tmux CLIENT is that size; (2) an explicit
    `resize-window` on the view's window — with manual sizing the window does NOT
    auto-follow the client, so this is what actually sets it deterministically."""
    try:
        cols = max(2, min(500, int(cols)))
        rows = max(2, min(300, int(rows)))
    except (TypeError, ValueError):
        return False
    rec, fd = _borrow_record(pid_id)
    if not rec:
        return False
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ,
                    struct.pack("HHHH", rows, cols, 0, 0))
    except OSError:
        return False
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
    # posix_spawn(setsid=True) gave the attach client no controlling TTY, so
    # the kernel has no foreground process group to notify: without an explicit
    # SIGWINCH the client keeps its attach-time size and tmux renders an
    # 80-column strip into a full-width xterm. Signal it directly; tmux's
    # handler re-reads TIOCGWINSZ from its stdin (the PTY slave).
    try:
        os.kill(rec["pid"], signal.SIGWINCH)
    except (OSError, KeyError):
        pass
    # explicit resize on THIS view's window (manual mode → authoritative).
    ok, _ = _tmux(
        "resize-window", "-t", f"{rec['view']}:", "-x", str(cols), "-y",
        str(rows), endpoint=rec.get("endpoint", config.TMUX_ENDPOINT_CREW))
    return ok


def close(pid_id):
    """Tear down a stream: close the PTY, kill the tmux-attach child, kill the
    grouped VIEW session (never the base session)."""
    with _LOCK:
        rec = _SESS.pop(pid_id, None)
    if not rec:
        return
    try: os.close(rec["fd"])
    except OSError: pass
    _tmux(
        "kill-session", "-t", rec["view"],
        endpoint=rec.get("endpoint", config.TMUX_ENDPOINT_CREW))
    # A forked attach that is killed but never waitpid()'d remains a zombie for
    # the lifetime of the dashboard. Check whether it already exited before
    # signalling (avoids a reused-pid kill if another handler reaped it), then reap.
    try:
        waited, _ = os.waitpid(rec["pid"], os.WNOHANG)
    except (ChildProcessError, OSError):
        return
    if waited:
        return
    try:
        os.kill(rec["pid"], signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError:
        return
    try:
        os.waitpid(rec["pid"], 0)
    except (ChildProcessError, OSError):
        pass


def read_loop(pid_id, on_bytes, alive, on_idle=None):
    """Block-read the PTY and call on_bytes(chunk) for each read, until EOF, the
    PTY closes, or alive() returns False. On each idle select-timeout (no PTY
    output) call on_idle() if given — the handler uses this to write an SSE
    heartbeat, which RAISES when the browser has disconnected. WITHOUT this, an
    idle pane would block forever in select() and never notice the dropped client,
    leaking the PTY + the grouped tmux view session (observed: orphaned _ngview*).
    Caller runs this in the SSE handler thread and must close(pid_id) in finally."""
    if get_fd(pid_id) is None:
        return
    last_hb = time.monotonic()
    while alive():
        # Re-fetch the fd EVERY iteration instead of caching it. When a newer attach
        # to the SAME pane evicts this view (open_attach → close), close() pops our
        # record under _LOCK and os.close()s the fd — and the evicting open_attach's
        # pty.fork() reuses that very fd integer. A loop holding the cached fd would
        # then read the NEW pty and paint it into THIS (old) client: a cross-stream
        # byte-steal that interleaves two paints into one xterm → the "duplicate
        # lines that don't go away". pid_id (the _ngview_* name) is never reused, so
        # once close() pops it get_fd() returns None forever → we exit before the
        # recycled fd is ever read.
        fd = get_fd(pid_id)
        if fd is None:
            break
        try:
            r, _, _ = select.select([fd], [], [], 0.5)
        except (OSError, ValueError):
            break
        now = time.monotonic()
        # Heartbeat at LEAST every ~1s, whether or not the PTY produced output. A
        # BUSY pane never hits the `not r` idle branch, so without this a dropped
        # browser whose socket buffers our data (no immediate raise) would never be
        # detected → leaked view. The periodic heartbeat write RAISES on a dead
        # client and breaks the loop → finally → close.
        if on_idle and (not r or now - last_hb >= 1.0):
            on_idle()            # raises on a dead client → propagates out → finally
            last_hb = now
        if not r:
            continue
        if get_fd(pid_id) != fd:  # evicted + fd recycled between select and read → bail
            break
        try:
            chunk = os.read(fd, 65536)
        except OSError:
            break
        if not chunk:
            break
        on_bytes(chunk)          # raises on a dead client → propagates out → finally


def reap_stale(max_age_unused=None):
    """Best-effort cleanup of orphaned _ngview_* grouped sessions: ours whose PTY
    record we no longer track (a crashed handler), AND any whose owning dashboard PID
    is dead (a previous run that exited without tearing its views down — otherwise
    they leak forever across restarts). Only touches _ngview_* views, never a base
    session."""
    mine = f"_ngview_{os.getpid()}_"
    with _LOCK:
        tracked = {
            (name, rec.get("endpoint", config.TMUX_ENDPOINT_CREW))
            for name, rec in _SESS.items()
        }
    # Never globally reap the shared pre-upgrade/default server. A session name
    # and user-settable tmux option are not durable ownership proof; legacy
    # views are closed while tracked, but crash leftovers require an explicit
    # operator restart rather than risking deletion of a personal lookalike.
    for endpoint in (config.TMUX_ENDPOINT_CREW,):
        ok, out = _tmux(
            "list-sessions", "-F", f"#{{session_name}}\t#{{{_VIEW_MARKER}}}",
            endpoint=endpoint)
        if not ok:
            continue
        for row in out.split("\n"):
            name, separator, owner = row.partition("\t")
            if not name.startswith("_ngview_"):
                continue
            # The reserved-looking name is not ownership proof: a user may have a
            # legitimate session with that name. Only groups we marked at creation
            # are eligible, and the encoded pid must agree with the marker.
            if not separator or not owner:
                continue
            try:
                pid = int(owner)
            except ValueError:
                continue
            if not name.startswith(f"_ngview_{pid}_"):
                continue
            if pid == os.getpid() and name.startswith(mine):
                if (name, endpoint) not in tracked:
                    _tmux("kill-session", "-t", name, endpoint=endpoint)
                continue
            # Marked view from another/previous dashboard — reap only if that PID
            # is gone (a live dashboard still owns its own views).
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                _tmux("kill-session", "-t", name, endpoint=endpoint)
            except OSError:
                pass  # not ours to judge (e.g. EPERM) → leave
