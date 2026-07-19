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
const STATUS_COLOR = { working: '#3fb950', needs_input: '#d29922', idle: '#6e7681', unknown: '#8b949e', not_started: '#58a6ff', down: '#484f58' };
// What each status reads as ON THE NODE, so the agent's state is legible from the
// graph alone: computing / waiting / asking you something / no claude running.
const STATUS_LABEL = { working: 'working…', needs_input: 'needs you', idle: 'idle', unknown: 'state unknown', not_started: 'runtime not started', down: 'session down' };
function statusLabel(status, agent) {
  if (status === 'down' && agent && agent.session_alive) return 'runtime down';
  return STATUS_LABEL[status] || status || 'state unknown';
}
const POS_KEY = 'crew.pos.v1';
let workspaceKey = 'crew';

function esc(s) {
  return (s || '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}
function positionStorageKey() {
  return workspaceKey === 'crew' ? POS_KEY : `${POS_KEY}.${encodeURIComponent(workspaceKey)}`;
}
function loadPos() { try { return JSON.parse(localStorage.getItem(positionStorageKey())) || {}; } catch (e) { return {}; } }
function savePos(m) { try { localStorage.setItem(positionStorageKey(), JSON.stringify(m)); } catch (e) {} }

// ---- view: an INFINITE pan + zoom canvas (Figma-feel) ----
// The canvas has no edges: nodes live in unbounded "world" coords and the view is a
// translate+scale on top. Zoom via the WHEEL (see installZoomControls' wheel
// listener below) anchored to the cursor, or Ctrl/Cmd +/- anchored to center; pan
// via left-drag on empty canvas or middle-drag anywhere; "fit" to frame everything.
//
// REVERSAL NOTE: an earlier version (commit ff173a0) deliberately left the wheel
// alone ("scroll is deliberately left free") so trackpad scroll never fought with
// page/graph navigation. Felix's explicit ask ("scrolling should zoom") reverses
// that: plain wheel AND ctrlKey wheel (trackpad pinch) both zoom-to-cursor now,
// including a horizontal two-finger swipe (deltaX) — there is no more "pan via
// wheel" gesture, since background-drag/middle-drag already cover panning without
// needing the wheel for it.
const VIEW_KEY = 'crew.view.v1';
function viewStorageKey() {
  return workspaceKey === 'crew' ? VIEW_KEY : `${VIEW_KEY}.${encodeURIComponent(workspaceKey)}`;
}
// a wide, Excalidraw-style range so zoom never hits a wall on real graphs; steps
// are MULTIPLICATIVE (×/÷ ZFACTOR) so each press feels even from 5% to 300%.
const ZMIN = 0.05, ZMAX = 3.0, ZFACTOR = 1.2;
// node-size floor WITH a release valve, reconciling two wants: (1) nudging zoom
// out shouldn't make cards unreadable — down to NODE_FLOOR we counter-scale each
// card by NODE_FLOOR/zoom (a CSS var the cards inherit) to hold its on-screen size;
// (2) you can still zoom WAY out — the counter-scale caps at NS_MAX, so past there
// cards shrink again into a true overview instead of freezing and piling up.
const NODE_FLOOR = 0.65, NS_MAX = 2.0;
function nodeScale() {
  const s = zoom < NODE_FLOOR ? NODE_FLOOR / zoom : 1;
  return +Math.min(NS_MAX, s).toFixed(3);
}
let zoom = 1, panX = 0, panY = 0;
function loadView() {
  zoom = 1; panX = 0; panY = 0;
  try { const v = JSON.parse(localStorage.getItem(viewStorageKey()));
    if (v && v.zoom >= ZMIN && v.zoom <= ZMAX) { zoom = v.zoom; panX = v.panX || 0; panY = v.panY || 0; } }
  catch (e) {}
}
loadView();

function applyView() {
  if (CANVAS) {
    CANVAS.style.transformOrigin = '0 0';
    CANVAS.style.transform = `translate(${panX}px,${panY}px) scale(${zoom})`;
    CANVAS.style.setProperty('--ns', nodeScale());   // node-size floor (inherited by cards)
  }
  const lbl = document.getElementById('zoomPct');
  if (lbl) lbl.textContent = Math.round(zoom * 100) + '%';
  // the dot grid pans + zooms WITH the content (Figma-style infinite surface): it
  // lives on the static viewport, so we just slide its origin by the pan and scale
  // its tile by the zoom. Second layer (solid base) is pinned.
  const wrap = document.getElementById('cgraph');
  if (wrap) {
    const t = 22 * zoom;
    wrap.style.backgroundSize = `${t}px ${t}px,auto`;
    wrap.style.backgroundPosition = `${panX}px ${panY}px,0 0`;
  }
  try { localStorage.setItem(viewStorageKey(), JSON.stringify({ zoom, panX, panY })); } catch (e) {}
}

// zoom around a screen-space anchor (default: viewport centre) so the point under
// it stays put — origin 0,0 means screen = pan + world*zoom, so world = (screen-pan)/zoom.
function setZoom(z, ax, ay) {
  const [W, H] = size();
  if (ax == null) { ax = W / 2; ay = H / 2; }
  const nz = Math.max(ZMIN, Math.min(ZMAX, Math.round(z * 100) / 100));
  const wx = (ax - panX) / zoom, wy = (ay - panY) / zoom;
  zoom = nz; panX = ax - wx * zoom; panY = ay - wy * zoom;
  applyView();
}
function zoomIn() { setZoom(zoom * ZFACTOR); }
function zoomOut() { setZoom(zoom / ZFACTOR); }

// frame every node in the viewport with padding (the "zoom to fit" button).
function zoomToFit() {
  const ns = [...NODES.values()];
  const [W, H] = size();
  if (!ns.length) { zoom = 1; panX = 0; panY = 0; applyView(); return; }
  let minx = Infinity, miny = Infinity, maxx = -Infinity, maxy = -Infinity;
  for (const n of ns) { minx = Math.min(minx, n.x); miny = Math.min(miny, n.y); maxx = Math.max(maxx, n.x); maxy = Math.max(maxy, n.y); }
  const PAD = 165;   // node half-extent (~115) + the ● handle + breathing room
  const bw = (maxx - minx) + PAD * 2, bh = (maxy - miny) + PAD * 2;
  zoom = Math.max(ZMIN, Math.min(ZMAX, Math.round(Math.min(W / bw, H / bh) * 100) / 100));
  panX = W / 2 - ((minx + maxx) / 2) * zoom;
  panY = H / 2 - ((miny + maxy) / 2) * zoom;
  applyView();
}

// ---- pan: drag empty canvas to move the whole view ----
let panDrag = null;
function startPan(e) {
  panDrag = { sx: e.clientX, sy: e.clientY, px: panX, py: panY };
  CANVAS.classList.add('panning');
  window.addEventListener('mousemove', onPanMove);
  window.addEventListener('mouseup', onPanUp);
}
function onPanMove(e) {
  if (!panDrag) return;
  panX = panDrag.px + (e.clientX - panDrag.sx);
  panY = panDrag.py + (e.clientY - panDrag.sy);
  applyView();
}
function onPanUp() {
  window.removeEventListener('mousemove', onPanMove);
  window.removeEventListener('mouseup', onPanUp);
  if (CANVAS) CANVAS.classList.remove('panning');
  panDrag = null;
}

// zoom-to-cursor: the wheel's target element tells us where the pointer is
// (clientX/Y), and #cgraph (CANVAS's un-transformed parent) gives the stable
// "screen space" origin setZoom expects (see its own comment — same coordinate
// system onDragMove/onConnMove already use for node/connect-line placement).
// A plain wheel tick, a ctrlKey wheel (trackpad pinch), AND a horizontal
// two-finger swipe (deltaX) all zoom — see the REVERSAL NOTE above. The
// exponential factor makes a light trackpad nudge and a hard mouse-wheel click
// both feel proportionate, and never overshoots regardless of how big a single
// event's delta is (a pinch can report much larger deltas than a wheel tick).
function onWheel(e) {
  if (!CANVAS) return;
  e.preventDefault();
  const g = CANVAS.parentNode;
  const r = g.getBoundingClientRect();
  const ax = e.clientX - r.left, ay = e.clientY - r.top;
  const d = e.deltaY !== 0 ? e.deltaY : e.deltaX;
  if (d === 0) return;
  setZoom(zoom * Math.exp(-d * 0.0018), ax, ay);
}

// Install the view controls ONCE: Ctrl/Cmd +/-/0 keys (preventDefault so the
// browser's own page-zoom doesn't fire), the header − / + / fit buttons, and the
// wheel (zoom-to-cursor — see onWheel above; { passive:false } so preventDefault
// actually stops the browser/page from scrolling or pinch-zooming instead).
let _zoomWired = false;
function installZoomControls() {
  if (_zoomWired) return;
  _zoomWired = true;
  const g = document.getElementById('cgraph');
  if (g) g.addEventListener('wheel', onWheel, { passive: false });
  window.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && connect) {
      e.preventDefault();
      cancelConnect(true);
      return;
    }
    if (!(e.ctrlKey || e.metaKey) || e.altKey) return;
    const a = document.activeElement, tag = a && a.tagName;
    const inField = (tag === 'INPUT' || tag === 'TEXTAREA' || (a && a.isContentEditable))
      && !(a.classList && a.classList.contains('xterm-helper-textarea'));
    if (inField) return;
    if (e.key === '=' || e.key === '+') { e.preventDefault(); zoomIn(); }
    else if (e.key === '-' || e.key === '_') { e.preventDefault(); zoomOut(); }
    else if (e.key === '0') { e.preventDefault(); zoomToFit(); }   // Ctrl/Cmd 0 = fit
  }, true);
  const zi = document.getElementById('zoomIn'), zo = document.getElementById('zoomOut'), zf = document.getElementById('zoomFit');
  if (zo) zo.onclick = () => zoomOut();
  if (zi) zi.onclick = () => zoomIn();
  if (zf) zf.onclick = () => zoomToFit();
}

