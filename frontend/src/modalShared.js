// Pure, DOM-free logic behind the modals — extracted so the vitest contract
// suite can pin these behaviors without rendering anything.

// Preserve exactly what the operator entered until the strict API boundary
// validates it. A form's visible default "0" is the deliberate no-cap value;
// deleting that value is incomplete input, not an implicit zero.
export function normalizeEdgeCapText(value) {
  return String(value == null ? '' : value).trim();
}

// Hook nodes are source-only. A drag may begin at either card, so normalize a
// hook/agent gesture to hook → agent before opening the edge form. Two hooks
// have no valid route and return null.
export function normalizeConnectionEndpoints(fromName, toName, nodes) {
  const from = (nodes || []).find(node => node.name === fromName);
  const to = (nodes || []).find(node => node.name === toName);
  const fromHook = from && from.kind === 'webhook';
  const toHook = to && to.kind === 'webhook';
  if (fromHook && toHook) return null;
  return {
    source: toHook ? toName : fromName,
    target: toHook ? fromName : toName,
    webhookEdge: !!(fromHook || toHook),
  };
}

// An edge's trigger list for a direction (forward / back), with legacy fallback.
export function edgeConds(edge, back) {
  const k = back ? 'back_conditions' : 'conditions';
  if (Array.isArray(edge[k]) && edge[k].length) return edge[k].filter(Boolean);
  if (!back && edge.condition) return [edge.condition];
  return [];
}

function positiveNumber(value) {
  const n = Number(value || 0);
  return Number.isFinite(n) && n > 0 ? n : 0;
}

// "5 msg/hr · 1,200 tok/hr · $2.5/hr" — or '' when the edge is uncapped.
export function capsText(edge) {
  const turns = positiveNumber(edge.max_turns);
  const tokens = positiveNumber(edge.token_cap);
  const cost = positiveNumber(edge.cost_cap);
  const parts = [];
  if (turns) parts.push(`${turns.toLocaleString('en-US')} msg/hr`);
  if (tokens) parts.push(`${tokens.toLocaleString('en-US')} tok/hr`);
  if (cost) parts.push(`$${cost.toLocaleString('en-US')}/hr`);
  return parts.join(' · ');
}

// The identity card's channel lists, derived from the same snapshot the graph
// renders. outgoing: peers THIS agent may message (forward when it's the
// source; the back direction of an undirected edge when it's the target).
export function identityChannels(name, edges) {
  const two = e => e.directed === false;
  const out = [], inc = [];
  for (const e of (edges || [])) {
    if (e.source_name === name) {
      out.push({ peer: e.target_name, conds: edgeConds(e, false), reply: !!e.reply_expected, e });
      if (two(e)) inc.push({ peer: e.target_name, conds: edgeConds(e, true), act: e.back_action, e });
    }
    if (e.target_name === name) {
      inc.push({ peer: e.source_name, conds: edgeConds(e, false), act: e.target_action, e });
      if (two(e)) out.push({ peer: e.source_name, conds: edgeConds(e, true), reply: !!e.back_reply, e });
    }
  }
  return { out, inc };
}

export function ageText(createdAt) {
  if (!createdAt) return '?';
  const s = Math.max(0, Math.floor(Date.now() / 1000 - createdAt));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  if (s < 86400) return `${Math.floor(s / 3600)}h`;
  return `${Math.floor(s / 86400)}d`;
}
