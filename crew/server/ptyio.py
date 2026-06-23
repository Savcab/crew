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
    pid, fd = pty.fork()
    if pid == 0:
        os.environ["TERM"] = "xterm-256color"
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
    fd = get_fd(pid_id)
    if fd is None:
        return
    last_hb = time.monotonic()
    while alive():
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
        try:
            chunk = os.read(fd, 65536)
        except OSError:
            break
        if not chunk:
            break
        on_bytes(chunk)          # raises on a dead client → propagates out → finally


def reap_stale():
    """Best-effort: kill any orphaned _ngview_<ourpid>_* grouped sessions whose PTY
    record we no longer track (e.g. a crashed handler). Safe — only touches our own
    grouped views, never base sessions."""
    ok, out = _tmux("list-sessions", "-F", "#{session_name}")
    if not ok:
        return
    prefix = f"_ngview_{os.getpid()}_"
    with _LOCK:
        tracked = set(_SESS.keys())
    for name in out.split("\n"):
        if name.startswith(prefix) and name not in tracked:
            _tmux("kill-session", "-t", name)