// ---- module state ----
let CANVAS = null, SVG = null, TEMP = null;   // DOM scaffold
const NODES = new Map();   // name -> {x,y,vx,vy,pinned, el, data}
let EDGES = [];            // {a,b,directed,data,line,label}
let H = {};                // handlers
let RAF = null;
let drag = null;           // {name, moved, sx, sy}
let dockClickTimer = null; // defer click so a dblclick can claim unpin exclusively
let connect = null;        // {from}
let dockedName = null;

function announceGraph(message) {
  const status = document.getElementById('graphKeyboardStatus');
  if (status) status.textContent = message || '';
}

function selectWorkspace(key) {
  const next = (typeof key === 'string' && key.trim()) ? key.trim() : 'crew';
  if (next === workspaceKey) return;
  workspaceKey = next;
  loadView();
  // A tenant switch may contain the same agent names. Rebuild instead of
  // accidentally retaining the previous tenant's in-memory coordinates.
  NODES.forEach(node => { try { node.el.remove(); } catch (e) {} });
  NODES.clear();
  EDGES.forEach(edge => {
    try { edge.line.remove(); } catch (e) {}
    try { if (edge.label) edge.label.remove(); } catch (e) {}
  });
  EDGES = [];
  if (connect) cancelConnect(false);
  applyView();
}

