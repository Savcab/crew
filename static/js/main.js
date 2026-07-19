// main.js — boot the crew dashboard: build the controllers, wire the + Agent
// button + the refresh selector, run the graph poll, and install the key
// dispatcher. The dashboard is now ONE surface: the agent graph, plus the
// bottom dock that opens an agent's live terminal when you click it.
//
// This is the only module that knows about all the others (graph / dock / modal /
// keys / term / api); they stay decoupled leaves and call back through the small
// handler bags built here.

import { api } from './api.js';
import { TerminalPane } from './term.js';
import { renderGraph, highlightDockedNode } from './graph.js';
import { createDock } from './dock.js';
import { createModalController } from './modal.js';
import { installKeys } from './keys.js';

function esc(s) {
  return (s || '').replace(/[&<>"]/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}

let toastTimer = null;
function toast(msg, err) {
  const t = document.getElementById('toast');
  if (!t) return;
  t.setAttribute('role', err ? 'alert' : 'status');
  t.textContent = msg;
  t.className = 'toast show' + (err ? ' err' : '');
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove('show'), 3500);
}

// ---- top-level state ----
let graphSnap = { agents: [], edges: [] };
let lastSig = '';
let graphLoadPromise = null;
let graphReloadQueued = false;

const modal = createModalController({
  api, toast,
  refresh: () => loadGraph(true),
});

const dock = createDock({
  TerminalPane, api,
  getWorkers: () => graphSnap.agents || [],
  onDockChange: () => highlightDockedNode((dock.dockedWorker() || {}).name),
  onShowIdentity: (w) => modal.openIdentity(w, graphSnap.edges || []),
  toast,
});

// ---- graph handlers (graph.js calls back into these) ----
// Click a node → open its big terminal. Drag the ● handle from one node onto
// another → describe the new edge. Agents are durable: there is intentionally no
// one-click delete here (remove via `crew remove-agent <name>` on the CLI).
const graphHandlers = {
  onDockAgent: (a) => dock.openDock(a),
  onConnect: (fromName, toName) => modal.openConnect(fromName, toName),
  onEditEdge: (e) => modal.openEditEdge(e),
  onCreateAgent: () => modal.openCreateAgent(),
};

// ---- render + poll ----
function renderCrew() {
  renderGraph(graphSnap, graphHandlers, { dockedName: (dock.dockedWorker() || {}).name });
}

function setGraphUnavailable(message) {
  const g = document.getElementById('cgraph');
  if (!g) return;
  // The error view replaces the rendered canvas.  Invalidate the data
  // signature too: after the backend recovers, its first good snapshot may be
  // byte-for-byte identical to the last one we rendered.  Keeping that cached
  // signature would suppress renderCrew() and strand this error view forever.
  lastSig = '';
  g.setAttribute('aria-busy', 'false');
  g.innerHTML = '<div class="empty" id="graphStatus" role="status" '
    + 'aria-live="polite">backend unavailable: ' + esc(message) + '</div>';
}

async function loadGraph(force) {
  // Modal refreshes and the background scheduler share one request. Coalesce
  // ordinary polls, but remember a forced post-mutation refresh: the active
  // request may have captured pre-mutation state, and automatic polling may be
  // switched off, so silently dropping that refresh can strand stale UI.
  if (graphLoadPromise) {
    if (force) graphReloadQueued = true;
    return graphLoadPromise;
  }
  graphLoadPromise = (async () => {
    let j;
    try { j = await api.graphSnapshot(); }
    catch (e) {
      setGraphUnavailable((e && e.message) || 'snapshot request failed');
      return;
    }
    if (!j || !j.ok) {
      setGraphUnavailable(
        (j && j.error) || 'is MorphDB + the crew server running?');
      return;
    }
    const g = document.getElementById('cgraph');
    if (g) g.setAttribute('aria-busy', 'false');
    graphSnap = j;
    const sig = JSON.stringify({ a: j.agents, e: j.edges });
    if (force || sig !== lastSig) { lastSig = sig; renderCrew(); }
    dock.syncDockedWorker();
    updateMeta();
    updatePendingBadge();
  })();
  try { return await graphLoadPromise; }
  finally {
    graphLoadPromise = null;
    if (graphReloadQueued) {
      graphReloadQueued = false;
      await loadGraph(true);
    }
  }
}

// ---- WAVE 4: pending-approval tray ----
function updatePendingBadge() {
  const btn = document.getElementById('pendingBtn');
  const badge = document.getElementById('pendingBadge');
  if (!btn || !badge) return;
  const n = graphSnap.pending_count || 0;
  btn.style.display = n > 0 ? '' : 'none';
  badge.textContent = String(n);
}
{
  const pendingBtn = document.getElementById('pendingBtn');
  if (pendingBtn) pendingBtn.onclick = async () => {
    let j;
    try { j = await api.pendingList(); }
    catch (e) { toast('request failed', true); return; }
    if (!j || !j.ok) { toast((j && j.error) || 'failed', true); return; }
    modal.openPending(j.pending || []);
  };
}

function updateMeta() {
  const meta = document.getElementById('meta');
  if (!meta) return;
  const agents = graphSnap.agents || [];
  const running = agents.filter(a => a.runtime_alive).length;
  meta.textContent = agents.length
    ? `${agents.length} agent${agents.length === 1 ? '' : 's'} · ${running} running`
    : '';
}

// ---- poll loop (rate from the header selector) ----
let pollTimer = null;
let pollRate = 1500;
let pollInFlight = false;
async function runPoll(force) {
  pollInFlight = true;
  try { await loadGraph(!!force); }
  finally {
    pollInFlight = false;
    pollTimer = pollRate > 0 ? setTimeout(runPoll, pollRate) : null;
  }
}
function startPoll() {
  if (pollTimer) clearTimeout(pollTimer);
  pollTimer = null;
  runPoll(true);
}
{
  const rate = document.getElementById('rate');
  if (rate) rate.onchange = () => {
    pollRate = parseInt(rate.value, 10) || 0;
    if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
    // If a request is already running its finally block observes the new rate
    // and schedules exactly one successor after that request completes.
    if (pollRate > 0 && !pollInFlight) {
      pollTimer = setTimeout(runPoll, pollRate);
    }
  };
}

// ---- + Agent button ----
{
  const addBtn = document.getElementById('addAgentBtn');
  if (addBtn) addBtn.onclick = () => modal.openCreateAgent();
}

// ---- key dispatcher ----
// Only the chrome chords that must NOT be typed into a pane: Esc closes the modal,
// bare 'x' closes the dock when the terminal isn't the live keyboard target.
installKeys({
  view: () => 'crew',
  paneFocused: () => !!(dock.paneFocused && dock.paneFocused()),
  modalOpen: () => modal.isOpen(),
  closeModal: () => modal.closeModal(),
  dockOpen: () => dock.dockOpen(),
  closeDock: () => dock.closeDock(),
  detachDock: () => dock.detach(),
});

window.addEventListener('resize', renderCrew);

// ---- boot ----
startPoll();
