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
  t.textContent = msg;
  t.className = 'toast show' + (err ? ' err' : '');
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove('show'), 3500);
}

// ---- top-level state ----
let graphSnap = { agents: [], edges: [] };
let lastSig = '';

const modal = createModalController({
  api, toast,
  refresh: () => loadGraph(true),
});

const dock = createDock({
  TerminalPane, api,
  getWorkers: () => graphSnap.agents || [],
  onDockChange: () => highlightDockedNode((dock.dockedWorker() || {}).name),
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

async function loadGraph(force) {
  let j;
  try { j = await api.graphSnapshot(); }
  catch (e) { return; }
  if (!j || !j.ok) {
    const g = document.getElementById('cgraph');
    if (g) g.innerHTML = '<div class="empty" style="padding:40px">backend unavailable: '
      + esc((j && j.error) || 'is MorphDB + the crew server running?') + '</div>';
    return;
  }
  graphSnap = j;
  const sig = JSON.stringify({ a: j.agents, e: j.edges });
  if (force || sig !== lastSig) { lastSig = sig; renderCrew(); }
  updateMeta();
}

function updateMeta() {
  const meta = document.getElementById('meta');
  if (!meta) return;
  const agents = graphSnap.agents || [];
  const running = agents.filter(a => a.alive).length;
  meta.textContent = agents.length
    ? `${agents.length} agent${agents.length === 1 ? '' : 's'} · ${running} running`
    : '';
}

// ---- poll loop (rate from the header selector) ----
let pollTimer = null;
let pollRate = 1500;
function startPoll() {
  loadGraph(true);
  if (pollTimer) clearInterval(pollTimer);
  if (pollRate > 0) pollTimer = setInterval(loadGraph, pollRate);
}
{
  const rate = document.getElementById('rate');
  if (rate) rate.onchange = () => {
    pollRate = parseInt(rate.value, 10) || 0;
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
    if (pollRate > 0) pollTimer = setInterval(loadGraph, pollRate);
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
});

window.addEventListener('resize', renderCrew);

// ---- boot ----
startPoll();
