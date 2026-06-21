#!/usr/bin/env python3
"""tmuxio — tmux primitives + session→pane targeting + the pipe-pane byte stream.

Ported from the OLD monolith (dashboard.py) as part of the ng refactor. This is
the layer that turns a dashboard "target" (usually a bare tmux session name like
'crew-worker-1') into the EXACT pane running `claude`, and that owns every tmux
shell-out: resize, send-keys, shell-window lifecycle, scrollback capture.

What changed vs OLD:
  * ADDED `tmux_b()` (binary-stdout variant — capture-pane WITH escapes is bytes,
    not text) and `seed_scrollback()` (the one-shot scrollback paint for xterm.js).
  * DROPPED the hand-rolled renderer: capture / capture_live / ansi_to_html /
    xterm256 / _inject_cursor_line / _is_blank_row are gone — xterm.js owns
    rendering now. `capture_frame` is KEPT because `detect_status` still reads a
    frame to infer worker state for the crew graph.
  * `fit_session` applies the SPEC "fit fix": resize the window regardless of
    attach state, clamping 20..500 cols / 5..300 rows (see its docstring).

No third-party deps. Pure stdlib.
"""
import os
import re
import shutil
import subprocess
import time

# Self-locating tmux binary (same resolution the OLD dashboard used). Falls back
# to the common Homebrew path so a stripped PATH (e.g. a launchd context) still
# finds it.
TMUX = shutil.which("tmux") or "/opt/homebrew/bin/tmux"


def tmux(*args, timeout=5):
    """Run a tmux command, decoding stdout as text. Returns (ok, output) where
    `output` is stdout on success or stderr on failure (handy for surfacing the
    tmux error straight back to the caller)."""
    try:
        out = subprocess.run([TMUX, *args], capture_output=True, text=True, timeout=timeout)
        return out.returncode == 0, (out.stdout if out.returncode == 0 else out.stderr)
    except Exception as e:
        return False, str(e)


def tmux_b(*args, timeout=5):
    """Binary-stdout variant of `tmux()`. `capture-pane -e` keeps the SGR/CSI
    escape bytes verbatim, and those are NOT guaranteed valid UTF-8 — decoding to
    text would mangle them. So the seed/scrollback path captures raw bytes and
    ships them base64'd to xterm.js, which re-parses the escapes itself.

    Returns (ok, bytes). On failure the error string is encoded to bytes so the
    return type stays consistent."""
    try:
        p = subprocess.run([TMUX, *args], capture_output=True, timeout=timeout)
        return p.returncode == 0, p.stdout
    except Exception as e:
        return False, str(e).encode()


def claude_ttys():
    """tty names that have a `claude` process attached (controlling terminal)."""
    ttys = set()
    try:
        out = subprocess.run(["ps", "-axo", "tty=,command="],
                             capture_output=True, text=True, timeout=5)
        for line in out.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            tty, _, cmd = line.partition(" ")
            if tty in ("??", "?", ""):
                continue
            first = cmd.split()[0] if cmd.split() else ""
            if "/bin/claude" in cmd or first == "claude" or first.endswith("/claude"):
                ttys.add(tty)
    except Exception:
        pass
    return ttys


# Spinner glyphs Claude Code cycles through while generating. v2.x rotates a set
# much wider than any single capture shows (✻✶✢✽✳✦✧∗·◐◓◑◒ …), and the word after
# it isn't always an "-ing" gerund ("Booping…", "Churned…"), and the hint is no
# longer always "esc to interrupt" (now "(2s · thinking with high effort)"). So we
# match on the STABLE shape — a spinner glyph + a word ending in the … ellipsis, or
# an elapsed-time status — rather than a fixed word list. The delivery gate
# (pane_ready) additionally compares two frames over time, which catches generation
# regardless of glyph/word, so this only needs to be good enough for the status dot.
_SPINNERS = "✻✶✢✽✳✦✧∗·◐◓◑◒◇✦"


