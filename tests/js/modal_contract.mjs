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
}

class FakeElement {
  constructor(id) {
    this.id = id;
    this.classList = new TokenList();
    this.style = {};
    this.innerHTML = '';
    this.textContent = '';
    this.isConnected = true;
    this.listeners = new Map();
  }
  addEventListener(type, fn) { this.listeners.set(type, fn); }
  querySelectorAll() { return []; }
  contains(node) { return node === this; }
  focus() { globalThis.document.activeElement = this; }
}

const elements = new Map();
for (const id of [
  'cmodal', 'modalTitle', 'modalBody', 'modalClose', 'id-foreman-toggle',
]) elements.set(id, new FakeElement(id));

globalThis.document = {
  activeElement: null,
  body: new FakeElement('body'),
  getElementById: id => elements.get(id) || null,
  querySelectorAll: () => [],
};
globalThis.window = { prompt: () => '' };
globalThis.requestAnimationFrame = fn => fn();
globalThis.getComputedStyle = () => ({ visibility: 'visible' });

const modalUrl = pathToFileURL(path.join(root, 'static/js/modal.js')).href;
const { createModalController, normalizeEdgeCapText } = await import(modalUrl);

// Cap text stays lossless. The form's explicit default "0" means uncapped;
// deleting that value is incomplete input and must reach strict server
// validation as blank rather than silently becoming an accepted zero.
assert.equal(normalizeEdgeCapText('0'), '0');
assert.equal(normalizeEdgeCapText(' 1000 '), '1000');
assert.equal(normalizeEdgeCapText('1.5'), '1.5');
assert.equal(normalizeEdgeCapText('NaN'), 'NaN');
assert.equal(normalizeEdgeCapText('Infinity'), 'Infinity');
assert.equal(normalizeEdgeCapText('-1'), '-1');
assert.equal(normalizeEdgeCapText('   '), '');
assert.equal(normalizeEdgeCapText(undefined), '');

const controller = createModalController({ api: {}, toast: () => {}, refresh: () => {} });
controller.openIdentity({
  name: 'agent-safe', role: 'browser fixture',
  identity: 'Mission <script>alert(1)</script>\nsecond line',
  notes: 'Review <b>carefully</b>',
  grants: [{ name: 'shared-docs', path: '/srv/<private>', mode: 'ro' }],
  home: '/tmp/agent-safe', runtime: 'codex',
  session_alive: true, live_status: 'not_started', blessed: true,
}, [{
  source_name: 'agent-safe', target_name: 'peer', directed: true,
  conditions: ['when ready'], max_turns: 5, token_cap: 1200, cost_cap: 2.5,
}]);

const html = elements.get('modalBody').innerHTML;
assert.match(html, /identity \/ mission/i);
assert.match(html, /Mission &lt;script&gt;alert\(1\)&lt;\/script&gt;/);
assert.doesNotMatch(html, /<script>/i);
assert.match(html, /Review &lt;b&gt;carefully&lt;\/b&gt;/);
assert.match(html, /refs\/shared-docs/);
assert.match(html, /\/srv\/&lt;private&gt;/);
assert.match(html, /recorded intent/i);
assert.match(html, /5 msg\/hr/);
assert.match(html, /1,200 tok\/hr/);
assert.match(html, /\$2\.5\/hr/);
assert.match(html, />runtime not started</);

console.log('modal UI checks: 16 passed');
