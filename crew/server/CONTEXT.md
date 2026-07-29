# crew/server — context

## What this area does
Serves the crew dashboard: one loopback HTTP process per project that hosts the
SPA, exposes the agent graph as a JSON/SSE API, and bridges a browser terminal to
the tmux session of each crew-spawned agent. Graph mutations the UI performs
(create/remove agents, wire edges, bless rows, approve pending edits) land here
and call crew.graphstore / crew.spawn in the same process; only cross-project
work (creating a graph, starting a sibling dashboard) shells out to the crew
CLI. A second, separate server terminates tunnelled public webhook traffic.

## Key files
- `app.py` — the whole dashboard: routing tables, auth, per-path validation, and
  every API handler, as one BaseHTTPRequestHandler on a ThreadingHTTPServer. Its
  module docstring lists the API surface.
- `hook_gateway.py` — standalone loopback server the tunnel points at; admits one
  exact hook path plus a secret-gated readiness probe, and nothing else.
- `ptyio.py` — the terminal transport: a real tmux attach client in a PTY per
  browser stream, so tmux itself renders and reflows. Docstring says why.
- `tmuxio.py` — tmux shell-outs: session-to-pane resolution, status detection for
  the graph, and the readiness gate messaging waits on before typing into a pane.
- `../../static/index.html` — the SPA this server hosts. Build output of
  frontend/ (vite outDir), not a source file; see frontend/CONTEXT.md.

## Invariants and gotchas
- Routing is hand-rolled if/elif over the parsed path, not a framework. A new
  endpoint touches FOUR places: the `_GET_API_PATHS` or `_POST_API_PATHS`
  frozenset, the validation tables, the `_dispatch_post` chain, and the handler
  itself. The frozensets exist to answer 405 with the right Allow header for the
  wrong verb, so skipping one makes the endpoint 404 instead.
- Every mutation is a POST behind three gates in this order: the operator
  capability cookie (`_operator_authorized`), a JSON content type, then
  `_csrf_authorized` — a fixed `X-Crew-CSRF: 1` header plus, when the browser
  sends Origin, an exact match against this request's scheme+Host. The Origin
  binding is not redundant: SameSite cookies are scoped per-site and ignore
  ports, so a page on another localhost port does get this cookie attached.
  `/api/pty/stream` is a GET but opens and resizes a tmux view, so it repeats
  the Origin check.
- `/api/health` is the only unauthenticated GET (the CLI reads it to make
  PID-file shutdown safe). `/api/auth/bootstrap` is the one POST handled before
  the cookie check — and therefore before CSRF/Origin — because its job is to
  trade the capability for the HttpOnly cookie. An empty `OPERATOR_CAPABILITY`
  fails closed: a bare `python3 -m crew.server.app` serves only the UI shell,
  static assets, and health.
- Field validation is table-driven and runs BEFORE any handler:
  `_BOOLEAN_FIELDS_BY_PATH`, `_TEXT_FIELDS_BY_PATH`, `_TEXT_LIST_FIELDS_BY_PATH`,
  keyed by path. Types are checked exactly and never coerced, because
  `bool("false")` is True and `bool(0)` is False — coercion silently inverts
  launch, deletion, topology, and governance intent. Adding a field to an
  endpoint means adding it to the table; omitted fields keep handler defaults.
- One dashboard process owns one project. Opening another graph starts a SIBLING
  dashboard on a free port through the CLI and hands the browser
  `http://127.0.0.1:<port>/#cap=<capability>` — the capability travels in a URL
  fragment, which never reaches an HTTP log or Referer. Siblings are found by
  globbing `dashboard-*.cap` under `config.VAR` and probing each `/api/health`;
  cookie names are per-port (`crew_operator_<port>`) so two dashboards do not
  overwrite each other's capability.
- Both servers bind 127.0.0.1 only, and `hook_gateway` raises rather than bind
  anything else. `/hooks/<token>` is the only surface intended to face the public
  internet: it consults no cookie, and its failures collapse to generic messages
  so a public caller learns nothing about MorphDB, the filesystem, or runtimes.
  The two hook routes accept different token shapes — `app.py` takes 40-128 URL-
  safe chars, the gateway exactly 43 on the raw request target.
- Handlers pass `actor="human"` to graphstore for bless, foreman, and pending
  decisions. Holding the operator cookie IS the human claim, so do not give an
  agent-reachable path into these handlers.
- Terminal endpoints refuse any tmux session crew does not own (see
  `_crew_live_session`), so a Claude Code, Codex, or shell session the user
  started themselves is never listed, attached, or resized.
- SSE handlers write their own response bytes. Once `_pty_stream` has sent
  headers a failure can no longer become a JSON error, which is why its setup
  failures are caught separately in `do_GET`.
- Behavior here is pinned by `../../tests/test_dashboard_auth.py`,
  `test_dashboard_api.py`, `test_hook_gateway.py`, and `test_ptyio.py`.

## When to update this file
- A new endpoint or module, or a change to the auth/CSRF/validation pipeline or
  to how capabilities are minted, stored, or handed to the browser.
- Moving the boundary between the dashboard, the hook gateway, and the terminal
  transport — which server or module owns which surface.
