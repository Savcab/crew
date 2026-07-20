// One shared status vocabulary for graph cards, the dock header, and the
// identity card — machine statuses are API contracts; operators only ever see
// these plain-language labels.
export const STATUS_COLOR = {
  working: '#3fb950', needs_input: '#d29922', idle: '#6e7681',
  unknown: '#8b949e', not_started: '#58a6ff', down: '#484f58',
};

export const STATUS_LABEL = {
  working: 'working…', needs_input: 'needs you', idle: 'idle',
  unknown: 'state unknown', not_started: 'runtime not started',
  down: 'session down',
};

// "down" with a live tmux session means the RUNTIME died, not the session.
export function statusLabel(status, agent) {
  if (status === 'down' && agent && agent.session_alive) return 'runtime down';
  return STATUS_LABEL[status] || status || 'state unknown';
}

export function liveStatus(agent) {
  return agent.live_status || (agent.session_alive ? 'unknown' : 'down');
}
