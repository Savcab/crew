// dock.js — the worker terminal dock controller.
//
// The dock is the full-width bottom band of the Crew view: ONE terminal showing
// the docked session's runtime window. Click an agent node in the graph → the dock
// opens on that session.
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
//   - getWorkers() : () => current crew snapshot's agents (for name lookup).
//   - onDockChange(): () => re-highlight the graph node.
//   - toast        : (msg, isErr) => show a toast.

export function createDock({ TerminalPane, api, getWorkers, onDockChange, onShowIdentity, toast }) {
  getWorkers = getWorkers || (() => []);
  onDockChange = onDockChange || (() => {});
  onShowIdentity = onShowIdentity || (() => {});
  toast = toast || (() => {});

  // ---- DOM ---- //
  const dock = document.getElementById('dock');
  const dockTermEl = document.getElementById('dockTerm');   // the (only) terminal host
  const closeBtn = document.getElementById('dockClose');
  dock.setAttribute('role', 'region');
  dock.setAttribute('aria-label', 'Agent terminal dock');
  dock.setAttribute('aria-hidden', 'true');

  // Unicode-only controls are compact visually but need explicit spoken names.
  const namedControls = {
    dockIdentity: 'Show agent identity', dockStart: 'Start agent runtime',
    dockPrev: 'Previous agent', dockNext: 'Next agent',
    dockMax: 'Maximize agent terminal', dockClose: 'Close agent terminal',
  };
  for (const [id, label] of Object.entries(namedControls)) {
    const control = document.getElementById(id);
    if (control) control.setAttribute('aria-label', label);
  }

  // ---- the single TerminalPane (one xterm) ---- //
  // Constructed ONCE, re-pointed via .open(target) on every worker switch.
  const pane = new TerminalPane();
  pane.attach(dockTermEl);
  // Test/debug hook: lets the headless browser check read the live grid + target.
  try { window.__dock = { claudePane: pane, get target() { return dockWorker && (dockWorker.session || dockWorker.name); } }; } catch (e) {}

  // ---- dock state ---- //
  let dockWorker = null;   // the worker-shaped record currently docked (or null)
  let returnFocus = null;  // graph/control that opened a previously hidden dock
  let syncResizeAria = () => {};

  // The runtime pane target = the session NAME; the backend resolves its window.
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

  // ---------- dock tabs: one tab per tmux window of the docked session ---- //
  // "+" creates a new shell window in the BASE session (the agent's own screen
  // never moves — grouped sessions select windows independently); clicking a
  // tab switches only this dock view. Tabs refresh on open and on every
  // snapshot sync, so windows created from inside the terminal appear too.
  const tabsEl = document.getElementById('dockTabs');
  let tabsGen = 0;   // stale-response guard across worker switches

  function clearTabs() {
    tabsGen += 1;
    if (tabsEl) tabsEl.innerHTML = '';
  }

  async function refreshTabs() {
    if (!tabsEl || !api || !api.ptyWindows) return;
    if (!dockWorker || dockWorker.session_alive === false) { clearTabs(); return; }
    const gen = ++tabsGen;
    let j;
    try { j = await api.ptyWindows(claudeTarget(), pane.ptyId || undefined); }
    catch (e) { return; }
    if (gen !== tabsGen || !dockWorker) return;
    if (!j || !j.ok) { if (tabsEl) tabsEl.innerHTML = ''; return; }
    renderTabs(j.windows || []);
  }

  function renderTabs(windows) {
    tabsEl.innerHTML = '';
    for (const w of windows) {
      const b = document.createElement('button');
      b.className = 'dock-tab' + (w.active ? ' active' : '');
      b.setAttribute('role', 'tab');
      b.setAttribute('aria-selected', w.active ? 'true' : 'false');
      b.dataset.window = w.id;
      b.textContent = w.name || w.id;
      b.title = `switch this view to tmux window ${w.name || w.id}`;
      b.onclick = () => selectTab(w.id);
      tabsEl.appendChild(b);
    }
    const add = document.createElement('button');
    add.className = 'dock-tab add';
    add.id = 'dockTabAdd';
    add.textContent = '+';
    add.title = "new shell tab in this agent's session";
    add.setAttribute('aria-label', 'New terminal tab');
    add.onclick = addTab;
    tabsEl.appendChild(add);
  }

  async function selectTab(windowId) {
    if (!pane.ptyId) return;
    try {
      const r = await api.ptyWindowSelect(pane.ptyId, windowId);
      if (r && r.ok) refreshTabs();
      else toast((r && r.error) || 'could not switch tab', true);
    } catch (e) { toast('could not switch tab', true); }
  }

  async function addTab() {
    if (!dockWorker) return;
    try {
      const r = await api.ptyWindowCreate(claudeTarget());
      if (r && r.ok && r.window) await selectTab(r.window.id);
      else toast((r && r.error) || 'could not create tab', true);
    } catch (e) { toast('could not create tab', true); }
  }

  // ---------- open / close ---------- //
  function dockWorkerByName(name) {
    if (!name) return;
    const w = (getWorkers() || []).find(x => x.name === name);
    if (w) openDock(w);
  }

  function renderDockWorker(w) {
    dock.setAttribute('aria-label', `${w.name} agent terminal`);
    document.getElementById('dockName').textContent = w.name;
    const st = w.live_status || (w.session_alive ? 'unknown' : 'down');
    document.getElementById('dockDot').style.cssText = 'background:' + (statusColor(st) || '#6e7681');
    document.getElementById('dockMeta').textContent =
      (w.role ? w.role + ' · ' : '')
      + `${w.runtime || 'claude'} · ${statusLabel(st, w)}`;
    // Offer to start whenever the configured runtime process is absent.
    const startBtn = document.getElementById('dockStart');
    if (startBtn) {
      startBtn.style.display = w.runtime_alive ? 'none' : '';
      startBtn.disabled = false;
    }
  }

  function openDock(w) {
    if (!dock.classList.contains('show')) {
      const active = document.activeElement;
      returnFocus = active && !dock.contains(active) ? active : null;
    }
    dockWorker = w;
    dock.setAttribute('aria-hidden', 'false');
    renderDockWorker(w);
    dock.classList.add('show');
    syncResizeAria();
    updateFocusUI();
    // RE-POINT the terminal at the new session: term.js tears down the old PTY
    // stream and opens a fresh `tmux attach` to this session's claude window.
    // EventSource retries failed GETs indefinitely. A snapshot-known down session
    // has nothing to attach to, so stay disconnected until Start succeeds instead
    // of creating a background 404/reconnect loop.
    pane.open(w.session_alive === false ? null : claudeTarget());
    onDockChange();   // → main.js: ring the graph node + re-render the board card
    // Tabs: once now, once shortly after (the PTY id lands asynchronously and
    // marks which window this view shows); the snapshot sync keeps them live.
    refreshTabs();
    setTimeout(refreshTabs, 700);
  }

  // Snapshot polling replaces worker records with fresh objects. Keep an open
  // dock's header/start affordance in sync without tearing down and recreating
  // its PTY on every poll. Only a real session up/down transition repoints it.
  function syncDockedWorker() {
    if (!dockWorker) return;
    const latest = (getWorkers() || []).find(w => w.name === dockWorker.name);
    if (!latest) { closeDock(); return; }
    if ((latest._guid || null) !== (dockWorker._guid || null)) {
      closeDock();
      return;
    }
    const wasSessionAlive = dockWorker.session_alive !== false;
    const isSessionAlive = latest.session_alive !== false;
    dockWorker = latest;
    renderDockWorker(latest);
    if (wasSessionAlive !== isSessionAlive) {
      pane.open(isSessionAlive ? claudeTarget() : null);
    }
    onDockChange();
    refreshTabs();
  }

  function closeDock() {
    const focusTarget = returnFocus && returnFocus.isConnected
      ? returnFocus : document.getElementById('addAgentBtn');
    returnFocus = null;
    dock.classList.remove('show');
    setDockLive(false);
    // Close the PTY stream while hidden (server detects the dropped SSE → kills the
    // grouped view session + the tmux-attach child). open(null) = close + reset.
    pane.open(null);
    dockWorker = null;
    clearTabs();
    dock.setAttribute('aria-hidden', 'true');
    dock.setAttribute('aria-label', 'Agent terminal dock');
    onDockChange();   // → main.js: clear the graph ring + the card highlight
    if (focusTarget && focusTarget.focus) focusTarget.focus();
  }

  // ---------- head buttons ---------- //
  closeBtn.onclick = closeDock;

  // ⓘ identity: show who this agent is + its channels (read-only card).
  const idBtn = document.getElementById('dockIdentity');
  if (idBtn) idBtn.onclick = () => { if (dockWorker) onShowIdentity(dockWorker); };

  // ▶ start session/runtime, then reattach so boot is visible here.
  const startBtn = document.getElementById('dockStart');
  if (startBtn) startBtn.onclick = async () => {
    if (!dockWorker || !api) return;
    const startingWorker = dockWorker;
    const startingName = startingWorker.name;
    const startingTarget = startingWorker.session || startingName;
    const startingGuid = startingWorker._guid;
    const stillShowingStartedWorker = () => !!dockWorker
      && dockWorker._guid === startingGuid
      && dockWorker.name === startingName
      && (dockWorker.session || dockWorker.name) === startingTarget;
    startBtn.disabled = true;
    try {
      const r = await api.agentStart({ name: startingName });
      if (r && r.ok) {
        toast(`starting ${startingName}…`);
        // The operator may close/cycle while the request is in flight. The
        // completion still belongs to its initiating worker and must not relabel,
        // reattach, or disable whatever worker is currently docked.
        if (!stillShowingStartedWorker()) return;
        startBtn.style.display = 'none';
        document.getElementById('dockMeta').textContent =
          (startingWorker.role ? startingWorker.role + ' · ' : '') + 'starting…';
        pane.open(startingTarget);   // session exists now → SSE attach succeeds
      } else {
        toast((r && r.error) || 'start failed', true);
        if (stillShowingStartedWorker()) startBtn.disabled = false;
      }
    } catch (e) {
      toast(`start ${startingName} failed`, true);
      if (stillShowingStartedWorker()) startBtn.disabled = false;
    }
  };

  // ‹ / › : cycle to the prev/next agent without going back to the graph.
  function cycle(delta) {
    const list = getWorkers() || [];
    if (!list.length || !dockWorker) return;
    let i = list.findIndex(w => w.name === dockWorker.name);
    if (i < 0) i = 0;
    openDock(list[(i + delta + list.length) % list.length]);
  }
  const prev = document.getElementById('dockPrev');
  const next = document.getElementById('dockNext');
  if (prev) prev.onclick = () => cycle(-1);
  if (next) next.onclick = () => cycle(1);

  // ⤢ maximize / restore: toggle a near-fullscreen height so the live terminal is
  // the star of the screen (the graph collapses to a sliver behind it). term.js's
  // ResizeObserver re-fits the xterm grid + pushes the new size to the PTY.
  const maxBtn = document.getElementById('dockMax');
  if (maxBtn) {
    maxBtn.setAttribute('aria-pressed', 'false');
    maxBtn.onclick = () => {
      const maximized = dock.classList.toggle('max');
      maxBtn.setAttribute('aria-pressed', maximized ? 'true' : 'false');
      maxBtn.setAttribute('aria-label', maximized
        ? 'Restore agent terminal' : 'Maximize agent terminal');
      dock.style.height = ''; // let the .max CSS height win
      setTimeout(() => { syncResizeAria(); pane.fit(); }, 0);
    };
  }

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
    // Ctrl+Esc promises to hand the keyboard back to dashboard chrome. Put it on
    // a concrete useful control rather than leaving focus on <body>.
    if (closeBtn && closeBtn.focus) closeBtn.focus();
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
    const STEP = 24;
    let dragging = false;

    handle.setAttribute('role', 'separator');
    handle.setAttribute('aria-orientation', 'horizontal');
    handle.setAttribute('aria-label', 'Resize agent terminal');
    handle.setAttribute('tabindex', '0');

    function limits() {
      // The second-window terminal (/?view=term) has no #crew wrapper — fall
      // back to the viewport (its dock fills the window; resize is hidden).
      const wrap = document.getElementById('crew');
      const height = wrap
        ? wrap.getBoundingClientRect().height : window.innerHeight;
      return { min: 120, max: Math.max(120, height - 120) };
    }
    function currentHeight() {
      const inline = parseFloat(dock.style.height);
      return Number.isFinite(inline) ? inline : dock.getBoundingClientRect().height;
    }
    function setHeight(value) {
      const { min, max } = limits();
      const height = Math.round(Math.max(min, Math.min(max, value)));
      dock.classList.remove('max');
      if (maxBtn) {
        maxBtn.setAttribute('aria-pressed', 'false');
        maxBtn.setAttribute('aria-label', 'Maximize agent terminal');
      }
      dock.style.height = height + 'px';
      updateAria(height, min, max);
    }
    function updateAria(height, min, max) {
      if (min === undefined || max === undefined) ({ min, max } = limits());
      handle.setAttribute('aria-valuemin', String(min));
      handle.setAttribute('aria-valuemax', String(max));
      handle.setAttribute('aria-valuenow', String(Math.round(height)));
    }
    const initialLimits = limits();
    handle.setAttribute('aria-valuemin', String(initialLimits.min));
    handle.setAttribute('aria-valuemax', String(initialLimits.max));
    syncResizeAria = () => updateAria(currentHeight());

    handle.addEventListener('mousedown', e => {
      dragging = true; dock.classList.add('resizing');
      document.body.style.userSelect = 'none'; e.preventDefault();
    });
    // capture phase: xterm stops mouseup on its canvas, so a bubble listener
    // misses a release over the terminal and the resize sticks to the cursor.
    window.addEventListener('mousemove', e => {
      if (!dragging) return;
      const wrap = document.getElementById('crew');
      const bottom = wrap
        ? wrap.getBoundingClientRect().bottom : window.innerHeight;
      setHeight(bottom - e.clientY);
    }, true);
    function stopDragging() {
      if (!dragging) return; dragging = false;
      dock.classList.remove('resizing'); document.body.style.userSelect = '';
      pane.fit();
    }
    window.addEventListener('mouseup', stopDragging, true);
    window.addEventListener('blur', stopDragging);
    handle.addEventListener('keydown', e => {
      const { min, max } = limits();
      let height = currentHeight();
      if (e.key === 'ArrowUp') height += STEP;
      else if (e.key === 'ArrowDown') height -= STEP;
      else if (e.key === 'PageUp') height += STEP * 3;
      else if (e.key === 'PageDown') height -= STEP * 3;
      else if (e.key === 'Home') height = min;
      else if (e.key === 'End') height = max;
      else return;
      e.preventDefault();
      setHeight(height);
      pane.fit();
    });
  })();

  // ---- public surface ---- //
  return {
    openDock,
    closeDock,
    dockWorkerByName,
    syncDockedWorker,
    dockOpen: () => dock.classList.contains('show'),
    paneFocused,
    detach,
    isLive: () => dock.classList.contains('live'),
    dockedWorker: () => dockWorker,
    claudeTarget,
    focusedTarget: claudeTarget,   // single pane → the focus target IS the claude target
  };
}

// ---- status color palette (mirrors the graph node dot states) ----
const SBADGE = { working: '#3fb950', needs_input: '#d29922', idle: '#6e7681', unknown: '#8b949e', not_started: '#58a6ff', down: '#484f58' };
const STATUS_LABEL = { working: 'working…', needs_input: 'needs you', idle: 'idle', unknown: 'state unknown', not_started: 'runtime not started', down: 'session down' };
function statusColor(status) { return SBADGE[status]; }
function statusLabel(status, worker) {
  if (status === 'down' && worker && worker.session_alive) return 'runtime down';
  return STATUS_LABEL[status] || status || 'state unknown';
}
