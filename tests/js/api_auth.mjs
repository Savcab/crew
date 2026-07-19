import assert from 'node:assert/strict';
import { pathToFileURL } from 'node:url';
import path from 'node:path';

const root = process.argv[2];
if (!root) throw new Error('repository root argument is required');

const listeners = new Map();
const calls = [];
const location = { hash: '', pathname: '/', search: '?view=graph' };

globalThis.window = {
  location,
  addEventListener(type, fn) { listeners.set(type, fn); },
};
globalThis.history = {
  replaceState(_state, _title, url) {
    calls.push(['history', url]);
    location.hash = '';
  },
};
globalThis.fetch = async (url, options = {}) => {
  calls.push(['fetch', url, options]);
  if (url === '/api/auth/bootstrap') {
    return { ok: true, status: 200, json: async () => ({ ok: true }) };
  }
  if (url === '/api/graph/snapshot') {
    return {
      ok: true, status: 200,
      json: async () => ({ ok: true, agents: [], edges: [] }),
    };
  }
  throw new Error(`unexpected fetch ${url}`);
};

const apiUrl = pathToFileURL(path.join(root, 'static/js/api.js')).href
  + `?auth-test=${Date.now()}`;
const { api } = await import(apiUrl);

assert.equal(calls.length, 0, 'a capability-free initial load must not bootstrap');
assert.equal(typeof listeners.get('hashchange'), 'function',
  'an already-open dashboard must observe a later CLI capability fragment');

location.hash = '#cap=fresh-local-capability';
listeners.get('hashchange')();
await api.graphSnapshot();

const fetches = calls.filter(call => call[0] === 'fetch');
assert.deepEqual(fetches.map(call => call[1]), [
  '/api/auth/bootstrap', '/api/graph/snapshot',
]);
assert.equal(JSON.parse(fetches[0][2].body).capability, 'fresh-local-capability');
assert.deepEqual(fetches[0][2].headers, {
  'Content-Type': 'application/json', 'X-Crew-CSRF': '1',
});
assert.deepEqual(calls.find(call => call[0] === 'history'), [
  'history', '/?view=graph',
]);
assert.equal(location.hash, '');

console.log('API auth UI checks: 1 passed');