def detect_status(text):
    """Infer Claude state from its visible screen.

    Order matters: 'working' is the strongest signal, so check it first.
    'needs_input' only fires on the STRUCTURED permission UI — a numbered selection
    menu — not on prose like 'what do you want done?'.
    """
    low = text.lower()

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
    proceed = ("do you want to proceed" in low or "needs your permission" in low
               or "do you want to make this edit" in low
               or "do you want to create" in low)
    if has_menu or proceed:
        return "needs_input"

    return "idle"


def list_claude_panes():
    """Only panes that have a claude process on their tty."""
    fmt = ("#{session_name}\t#{window_index}\t#{window_name}\t#{pane_index}"
           "\t#{pane_id}\t#{pane_tty}\t#{pane_current_path}")
    ok, raw = tmux("list-panes", "-a", "-F", fmt)
    panes = []
    if not ok:
        return panes
    ctty = claude_ttys()
    for line in raw.strip().splitlines():
        p = line.split("\t")
        if len(p) < 7:
            continue
        sess, win_idx, win_name, pane_idx, pane_id, pane_tty, cwd = p[:7]
        if pane_tty.replace("/dev/", "") not in ctty:
            continue
        target = f"{sess}:{win_idx}.{pane_idx}"
        panes.append({
            "session": sess, "target": target, "pane_id": pane_id,
            "cwd": cwd, "cwd_short": shorten(cwd),
        })
    panes.sort(key=lambda x: x["session"])
    return panes


def shorten(path):
    home = os.path.expanduser("~")
    if path.startswith(home):
        path = "~" + path[len(home):]
    return path


# Independent claude sessions = panes running `claude` whose tmux session is NOT
# a crew manager or worker. The crew graph shows them as their own cluster so the
# user can see (and dock into) every Claude on the box, not just crew-managed ones.
# Cached because the underlying ps scan (~180ms) + a per-pane status capture is far
# too heavy to run on every 1.5s crew-snapshot poll (see the keystroke-latency note).
_INDEP_CACHE = {"at": 0.0, "data": []}
_INDEP_TTL = 3.0  # seconds — matches _PANE_TTL; status is "fresh enough" at 3s


def independent_sessions(known):
    """Claude sessions not in `known` (a set of crew manager/worker session names
    AND identity names), one node per session, with live status. Cached ~3s."""
    now = time.monotonic()
    if (now - _INDEP_CACHE["at"]) < _INDEP_TTL:
        return _INDEP_CACHE["data"]
    out, seen = [], set()
    for p in list_claude_panes():
        s = p["session"]
        if s in known or s in seen:
            continue
        seen.add(s)
        out.append({
            "session": s, "target": p["target"],
            "cwd": p["cwd"], "cwd_short": p["cwd_short"],
            "status": detect_status(capture_frame(p["target"])),
        })
    out.sort(key=lambda x: x["session"])
    _INDEP_CACHE["data"] = out
    _INDEP_CACHE["at"] = now
    return out


# session-name → claude pane_id map, cached briefly. list_claude_panes() shells
# out to `ps -axo` (~180ms!), so calling it on EVERY keystroke (resolve_target is
# on the /api/send + /api/key hot path) made typing crawl. The map only changes
# when a worker's claude restarts, so a short TTL is safe and makes resolve ~free.
_PANE_CACHE = {"at": 0.0, "map": {}}
_PANE_TTL = 3.0  # seconds

# remembers the (cols,rows) we last fitted each target to, so the per-keystroke
# capture can skip fit_session's `display-message` probe (a whole subprocess
# spawn) when nothing changed. Expires so an attach/detach is re-checked.
_FIT_CACHE = {}   # target -> (cols, rows, monotonic_ts)
_FIT_TTL = 5.0


def _session_pane_map(force=False):
    now = time.monotonic()
    if not force and (now - _PANE_CACHE["at"]) < _PANE_TTL:
        return _PANE_CACHE["map"]
    m = {}
    for p in list_claude_panes():
        m.setdefault(p["session"], p["pane_id"])  # first (lowest-index) claude pane
    _PANE_CACHE["map"] = m
    _PANE_CACHE["at"] = now
    return m