function updateConnectStyles() {
  for (const [name, node] of NODES) {
    node.el.classList.toggle('picked', !!connect && name === connect.from);
    node.el.classList.toggle('pickable', !!connect && name !== connect.from);
    paintNode(node);
  }
}

// ---- scaffold (built once into #cgraph) ----
function ensureScaffold(g) {
  if (CANVAS && CANVAS.parentNode === g) return;
  // CANVAS is missing or DETACHED (e.g. a failed poll replaced #cgraph's innerHTML
  // with a "backend unavailable" message). Drop stale node/edge state so reconcile
  // rebuilds them into the fresh canvas — otherwise the NODES map still holds the
  // orphaned elements, reconcile takes the "already exists" branch, and the nodes
  // are repainted but never re-appended → the graph stays blank until a full reload.
  NODES.forEach(n => { try { n.el.remove(); } catch (e) {} });
  NODES.clear();
  EDGES = [];
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
  // mousedown: middle-button drag pans from ANYWHERE (even over a node — it
  // bubbles up here since node/handle mousedown handlers only claim button 0);
  // left-button on truly empty canvas (not a node/handle) also pans, same as
  // before — cancel an in-progress connect instead if one's active.
  CANVAS.addEventListener('mousedown', e => {
    if (e.button === 1) { e.preventDefault(); startPan(e); return; }
    if (e.target === CANVAS || e.target === SVG) {
      if (connect) { cancelConnect(); return; }
      startPan(e);
    }
  });
  installZoomControls();
  applyView();
}

