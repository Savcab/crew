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
// ctx contract (the sibling modules — main.js, dock.js, modal.js — provide these;
// all optional, missing ones are treated as "feature absent"):
//   view()            -> 'crew'                       (the only view now)
//   paneFocused()     -> bool   is the keyboard live inside an xterm pane right
//                               now? (a focused TerminalPane). When true we only
//                               claim the few chords that must beat xterm —
//                               everything else falls through to the pane.
//   modalOpen()       -> bool   is the form modal showing?
//   closeModal()              dismiss the modal.
//   dockOpen()        -> bool   is the agent terminal dock showing?
//   closeDock()               close the dock.
//
// The handler is registered in the CAPTURE phase on `window` so it runs BEFORE
// xterm's textarea keydown handler. For chords we DON'T claim we never call
// preventDefault, so they propagate down to xterm normally and live typing is
// unaffected.

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