def resolve_target(target):
    """Resolve a dashboard target to the EXACT pane we should capture/type into.

    The frontend often passes a bare session name (e.g. 'crew-worker-1' — the
    identity a worker registered under via agent-mail). tmux would then act on
    whatever pane is *active* in that session, so if the user split the window
    (the manager runs a 3-pane claude window) we'd capture a stray split and the
    cursor lands in the wrong place. We pin to the pane actually running claude —
    its `pane_id` (%N) is unambiguous and survives window/pane switches.

    Backed by a 3s cache so the per-keystroke send/key path never pays the
    ~180ms `ps` scan. On a cache MISS for a known-bare name (claude maybe just
    restarted → pane_id changed) we refresh once before giving up."""
    if not target:
        return target
    # already a pane-id or a fully-qualified session:win.pane → trust it as-is
    # (covers the ':shell' window target too).
    if target.startswith("%") or ":" in target:
        return target
    m = _session_pane_map()
    if target in m:
        return m[target]
    # not found in the cached map — force one refresh (handles a just-restarted
    # claude whose pane_id changed) before falling back to the bare name.
    m = _session_pane_map(force=True)
    return m.get(target, target)


def claude_pane(session):
    """The pane_id INSIDE <session> that is actually running claude — robust to the
    user splitting the claude window (Ctrl-b %/") or adding panes. We search EVERY
    pane in the session (`-s`) and pick the one whose tty has a claude process, so
    a crew message always lands in claude's prompt, never a stray shell split.

    Fallbacks, in order: the `claude`-named window's first pane, then the bare
    session name (tmux's active pane) so delivery still attempts something sane."""
    if not session:
        return session
    ctty = claude_ttys()
    ok, raw = tmux("list-panes", "-s", "-t", session, "-F", "#{pane_id}\t#{pane_tty}")
    if ok:
        for line in raw.strip().splitlines():
            parts = line.split("\t")
            if len(parts) == 2 and parts[1].replace("/dev/", "") in ctty:
                return parts[0]
    ok2, pid = tmux("list-panes", "-t", f"{session}:claude", "-F", "#{pane_id}")
    if ok2 and pid.strip():
        return pid.strip().splitlines()[0]
    return session


def pane_ready(target):
    """True only when the pane is an IDLE claude prompt ready for a NEW message.

    Robust to Claude Code's ever-changing 'working' UI: we require BOTH that the
    visible frame parses as idle (no interrupt hint / spinner / menu) AND that the
    frame is STABLE across a short interval. A generating claude streams output and
    animates its spinner + elapsed counter, so two captures ~0.45s apart differ; an
    idle prompt does not. The stability check is the authoritative guard — it stops
    a message being typed mid-generation even when the spinner text alone fools
    detect_status (the bug: v2.1.185 doesn't print "esc to interrupt" in the frame
    and rotates non-"-ing" spinner words, so single-frame detection read 'idle')."""
    f1 = capture_frame(target)
    if detect_status(f1) != "idle":
        return False
    time.sleep(0.45)
    f2 = capture_frame(target)
    if f1 != f2:
        return False                       # frame changed → actively generating
    return detect_status(f2) == "idle"


