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
    const on = force === undefined ? !this.values.has(name) : !!force;
    if (on) this.values.add(name); else this.values.delete(name);
    return on;
  }
}

class FakeElement {
  constructor(id) {
    this.id = id;
    this.classList = new TokenList();
    this.style = {};
    this.attributes = new Map();
    this.listeners = new Map();
    this.textContent = '';
    this.parent = null;
    this.isConnected = true;
    this.clientWidth = 800;
    this.clientHeight = 400;
    this.rect = { top: 0, bottom: 600, height: 600, width: 800 };
  }
  setAttribute(name, value) { this.attributes.set(name, String(value)); }
  getAttribute(name) { return this.attributes.get(name) ?? null; }
  addEventListener(type, fn) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(fn);
  }
  dispatch(type, event = {}) {
    for (const fn of this.listeners.get(type) || []) fn(event);
  }
  contains(node) {
    for (let at = node; at; at = at.parent) if (at === this) return true;
    return false;
  }
  focus() { globalThis.document.activeElement = this; this.focusCount = (this.focusCount || 0) + 1; }
  blur() { if (globalThis.document.activeElement === this) globalThis.document.activeElement = null; }
  getBoundingClientRect() { return this.rect; }
}

class FakeTerminal {
  constructor() {
    this.cols = 80;
    this.rows = 24;
    this.buffer = { active: { viewportY: 0, baseY: 0 } };
    this.writes = [];
  }
  loadAddon(addon) { this.addon = addon; }
  onData(fn) { this.dataHandler = fn; }
  onBinary(fn) { this.binaryHandler = fn; }
  attachCustomKeyEventHandler(fn) { this.keyHandler = fn; }
  open(host) {
    this.host = host;
    this.textarea = new FakeElement('xterm-textarea');
    this.textarea.parent = host;
  }
  reset() { this.resetCount = (this.resetCount || 0) + 1; }
  write(bytes, callback) { this.writes.push(Uint8Array.from(bytes)); if (callback) callback(); }
  scrollToBottom() { this.scrolled = true; }
  focus() { this.focused = true; if (this.textarea) this.textarea.focus(); }
  blur() { this.focused = false; if (this.textarea) this.textarea.blur(); }
  dispose() { this.disposed = true; }
}

class FakeFitAddon { fit() {} }

class FakeEventSource {
  static instances = [];
  constructor(url) {
    this.url = url;
    this.listeners = new Map();
    FakeEventSource.instances.push(this);
  }
  addEventListener(type, fn) { this.listeners.set(type, fn); }
  emit(type, data) {
    const fn = this.listeners.get(type);
    if (fn) fn({ data });
  }
  close() { this.closed = true; }
}

function installBrowserGlobals() {
  const elements = new Map();
  const ids = [
    'dock', 'dockTerm', 'dockName', 'dockDot', 'dockMeta', 'dockStart',
    'dockClose', 'dockIdentity', 'dockPrev', 'dockNext', 'dockMax',
    'dockResize', 'crew', 'addAgentBtn',
  ];
  ids.forEach(id => elements.set(id, new FakeElement(id)));
  const dock = elements.get('dock');
  for (const [id, el] of elements) {
    if (id.startsWith('dock') && id !== 'dock') el.parent = dock;
  }
  dock.rect = { top: 300, bottom: 600, height: 300, width: 800 };
  elements.get('crew').rect = { top: 0, bottom: 600, height: 600, width: 800 };

  globalThis.document = {
    activeElement: null,
    getElementById: id => elements.get(id) || null,
  };
  globalThis.window = {
    location: { hash: '', pathname: '/', search: '' },
    Terminal: FakeTerminal,
    FitAddon: { FitAddon: FakeFitAddon },
    getSelection: () => '',
    listeners: new Map(),
    addEventListener(type, fn) {
      if (!this.listeners.has(type)) this.listeners.set(type, []);
      this.listeners.get(type).push(fn);
    },
  };
  globalThis.EventSource = FakeEventSource;
  globalThis.fetch = async () => ({ ok: true, json: async () => ({ ok: true }) });
  return elements;
}

const elements = installBrowserGlobals();
const apiUrl = pathToFileURL(path.join(root, 'static/js/api.js')).href;
const termUrl = pathToFileURL(path.join(root, 'static/js/term.js')).href;
const dockUrl = pathToFileURL(path.join(root, 'static/js/dock.js')).href;
const { api } = await import(apiUrl);
const { TerminalPane } = await import(termUrl);
const { createDock } = await import(dockUrl);

