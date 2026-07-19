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
grouped view gets `window-size largest` (the dashboard can drive the size up
without shrinking a real terminal) and `status off` (pure pane content).

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

# id -> {"fd", "pid", "view", "key"} ; id IS the grouped-view session name (unique).
# `key` = "<session>:<window>" — the pane being viewed. Used to enforce ONE live
# view per pane (see open_attach): tmux ties window SIZE to the window object and
# can't show one pane at two sizes, so two concurrent views (e.g. two browser tabs
# on the same worker) would fight over the shared window's size and the panel would
# keep resizing. We evict the prior view for a key when a new one opens → newest
# viewer wins deterministically, no tug-of-war.
_SESS = {}
_LOCK = threading.Lock()
_N = [0]

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


def _ensure_native_scroll():
    """Append the smcup@/rmcup@ terminal-override for the dashboard TERM once, so
    tmux keeps the browser terminal in its main screen (→ native xterm scrollback).
    Scoped to _DASH_TERM; the user's xterm* clients keep the alternate screen.
    Idempotent across dashboard processes (checks the live value before appending)."""
    if _OVERRIDE_DONE[0]:
        return
    # Lock + re-check so two terminals attaching at once don't both append (the
    # live-value check alone races: both read the empty value before either sets it).
    with _LOCK:
        if _OVERRIDE_DONE[0]:
            return
        _, cur = _tmux("show-options", "-g", "-v", "terminal-overrides")
        if _NOALT_OVERRIDE not in (cur or ""):
            _tmux("set-option", "-ga", "terminal-overrides", _NOALT_OVERRIDE)
        _OVERRIDE_DONE[0] = True


def _tmux(*args, timeout=5):
    try:
        p = subprocess.run(["tmux", *args], capture_output=True, text=True, timeout=timeout)
        return p.returncode == 0, p.stdout.strip()
    except Exception:
        return False, ""


def open_attach(session, window="claude"):
    """Spawn `tmux attach` to a grouped view of <session>:<window> in a PTY.
    Returns (id, fd) or (None, None) if the base session is gone. `id` is the
    grouped-view session name (also the key for input/resize/close)."""
    if not session:
        return None, None
    ok, _ = _tmux("has-session", "-t", session)
    if not ok:
        return None, None
    key = f"{session}:{window}"
    # EVICT any existing live view of this same pane (another tab / a stale
    # reconnect). Two views share tmux's one window-size and would fight → the
    # panel resizes repeatedly. Closing the old one first makes the newest viewer
    # the sole owner of the size.
    with _LOCK:
        stale = [vid for vid, r in _SESS.items() if r.get("key") == key]
    for vid in stale:
        close(vid)
    with _LOCK:
        _N[0] += 1
        n = _N[0]
    view = f"_ngview_{os.getpid()}_{n}"
    # grouped session: shares <session>'s windows but has its OWN selected-window.
    _tmux("new-session", "-d", "-t", session, "-s", view)
    _tmux("select-window", "-t", f"{view}:{window}")
    _tmux("set-option", "-t", view, "status", "off")
    # window-size MANUAL: the dashboard view owns its size — set_size() does an
    # explicit resize-window to the browser's grid. We tried 'largest' first but
    # grouped sessions share ONE window object, so 'largest' makes the window = the
    # MAX of all attached clients (a bigger real terminal, or a leftover view, then
    # dominates → the dashboard can't control its own size → xterm≠window scatter).
    # 'manual' + explicit resize is deterministic: the view is exactly what the
    # browser asked for. (A real terminal on the base shares this window — native
    # tmux; the user chose the dashboard size by viewing it here.)
    _tmux("set-option", "-t", view, "window-size", "manual")
    # mouse OFF for the dashboard view, so a trackpad scroll never puts tmux into
    # COPY-MODE — copy-mode is a SHARED pane state across the grouped session, so it
    # would freeze the agent's real pane and break `crew message` send-keys. With the
    # no-alt-screen override (see _ensure_native_scroll) the browser terminal stays in
    # its main buffer, so mouse-off scroll is handled by xterm's OWN scrollback
    # natively — not translated to arrow keys (that only happens in the alt screen).
    _tmux("set-option", "-t", view, "mouse", "off")
    # If the shared pane is already stuck in copy-mode from an earlier scroll, drop it
    # so this fresh attach shows live content, not frozen history. (-X cancel is a
    # no-op when the pane isn't in a mode.)
    _tmux("send-keys", "-t", f"{view}:{window}", "-X", "cancel")
    # Install the no-alt-screen override BEFORE the client attaches (it decides
    # alt-screen at attach time), so this attach lands in the main buffer.
    _ensure_native_scroll()
    pid, fd = pty.fork()
    if pid == 0:
        os.environ["TERM"] = _DASH_TERM
        os.execvp("tmux", ["tmux", "attach-session", "-t", view])
        os._exit(1)
    with _LOCK:
        _SESS[view] = {"fd": fd, "pid": pid, "view": view, "key": key}
    return view, fd


def get_fd(pid_id):
    with _LOCK:
        rec = _SESS.get(pid_id)
    return rec["fd"] if rec else None


def write_input(pid_id, data_bytes):
    """Write raw bytes (decoded keystrokes / mouse / escapes) to the PTY."""
    fd = get_fd(pid_id)
    if fd is None:
        return False
    try:
        os.write(fd, data_bytes)
        return True
    except OSError:
        return False


def set_size(pid_id, cols, rows):
    """Size the view's window to (cols,rows). Two steps because window-size is
    MANUAL: (1) TIOCSWINSZ the PTY so the tmux CLIENT is that size; (2) an explicit
    `resize-window` on the view's window — with manual sizing the window does NOT
    auto-follow the client, so this is what actually sets it deterministically."""
    with _LOCK:
        rec = _SESS.get(pid_id)
    if not rec:
        return False
    try:
        cols = max(2, min(500, int(cols)))
        rows = max(2, min(300, int(rows)))
    except (TypeError, ValueError):
        return False
    try:
        fcntl.ioctl(rec["fd"], termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    except OSError:
        pass
    # explicit resize on THIS view's window (manual mode → authoritative).
    ok, _ = _tmux("resize-window", "-t", f"{rec['view']}:", "-x", str(cols), "-y", str(rows))
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
    try: os.kill(rec["pid"], signal.SIGKILL)
    except OSError: pass
    _tmux("kill-session", "-t", rec["view"])


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
    ok, out = _tmux("list-sessions", "-F", "#{session_name}")
    if not ok:
        return
    mine = f"_ngview_{os.getpid()}_"
    with _LOCK:
        tracked = set(_SESS.keys())
    for name in out.split("\n"):
        if not name.startswith("_ngview_"):
            continue
        if name.startswith(mine):
            if name not in tracked:
                _tmux("kill-session", "-t", name)
            continue
        # _ngview_<pid>_<n> from another/previous dashboard — reap only if that PID
        # is gone (a live dashboard still owns its own views).
        try:
            pid = int(name.split("_")[2])
        except (IndexError, ValueError):
            continue
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            _tmux("kill-session", "-t", name)   # dead owner → orphan → reap
        except OSError:
            pass                                # not ours to judge (e.g. EPERM) → leave
