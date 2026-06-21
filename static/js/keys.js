// keys.js — ONE keydown dispatcher for the whole dashboard.
//
// OLD (dashboard.py) had SIX separate `window.addEventListener('keydown', …)`
// listeners — the live-terminal router, the terminals-nav handler, the dock
// router, the side-panel-terminal router, the global Ctrl-G/Ctrl-T handler, and
// a dedicated Cmd-D handler — each re-reading a pile of mutable globals
// (liveMode/dockLive/spLive/view/sel/dockWorker/spMode…) and each independently
// calling preventDefault/stopPropagation. They FOUGHT: ordering between them was
// load-bearing and fragile (e.g. the Cmd-D handler only worked because it was
// registered in the capture phase AFTER the live routers, and the tab-switch
// chords had to be special-cased to "fall through" the live router).
//
// With xterm.js, LIVE typing is no longer our problem: each TerminalPane wires
// `term.onData(bytes) → POST /api/send`, and xterm's own textarea handler turns
// keystrokes (incl. C-c, arrows, Shift+Enter→its byte seq, paste) into those
// bytes. So there is NO per-key tmux mapping in this file — the entire
// keyEventToTmux / liveSend / NAMED-key machinery from OLD is DELETED.
//
// What's left is the set of DASHBOARD chords: things that must act on the app
// chrome (switch tab, toggle the diff panel, close the dock, release/close the
// side panel, dismiss the modal) rather than be typed into a pane. This module
// owns exactly those, behind ONE listener with an EXPLICIT focus model: the
// caller hands us `ctx`, which answers "which view are we in?" and "is the
// keyboard currently inside an xterm pane?" — instead of us guessing from six
// booleans. Everything we don't claim is left untouched so it reaches xterm.
//
// installKeys(ctx) → returns an uninstall() fn (handy for tests / teardown).
//
// ctx contract (the sibling modules — main.js, dock.js, sidepanel.js, modal.js —
// provide these; all optional, missing ones are treated as "feature absent"):
//   view()            -> 'terminals' | 'crew'        (current top-level tab)
//   paneFocused()     -> bool   is the keyboard live inside an xterm pane right
//                               now? (a focused TerminalPane). When true we only
//                               claim the few chords that must beat xterm —
//                               everything else falls through to the pane.
//   modalOpen()       -> bool   is the task/form modal showing?
//   closeModal()              dismiss the modal.
//   dockOpen()        -> bool   is the worker dock showing?
//   closeDock()               close the dock.
//   selectSessionIndex(i)     terminals tab: focus the i-th session (0-based).
//   cycleSession(delta)       terminals tab: move +1/-1 through the session list.
//   sidePanelOpen()   -> bool  is the right-edge side panel showing?
//   sidePanelLive()   -> bool  is the side-panel TERMINAL the live keyboard target?
//   releaseSidePanel()        drop the side-panel terminal's live focus (Ctrl-Esc).
//   closeSidePanel()          close the side panel entirely (Esc when not live).
//   toggleDiff()              Cmd-D: toggle the worktree-diff panel for the
//                             docked worker (open it, or close it if already on).
//
// The handler is registered in the CAPTURE phase on `window` so it runs BEFORE
// xterm's textarea keydown handler. That's essential for the chords we want to
// win even while a pane is focused (Cmd-D especially — see below). For chords we
// DON'T claim we never call preventDefault, so they propagate down to xterm
// normally and live typing is unaffected.

