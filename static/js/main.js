// main.js — boot: construct the controllers, wire the buttons + tabs + key
// dispatcher, and run the graph poll. This is the ONE module that knows about all
// the others (graph/dock/sidepanel/modal/keys/term/api); they stay decoupled
// leaves and call back through the small handler bags built here.

import { api } from './api.js';
import { TerminalPane } from './term.js';
import { renderGraph, highlightDockedNode } from './graph.js';
import { createDock } from './dock.js';
import { createSidePanel } from './sidepanel.js';
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
let view = 'crew';
let graphSnap = { agents: [], edges: [], independent: [] };
let lastSig = '';

// ---- terminals-tab state (ported from the ng dashboard) ----
let sessions = [];
let sel = null;
const STLABEL = { idle: 'idle', working: 'working', needs_input: 'needs input' };
const RANK = { needs_input: 0, working: 1, idle: 2 };

// sidepanel expects an api shaped {getPr,getDiff,ensureShell} + getState().crewSnap.workers
const sidepanelApi = {
  getPr: (url) => api.pr(url),
  getDiff: (target, opts) => api.diff({ t: target, pr: (opts || {}).pr }),
  ensureShell: (session) => api.shell(session),
};
const sidepanel = createSidePanel({
  api: sidepanelApi, TerminalPane, esc, toast,
  getState: () => ({ crewSnap: { workers: graphSnap.agents } }),
});

const modal = createModalController({
  api, toast,
  refresh: () => loadGraph(true),
});

const dock = createDock({
  TerminalPane, sidepanel,
  getWorkers: () => graphSnap.agents || [],
  onDockChange: () => highlightDockedNode((dock.dockedWorker() || {}).name),
  onViewTask: () => {},
  toast,
});

// ---- graph handlers (graph.js calls back into these) ----
// Click a node → open its big terminal. Drag the ● handle from one node onto
// another → describe the new edge. No "connect mode" — it's direct manipulation.
const graphHandlers = {
  onDockAgent: (a) => dock.openDock(a),
  onConnect: (fromName, toName) => modal.openConnect(fromName, toName),
  onEditEdge: (e) => modal.openEditEdge(e),
  onRemoveAgent: (a) => {
    if (!confirm(`Remove agent "${a.name}"? This deletes it from the crew and kills its session (its home dir + files stay on disk).`)) return;
    api.agentRemove(a.name).then(r => {
      if (r && r.ok === false) { toast(r.error || 'remove failed', true); return; }
      toast(`removed ${a.name}`); loadGraph(true);
    }).catch(() => toast('remove failed', true));
  },
  onAdopt: (s) => modal.openAdopt(s),
};

// ---- crew view: render + poll ----
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
  const sig = JSON.stringify({ a: j.agents, e: j.edges, i: j.independent });
  if (force || sig !== lastSig) { lastSig = sig; renderCrew(); }
}

let pollTimer = null;
function startPoll() {
  loadGraph(true);
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(loadGraph, 1500);
}
function stopPoll() { if (pollTimer) { clearInterval(pollTimer); pollTimer = null; } }

// ---- terminals view (single xterm re-pointed at the selected session) ----
const termWrap = document.getElementById('term-wrap');
const termPane = new TerminalPane();
termPane.attach(document.getElementById('term'));

function renderSide() {
  const side = document.getElementById('side');
  if (!side) return;
  side.innerHTML = '';
  for (const s of sessions) {
    const d = document.createElement('div');
    d.className = 'tab' + (s.target === sel ? ' sel' : '');
    const badge = s.status === 'needs_input' ? '<span class="badge">!</span>' : '';
    d.innerHTML = `<div class="name"><span class="dot ${s.status}"></span>${esc(s.session)}${badge}
      <span class="st">${STLABEL[s.status] || ''}</span></div>
      <div class="cwd">${esc(s.cwd_short || '')}</div>`;
    d.onclick = () => selectSession(s.target);
    side.appendChild(d);
  }
}
function selectSession(t) {
  sel = t;
  const s = sessions.find(x => x.target === t);
  const tname = document.getElementById('tname');
  const ttgt = document.getElementById('ttgt');
  if (tname) { tname.textContent = s ? s.session : ''; tname.className = ''; }
  if (ttgt) ttgt.textContent = s ? s.target : '';
  termPane.open(s ? s.session : t);
  if (termWrap) termWrap.classList.add('live');
  termPane.setLive(true);
  renderSide();
  const keysBar = document.getElementById('keys');
  if (keysBar) keysBar.classList.toggle('show', !!s && s.status === 'needs_input');
}
async function loadSessions() {
  let j;
  try { j = await api.sessions(); }
  catch (e) { return; }
  sessions = (j && j.sessions) || [];
  sessions.sort((a, b) => (RANK[a.status] - RANK[b.status]) || a.session.localeCompare(b.session));
  if (sel && !sessions.find(s => s.target === sel)) sel = null;
  if (!sel && sessions.length) selectSession(sessions[0].target);
  const need = sessions.filter(s => s.status === 'needs_input').length;
  const meta = document.getElementById('meta');
  if (meta) meta.textContent = sessions.length + ' Claude' + (sessions.length === 1 ? '' : 's')
    + (need ? '  ·  ' + need + ' need input' : '');
  renderSide();
}
function selectSessionIndex(i) { if (i >= 0 && i < sessions.length) selectSession(sessions[i].target); }
function cycleSession(delta) {
  if (!sessions.length) return;
  let i = sessions.findIndex(s => s.target === sel);
  if (i < 0) i = 0;
  i = (i + delta + sessions.length) % sessions.length;
  selectSession(sessions[i].target);
}
(function wireKeyButtons() {
  const keysBar = document.getElementById('keys');
  if (!keysBar) return;
  keysBar.querySelectorAll('.k').forEach(b => b.onclick = () => {
    if (!sel) return; api.key({ t: sel, key: b.dataset.key });
  });
})();

