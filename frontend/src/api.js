// api.js — one fetch wrapper per HTTP endpoint. Pure transport: build a URL /
// shape a body / parse JSON. NO DOM, no state, no rendering — every other module
// talks to the backend ONLY through this object. Native ES module, no build step.
//
// The dashboard manages ONLY crew agents, so the surface is small: the graph
// snapshot, the PTY terminal transport (crew sessions only — the server refuses
// anything else), and the agent/edge mutations. The backend resolves a session
// NAME → its live runtime pane on every call, so the FE passes the NAME as `t`.

const JSON_HEADERS = {
  "Content-Type": "application/json",
  // Cookie auth alone is vulnerable to same-site requests from another
  // localhost port. This non-simple header forces a cross-origin preflight;
  // the dashboard never grants CORS access.
  "X-Crew-CSRF": "1",
};

// The CLI opens `/#cap=...`. URL fragments never reach the HTTP server or its
// logs; exchange it once for an HttpOnly SameSite cookie, then erase it from
// the address bar/history. Reloads reuse the cookie and need no fragment.
let authReady = Promise.resolve();

function _exchangeFragmentCapability() {
  const fragment = new URLSearchParams(window.location.hash.replace(/^#/, ''));
  const capability = fragment.get('cap');
  if (!capability) return authReady;

  // Serialize repeated fragment changes, but let a later correct capability
  // recover from an earlier rejected one. Keeping the rejected promise itself
  // in authReady means API callers still see the authorization error.
  authReady = authReady.catch(() => {}).then(() => fetch('/api/auth/bootstrap', {
      method: 'POST', headers: JSON_HEADERS,
      body: JSON.stringify({ capability }),
    }).then(async r => {
      const body = await r.json();
      if (!r.ok || !body.ok) throw new Error(body.error || 'operator authorization failed');
      history.replaceState(null, '', window.location.pathname + window.location.search);
    }));
  // A hashchange may happen while no request is waiting on authReady. Attach a
  // rejection observer so browsers do not emit an unhandled-promise error;
  // awaiting the original promise below still rejects normally.
  void authReady.catch(() => {});
  return authReady;
}

_exchangeFragmentCapability();
window.addEventListener('hashchange', _exchangeFragmentCapability);

async function _jsonResponse(response) {
  let body;
  try {
    body = await response.json();
  } catch (error) {
    throw new Error(`dashboard returned HTTP ${response.status} without JSON`);
  }
  if (!response.ok) {
    throw new Error((body && body.error)
      || `dashboard request failed (HTTP ${response.status})`);
  }
  return body;
}

async function _get(path) {
  await authReady;
  const response = await fetch(path);
  return _jsonResponse(response);
}

// POST <path> with a JSON body → parsed JSON. `body` defaults to {} so the backend's
// `json.loads(raw or b"{}")` always sees a dict.
async function _post(path, body) {
  await authReady;
  const response = await fetch(path, {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify(body || {}),
  });
  return _jsonResponse(response);
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

  // ===== apps gallery: every project is one graph =====

  // All known graphs + descriptions + agent counts + live dashboards.
  projects() {
    return _get("/api/projects");
  },
  // Create a graph: registers the project, pushes schema, and (by default)
  // seeds + launches a foreman primed to build the crew from a description.
  // `title` is free text (spaces fine) — the server derives the machine slug.
  projectCreate({ name, title, description, foreman, launch } = {}) {
    return _post("/api/project/create",
      { name, title, description, foreman, launch });
  },
  // Find-or-spawn the dashboard owning a graph → its capability URL to visit.
  projectOpen(name) {
    return _post("/api/project/open", { name });
  },

  // ===== dock tabs: the docked session's tmux windows =====

  // List a crew session's windows for the tab bar. `id` (the live stream's PTY
  // id) marks which window THAT view is showing. → {ok, windows:[{id,name,active}]}.
  ptyWindows(target, id) {
    return _get("/api/pty/windows?t=" + q(target || "")
      + (id ? "&id=" + q(id) : ""));
  },
  // New shell window (the "+" tab) in the BASE session — the agent's own
  // current window is untouched. → {ok, window:{id,name}}.
  ptyWindowCreate(target) {
    return _post("/api/pty/window/create", { t: target });
  },
  // Switch THIS view's current window (a tab click); grouped sessions select
  // independently, so only the dock's screen changes.
  ptyWindowSelect(id, window) {
    return _post("/api/pty/window/select", { id, window });
  },

  // ===== POST agent-graph mutations =====
  // The dashboard calls crew.graphstore / crew.spawn server-side (no CLI shell-
  // out). Each → {ok, ...} | {ok:false, error}.

  // Spawn a new long-running agent: home-uniqueness enforced (one per dir, no
  // nesting), tmux session + selected runtime launched, native identity written.
  // `launch_cmd` overrides the per-environment default launch command.
  agentCreate({ name, role, identity, home, repo, launch, runtime, launch_cmd } = {}) {
    return _post("/api/agent/create", { name, role, identity, home, repo, launch, runtime, launch_cmd });
  },

  // Revive a down agent: re-create its tmux session or start its runtime.
  // The record already exists; only the live session died. → {ok, agent}.
  agentStart({ name } = {}) {
    return _post("/api/agent/start", { name });
  },

  // Delete an agent (and, by default, kill its tmux session; home dir is kept).
  agentRemove(name) {
    return _post("/api/agent/remove", { name });
  },

  // Create/configure a source-only webhook graph node. The returned/snapshot
  // `public_url` may be relative (local dashboard) or an externally configured
  // reverse-proxy origin; the UI resolves relative values against this page.
  webhookCreate({ name, description, template } = {}) {
    return _post("/api/webhook/create", { name, description, template });
  },
  webhookUpdate({ guid, description, template } = {}) {
    return _post("/api/webhook/update", { guid, description, template });
  },
  webhookRotate(guid) {
    return _post("/api/webhook/rotate", { guid });
  },
  webhookDelete(guid) {
    return _post("/api/webhook/delete", { guid });
  },

  // Connect two agents → defines a relationship AND authorizes messaging. Each
  // direction carries a LIST of trigger `conditions`, the receiver's action, and a
  // reply flag; the `back_*` fields describe the target→source direction of a
  // two-way (`directed:false`) edge. source/target are agent names.
  // `token_cap`/`cost_cap` (WAVE 3) budget the TARGET's hourly runtime spend —
  // 0/undefined means uncapped.
  edgeCreate(f = {}) {
    return _post("/api/edge/create", {
      source: f.source, target: f.target, label: f.label,
      conditions: f.conditions, target_action: f.target_action, reply_expected: f.reply_expected,
      back_conditions: f.back_conditions, back_action: f.back_action, back_reply: f.back_reply,
      max_turns: f.max_turns, token_cap: f.token_cap, cost_cap: f.cost_cap,
      directed: f.directed, transform: f.transform,
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