export function installKeys(ctx) {
  ctx = ctx || {};

  // Small helpers so a missing ctx method is a no-op / false rather than a crash
  // (the modules wire these up incrementally; keys.js must not assume all exist).
  const call = (name, ...args) => {
    const fn = ctx[name];
    if (typeof fn === "function") return fn(...args);
    return undefined;
  };
  const view = () => call("view");
  const paneFocused = () => !!call("paneFocused");

  // Is the user typing into a real form control (the modal's inputs/textareas,
  // the broadcast box, a <select>)? Then dashboard letter-chords like bare 'x'
  // must NOT fire — they're text the user is entering. (xterm panes are handled
  // via paneFocused(), not this — their focus lives on a textarea too, but we
  // route those through the focus model, not the activeElement tag.)
  function inFormField() {
    const a = document.activeElement;
    if (!a) return false;
    const tag = a.tagName || "";
    if (/^(INPUT|TEXTAREA|SELECT)$/.test(tag)) {
      // xterm's helper textarea is how a focused pane receives keys — that's NOT
      // a "form field" for our purposes (paneFocused() governs it). Exclude it
      // so the focus model, not the tag test, decides pane behaviour.
      if (a.classList && a.classList.contains("xterm-helper-textarea")) return false;
      return true;
    }
    return a.isContentEditable === true;
  }

  // The bracket chords arrive with inconsistent `e.key` across layouts/IMEs
  // (']' vs 'Dead' vs '«'), so we also accept the physical `e.code`. Direction:
  // BracketRight / ']' → next, BracketLeft / '[' → prev. Returns +1, -1, or 0.
  function bracketDelta(e) {
    if (e.key === "]" || e.code === "BracketRight") return 1;
    if (e.key === "[" || e.code === "BracketLeft") return -1;
    return 0;
  }

  function onKeydown(e) {
    // ----- modal Escape: dismiss the task/form modal -------------------------
    // Highest priority and view-independent — a modal is a true overlay; Esc
    // should always close it first (OLD checked this at the top of its nav
    // handler for the same reason). Don't preventDefault if there's no modal —
    // a stray Esc must still reach a focused pane / the side panel below.
    if (e.key === "Escape" && call("modalOpen")) {
      e.preventDefault();
      call("closeModal");
      return;
    }

    // ----- Cmd-D: toggle the worktree-diff panel for the docked worker -------
    // The browser binds Cmd-D to "add bookmark" — that's the symptom we're
    // fixing: pressing it popped the bookmark dialog instead of the diff. Because
    // this dispatcher runs in the CAPTURE phase (registered with `true`), we see
    // the event BEFORE the browser's default handler, so preventDefault() here
    // suppresses the bookmark. Cmd-D is NEVER terminal input, so we claim it even
    // while a pane is focused (unlike a terminal's Ctrl-D = EOF, which we don't
    // touch). Crew tab only — elsewhere there's no diff panel to toggle, so we
    // leave Cmd-D to the browser there.
    if (e.metaKey && !e.ctrlKey && !e.altKey && !e.shiftKey && (e.key === "d" || e.key === "D")) {
      if (view() !== "crew") return;   // not our chord here → browser keeps Cmd-D
      e.preventDefault();
      e.stopPropagation();
      call("toggleDiff");
      return;
    }

    // ----- tab-switch chords (terminals view) --------------------------------
    // RESERVED for the dashboard even while a pane is focused — in OLD these had
    // to be special-cased to "fall through" the live router; here they're just
    // claimed up front. xterm never needs Cmd-1..9 or Cmd+[ / Cmd+] (those are
    // dashboard navigation, not terminal input), so it's safe to take them.
    //   Cmd+1..9            → select that session by index
    //   Cmd+Shift+[ / ]     → prev / next session
    //   Ctrl+Cmd+[ / ]      → prev / next session (alt chord, matches OLD)
    if (view() === "terminals") {
      // Cmd+digit (NOT Ctrl) → jump to the Nth session.
      if (e.metaKey && !e.ctrlKey && /^[1-9]$/.test(e.key)) {
        e.preventDefault();
        e.stopPropagation();
        call("selectSessionIndex", +e.key - 1);
        return;
      }
      // Ctrl+Cmd+[ / ] → cycle.
      if (e.metaKey && e.ctrlKey) {
        const d = bracketDelta(e);
        if (d) {
          e.preventDefault();
          e.stopPropagation();
          call("cycleSession", d);
          return;
        }
      }
      // Cmd+Shift+[ / ] → cycle (the macOS-native "switch tab" feel).
      if (e.metaKey && e.shiftKey) {
        const d = bracketDelta(e);
        if (d) {
          e.preventDefault();
          e.stopPropagation();
          call("cycleSession", d);
          return;
        }
      }
    }

    // ----- side-panel terminal: Esc / Ctrl-Esc semantics ---------------------
    // The side panel can hold a live terminal. Two distinct gestures, like OLD:
    //   * Ctrl+Esc (or Cmd+Esc) while the SP terminal is live → RELEASE it (drop
    //     live keyboard focus) but keep the panel open. This must beat xterm,
    //     which would otherwise eat Esc as terminal input — hence we run in the
    //     capture phase and preventDefault.
    //   * plain Esc while the panel is open but NOT live → CLOSE the panel.
    // A plain Esc while the SP terminal IS live is deliberately NOT claimed here:
    // it belongs to the pane (Claude uses Esc to cancel), so it falls through to
    // xterm untouched.
    if (e.key === "Escape" && (e.ctrlKey || e.metaKey) && call("sidePanelLive")) {
      e.preventDefault();
      e.stopPropagation();
      call("releaseSidePanel");
      return;
    }
    if (e.key === "Escape" && call("sidePanelOpen") && !call("sidePanelLive")) {
      // No stopPropagation: closing the panel is harmless to let bubble, and a
      // panel that isn't live has no pane competing for this Esc.
      e.preventDefault();
      call("closeSidePanel");
      return;
    }

    // ----- 'x' closes the dock (crew view, dock open, not focused in a pane) --
    // The dock's "press x to close" affordance only makes sense when the keyboard
    // is NOT live inside a dock pane (otherwise 'x' is a character the user is
    // typing into Claude/shell) and NOT in a form field. The focus model makes
    // this clean: paneFocused() is the single source of truth that replaced OLD's
    // dockLive boolean. Bare 'x' only — never Cmd/Ctrl/Alt+x (those are browser
    // chords like Cmd+X cut).
    if (
      view() === "crew" &&
      (e.key === "x" || e.key === "X") &&
      !e.metaKey && !e.ctrlKey && !e.altKey &&
      call("dockOpen") &&
      !paneFocused() &&
      !inFormField()
    ) {
      e.preventDefault();
      call("closeDock");
      return;
    }

    // Anything we didn't claim above is left entirely alone: no preventDefault,
    // no stopPropagation, so it propagates to xterm (live typing) or the browser
    // (copy/paste/find/reload) exactly as it would without this dispatcher.
  }

  // Capture phase so we run before xterm's textarea keydown handler for the few
  // chords we claim (Cmd-D, Ctrl-Esc, the tab-switch chords). Everything else we
  // leave to fall through to that handler.
  window.addEventListener("keydown", onKeydown, true);

  // Return a teardown so tests / a future hot-reload can detach cleanly.
  return function uninstall() {
    window.removeEventListener("keydown", onKeydown, true);
  };
}
