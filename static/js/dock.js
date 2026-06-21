// dock.js — the worker terminal dock controller.
//
// The dock is the full-width bottom band of the Crew view: ONE terminal showing
// the docked session's claude window. Click a worker/manager/independent node in
// the graph (or a task card) → the dock opens on that session.
//
// SINGLE-PANE (the dashboard's bolted-on shell tabs were REMOVED). With the PTY
// transport every pane is a real `tmux attach` client, so tmux's OWN windows and
// splits work INSIDE the dock terminal — `Ctrl-b c` (new window), `Ctrl-b "` /
// `Ctrl-b %` (split), `Ctrl-b n/p` (switch). The old `shell-N` tmux windows +
// /api/shell* routes + the tab bar were a workaround for the scraper only being
// able to render one snapshot; the PTY model makes them redundant, so they're gone.
//
// Each pane is a TerminalPane (term.js): one xterm.js Terminal bound to a
// /api/pty/stream. xterm + tmux handle scrollback / scroll / cursor / selection /
// mouse / resize natively — no render plumbing here.
//
// Dependencies injected by main.js (createDock) to stay decoupled:
//   - TerminalPane : class from term.js (attach / open(target|null) / setLive / fit / dispose).
//   - sidepanel    : { openDiffPanel(target, name) }.
//   - getWorkers() : () => current crew snapshot's workers (for name lookup).
//   - onDockChange(): () => re-highlight the graph node + re-render the board.
//   - onViewTask(id): () => open the task modal.
//   - toast        : (msg, isErr) => show a toast.

