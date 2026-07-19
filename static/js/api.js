// api.js — one fetch wrapper per HTTP endpoint. Pure transport: build a URL /
// shape a body / parse JSON. NO DOM, no state, no rendering — every other module
// talks to the backend ONLY through this object. Native ES module, no build step.
//
// The dashboard manages ONLY crew agents, so the surface is small: the graph
// snapshot, the PTY terminal transport (crew sessions only — the server refuses
// anything else), and the agent/edge mutations. The backend resolves a session
// NAME → its live claude pane on every call, so the FE passes the NAME as `t`.

const JSON_HEADERS = { "Content-Type": "application/json" };

async function _get(path) {
  const r = await fetch(path);
  return r.json();
}

// POST <path> with a JSON body → parsed JSON. `body` defaults to {} so the backend's
// `json.loads(raw or b"{}")` always sees a dict.
async function _post(path, body) {
  const r = await fetch(path, {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify(body || {}),
  });
  return r.json();
}

const q = encodeURIComponent;

export const api = {
  // ===== GET =====

  // Full agent-graph snapshot: crew agents (+ live tmux status) + edges. Polled by
  // the graph view. → {ok, agents, edges} | {ok:false, error}.
  graphSnapshot() {
    return _get("/api/graph/snapshot");
  },

  // ===== PTY transport (the real terminal: a `tmux attach` client in a PTY) =====

  // SSE URL for a PTY-attach stream. The caller feeds it to `new EventSource(...)`
  // (term.js). Pass the session NAME. The server refuses any non-crew session.
  ptyStreamUrl(target, cols, rows) {
    return "/api/pty/stream?t=" + q(target || "") + "&cols=" + (cols || 80) + "&rows=" + (rows || 24);
  },
  // Write raw bytes (base64) to a PTY — keystrokes, mouse sequences, chord escapes.
  ptyInput(id, b64) {
    return _post("/api/pty/input", { id, b64 });
  },
  // Resize a PTY (TIOCSWINSZ) → tmux resizes the view's window to match xterm's grid.
  ptyResize(id, cols, rows) {
    return _post("/api/pty/resize", { id, cols, rows });
  },

  // ===== POST agent-graph mutations =====
  // The dashboard calls crew.graphstore / crew.spawn server-side (no CLI shell-
  // out). Each → {ok, ...} | {ok:false, error}.

  // Spawn a new long-running agent: home-uniqueness enforced (one per dir, no
  // nesting), tmux session + claude launched, identity.md + CLAUDE.md written.
  // `launch_cmd` overrides the per-environment default launch command.
  agentCreate({ name, role, identity, home, repo, launch, launch_cmd } = {}) {
    return _post("/api/agent/create", { name, role, identity, home, repo, launch, launch_cmd });
  },

  // Revive a down agent: re-create its tmux session + relaunch claude in its home.
  // The record already exists; only the live session died. → {ok, agent}.
  agentStart({ name } = {}) {
    return _post("/api/agent/start", { name });
  },

  // Delete an agent (and, by default, kill its tmux session; home dir is kept).
  agentRemove(name) {
    return _post("/api/agent/remove", { name });
  },

  // Connect two agents → defines a relationship AND authorizes messaging. Each
  // direction carries a LIST of trigger `conditions`, the receiver's action, and a
  // reply flag; the `back_*` fields describe the target→source direction of a
  // two-way (`directed:false`) edge. source/target are agent names.
  // `token_cap`/`cost_cap` (WAVE 3) budget the TARGET's hourly claude spend —
  // 0/undefined means uncapped.
  edgeCreate(f = {}) {
    return _post("/api/edge/create", {
      source: f.source, target: f.target, label: f.label,
      conditions: f.conditions, target_action: f.target_action, reply_expected: f.reply_expected,
      back_conditions: f.back_conditions, back_action: f.back_action, back_reply: f.back_reply,
      max_turns: f.max_turns, token_cap: f.token_cap, cost_cap: f.cost_cap,
      directed: f.directed,
    });
  },

  // Edit an edge by guid (label / description / condition / target_action /
  // reply_expected / max_turns / token_cap / cost_cap / directed) — a straight
  // pass-through, so the caller's field set drives what actually changes.
  edgeUpdate(fields = {}) {
    return _post("/api/edge/update", fields);
  },

  // Delete an edge by guid.
  edgeDelete({ guid } = {}) {
    return _post("/api/edge/delete", { guid });
  },

  // ===== WAVE 3: bless + foreman (both human-only server-side) =====

  // Mark an agent-authored agent row as reviewed/trusted.
  agentBless(name) {
    return _post("/api/agent/bless", { name });
  },

  // Mark an agent-authored edge row as reviewed/trusted.
  edgeBless(guid) {
    return _post("/api/edge/bless", { guid });
  },

  // Grant (default) or revoke (`revoke:true`) the foreman (can_edit_graph)
  // flag. Singleton-enforced server-side: granting while another agent
  // already holds it is refused, naming the current holder.
  agentForeman({ name, revoke } = {}) {
    return _post("/api/agent/foreman", { name, revoke: !!revoke });
  },

  // ===== WAVE 4: the pending-approval queue =====
  // A foreman's connect to a human-made node, or any agent's cap RAISE on an
  // edge it's an endpoint of, queues instead of refusing outright — these are
  // how the human resolves what's waiting. `pending_count` on the graph
  // snapshot badges the tray without a second poll; this list is fetched only
  // when the tray is actually opened.

  // Every result="pending" graph_edit row, newest first, each carrying a
  // server-rendered `summary` string. → {ok, pending} | {ok:false, error}.
  pendingList() {
    return _get("/api/pending");
  },

  // Execute the stored request (create_edge / update_edge) and mark it
  // approved. Human-only server-side.
  pendingApprove(guid) {
    return _post("/api/pending/approve", { guid });
  },

  // Mark the request rejected (+ optional reason) without executing it.
  // Human-only server-side.
  pendingReject(guid, reason) {
    return _post("/api/pending/reject", { guid, reason });
  },

  // ===== UI WAVE B: one-blob LLM expansion =====

  // Turn one freeform sentence into structured create-agent/connect-edge
  // fields. kind: 'agent' | 'edge'; source/target only meaningful for 'edge'.
  // → {ok:true, fields:{...}} | {ok:false, error, fallback:{...}} — on ANY
  // failure the server hands back a `fallback` with the raw text stuffed
  // verbatim into role/condition, so the caller always has something to
  // prefill the form with.
  expand({ kind, text, source, target } = {}) {
    return _post("/api/expand", { kind, text, source, target });
  },
};

export default api;
