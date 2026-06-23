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


# session-name → claude pane_id map, cached briefly. list_claude_panes() shells
# out to `ps -axo` (~180ms!), so resolving it fresh on every graph-snapshot poll
# would be heavy. The map only changes when a worker's claude restarts, so a short
# TTL is safe and keeps the poll cheap.
_PANE_CACHE = {"at": 0.0, "map": {}}
_PANE_TTL = 3.0  # seconds


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


def pane_ready(target):
    """True only when the pane is an IDLE claude prompt ready for a NEW message.

    Robust to Claude Code's ever-changing 'working' UI (v2.1.185 stopped printing
    "esc to interrupt" in the frame and rotates non-"-ing" spinner words, so a
    single-frame text check read 'idle' mid-generation). We instead require the
    frame to parse idle AND stay byte-identical across the whole READY_DWELL window
    — a streaming claude changes the frame within that span; a waiting prompt does
    not. See READY_DWELL for the one residual case (a very long inter-chunk pause)
    and why it's safe (Claude buffers the input)."""
    last = capture_frame(target)
    if detect_status(last) != "idle":
        return False
    step = READY_DWELL / _READY_STEPS
    for _ in range(_READY_STEPS):
        time.sleep(step)
        f = capture_frame(target)
        if f != last or detect_status(f) != "idle":
            return False                   # changed or no longer idle → still working
        last = f
    return True


def capture_frame(target):
    """Just the current visible frame — cheap, used for status detection.

    KEPT (the rest of the OLD capture/render stack is dropped) because
    `detect_status` reads a frame to infer worker state for the crew graph."""
    ok, text = tmux("capture-pane", "-t", target, "-p")
    return text if ok else ""