export function createDock({ TerminalPane, sidepanel, getWorkers, onDockChange, onViewTask, toast }) {
  getWorkers = getWorkers || (() => []);
  onDockChange = onDockChange || (() => {});
  onViewTask = onViewTask || (() => {});
  toast = toast || (() => {});

  // ---- DOM ---- //
  const dock = document.getElementById('dock');
  const dockTermEl = document.getElementById('dockTerm');   // the (only) terminal host

  // ---- the single TerminalPane (one xterm) ---- //
  // Constructed ONCE, re-pointed via .open(target) on every worker switch.
  const pane = new TerminalPane();
  pane.attach(dockTermEl);
  // Test/debug hook: lets the headless browser check read the live grid + target.
  try { window.__dock = { claudePane: pane, get target() { return dockWorker && (dockWorker.session || dockWorker.name); } }; } catch (e) {}

  // ---- dock state ---- //
  let dockWorker = null;   // the worker-shaped record currently docked (or null)

  // The claude PANE target = the session NAME; the BACKEND resolve_target()s it to
  // the live claude pane (NEVER the stale crewdb pane_id — hard constraint).
  function claudeTarget() { return dockWorker ? (dockWorker.session || dockWorker.name) : null; }

  // ---------- focus / live UI ---------- //
  // xterm sends keystrokes whenever its element has DOM focus; "live" = focused +
  // the green border overlay (CSS `#dock.live .dockpane.focused`).
  function updateFocusUI() {
    dockTermEl.classList.toggle('focused', true);
    pane.setLive(dock.classList.contains('live'));
  }
  function setDockLive(on) { dock.classList.toggle('live', on); updateFocusUI(); }
  function focusPane() { setDockLive(true); }

  // ---------- open / close ---------- //
  function dockWorkerByName(name) {
    if (!name) return;
    const w = (getWorkers() || []).find(x => x.name === name);
    if (w) openDock(w);
  }

  function openDock(w, opts) {
    opts = opts || {};
    const solo = !!opts.solo;   // independent (non-crew) session: no task
    dockWorker = w;
    document.getElementById('dockName').textContent = w.name;
    const busy = w.current_task_id != null;
    document.getElementById('dockDot').style.cssText =
      'background:' + (busy ? (statusColor(w.current_task_status) || '#3fb950') : '#6e7681');
    document.getElementById('dockMeta').textContent =
      solo ? 'independent session' : busy ? ('#' + w.current_task_id + ' ' + (w.current_task_title || '')) : 'idle';
    const tb = document.getElementById('dockTask');
    if (busy) { tb.style.display = ''; tb.onclick = () => onViewTask(w.current_task_id); }
    else tb.style.display = 'none';
    dock.classList.add('show');
    updateFocusUI();
    // RE-POINT the terminal at the new session: term.js tears down the old PTY
    // stream and opens a fresh `tmux attach` to this session's claude window.
    pane.open(claudeTarget());
    onDockChange();   // → main.js: ring the graph node + re-render the board card
  }

  function closeDock() {
    dock.classList.remove('show');
    setDockLive(false);
    // Close the PTY stream while hidden (server detects the dropped SSE → kills the
    // grouped view session + the tmux-attach child). open(null) = close + reset.
    pane.open(null);
    dockWorker = null;
    onDockChange();   // → main.js: clear the graph ring + the card highlight
  }

  // ---------- head buttons ---------- //
  // ⊞ diff → worktree diff in the side panel (sidepanel.js owns the render).
  document.getElementById('dockDiff').onclick = () => {
    const t = claudeTarget(); if (!t) return;
    sidepanel.openDiffPanel(t, dockWorker ? dockWorker.name : t);
  };
  document.getElementById('dockClose').onclick = closeDock;
  // ⤢ maximize / restore: toggle a near-fullscreen height so the live terminal is
  // the star of the screen (the graph collapses to a sliver behind it). term.js's
  // ResizeObserver re-fits the xterm grid + pushes the new size to the PTY.
  const maxBtn = document.getElementById('dockMax');
  if (maxBtn) maxBtn.onclick = () => {
    dock.classList.toggle('max');
    dock.style.height = '';   // let the .max CSS height win (clear any drag-set inline height)
  };

  // ---------- per-pane focus wiring ---------- //
  // Click into the terminal → go LIVE. CAPTURE-phase mousedown is the reliable
  // signal: xterm handles+stops mouseup on its canvas (so a bubbling listener never
  // fires) and already holds textarea focus (so focusin doesn't re-fire) — only a
  // capture-phase mousedown sees the click before xterm consumes it. Skip when a
  // text selection is in progress so a drag-copy isn't hijacked.
  dockTermEl.addEventListener('mousedown', () => {
    if ((window.getSelection() + '').length === 0) focusPane();
  }, true);

  // detach(): drop live focus (Ctrl-Esc). Blurs the xterm so it stops capturing.
  function detach() {
    setDockLive(false);
    if (document.activeElement && dock.contains(document.activeElement)) document.activeElement.blur();
  }
  // paneFocused(): is the keyboard live inside the dock terminal right now?
  function paneFocused() {
    return dock.classList.contains('live')
      && !!document.activeElement && dock.contains(document.activeElement);
  }

  // ---------- top-edge drag-resize ----------
  // Drag the dock's top edge to resize its height; term.js's ResizeObserver re-fits
  // the grid and pushes the new size to the PTY (→ tmux resizes the window).
  (function () {
    const handle = document.getElementById('dockResize');
    if (!handle) return;
    let dragging = false;
    handle.addEventListener('mousedown', e => {
      dragging = true; dock.classList.add('resizing');
      document.body.style.userSelect = 'none'; e.preventDefault();
    });
    window.addEventListener('mousemove', e => {
      if (!dragging) return;
      const wrap = document.getElementById('crew');
      const r = wrap.getBoundingClientRect();
      let h = r.bottom - e.clientY;
      h = Math.max(120, Math.min(r.height - 120, h));
      dock.style.height = h + 'px';
    });
    window.addEventListener('mouseup', () => {
      if (!dragging) return; dragging = false;
      dock.classList.remove('resizing'); document.body.style.userSelect = '';
    });
  })();

  // ---- public surface ---- //
  return {
    openDock,
    closeDock,
    dockWorkerByName,
    dockOpen: () => dock.classList.contains('show'),
    paneFocused,
    detach,
    isLive: () => dock.classList.contains('live'),
    dockedWorker: () => dockWorker,
    claudeTarget,
    focusedTarget: claudeTarget,   // single pane → the focus target IS the claude target
  };
}

// ---- status color palette (mirrors the graph/board SBADGE) ----
const SBADGE = {
  ready: '#6e7681', in_progress: '#3fb950', agent_testing: '#39c5cf',
  human_review: '#d29922', teammate_review: '#bc8cff', merged: '#8957e5',
};
function statusColor(status) { return SBADGE[status]; }
