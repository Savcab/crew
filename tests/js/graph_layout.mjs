import assert from 'node:assert/strict';
import { pathToFileURL } from 'node:url';
import path from 'node:path';

const root = process.argv[2];
if (!root) throw new Error('repository root argument is required');

class TokenList {
  constructor() { this.values = new Set(); }
  add(...names) { names.forEach(name => this.values.add(name)); }
  remove(...names) { names.forEach(name => this.values.delete(name)); }
  contains(name) { return this.values.has(name); }
  toggle(name, force) {
    const enabled = force === undefined ? !this.values.has(name) : !!force;
    if (enabled) this.values.add(name); else this.values.delete(name);
    return enabled;
  }
}

class FakeStyle {
  setProperty(name, value) { this[name] = String(value); }
}

const created = [];
class FakeElement {
  constructor(tag = 'div', id = '') {
    this.tagName = tag.toUpperCase();
    this.id = id;
    this.className = '';
    this.classList = new TokenList();
    this.style = new FakeStyle();
    this.dataset = {};
    this.attributes = new Map();
    this.children = [];
    this.parentNode = null;
    this.listeners = new Map();
    this.clientWidth = 800;
    this.clientHeight = 520;
    this._innerHTML = '';
    created.push(this);
  }
  set innerHTML(value) {
    this._innerHTML = String(value);
    this.children = [];
  }
  get innerHTML() { return this._innerHTML; }
  setAttribute(name, value) { this.attributes.set(name, String(value)); }
  addEventListener(type, fn) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(fn);
  }
  dispatch(type, event = {}) {
    for (const fn of this.listeners.get(type) || []) fn(event);
  }
  appendChild(child) {
    child.parentNode = this;
    this.children.push(child);
    return child;
  }
  remove() {
    if (!this.parentNode) return;
    this.parentNode.children = this.parentNode.children.filter(
      child => child !== this);
    this.parentNode = null;
  }
  querySelector(selector) {
    if (selector === '.conn-handle') return new FakeElement('div');
    if (selector === '.empty') {
      return this.children.find(child => child.className === 'empty') || null;
    }
    return null;
  }
  closest(selector) {
    if (selector === '.cnode.agent'
        && this.className.split(/\s+/).includes('cnode')
        && this.className.split(/\s+/).includes('agent')) return this;
    return this.parentNode && this.parentNode.closest
      ? this.parentNode.closest(selector) : null;
  }
  focus() { document.activeElement = this; }
}

const elements = new Map();
for (const id of ['cgraph', 'cgraph-meta']) {
  elements.set(id, new FakeElement('div', id));
}

const storage = new Map();
globalThis.localStorage = {
  getItem: key => storage.get(key) ?? null,
  setItem: (key, value) => storage.set(key, String(value)),
};
globalThis.document = {
  activeElement: null,
  getElementById: id => elements.get(id) || null,
  createElement: tag => new FakeElement(tag),
  createElementNS: (_namespace, tag) => new FakeElement(tag),
};
const windowListeners = new Map();
globalThis.window = {
  addEventListener(type, fn) {
    if (!windowListeners.has(type)) windowListeners.set(type, []);
    windowListeners.get(type).push(fn);
  },
  removeEventListener(type, fn) {
    windowListeners.set(
      type, (windowListeners.get(type) || []).filter(item => item !== fn));
  },
  dispatch(type, event = {}) {
    for (const fn of [...(windowListeners.get(type) || [])]) fn(event);
  },
};

const timers = new Map();
let nextTimerId = 1;
globalThis.setTimeout = callback => {
  const id = nextTimerId++;
  timers.set(id, callback);
  return id;
};
globalThis.clearTimeout = id => timers.delete(id);
function runTimers() {
  const pending = [...timers.values()];
  timers.clear();
  for (const callback of pending) callback();
}

const animationFrames = [];
let nextFrameId = 1;
globalThis.requestAnimationFrame = callback => {
  animationFrames.push(callback);
  return nextFrameId++;
};

const graphUrl = pathToFileURL(path.join(root, 'static/js/graph.js')).href;
const { renderGraph } = await import(graphUrl);

function agent(name) {
  return {
    name, runtime: 'custom', role: 'layout fixture',
    session_alive: false, runtime_alive: false, live_status: 'not_started',
  };
}

function runFrame() {
  const callback = animationFrames.shift();
  assert.ok(callback, 'graph did not schedule its initial layout frame');
  callback();
}

function nodeElement(name) {
  return created.find(element => element.dataset.sess === name);
}

// A user can deliberately pin two cards in the same place. Because neither card
// may move, collision handling must not manufacture animation energy forever.
storage.set('crew.pos.v1', JSON.stringify({
  pinned_a: { x: 200, y: 200, pinned: true },
  pinned_b: { x: 200, y: 200, pinned: true },
}));
renderGraph({
  workspace_key: 'crew',
  agents: [agent('pinned_a'), agent('pinned_b')],
  edges: [],
}, {});

let framesRun = 0;
while (animationFrames.length && framesRun < 12) {
  runFrame();
  framesRun += 1;
}
assert.equal(
  animationFrames.length,
  0,
  'an immovable pinned collision kept scheduling animation frames',
);
assert.equal(nodeElement('pinned_a').style.left, '200px');
assert.equal(nodeElement('pinned_b').style.left, '200px');

// The guard above must not disable collision avoidance when at least one node is
// movable: the free card still separates while the pinned card stays put.
storage.set('crew.pos.v1.movable', JSON.stringify({
  anchor: { x: 200, y: 200, pinned: true },
  free: { x: 220, y: 200, pinned: false },
}));
renderGraph({
  workspace_key: 'movable',
  agents: [agent('anchor'), agent('free')],
  edges: [],
}, {});
runFrame();
assert.equal(nodeElement('anchor').style.left, '200px');
assert.notEqual(nodeElement('free').style.left, '220px');

// Single-click opens the dock, but a deliberate double-click is reserved for
// unpinning and must not also open a terminal as a side effect.
const docked = [];
renderGraph({
  workspace_key: 'clicks',
  agents: [agent('click_agent')],
  edges: [],
}, { onDockAgent: value => docked.push(value.name) });
const clickNode = nodeElement('click_agent');
const clickEvent = {
  button: 0, clientX: 200, clientY: 200,
  preventDefault() {},
};
clickNode.dispatch('mousedown', clickEvent);
window.dispatch('mouseup', clickEvent);
assert.deepEqual(docked, [], 'single click opened before its double-click window');
runTimers();
assert.deepEqual(docked, ['click_agent'], 'single click no longer opens the dock');

clickNode.dispatch('mousedown', clickEvent);
window.dispatch('mouseup', clickEvent);
clickNode.dispatch('mousedown', clickEvent);
window.dispatch('mouseup', clickEvent);
const canvas = created.find(element => element.className === 'gcanvas');
canvas.ondblclick({ target: clickNode });
runTimers();
assert.deepEqual(
  docked,
  ['click_agent'],
  'double-click unpin also opened the agent terminal',
);

console.log('graph layout checks: 5 passed');
