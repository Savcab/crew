// api.js — one fetch wrapper per HTTP endpoint (SPEC §HTTP API).
//
// Pure transport: build a URL / shape a body / parse JSON. NO DOM, no state, no
// rendering — every other module talks to the backend ONLY through this object,
// so the request/response shapes live in exactly one place and stay byte-for-byte
// identical to the OLD dashboard (SPEC: "keep all paths/shapes identical so
// nothing else breaks"). Native ES module, no build step.
//
// The backend resolves a session NAME → its live claude pane on every call
// (resolve_target); the FE therefore passes the session NAME as `t`, never a
// pane id (the crewdb `pane` column goes stale on claude restart). That guard
// lives server-side — we just forward the name.

const JSON_HEADERS = { "Content-Type": "application/json" };

// --- private helpers --------------------------------------------------------

// GET <path> → parsed JSON. Every GET endpoint replies JSON (even errors:
// {ok:false,error}/{error}), so callers branch on the parsed body, never status.
async function _get(path) {
  const r = await fetch(path);
  return r.json();
}

// POST <path> with a JSON body → parsed JSON. Mirrors the OLD frontend's
// fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},
// body:JSON.stringify(body)}).json() idiom exactly. `body` defaults to {} so the
// backend's `json.loads(raw or b"{}")` always sees a dict.
async function _post(path, body) {
  const r = await fetch(path, {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify(body || {}),
  });
  return r.json();
}

// encodeURIComponent shorthand — query params may contain '&', '=', spaces
// (PR urls, task ids, refs), so EVERY interpolated value goes through this.
const q = encodeURIComponent;

// --- the API surface --------------------------------------------------------

