// graph.js — the crew canvas: a force-directed, DRAGGABLE graph of agents.
//
// Agents auto-space themselves (a small physics sim repels nodes apart and pulls
// connected ones together) so they never cram, no matter how many. You arrange
// the team by hand: DRAG an agent's body to move it (it pins where you drop it),
// DRAG from its ● handle onto another agent to draw a relationship, and CLICK an
// agent to drop into its live terminal. Positions persist (localStorage).
//
// Stateful at module scope: the node set + their positions survive every poll, so
// the layout is stable (no jumping) and a data refresh just repaints contents.
//
// handlers:
//   onDockAgent(agent)          click a node → open its big terminal
//   onConnect(fromName,toName)   drag ● from one node onto another → describe edge
//   onEditEdge(edge)            click an edge label → edit/delete

const SVGNS = 'http://www.w3.org/2000/svg';
const STATUS_COLOR = { working: '#3fb950', needs_input: '#d29922', idle: '#6e7681', down: '#484f58' };
const POS_KEY = 'crew.pos.v1';

function esc(s) {
  return (s || '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}
function loadPos() { try { return JSON.parse(localStorage.getItem(POS_KEY)) || {}; } catch (e) { return {}; } }
function savePos(m) { try { localStorage.setItem(POS_KEY, JSON.stringify(m)); } catch (e) {} }

// ---- module state ----
let CANVAS = null, SVG = null, TEMP = null;   // DOM scaffold
const NODES = new Map();   // name -> {x,y,vx,vy,pinned, el, data}
let EDGES = [];            // {a,b,directed,data,line,label}
let H = {};                // handlers
let RAF = null;
let drag = null;           // {name, moved, sx, sy}
let connect = null;        // {from}
let dockedName = null;

// ---- scaffold (built once into #cgraph) ----
function ensureScaffold(g) {
  if (CANVAS && CANVAS.parentNode === g) return;
  g.innerHTML = '';
  g.style.display = 'flex';
  g.style.flexDirection = 'column';
  g.style.overflow = 'hidden';
  CANVAS = document.createElement('div');
  CANVAS.className = 'gcanvas';
  SVG = document.createElementNS(SVGNS, 'svg');
  SVG.setAttribute('class', 'cedge-svg');
  // bigger, brighter arrowhead so message DIRECTION reads at a glance (was a small
  // low-contrast triangle that was easy to miss / mistake convergence for divergence).
  SVG.innerHTML =
    `<defs><marker id="arrow" viewBox="0 0 10 10" refX="8.5" refY="5" markerWidth="11" markerHeight="11"
       orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="#79c0ff"></path></marker></defs>`;
  TEMP = document.createElementNS(SVGNS, 'line');
  TEMP.setAttribute('class', 'cedge-temp');
  TEMP.style.display = 'none';
  SVG.appendChild(TEMP);
  CANVAS.appendChild(SVG);
  g.appendChild(CANVAS);
  // a click on empty canvas cancels an in-progress connect
  CANVAS.addEventListener('mousedown', e => { if (e.target === CANVAS || e.target === SVG) cancelConnect(); });
}

function size() { return [CANVAS.clientWidth || 800, CANVAS.clientHeight || 520]; }

// ---- node DOM ----
function paintNode(node) {
  const a = node.data;
  const st = a.alive ? (a.live_status || 'idle') : 'down';
  const dot = STATUS_COLOR[st] || '#6e7681';
  const glow = st === 'working' ? 'box-shadow:0 0 8px ' + dot : '';
  const role = a.role ? `<div class="sub">${esc(a.role)}</div>` : '<div class="sub dim">no role</div>';
  node.el.innerHTML =
    `<div class="nm"><span class="dot" style="background:${dot};${glow}"></span>${esc(a.name)}</div>`
    + role
    + `<div class="sub state ${st}">${a.alive ? 'click to open terminal' : 'session down'}</div>`
    + `<div class="conn-handle" title="drag onto another agent to connect">●</div>`;
  node.el.classList.toggle('docked', dockedName === a.name);
  // wire interactions (rebound each paint — cheap, few nodes). Agents are durable:
  // no delete affordance on the node — removal is a deliberate CLI action.
  const handle = node.el.querySelector('.conn-handle');
  handle.onmousedown = e => { e.stopPropagation(); e.preventDefault(); startConnect(node, e); };
}

function makeNode(a, x, y, pinned) {
  const el = document.createElement('div');
  el.className = 'cnode agent';
  el.dataset.sess = a.name;
  el.style.left = x + 'px'; el.style.top = y + 'px';
  const node = { x, y, vx: 0, vy: 0, pinned: !!pinned, el, data: a };
  el.addEventListener('mousedown', e => { if (e.button === 0) startDrag(node, e); });
  CANVAS.appendChild(el);
  paintNode(node);
  return node;
}

// ---- reconcile data → DOM (keeps positions stable across polls) ----
function reconcile(snap) {
  const [W, Hh] = size();
  const saved = loadPos();
  const agents = snap.agents || [];
  const seen = new Set();
  agents.forEach((a, i) => {
    seen.add(a.name);
    let node = NODES.get(a.name);
    if (node) { node.data = a; paintNode(node); return; }
    const sp = saved[a.name];
    const x = sp ? sp.x : W / 2 + Math.cos(i * 2.4) * (190 + i * 30);
    const y = sp ? sp.y : Hh / 2 + Math.sin(i * 2.4) * (150 + i * 24);
    NODES.set(a.name, makeNode(a, x, y, sp && sp.pinned));
  });
  for (const [name, node] of NODES) {
    if (!seen.has(name)) { node.el.remove(); NODES.delete(name); }
  }
  // edges: rebuild the small set each data change
  EDGES.forEach(e => { e.line.remove(); if (e.label) e.label.remove(); });
  EDGES = [];
  (snap.edges || []).forEach(e => {
    const a = NODES.get(e.source_name), b = NODES.get(e.target_name);
    if (!a || !b) return;
    const directed = e.directed !== false;
    const line = document.createElementNS(SVGNS, 'line');
    line.setAttribute('class', 'cedge');
    line.setAttribute('stroke', '#4d6b94');
    line.setAttribute('stroke-width', 2);
    if (directed) line.setAttribute('marker-end', 'url(#arrow)');
    line.style.cursor = 'pointer';
    line.onclick = () => H.onEditEdge(e);
    SVG.appendChild(line);
    // a quiet single-line annotation on the cable (condition preferred — it's the
    // meaningful "when"); full detail in the hover tooltip. No label → none drawn
    // (the arrow already shows direction; click the line itself to edit).
    const labelText = (e.condition || e.label || '').trim();
    let label = null;
    if (labelText) {
      label = document.createElement('div');
      label.className = 'cedge-label';
      label.innerHTML = `<span class="el-name">${esc(labelText)}</span>`;
      label.title = [e.label && ('“' + e.label + '”'), e.description,
                     e.condition && ('when: ' + e.condition)].filter(Boolean).join('\n');
      label.onclick = () => H.onEditEdge(e);
      CANVAS.appendChild(label);
    }
    EDGES.push({ a, b, directed, data: e, line, label });
  });
}

// ---- force sim ----
function tick() {
  RAF = null;
  const [W, Hh] = size();
  const arr = [...NODES.values()];
  const cx = W / 2, cy = Hh / 2;
  const KR = 110000, KC = 0.006, KS = 0.02, L = 330, DAMP = 0.86;
  for (const n of arr) { n.fx = 0; n.fy = 0; }
  for (let i = 0; i < arr.length; i++) {
    for (let j = i + 1; j < arr.length; j++) {
      const a = arr[i], b = arr[j];
      let dx = a.x - b.x, dy = a.y - b.y; let d2 = dx * dx + dy * dy; if (d2 < 1) d2 = 1;
      const f = KR / d2, d = Math.sqrt(d2);
      const ux = dx / d, uy = dy / d;
      a.fx += ux * f; a.fy += uy * f; b.fx -= ux * f; b.fy -= uy * f;
    }
  }
  for (const n of arr) { n.fx += (cx - n.x) * KC; n.fy += (cy - n.y) * KC; }
  for (const e of EDGES) {
    const a = e.a, b = e.b;
    let dx = b.x - a.x, dy = b.y - a.y; const d = Math.hypot(dx, dy) || 1;
    const force = (d - L) * KS, ux = dx / d, uy = dy / d;
    a.fx += ux * force; a.fy += uy * force; b.fx -= ux * force; b.fy -= uy * force;
  }
  let energy = 0;
  for (const n of arr) {
    if (n.pinned || (drag && drag.name === n.data.name && drag.moved)) { n.vx = n.vy = 0; continue; }
    n.vx = (n.vx + n.fx) * DAMP; n.vy = (n.vy + n.fy) * DAMP;
    n.vx = Math.max(-30, Math.min(30, n.vx)); n.vy = Math.max(-30, Math.min(30, n.vy));
    n.x += n.vx; n.y += n.vy;
    n.x = Math.max(80, Math.min(W - 80, n.x)); n.y = Math.max(48, Math.min(Hh - 40, n.y));
    energy += n.vx * n.vx + n.vy * n.vy;
  }
  // hard separation pass — guarantees no two nodes ever overlap, regardless of
  // where the forces settle. A node card is ~200x90, so keep centers >= MINSEP.
  const MINSEP = 250;
  for (let i = 0; i < arr.length; i++) {
    for (let j = i + 1; j < arr.length; j++) {
      const a = arr[i], b = arr[j];
      let dx = b.x - a.x, dy = b.y - a.y, d = Math.hypot(dx, dy) || 1;
      if (d < MINSEP) {
        const ux = dx / d, uy = dy / d, overlap = MINSEP - d;
        const am = a.pinned ? 0 : (b.pinned ? overlap : overlap / 2);
        const bm = b.pinned ? 0 : (a.pinned ? overlap : overlap / 2);
        a.x -= ux * am; a.y -= uy * am; b.x += ux * bm; b.y += uy * bm;
        if (energy < 0.1) energy = 0.2;   // keep ticking until separations resolve
      }
    }
  }
  for (const n of arr) { n.x = Math.max(80, Math.min(W - 80, n.x)); n.y = Math.max(48, Math.min(Hh - 40, n.y)); }
  paintPositions();
  if (energy > 0.08 || drag || connect) kick();
}
function kick() { if (!RAF) RAF = requestAnimationFrame(tick); }

function paintPositions() {
  for (const n of NODES.values()) { n.el.style.left = n.x + 'px'; n.el.style.top = n.y + 'px'; }
  for (const e of EDGES) {
    const [x1, y1, x2, y2] = trim(e.a.x, e.a.y, e.b.x, e.b.y, 64);
    e.line.setAttribute('x1', x1); e.line.setAttribute('y1', y1);
    e.line.setAttribute('x2', x2); e.line.setAttribute('y2', y2);
    if (e.label) {
      e.label.style.left = ((e.a.x + e.b.x) / 2) + 'px';
      e.label.style.top = ((e.a.y + e.b.y) / 2) + 'px';
    }
  }
}
function trim(x1, y1, x2, y2, pad) {
  const dx = x2 - x1, dy = y2 - y1, len = Math.hypot(dx, dy) || 1;
  return [x1 + dx / len * pad, y1 + dy / len * pad, x2 - dx / len * pad, y2 - dy / len * pad];
}

// ---- drag a node (move + pin) / click (open terminal) ----
function startDrag(node, e) {
  drag = { name: node.data.name, moved: false, sx: e.clientX, sy: e.clientY };
  window.addEventListener('mousemove', onDragMove);
  window.addEventListener('mouseup', onDragUp);
  e.preventDefault();
}
function onDragMove(e) {
  if (!drag) return;
  if (!drag.moved && Math.abs(e.clientX - drag.sx) + Math.abs(e.clientY - drag.sy) > 4) drag.moved = true;
  if (!drag.moved) return;
  const node = NODES.get(drag.name); if (!node) return;
  // Pin the moment a drag starts so the force sim + separation pass treat this
  // node as fixed — it stays exactly under the cursor and the OTHER nodes flow
  // out of its way, instead of the sim shoving it back.
  node.pinned = true;
  const r = CANVAS.getBoundingClientRect();
  node.x = clamp(e.clientX - r.left, 80, (r.width || 800) - 80);
  node.y = clamp(e.clientY - r.top, 48, (r.height || 520) - 40);
  node.vx = node.vy = 0; node.el.classList.add('dragging');
  paintPositions(); kick();
}
function clamp(v, lo, hi) { return v < lo ? lo : v > hi ? hi : v; }
function onDragUp() {
  window.removeEventListener('mousemove', onDragMove);
  window.removeEventListener('mouseup', onDragUp);
  if (!drag) return;
  const node = NODES.get(drag.name);
  if (node) node.el.classList.remove('dragging');
  if (!drag.moved) { if (node) H.onDockAgent(node.data); }
  else if (node) {
    node.pinned = true;
    const m = loadPos(); m[node.data.name] = { x: Math.round(node.x), y: Math.round(node.y), pinned: true };
    savePos(m);
  }
  drag = null;
}

// double-click a node → unpin it (let the sim re-space it)
function onDblNode(name) {
  const node = NODES.get(name); if (!node) return;
  node.pinned = false;
  const m = loadPos(); delete m[name]; savePos(m); kick();
}

// ---- drag-to-connect from the ● handle ----
function startConnect(node, e) {
  connect = { from: node.data.name };
  TEMP.style.display = '';
  window.addEventListener('mousemove', onConnMove);
  window.addEventListener('mouseup', onConnUp);
  onConnMove(e);
}
function onConnMove(e) {
  if (!connect) return;
  const from = NODES.get(connect.from); if (!from) return;
  const r = CANVAS.getBoundingClientRect();
  TEMP.setAttribute('x1', from.x); TEMP.setAttribute('y1', from.y);
  TEMP.setAttribute('x2', e.clientX - r.left); TEMP.setAttribute('y2', e.clientY - r.top);
  kick();
}
function onConnUp(e) {
  window.removeEventListener('mousemove', onConnMove);
  window.removeEventListener('mouseup', onConnUp);
  TEMP.style.display = 'none';
  if (!connect) return;
  const el = document.elementFromPoint(e.clientX, e.clientY);
  const host = el && el.closest ? el.closest('.cnode.agent') : null;
  const to = host && host.dataset.sess;
  const from = connect.from;
  connect = null;
  if (to && to !== from) H.onConnect(from, to);
}
function cancelConnect() {
  if (!connect) return;
  window.removeEventListener('mousemove', onConnMove);
  window.removeEventListener('mouseup', onConnUp);
  TEMP.style.display = 'none'; connect = null;
}

// ---- public ----
export function renderGraph(snap, handlers, opts) {
  H = handlers || {};
  dockedName = (opts || {}).dockedName || null;
  const g = document.getElementById('cgraph'); if (!g) return;
  ensureScaffold(g);
  reconcile(snap || {});
  // dblclick to unpin (delegated)
  CANVAS.ondblclick = (e) => {
    const host = e.target.closest && e.target.closest('.cnode.agent');
    if (host) onDblNode(host.dataset.sess);
  };
  // meta line
  const meta = document.getElementById('cgraph-meta');
  if (meta) {
    const na = (snap.agents || []).length, ne = (snap.edges || []).length;
    meta.textContent = `${na} agent${na === 1 ? '' : 's'} · ${ne} edge${ne === 1 ? '' : 's'}`;
  }
  if (!(snap.agents || []).length) {
    const e = document.createElement('div');
    e.className = 'empty'; e.style.cssText = 'position:absolute;left:50%;top:42%;transform:translate(-50%,-50%);text-align:center';
    const msg = document.createElement('div');
    msg.style.cssText = 'margin-bottom:14px;font-size:13px;color:var(--dim)';
    msg.textContent = 'Your crew is empty.';
    const btn = document.createElement('button');
    btn.className = 'btn primary';
    btn.textContent = '+ Create your first agent';
    btn.onclick = () => { if (H.onCreateAgent) H.onCreateAgent(); };
    e.appendChild(msg); e.appendChild(btn);
    CANVAS.appendChild(e);
  }
  kick();
}

export function highlightDockedNode(name) {
  dockedName = name || null;
  for (const [n, node] of NODES) node.el.classList.toggle('docked', n === dockedName);
}
