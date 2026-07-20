// termLink.js — the cross-window handoff behind "open terminals in a second
// window" (dual-monitor flow): the GRAPH window broadcasts which agent to
// show; the TERMINAL window (/?view=term) listens and re-points its dock.
// BroadcastChannel is per-origin, so two dashboards on different ports (two
// projects) can never cross-talk.
//
// Protocol (all messages {type, ...}):
//   graph → term : {type:'open', name}      show this agent's terminal
//   term  → graph: {type:'term-ready'}      just loaded — resend your last ask
//   term  → graph: {type:'term-bye'}        closing — fall back to window.open
//
// Degradation is deliberate: if liveness tracking is ever wrong, the worst
// case is an extra window.open into the SAME named window ('crewTerm'), which
// re-uses it rather than spawning duplicates.

const CHANNEL = 'crew-term';

export function graphTermLink({ openWindow } = {}) {
  const bc = new BroadcastChannel(CHANNEL);
  const spawn = openWindow || (() => window.open('/?view=term', 'crewTerm'));
  let alive = false;
  let lastName = null;
  bc.onmessage = event => {
    const m = event.data || {};
    if (m.type === 'term-ready') {
      alive = true;
      if (lastName) bc.postMessage({ type: 'open', name: lastName });
    } else if (m.type === 'term-bye') {
      alive = false;
    }
  };
  return {
    open(name) {
      lastName = name;
      bc.postMessage({ type: 'open', name });
      if (!alive) spawn();
    },
    isAlive: () => alive,
    close: () => bc.close(),
  };
}

export function termWindowLink({ onOpen }) {
  const bc = new BroadcastChannel(CHANNEL);
  bc.onmessage = event => {
    const m = event.data || {};
    if (m.type === 'open' && m.name) onOpen(m.name);
  };
  window.addEventListener('pagehide', () => {
    try { bc.postMessage({ type: 'term-bye' }); } catch (e) { /* closing */ }
  });
  bc.postMessage({ type: 'term-ready' });
  return bc;
}