function size() { return [CANVAS.clientWidth || 800, CANVAS.clientHeight || 520]; }

// ---- node DOM ----
function paintNode(node) {
  const a = node.data;
  const st = a.live_status || (a.session_alive ? 'unknown' : 'down');
  const stateLabel = statusLabel(st, a);
  const dot = STATUS_COLOR[st] || '#6e7681';
  const glow = st === 'working' ? 'box-shadow:0 0 8px ' + dot : '';
  const role = a.role ? `<div class="sub">${esc(a.role)}</div>` : '<div class="sub dim">no role</div>';
  // WAVE 3: a foreman (can_edit_graph) gets a small badge in its card, and an
  // unblessed row (agent-authored, not yet reviewed) reads dashed + amber —
  // same "needs your attention" signal the amber status color already uses.
  const foremanBadge = a.can_edit_graph ? '<span class="foreman-badge" title="foreman — can edit the graph">⚑ foreman</span>' : '';
  const unblessed = a.blessed === false;
  node.el.innerHTML =
    `<div class="nm"><span class="dot" style="background:${dot};${glow}"></span>${esc(a.name)} <span class="runtime-badge">${esc(a.runtime || 'claude')}</span>${foremanBadge}</div>`
    + role
    + `<div class="sub state ${st}">${stateLabel}</div>`
    + `<div class="conn-handle" title="drag onto another agent to connect">●</div>`;
  // status class on the CARD (down dims it; needs_input pulses) so state reads at a
  // glance; title repeats the state + the click hint that used to be inline text.
  node.el.classList.remove('st-working', 'st-needs_input', 'st-idle', 'st-unknown', 'st-not_started', 'st-down');
  node.el.classList.add('st-' + st);
  node.el.classList.toggle('unblessed', unblessed);
  node.el.title = `${a.name} — ${stateLabel}`
    + (unblessed ? ' · unblessed' : '') + ' · click to open terminal';
  const keyboardAction = connect
    ? (connect.from === a.name
      ? 'Connection source. Press Escape to cancel.'
      : `Press Enter to connect from ${connect.from}.`)
    : 'Press Enter to open its terminal, or C to start a connection.';
  node.el.setAttribute('aria-label',
    `${a.name}, ${a.runtime || 'claude'}, ${stateLabel}. ${keyboardAction}`);
  node.el.classList.toggle('docked', dockedName === a.name);
  // wire interactions (rebound each paint — cheap, few nodes). Agents are durable:
  // no delete affordance on the node — removal is a deliberate CLI action.
  const handle = node.el.querySelector('.conn-handle');
  handle.onmousedown = e => { e.stopPropagation(); e.preventDefault(); startConnect(node, e); };
}

