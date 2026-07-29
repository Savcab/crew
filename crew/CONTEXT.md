# crew — context

## What this area does
The CLI and domain layer of Crew: a general directed graph of long-running
coding agents. A node is one durable agent identity bound to one home directory
and one tmux session; an edge is a user-defined relationship that is ALSO the
authorization for source→target messaging. This package owns the durable data
(in MorphDB), the permission gate over graph edits, agent lifecycle, and the one
messaging path. The dashboard/API lives in `server/`, harness goal-reading in
`harness/`, and the browser UI in the repo's `frontend/` — each has its own doc.

## Key files
- `cli.py` — every operator verb; resolves the caller identity once, then delegates.
- `config.py` — paths, ports, tenancy, caps, launch commands; the knobs everything reads.
- `graphstore.py` — agents/edges/messages in MorphDB over plain HTTP, plus the graph invariants.
- `schema.py` — idempotent bootstrap of the MorphDB app and the agent/edge/audit data model.
- `guard.py` — who may edit the graph, the teaching refusals, and the audit/pending trail.
- `spawn.py` — home dir, worktree, tmux session, runtime launch, identity.md.
- `identity.py` — pure rendering of an agent's identity.md, plus the one writer.
- `runtime.py` — per-runtime facts (executable, window name, native instruction file).
- `mail.py` — the single gated delivery path: queueing, pane-ready typing, caps, budgets.
- `usage.py` — hourly token/cost metering read from runtime transcripts, with availability.
- `notify.py` — fire-and-forget operator webhook for otherwise-silent failures.
- `webhooks.py` — public webhook nodes: turn a request into messages across outgoing edges.
- `ingress.py` — foreground Cloudflare Quick Tunnel lifecycle for those webhooks.
- `ingress_state.py` — the kernel lock that is the source of truth for one live ingress.
- `ingress_watchdog.py` — pipe-EOF child reaper so a killed CLI cannot orphan cloudflared.
- `harness/` — reads what each agent is working toward from its coding harness.
- `server/` — dashboard HTTP API, hook gateway, tmux/PTY bridges.

## Invariants and gotchas
- Config is mostly FROZEN AT IMPORT. Module constants (MORPHDB_HOST, MAX_AGENTS,
  LAUNCH_CMD, WEBHOOK_URL, the cap ceilings) capture env once. The live-read
  exceptions are the functions: config.current_project(), config.current_app(),
  config.crew_root(), config.expand_cmd(), and notify's CREW_WEBHOOK_URL. A test
  that sets $MORPHDB_HOST after importing crew.config silently keeps the old
  value; $CREW_APP works any time.
- The actor is resolved ONCE in cli.main() the same anti-spoofing way messaging
  resolves senders: the live tmux pane wins over $CREW_AGENT / $AGENT_MAIL_NAME,
  so an agent's own shell cannot claim human authority. Resolution fails CLOSED —
  an inherited agent marker that no longer resolves is an error, not a demotion
  to "human". Everything mutating then passes that actor to guard.check().
- Some ops are human-only forever, not merely foreman-gated: remove, bless,
  foreman, approve, reject, revoke_grant, project_create, init, dashboard_control,
  ingress_control, and attaching or changing an edge transform. Agent-facing
  field allowlists are deliberately positive (FOREMAN_AGENT_FIELDS), so a newly
  added persistence field defaults to protected rather than agent-writable.
- At most ONE foreman: granting can_edit_graph is refused while another agent
  holds it (revoking, and re-granting to the current holder, are always allowed).
- Semantic "all" reads go through graphstore._list_all_exact, which pages to
  MorphDB's exact total and raises GraphError on a short or invalid page. The
  foreman singleton, home-conflict, cascade delete, pair authorization and spawn
  quota decisions all depend on this — never answer them from a first page.
- Tenancy is one MorphDB app per project: "crew" for the default project,
  "crew-<project>" otherwise; an explicit $CREW_APP pins the tenant and wins.
  Because MorphDB has no list-apps endpoint, cross-project checks read the
  durable project registry (graphstore.home_conflict_across_apps).
- One agent per directory and no agent nested inside another agent's home,
  checked before anything is created on disk.
- mail.deliver is the only bus; there is no ungated path. An interactive send
  drains queued mail inline before its own delivery, so a box with no dashboard
  running still flushes whenever any crew CLI runs; webhook ingress deliberately
  skips that step and leaves the drain to the dashboard's background flusher.
- Budgets fail closed: usage metrics carry available/value/reason, and a
  configured cap whose dimension cannot be measured refuses the send rather than
  reading as a measured zero. Non-Claude runtimes are deliberately unavailable.
- Harness state directories belong to the harness. Crew opens them read-only and
  briefly and NEVER writes there.
- A webhook URL is a capability for exactly one operation — enqueue a message
  across that hook's outgoing edges. Graph edits and terminal APIs stay behind
  the dashboard cookie boundary.

## When to update this file
- A module is added, removed, or renamed here, or one of the key files above
  stops being the place a job lives.
- A gate, invariant or fail-closed behavior above changes: new human-only op,
  new protected field, a different tenancy or actor-resolution rule.