// ---- header tabs / view switch ----
function setView(v) {
  view = v;
  const term = document.getElementById('terminals');
  const crew = document.getElementById('crew');
  if (term) term.classList.toggle('on', v === 'terminals');
  if (crew) crew.classList.toggle('on', v === 'crew');
  const vtTerm = document.getElementById('vt-term');
  const vtCrew = document.getElementById('vt-crew');
  if (vtTerm) vtTerm.classList.toggle('on', v === 'terminals');
  if (vtCrew) vtCrew.classList.toggle('on', v === 'crew');
  if (v === 'crew') startPoll();
  else { stopPoll(); loadSessions(); }
}
{
  const vtTerm = document.getElementById('vt-term');
  const vtCrew = document.getElementById('vt-crew');
  if (vtTerm) vtTerm.onclick = () => setView('terminals');
  if (vtCrew) vtCrew.onclick = () => setView('crew');
}

// ---- + Agent button ----
{
  const addBtn = document.getElementById('addAgentBtn');
  if (addBtn) addBtn.onclick = () => modal.openCreateAgent();
}

// ---- broadcast bar ----
(function wireBroadcast() {
  const bcast = document.getElementById('bcast');
  const bcastMsg = document.getElementById('bcastMsg');
  const bcastBtn = document.getElementById('bcastBtn');
  const bcastSend = document.getElementById('bcastSend');
  if (!bcast || !bcastMsg || !bcastBtn) return;
  bcastBtn.onclick = () => {
    bcast.classList.toggle('show');
    bcastBtn.classList.toggle('on', bcast.classList.contains('show'));
    if (bcast.classList.contains('show')) bcastMsg.focus();
  };
  async function doBroadcast() {
    const keys = bcastMsg.value;
    if (!keys) return;
    if (!confirm('Send to ALL ' + sessions.length + ' Claude sessions?\n\n"' + keys + '"')) return;
    try { await api.broadcast(keys); } catch (e) { toast('broadcast failed', true); return; }
    bcastMsg.value = ''; toast('sent to all sessions');
  }
  if (bcastSend) bcastSend.onclick = doBroadcast;
  bcastMsg.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); doBroadcast(); } });
})();

// ---- key dispatcher ----
installKeys({
  view: () => view,
  paneFocused: () => {
    if (dock.paneFocused && dock.paneFocused()) return true;
    if (sidepanel.isLive && sidepanel.isLive()) return true;
    if (view === 'terminals' && termWrap && termWrap.classList.contains('live')) {
      const a = document.activeElement, host = document.getElementById('term');
      if (a && host && host.contains(a)) return true;
    }
    return false;
  },
  modalOpen: () => modal.isOpen(),
  closeModal: () => modal.closeModal(),
  dockOpen: () => dock.dockOpen(),
  closeDock: () => dock.closeDock(),
  selectSessionIndex, cycleSession,
  sidePanelOpen: () => sidepanel.isOpen(),
  sidePanelLive: () => sidepanel.isLive(),
  releaseSidePanel: () => sidepanel.setLive(false),
  closeSidePanel: () => sidepanel.close(),
  toggleDiff: () => {
    if (sidepanel.isOpen() && sidepanel.mode() === 'diff') { sidepanel.close(); return; }
    const t = dock.claudeTarget();
    if (t) { sidepanel.openDiffPanel(t, (dock.dockedWorker() || {}).name || t); return; }
    const a = (graphSnap.agents || [])[0];
    if (a) sidepanel.openDiffPanel(a.session || a.name, a.name);
    else toast('no agent to diff');
  },
});

window.addEventListener('resize', () => { if (view === 'crew') renderCrew(); });

// ---- boot ----
setView('crew');
setInterval(() => { if (view === 'terminals') loadSessions(); }, 2000);