function makeNode(a, x, y, pinned) {
  const el = document.createElement('div');
  el.className = 'cnode agent';
  el.tabIndex = 0;
  el.setAttribute('role', 'button');
  el.dataset.sess = a.name;
  el.style.left = x + 'px'; el.style.top = y + 'px';
  const node = { x, y, vx: 0, vy: 0, pinned: !!pinned, el, data: a };
  el.addEventListener('mousedown', e => { if (e.button === 0) startDrag(node, e); });
  el.addEventListener('keydown', e => {
    if (e.key === 'c' || e.key === 'C') {
      e.preventDefault();
      startKeyboardConnect(node);
      return;
    }
    if (e.key !== 'Enter' && e.key !== ' ') return;
    e.preventDefault();
    if (connect && connect.from !== node.data.name) {
      finishKeyboardConnect(node.data.name);
    } else if (connect) {
      cancelConnect(true);
    } else if (H.onDockAgent) {
      H.onDockAgent(node.data);
    }
  });
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
    const unblessed = e.blessed === false;
    const line = document.createElementNS(SVGNS, 'line');
    line.setAttribute('class', 'cedge' + (unblessed ? ' unblessed' : ''));
    // WAVE 3: an unblessed (agent-authored, not yet reviewed) edge reads
    // dashed + amber, same signal as an unblessed node.
    line.setAttribute('stroke', unblessed ? '#d29922' : '#4d6b94');
    line.setAttribute('stroke-width', 2);
    if (unblessed) line.setAttribute('stroke-dasharray', '6 4');
    // two-way edges get an arrowhead at BOTH ends (↔); one-way only at the target.
    line.setAttribute('marker-end', 'url(#arrow)');
    if (!directed) line.setAttribute('marker-start', 'url(#arrow)');
    line.setAttribute('tabindex', '0');
    line.setAttribute('focusable', 'true');
    line.setAttribute('role', 'button');
    line.setAttribute('aria-label',
      `Edit ${directed ? 'directed' : 'two-way'} edge ${e.source_name} to ${e.target_name}`);
    line.style.cursor = 'pointer';
    line.onclick = () => H.onEditEdge(e);
    line.onkeydown = event => {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        H.onEditEdge(e);
      }
    };
    SVG.appendChild(line);
    // edge label: the LIST of trigger conditions riding on the cable. Forward
    // direction's conditions as lines; for a two-way edge, the back direction's too
    // (prefixed ↩). Full detail (+ what each receiver does) in the hover tooltip.
    const fwd = (Array.isArray(e.conditions) && e.conditions.length) ? e.conditions
              : (e.condition ? [e.condition] : []);
    const back = Array.isArray(e.back_conditions) ? e.back_conditions.filter(Boolean) : [];
    const lines = [...fwd.map(t => ({ t, back: false })),
                   ...(directed ? [] : back.map(t => ({ t, back: true })))];
    let label = null;
    if (lines.length || e.label) {
      label = document.createElement('div');
      label.className = 'cedge-label';
      label.innerHTML = (lines.length ? lines : [{ t: e.label, back: false }])
        .map(o => `<span class="el-cond${o.back ? ' back' : ''}">${o.back ? '↩ ' : ''}${esc(o.t)}</span>`).join('');
      const tip = [];
      if (e.label) tip.push('“' + e.label + '”');
      fwd.forEach(c => tip.push(`${e.source_name} → ${e.target_name} when: ${c}`));
      if (e.target_action) tip.push(`${e.target_name} then: ${e.target_action}`);
      if (!directed) {
        back.forEach(c => tip.push(`${e.target_name} → ${e.source_name} when: ${c}`));
        if (e.back_action) tip.push(`${e.source_name} then: ${e.back_action}`);
      }
      label.title = tip.join('\n');
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
        // A pinned/pinned overlap cannot resolve without violating the user's
        // saved positions. Do not manufacture energy for that immovable pair,
        // or it keeps requestAnimationFrame alive forever. Any movable endpoint
        // still gets the existing follow-up ticks until separation settles.
        if ((!a.pinned || !b.pinned) && energy < 0.1) energy = 0.2;
      }
    }
  }
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
  if (dockClickTimer) {
    clearTimeout(dockClickTimer);
    dockClickTimer = null;
  }
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
  const r = CANVAS.getBoundingClientRect();   // reflects the pan+zoom transform
  // map the cursor from screen px into UNBOUNDED world coords (undo pan+zoom). No
  // clamping — the canvas is infinite, so a node drags anywhere with no border.
  node.x = (e.clientX - r.left) / zoom;
  node.y = (e.clientY - r.top) / zoom;
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
  if (!drag.moved) {
    if (node) {
      // startDrag prevents the browser's default mousedown focus so dragging
      // does not steal selection. Restore it explicitly on the click path
      // before the dock records document.activeElement as its return target.
      node.el.focus();
      const clicked = node;
      // A double-click means "unpin", not "open then unpin". Delay the
      // single-click action through the browser's double-click window; the
      // second mousedown and onDblNode both cancel it deterministically.
      dockClickTimer = setTimeout(() => {
        dockClickTimer = null;
        if (NODES.get(clicked.data.name) === clicked && H.onDockAgent) {
          H.onDockAgent(clicked.data);
        }
      }, 220);
    }
  }
  else if (node) {
    node.pinned = true;
    const m = loadPos(); m[node.data.name] = { x: Math.round(node.x), y: Math.round(node.y), pinned: true };
    savePos(m);
  }
  drag = null;
}