def fit_session(target, cols, rows):
    """Resize the WINDOW of a detached pane so it fills the dashboard pane.

    tmux pins a detached session's window at its last-attached size (default
    80x24), so a worker pane renders into the top-left corner of the wide dock and
    looks blank / not-full-width. We size the captured window to the browser
    pane's (cols, rows).

    Key correctness points (a prior version got these wrong):
      * We resize the WINDOW of the *resolved* target (pane-id / `sess:shell`), not
        the bare session name — a bare name hits the session's ACTIVE window, which
        is usually a DIFFERENT window (the shell pane never resized, and the claude
        window got churned every poll).
      * `window-size manual` is scoped to that window (`-w`), so the dock's fit
        only pins THIS window, not the whole session.

    CRITICAL — we NEVER resize an ATTACHED session. When a real terminal is attached
    to the worker, ITS client owns the window size; pinning `window-size manual` at
    the dashboard's dimensions makes tmux unable to reconcile the two and corrupts
    the real terminal — output staircases and tmux letterboxes the window with a
    dotted fill. (An earlier "resize even when attached" experiment caused exactly
    that and is reverted here.) So: if the session is attached, we UNDO any manual
    pin we previously left (`set-option -uw` → inherit) and return without resizing.
    Only DETACHED windows — which tmux otherwise freezes at 80x24, leaving the dock
    half-blank — get fitted to the viewer's (cols, rows)."""
    try:
        cols = int(cols); rows = int(rows)
    except (TypeError, ValueError):
        return
    if not (20 <= cols <= 500 and 5 <= rows <= 300):
        return
    # fast path: we already fitted this target to this exact size recently → the
    # window is correct, skip the display-message probe (saves a subprocess spawn
    # on every keystroke capture). The TTL lets attach/detach get re-detected.
    c = _FIT_CACHE.get(target)
    if c and c[0] == cols and c[1] == rows and (time.monotonic() - c[2]) < _FIT_TTL:
        return
    ok, info = tmux("display-message", "-t", target, "-p",
                    "#{session_attached},#{window_width},#{window_height}")
    if not ok:
        return
    try:
        att, ww, wh = info.strip().split(",", 2)
        if int(att):
            # attached → the real client owns the size; undo any manual override we
            # left so the window tracks the client again, and NEVER resize it.
            tmux("set-option", "-uw", "-t", target, "window-size")
            _FIT_CACHE.pop(target, None)   # attached: don't cache a fitted size
            return
        if int(ww) == cols and int(wh) == rows:
            _FIT_CACHE[target] = (cols, rows, time.monotonic())  # confirmed correct
            return             # already the right size — don't thrash
    except ValueError:
        return
    # detached only: fit the window to the dock viewport (the SPEC "fit fix") — a
    # detached worker's 80x24 default would otherwise leave the dock half-blank.
    tmux("set-option", "-w", "-t", target, "window-size", "manual")
    tmux("resize-window", "-t", target, "-x", str(cols), "-y", str(rows))
    _FIT_CACHE[target] = (cols, rows, time.monotonic())


def capture_frame(target):
    """Just the current visible frame — cheap, used for status detection.

    KEPT (the rest of the OLD capture/render stack is dropped) because
    `detect_status` reads a frame to infer worker state for the crew graph."""
    ok, text = tmux("capture-pane", "-t", target, "-p")
    return text if ok else ""


def pane_size(target):
    """(cols, rows) of the target pane RIGHT NOW, or None. This is the authoritative
    grid the dashboard's xterm must match exactly — in the PASSIVE sizing model the
    dashboard never resizes the tmux window, it mirrors whatever size the window
    already is (set by the real terminal attached to the session, or tmux's default
    when detached). Pane==window for our single-pane claude/shell windows."""
    ok, info = tmux("display-message", "-t", target, "-p", "#{pane_width},#{pane_height}")
    if not ok:
        return None
    try:
        w, h = info.strip().split(",", 1)
        return int(w), int(h)
    except (ValueError, AttributeError):
        return None


def seed_frame(target):
    """The one-shot initial paint for xterm.js: ONLY the current visible frame (not
    the scrollback), WITH escape sequences preserved, as raw bytes.

    WHY frame-only (was -S -2000 full scrollback): tmux stores scrollback as FIXED-
    WIDTH physical rows and CANNOT reflow old history when the window width changes.
    Any window-size churn (a real terminal of a different size, an earlier resize)
    leaves wide rows frozen in the buffer; seeding them into a narrower xterm renders
    them wrapped with gaps ("scattered text"). The VISIBLE FRAME, by contrast, tmux
    always re-renders at the CURRENT width, so it matches the pane_size grid we set
    on xterm exactly — always clean. The live stream then appends fresh, correctly-
    wrapped output; xterm accumulates its own scrollback from there.

    `capture-pane -ep` (no -S) = just the visible screen. Raw bytes (escapes aren't
    guaranteed UTF-8 → SSE layer base64's them). Empty bytes on failure."""
    ok, raw = tmux_b("capture-pane", "-t", target, "-ep")
    return raw if ok else b""