// A closed EventSource can still have already-queued callbacks. Those callbacks
// must not overwrite the new PTY id or paint stale bytes after switching agents.
const resizeCalls = [];
api.ptyResize = (...args) => { resizeCalls.push(args); return Promise.resolve({ ok: true }); };
api.ptyInput = () => Promise.resolve({ ok: true });
const pane = new TerminalPane();
pane.attach(elements.get('dockTerm'));
pane.open('first-session');
const firstSource = FakeEventSource.instances.at(-1);
pane.open('second-session');
const secondSource = FakeEventSource.instances.at(-1);
assert.equal(firstSource.closed, true);
firstSource.emit('id', 'old-pty');
firstSource.emit('data', Buffer.from('stale').toString('base64'));
assert.equal(pane.ptyId, null, 'a stale id event replaced the new stream state');
assert.equal(pane.term.writes.length, 0, 'stale output painted after stream switch');
secondSource.emit('id', 'new-pty');
secondSource.emit('data', Buffer.from('fresh').toString('base64'));
assert.equal(pane.ptyId, 'new-pty');
assert.equal(Buffer.from(pane.term.writes[0]).toString(), 'fresh');

// Input POSTs are a threaded HTTP boundary. Keep them ordered and never allow a
// queued old-PTY keystroke to jump to a newer stream.
const inputCalls = [];
let releaseFirstInput;
api.ptyInput = (id, b64) => {
  inputCalls.push([id, Buffer.from(b64, 'base64').toString()]);
  if (inputCalls.length === 1) {
    return new Promise(resolve => { releaseFirstInput = resolve; });
  }
  return Promise.resolve({ ok: true });
};
pane.term.dataHandler('a');
pane.term.dataHandler('b');
await Promise.resolve();
await Promise.resolve();
assert.deepEqual(inputCalls, [['new-pty', 'a']], 'input requests were concurrent');
releaseFirstInput({ ok: true });
await new Promise(resolve => setTimeout(resolve, 0));
assert.deepEqual(inputCalls, [['new-pty', 'a'], ['new-pty', 'b']]);

// A browser paste may exceed the API's decoded-body ceiling. Split it into
// bounded sequential requests while preserving every byte exactly.
await pane._inputTail;
const pasteChunks = [];
api.ptyInput = (id, b64) => {
  pasteChunks.push(Buffer.from(b64, 'base64'));
  return Promise.resolve({ ok: true });
};
const largePaste = Buffer.alloc(300 * 1024);
for (let i = 0; i < largePaste.length; i += 1) largePaste[i] = i % 251;
pane._toPty(Uint8Array.from(largePaste));
await pane._inputTail;
assert.ok(pasteChunks.length > 1, 'oversized paste was sent as one rejected POST');
assert.ok(pasteChunks.every(chunk => chunk.length <= 64 * 1024));
assert.deepEqual(Buffer.concat(pasteChunks), largePaste);

pane.dispose();
assert.equal(pane.ptyId, null, 'dispose retained a writable server-side PTY id');

class FakePane {
  constructor() { this.opens = []; }
  attach(host) { this.host = host; return this; }
  open(target) { this.opens.push(target); return this; }
  setLive(on) { this.live = on; return this; }
  fit() { this.fitCount = (this.fitCount || 0) + 1; return this; }
}

const workers = [];
const toasts = [];
let resolveStart;
const dockApi = {
  agentStart: () => new Promise(resolve => { resolveStart = resolve; }),
};
const dockController = createDock({
  TerminalPane: FakePane,
  api: dockApi,
  getWorkers: () => workers,
  onDockChange: () => {},
  onShowIdentity: () => {},
  toast: (...args) => toasts.push(args),
});
const dockPane = window.__dock.claudePane;

// Opening a known-down agent must not start EventSource's infinite 404 retry loop.
const opener = new FakeElement('graph-agent-a');
document.activeElement = opener;
const agentA = {
  name: 'agent-a', session: 'session-a', runtime: 'claude',
  session_alive: false, runtime_alive: false, live_status: 'down',
};
dockController.openDock(agentA);
assert.equal(dockPane.opens.at(-1), null, 'down agent opened a retrying PTY stream');
assert.equal(
  elements.get('dockMeta').textContent,
  'claude · session down',
  'dock exposed the raw runtime status "down" instead of the operator label');

// A delayed start response belongs to the worker that initiated it. Switching to
// another worker while it is in flight must not re-open or relabel that worker.
const startPromise = elements.get('dockStart').onclick();
const agentB = {
  name: 'agent-b', session: 'session-b', runtime: 'claude',
  session_alive: true, runtime_alive: true, live_status: 'idle',
};
dockController.openDock(agentB);
const bOpensBefore = dockPane.opens.filter(value => value === 'session-b').length;
resolveStart({ ok: true });
await startPromise;
assert.equal(dockController.dockedWorker().name, 'agent-b');
assert.equal(
  dockPane.opens.filter(value => value === 'session-b').length,
  bOpensBefore,
  'agent A start completion re-opened agent B');
assert.equal(toasts.at(-1)[0], 'starting agent-a…');