// double-click a node → unpin it (let the sim re-space it)
function onDblNode(name) {
  if (dockClickTimer) {
    clearTimeout(dockClickTimer);
    dockClickTimer = null;
  }
  const node = NODES.get(name); if (!node) return;
  node.pinned = false;
  const m = loadPos(); delete m[name]; savePos(m); kick();
}

// ---- drag-to-connect from the ● handle ----
function startConnect(node, e) {
  connect = { from: node.data.name, keyboard: false };
  updateConnectStyles();
  TEMP.style.display = '';
  window.addEventListener('mousemove', onConnMove);
  window.addEventListener('mouseup', onConnUp);
  onConnMove(e);
}
function startKeyboardConnect(node) {
  if (connect) cancelConnect(false);
  connect = { from: node.data.name, keyboard: true };
  if (TEMP) TEMP.style.display = 'none';
  updateConnectStyles();
  announceGraph(`Connecting from ${node.data.name}. Tab to a target agent and press Enter. Press Escape to cancel.`);
}
function finishKeyboardConnect(to) {
  if (!connect || !to || to === connect.from) return;
  const from = connect.from;
  connect = null;
  updateConnectStyles();
  announceGraph(`Connection form opened for ${from} to ${to}.`);
  if (H.onConnect) H.onConnect(from, to);
}
function onConnMove(e) {
  if (!connect) return;
  const from = NODES.get(connect.from); if (!from) return;
  const r = CANVAS.getBoundingClientRect();
  TEMP.setAttribute('x1', from.x); TEMP.setAttribute('y1', from.y);
  TEMP.setAttribute('x2', (e.clientX - r.left) / zoom); TEMP.setAttribute('y2', (e.clientY - r.top) / zoom);
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
  updateConnectStyles();
  if (to && to !== from) H.onConnect(from, to);
}
function cancelConnect(announce = true) {
  if (!connect) return;
  window.removeEventListener('mousemove', onConnMove);
  window.removeEventListener('mouseup', onConnUp);
  if (TEMP) TEMP.style.display = 'none';
  connect = null;
  updateConnectStyles();
  if (announce) announceGraph('Connection cancelled.');
}

// ---- public ----
export function renderGraph(snap, handlers, opts) {
  H = handlers || {};
  dockedName = (opts || {}).dockedName || null;
  selectWorkspace((snap || {}).workspace_key);
  const g = document.getElementById('cgraph'); if (!g) return;
  ensureScaffold(g);
  reconcile(snap || {});
  // one empty-state block at most: drop any prior one before (maybe) re-adding it,
  // so repeated renders (the 1.5s poll, window resize) can't stack duplicates.
  CANVAS.querySelector('.empty')?.remove();
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