def send(target, keys, enter):
    ok, err = tmux("send-keys", "-t", target, "-l", keys)
    if not ok:
        return False, err
    if enter:
        ok, err = tmux("send-keys", "-t", target, "Enter")
    return ok, err


# A dock terminal is a tmux window named `shell` / `shell-2` / `shell-3` … in the
# worker's session. We only ever create/kill windows matching this pattern, so a
# stray request can never kill the claude window or another session's windows.
_SHELL_WIN_RE = re.compile(r"^shell(-\d+)?$")


def _session_cwd(session):
    ok, cwd = tmux("display-message", "-t", session, "-p", "#{pane_current_path}")
    return cwd.strip() if ok and cwd.strip() else None


def list_shell_windows(session):
    """Names of the session's shell windows (shell, shell-2, …), in order."""
    if not session:
        return []
    ok, out = tmux("list-windows", "-t", session, "-F", "#{window_name}")
    if not ok:
        return []
    names = [w for w in (out or "").split("\n") if _SHELL_WIN_RE.match(w)]
    # sort so the bare `shell` comes first, then shell-2, shell-3, …
    def _key(n):
        return 1 if n == "shell" else int(n.split("-")[1])
    return sorted(set(names), key=_key)


def ensure_shell_window(session):
    """Make sure `<session>` has at least one shell window, creating the default
    `shell` if none exist. Returns (ok, target) for the FIRST shell window."""
    if not session:
        return False, ""
    have = list_shell_windows(session)
    if have:
        return True, f"{session}:{have[0]}"
    cwd = _session_cwd(session)
    args = ["new-window", "-d", "-t", session, "-n", "shell"]
    if cwd:
        args += ["-c", cwd]
    ok, err = tmux(*args)
    if not ok:
        return False, err
    return True, f"{session}:shell"


def new_shell_window(session):
    """Create the next-numbered shell window (shell-2, shell-3, …). Returns
    (ok, target)."""
    if not session:
        return False, ""
    have = list_shell_windows(session)
    if not have:                      # none yet → the default 'shell'
        return ensure_shell_window(session)
    nums = [1 if n == "shell" else int(n.split("-")[1]) for n in have]
    name = f"shell-{max(nums) + 1}"
    cwd = _session_cwd(session)
    args = ["new-window", "-d", "-t", session, "-n", name]
    if cwd:
        args += ["-c", cwd]
    ok, err = tmux(*args)
    if not ok:
        return False, err
    return True, f"{session}:{name}"


def kill_shell_window(session, window):
    """Kill ONE shell window by name. Refuses any window not matching the shell
    pattern (so the claude window can never be killed through this path)."""
    if not session or not window:
        return False, "session and window required"
    if not _SHELL_WIN_RE.match(window):
        return False, f"refusing to kill non-shell window: {window}"
    return tmux("kill-window", "-t", f"{session}:{window}")


# allow-list of named keys the UI may send (no -l, so tmux interprets them).
# Covers everything live-terminal passthrough needs: nav, editing, and the
# common ctrl-combos. Printable characters go through send() with -l (literal),
# so they never need to be in this set.
_LETTERS = "abcdefghijklmnopqrstuvwxyz"
_DIGITS = "0123456789"
ALLOWED_KEYS = {
    "Enter", "Escape", "Up", "Down", "Left", "Right", "Tab", "BSpace", "Space",
    "Home", "End", "PageUp", "PageDown", "DC", "IC", "BTab",
    "M-Enter",  # Shift+Enter in the UI → newline-in-prompt (Claude reads Meta+Enter)
    "y", "n",
} | {f"C-{c}" for c in _LETTERS + _DIGITS} \
  | {f"M-{c}" for c in _LETTERS + _DIGITS} \
  | set(_DIGITS)


def send_key(target, key):
    if key not in ALLOWED_KEYS:
        return False, f"key not allowed: {key}"
    return tmux("send-keys", "-t", target, key)