// A name/session can be reused after removal. An in-flight Start response must
// remain bound to the immutable row GUID and never relabel/reattach a same-name
// replacement that the operator opened while the request was pending.
const oldGeneration = {
  _guid: 'old-generation', name: 'agent-reused', session: 'session-reused',
  runtime: 'claude', session_alive: false, runtime_alive: false,
  live_status: 'down', role: 'old generation',
};
dockController.openDock(oldGeneration);
const oldGenerationStart = elements.get('dockStart').onclick();
const newGeneration = {
  ...oldGeneration,
  _guid: 'new-generation', session_alive: true, runtime_alive: true,
  live_status: 'idle', role: 'replacement generation',
};
dockController.openDock(newGeneration);
const replacementOpensBefore = dockPane.opens.filter(
  value => value === 'session-reused').length;
resolveStart({ ok: true });
await oldGenerationStart;
assert.equal(dockController.dockedWorker()._guid, 'new-generation');
assert.equal(
  elements.get('dockMeta').textContent,
  'replacement generation · claude · idle',
  'old Start completion relabeled the same-name replacement');
assert.equal(
  dockPane.opens.filter(value => value === 'session-reused').length,
  replacementOpensBefore,
  'old Start completion reattached the same-name replacement');

const crashedRuntime = {
  name: 'agent-crashed', session: 'session-crashed', runtime: 'claude',
  session_alive: true, runtime_alive: false, live_status: 'down',
};
dockController.openDock(crashedRuntime);
assert.equal(
  elements.get('dockMeta').textContent,
  'claude · runtime down',
  'dock mislabeled a crashed runtime as a missing tmux session');

// Machine status values are API contracts; every operator-facing dock label
// must use the same plain-language vocabulary as the graph cards.
const notStarted = {
  name: 'agent-not-started', runtime: 'claude', session_alive: true,
  runtime_alive: false, live_status: 'not_started',
};
dockController.openDock(notStarted);
assert.equal(
  elements.get('dockMeta').textContent,
  'claude · runtime not started',
  'dock exposed the raw runtime status "not_started"');
const customUnknown = {
  name: 'agent-custom', runtime: 'custom', session_alive: true,
  runtime_alive: true, live_status: 'unknown',
};
dockController.openDock(customUnknown);
assert.equal(
  elements.get('dockMeta').textContent,
  'custom · state unknown',
  'dock exposed the raw runtime status "unknown"');
const exitedCustom = {
  name: 'agent-custom-exited', runtime: 'custom', session_alive: true,
  runtime_alive: false, live_status: 'unknown',
};
dockController.openDock(exitedCustom);
assert.notEqual(
  elements.get('dockStart').style.display,
  'none',
  'dock hid Start after the configured custom runtime process exited');
dockController.openDock(customUnknown);

// Snapshot polling must refresh an already-open dock's status.  Starting a
// previously down runtime otherwise leaves the header stuck on "starting…"
// even after the graph card has observed that the session is alive.
workers.push({
  ...customUnknown,
  role: 'runtime fixture',
  runtime_alive: true,
  live_status: 'unknown',
});
dockController.syncDockedWorker();
assert.equal(
  elements.get('dockMeta').textContent,
  'runtime fixture · custom · state unknown',
  'open dock did not synchronize the latest worker snapshot');

// Ctrl+Esc detaches to a real dock control, and closing returns focus to the graph
// control that opened the dock instead of leaving focus inside display:none UI.
const terminalChild = new FakeElement('terminal-child');
terminalChild.parent = elements.get('dockTerm');
document.activeElement = terminalChild;
dockController.detach();
assert.equal(document.activeElement, elements.get('dockClose'));
dockController.closeDock();
assert.equal(document.activeElement, opener);

// Polling must not silently swap an open dock onto a replacement row merely
// because the user reused the same name/session after removal.
const syncOld = {
  _guid: 'sync-old', name: 'agent-sync-reused', session: 'sync-session',
  runtime: 'claude', session_alive: true, runtime_alive: true,
  live_status: 'idle',
};
const syncReplacement = { ...syncOld, _guid: 'sync-new' };
dockController.openDock(syncOld);
workers.splice(0, workers.length, syncReplacement);
dockController.syncDockedWorker();
assert.equal(
  dockController.dockedWorker(),
  null,
  'snapshot sync silently adopted a same-name replacement GUID');

// The dock and mouse resize handle expose useful semantics and an equivalent
// keyboard path. ArrowUp grows the dock by one step.
assert.equal(elements.get('dock').getAttribute('role'), 'region');
assert.equal(elements.get('dockResize').getAttribute('role'), 'separator');
assert.equal(elements.get('dockResize').getAttribute('tabindex'), '0');
let prevented = false;
elements.get('dockResize').dispatch('keydown', {
  key: 'ArrowUp',
  preventDefault() { prevented = true; },
});
assert.equal(prevented, true);
assert.equal(elements.get('dock').style.height, '324px');
assert.equal(elements.get('dockResize').getAttribute('aria-valuenow'), '324');

console.log('terminal transport UI checks: 24 passed');
