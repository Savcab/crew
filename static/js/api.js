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

  // Delete an agent (and, by default, kill its tmux session; home dir is kept).
  agentRemove(name) {
    return _post("/api/agent/remove", { name });
  },

  // Operator → agent: seed/steer one of your agents directly (NOT peer mail, so it
  // bypasses the edge gate). Readiness-gated server-side. → {ok, message}.
  agentSay({ name, text } = {}) {
    return _post("/api/agent/say", { name, text });
  },

  // Connect two agents → defines a relationship AND authorizes source→target
  // messaging. The edge carries BOTH sides: `condition` (when source messages) and
  // `target_action`/`reply_expected` (what target does on receipt), plus `max_turns`
  // (cap on exchanges). `directed:false` makes it two-way. source/target are names.
  edgeCreate(f = {}) {
    return _post("/api/edge/create", {
      source: f.source, target: f.target, label: f.label, description: f.description,
      condition: f.condition, target_action: f.target_action,
      reply_expected: f.reply_expected, max_turns: f.max_turns, directed: f.directed,
    });
  },

  // Edit an edge by guid (label / description / condition / target_action /
  // reply_expected / max_turns / directed).
  edgeUpdate(fields = {}) {
    return _post("/api/edge/update", fields);
  },

  // Delete an edge by guid.
  edgeDelete({ guid } = {}) {
    return _post("/api/edge/delete", { guid });
  },
};

export default api;