export const api = {
  // ===== GET =====

  // Live claude sessions + per-session status (detect_status). Drives the
  // header counter, the session list, and the crew node states.
  sessions() {
    return _get("/api/sessions");
  },

  // Native PR render via the authenticated gh CLI (GitHub blocks iframes).
  // → {ok, ...} | {ok:false, error:"url required"} when url is empty.
  pr(url) {
    return _get("/api/pr?url=" + q(url || ""));
  },

  // Worktree diff for a session's live pane (its cwd). `base` is an optional
  // ref; `pr` gates the gh round-trip (pr=1 default attaches link+CI+comments,
  // pr=0 = fast diff-only refresh). Two-phase reopen in sidepanel.js calls this
  // twice: pr:0 (instant) then pr:1 (enrich) — so default pr to 1 only when the
  // caller leaves it undefined, and let an explicit 0 through.
  diff({ t, base, pr } = {}) {
    let url = "/api/diff?t=" + q(t || "");
    if (base) url += "&base=" + q(base);
    // pr defaults to 1 (matches backend default); pass 0 to skip the gh fetch.
    url += "&pr=" + (pr === 0 || pr === "0" ? "0" : "1");
    return _get(url);
  },

  // Full agent-graph snapshot: agents (+ live tmux status) + edges + every
  // independent claude session on the box. Polled by the crew (graph) view.
  graphSnapshot() {
    return _get("/api/graph/snapshot");
  },

  // ===== PTY transport (the real terminal: a `tmux attach` client in a PTY) =====

  // SSE URL for a PTY-attach stream. The caller feeds it to `new EventSource(...)`
  // (term.js). Server spawns `tmux attach` to a grouped view of <target> in a PTY
  // sized to cols×rows, emits an `id` event (the PTY id) then `data` events (raw
  // PTY output, base64). Pass the session NAME.
  ptyStreamUrl(target, cols, rows) {
    return "/api/pty/stream?t=" + q(target || "") + "&cols=" + (cols || 80) + "&rows=" + (rows || 24);
  },
  // Write raw bytes (base64) to a PTY — keystrokes, mouse sequences, chord escapes.
  ptyInput(id, b64) {
    return _post("/api/pty/input", { id, b64 });
  },
  // Resize a PTY (TIOCSWINSZ) → tmux resizes the window to match xterm's grid.
  ptyResize(id, cols, rows) {
    return _post("/api/pty/resize", { id, cols, rows });
  },

  // ===== POST =====

  // Send raw literal keystrokes to a session's live pane. `t` is the session
  // NAME (backend resolve_target pins it to the SAME claude pane we stream from,
  // so keys never land in the session's active-but-different pane and vanish).
  // xterm's onData already encodes special keys to byte sequences, so live
  // typing rides this literal path with enter:false (the named-key path is
  // /api/key, for dashboard-synthesized keys only).
  send({ t, keys, enter } = {}) {
    const body = { t, keys: keys || "" };
    // omit `enter` unless explicitly set so the backend default (True) is
    // preserved for legacy "send a message + ⏎" callers; xterm passthrough
    // passes enter:false.
    if (enter !== undefined) body.enter = enter;
    return _post("/api/send", body);
  },

  // Send ONE named key (ALLOWED_KEYS allow-list server-side, incl. Enter, Esc,
  // arrows, M-Enter=Shift+Enter→newline). Same resolve_target pane as /api/send.
  key({ t, key } = {}) {
    return _post("/api/key", { t, key: key || "" });
  },

  // Resize the focused pane's tmux window to the xterm fit-addon's cols/rows.
  // Only the visible/focused pane calls this — background panes do NOT resize,
  // which kills the multi-viewer size-thrash bug (last-focused viewer owns the
  // window size). Backend clamps the values (fit_session).
  resize({ t, cols, rows } = {}) {
    return _post("/api/resize", { t, cols, rows });
  },

  // Send a message (Enter appended) to EVERY live claude session at once.
  // → {results:[{target, ok}, …]}.
  broadcast(keys) {
    return _post("/api/broadcast", { keys: keys || "" });
  },

  // Ensure the session has at least one shell window; → its target + the full
  // window list for the dock tab bar. `session` is the session NAME.
  shell(session) {
    return _post("/api/shell", { session });
  },

  // Create the next-numbered shell window (shell-2, shell-3, …) in the session.
  shellNew(session) {
    return _post("/api/shell/new", { session });
  },

  // Kill ONE shell window by name. The backend REFUSES any window not matching
  // ^shell(-\d+)?$ — the claude window can never be killed through the API.
  shellKill({ session, window } = {}) {
    return _post("/api/shell/kill", { session, window });
  },

  // ===== POST agent-graph mutations =====
  // The dashboard calls crew.graphstore / crew.spawn server-side (no CLI shell-
  // out). Each → {ok, ...} | {ok:false, error}.

  // Spawn a new long-running agent: home-uniqueness enforced (one per dir, no
  // nesting), tmux session + claude launched, identity.md written.
  agentCreate({ name, role, identity, home, repo, launch } = {}) {
    return _post("/api/agent/create", { name, role, identity, home, repo, launch });
  },

  // Adopt an already-running independent claude session as a crew agent (anchors
  // a durable identity to its cwd; starts no new terminal).
  agentAdopt({ session, name, role } = {}) {
    return _post("/api/agent/adopt", { session, name, role });
  },

  // Delete an agent (and, by default, kill its tmux session; home dir is kept).
  agentRemove(name) {
    return _post("/api/agent/remove", { name });
  },

  // Connect two agents → defines a relationship AND authorizes source→target
  // messaging. `directed:false` makes it two-way. source/target are agent names.
  edgeCreate({ source, target, label, description, condition, directed } = {}) {
    return _post("/api/edge/create", { source, target, label, description, condition, directed });
  },

  // Edit an edge by guid (label / description / condition / directed).
  edgeUpdate(fields = {}) {
    return _post("/api/edge/update", fields);
  },

  // Delete an edge by guid.
  edgeDelete({ guid } = {}) {
    return _post("/api/edge/delete", { guid });
  },
};

export default api;
