# crew

**Run persistent coding-agent teams as a graph you design.**

Crew gives each agent its own workspace and tmux session, then lets you connect
Claude Code, Codex CLI, or custom interactive agents with governed messaging
relationships. The dashboard is an n8n-like visual surface for arranging and
watching the team, but Crew is not a DAG scheduler: it does not evaluate edge
conditions or automatically execute nodes. Agents decide when a written
condition applies and call `crew message`; Crew enforces whether that message is
authorized, how it is transformed, and which limits apply.

```text
   ┌─────────┐   "when the plan is ready"   ┌─────────┐
   │ planner │ ───────────────────────────▶ │ builder │
   └─────────┘                               └────┬────┘
          ▲       "when review is needed"        │
          └───────────────────────────────────────┘
```

What Crew provides:

- A durable agent record, dedicated workspace, managed tmux session, and
  runtime-specific identity for every agent.
- A visual graph and live terminal dashboard for Crew-managed sessions only.
- Source-only webhook nodes that turn external HTTP payloads into durable
  messages for every connected agent.
- Directed or two-way agent mail with durable logs, queueing, rate limits,
  usage budgets, and optional transforms.
- Projects that isolate MorphDB data, default workspaces, and tmux session
  names.
- Governed graph self-modification: one foreman can make bounded changes, while
  sensitive operations remain human-only or require approval.
- Audit history, pending approvals, file grants, and operator notifications.

## Product model

| Concept | Meaning |
|---|---|
| **Agent** | One durable record, one non-overlapping workspace, one managed tmux session, and one selected runtime: `claude`, `codex`, `hermes`, or `custom`. |
| **Webhook node** | A source-only graph node with a secret POST URL. It renders an incoming JSON, form, or text body into one message and durably fans it out across its directed agent edges. |
| **Edge** | One message authorization. A directed edge permits source → target; a two-way edge permits both directions. Each direction can describe conditions, receiver actions, and reply expectations. |
| **Identity** | Every agent gets `identity.md`. Claude also gets a managed block in `CLAUDE.md`; Codex gets one in `AGENTS.md` and in an existing active `AGENTS.override.md`. Crew preserves content outside its markers. Hermes and custom runtimes get only `identity.md`. |
| **Project** | An isolated MorphDB app plus project-scoped default workspaces and tmux names. The default project uses app `crew`; project `demo` uses app `crew-demo`. |
| **Foreman** | The single agent allowed to make bounded topology changes through the CLI. Its authority is constrained by ownership, quotas, and finite edge caps. |
| **Pending request** | A foreman action that needs human authority, such as connecting to a human-created node, raising an edge cap, or requesting a file grant. |

### What is enforced

The natural-language edge fields—conditions and receiver actions—are standing
instructions written into each endpoint's identity. They are advisory because a
model can ignore instructions. Crew does not watch a condition and fire an edge
automatically.

The delivery path enforces these properties in code:

- Agent names are unique inside a project. Creation and the durable agent-update
  boundary take an interprocess invariant lock, so concurrent writers cannot
  claim the same identity. Crew does not currently expose an agent-rename CLI
  or dashboard control.
- A unique edge must authorize the ordered sender → target pair. Ambiguous or
  overlapping authorizations are rejected, including concurrent create/create
  and create/update requests from separate processes.
- A reply expectation is valid only on a two-way edge, so the reply is actually
  authorized.
- `max_turns`, `token_cap`, and `cost_cap` are checked before accepting a new
  message.
- An attached transform runs during acceptance and never during queue flush.
  Once its durable message/filter row exists, retries reuse that result.
- Sender identity inside a managed agent comes from its live tmux session, not
  from a mutable `CREW_AGENT` value.
- A detached CLI process that still carries `CREW_AGENT` or
  `AGENT_MAIL_NAME` must resolve that marker to a current agent; stale or forged
  markers fail closed instead of inheriting operator authority.

The gate covers the sanctioned `crew message` channel. Crew does not sandbox an
agent's shell, so an unrestricted same-user process can still use side channels
such as direct tmux commands, shared files, or localhost requests. An
unrestricted same-user process can also strip every inherited environment
marker; use a separate OS user or sandbox when caller identity must resist a
hostile local process.

## Requirements

| Tool | Requirement |
|---|---|
| Python | 3.10 or newer. The Crew server and CLI use the standard library. |
| tmux | Required; each agent lives in a managed session. |
| MorphDB | Required data backend: `python3 -m pip install morphdb`. |
| Agent runtime | Install Claude Code, Codex CLI, or Hermes, or provide a custom interactive launch command. Claude is the default. |
| Git | Optional; required only for `spawn-agent --repo`. |
| cloudflared | Optional; the official binary is required only for temporary public webhook ingress. |

MorphDB defaults to `127.0.0.1:8787`. The dashboard binds only to
`127.0.0.1:8788`. Override them with `MORPHDB_HOST` and `CREW_PORT`.
For a temporary public HTTPS origin, install the official `cloudflared` binary
and run `crew ingress run`. Crew itself never installs or updates the binary,
and the dashboard remains loopback-only. Operators with their own hook-only
proxy may instead set `CREW_WEBHOOK_PUBLIC_BASE_URL` to its TLS origin.

The optional natural-language **Generate** action in the dashboard uses a
`claude -p --output-format json`-shaped command by default. Manual forms work
without Claude. Set `CREW_EXPAND_CMD` to another compatible command when needed;
malformed or unavailable expansion falls back to the original text for review.

## Setup

From this checkout:

```bash
python3 -m pip install morphdb
morphdb start

# Optional for the operator shell. Managed agent sessions receive this PATH
# entry automatically, so they can call `crew` without a global install.
export PATH="$PWD/bin:$PATH"

./bin/crew init
```

`crew init` creates or updates the current project's MorphDB schema and starts
the dashboard. It prints an operator URL containing that dashboard process's
capability in the URL fragment. Use that exact URL, or run:

```bash
./bin/crew dashboard open
```

Do not start the control plane with `python3 -m crew.server.app`: without a
capability supplied by the CLI, mutations and terminal attachment intentionally
fail closed.

### Private tmux endpoint and upgrades

Crew uses its own owner-only tmux socket for every lifecycle, mail, status, and
dashboard-terminal operation. It never follows an inherited `TMUX`,
`TMUX_PANE`, or `TMUX_TMPDIR` into a personal tmux server. On macOS the default
socket is under `/private/tmp/claude/crew-<uid>-tmux/crew.sock`, which keeps it
reachable from agent sandboxes that allow the Claude runtime tree. Other
platforms use Crew's private per-user runtime directory. When an installation
needs a different sandbox-allowed location, put that absolute directory path on
one line in the owner-only fixed config file `~/.config/crew/tmux-root` (mode
`0600`). Crew creates or tightens the selected directory to mode `0700` and
refuses unsafe config files, symlinks, foreign ownership, and unsafe socket
files. Mutable agent/process values such as `CREW_TMUX_TMPDIR`,
`CREW_TMUX_SOCKET`, `TMUX`, `TMUX_PANE`, and `TMUX_TMPDIR` never select Crew's
control-plane endpoint.

Managed panes export `CREW_TMUX_SOCKET`. For a manual attachment from a shell
that carries that context, use the explicit endpoint and the session name shown
by `crew status`:

```bash
tmux -S "$CREW_TMUX_SOCKET" attach-session -t planner
```

Pre-upgrade Crew sessions may still be running on tmux's system-default server.
Crew recognizes one only when its canonical session, complete Crew ownership
environment, and exact stored-pane binding all match; ordinary extra split
panes are allowed. Neither `crew up` nor `crew restart` interrupts an active
legacy conversation. Migrate it explicitly when ready with `crew down <agent>`,
then `crew up <agent>`. A same-named personal/default session is ignored and
remains untouched.

## Quickstart

Create a Claude planner and a Codex builder:

```bash
./bin/crew spawn-agent planner \
  --role "plans small implementation tasks" \
  --runtime claude

./bin/crew spawn-agent builder \
  --role "implements and verifies the plan" \
  --runtime codex
```

Connect them in both directions. The written conditions tell the agents when to
send; `--undirected` is what authorizes the return message.

```bash
./bin/crew connect planner builder \
  --label "plan and implementation" \
  --when "when an implementation plan is ready" \
  --does "implement it, run tests, and report the result" \
  --reply \
  --undirected \
  --when-back "when implementation or review is complete" \
  --does-back "review the result and decide the next step"
```

Seed the first agent from an operator shell, then watch the live sessions:

```bash
./bin/crew kickoff planner "Plan and implement a small verified change."
./bin/crew dashboard open
```

Inside `planner`'s managed terminal, Crew's CLI is already on `PATH`:

```bash
crew whoami
crew peers
crew message builder "The plan is in progress.md; implement and test it."
```

If no explicit workspace is supplied, the default is:

```text
$CREW_ROOT/<project>/<agent>
```

`CREW_ROOT` defaults to `~/crew`, so `planner` in the default project uses
`~/crew/default/planner`; agent `builder` in project `demo` uses
`~/crew/demo/builder`. One agent may not share, contain, or be nested inside
another agent's workspace.

### Runtime selection

```bash
# Claude default
crew spawn-agent a --runtime claude

# Codex, launched unattended with per-workspace trust and Crew's PATH
crew spawn-agent b --runtime codex

# Hermes's TUI (one per-user install; goals come from its kanban board)
crew spawn-agent h --runtime hermes

# Any other interactive command; no native instruction file is assumed
crew spawn-agent c --runtime custom --launch-cmd "my-agent --interactive"

# Create the tmux session and identity, but do not start the runtime yet
crew spawn-agent d --runtime codex --no-launch
crew up d
```

Without `--runtime`, Crew infers Claude, Codex, or Hermes from `--launch-cmd`;
otherwise it uses `CREW_RUNTIME`, defaulting to Claude. Defaults can be changed
with `CREW_CLAUDE_LAUNCH_CMD`, `CREW_CODEX_LAUNCH_CMD`, and
`CREW_HERMES_LAUNCH_CMD` (the legacy `CREW_LAUNCH_CMD` still configures
Claude).

The built-in commands are intentionally unattended:

- Claude: `claude --dangerously-skip-permissions`
- Codex: `codex --dangerously-bypass-approvals-and-sandbox --disable hooks`,
  with per-workspace trust and Crew's CLI path added

These flags remove approval and sandbox barriers; use them only in workspaces
and under an OS account whose access you are willing to give the agent.

`--no-launch` still creates a real tmux session and is reported as
`not_started`, not down. Claude and Codex process state is reported separately
from session liveness. A custom process can be detected by its executable, but
its interactive state is reported as `unknown`; Crew will not claim it is idle.

### Harness goals

`crew activity` is what an agent chooses to tell you. `crew harness` is what its
coding harness already knows — the open goals it is working toward:

```bash
crew harness             # every agent
crew harness AgentA      # one
crew harness --json      # machine-readable
```

Crew operates one level above coding harnesses, and each harness records its
goals completely differently. `crew.harness.Harness` is the base class that
normalizes that: its responsibility is only what Crew shows the user (the open
goals), never a full model of a harness. Three clients implement it, each
reading its harness's own durable state, so an agent needs no cooperation and
has nothing to keep up to date:

| Runtime | Where the goals live |
|---|---|
| Claude Code | task store of the live session registered for the agent's home |
| Codex CLI | `goals_<N>.sqlite` thread goals, joined to the thread whose `cwd` is the agent's home |
| Hermes | the `~/.hermes/kanban.db` board (machine-wide — Hermes runs as one per-user install) |

A runtime Crew has no reader for says so rather than reporting an empty goal:

```
  toolbox: Custom command has no goal state Crew can read
```

Reading another product's private layout is the trade for needing no
cooperation, and it can break when that product moves its files.
`CREW_RUN_HARNESS_LIVE=1 python3 tests/test_harness_live.py` is the canary —
run it against the real installs after upgrading any of the three. Nothing
under a harness's state directory is ever written; `CREW_CLAUDE_STATE_DIR`,
`CREW_CODEX_STATE_DIR`, and `CREW_HERMES_STATE_DIR` relocate where Crew reads
from. Adding a harness is one `Harness` subclass plus its runtime adapter.

## Projects and worktrees

Create and select projects with the top-level `--project` option before the
subcommand, or set `CREW_PROJECT`:

```bash
crew project create demo
crew project list
crew --project demo spawn-agent api --runtime codex
CREW_PROJECT=demo crew agents
```

Named-project sessions are prefixed, such as `demo__api`, while the agent's mail
identity remains `api`. The same plain agent name can therefore exist in two
projects without sharing data or sessions. Caller resolution fails closed if an
agent pane tries to select a project other than the one that owns it.

The dashboard serves one project per process and has no project switcher. Stop
the current dashboard before opening another project on the same port, or use a
different port for concurrent dashboards:

```bash
crew dashboard stop
crew --project demo dashboard start

# Or leave the default dashboard on 8788 and use 8790 for demo.
CREW_PORT=8790 crew --project demo dashboard start
```

`--repo /path/to/repo` creates a persistent named worktree branch
`crew/<project>/<agent>`. The default project's worktree directory is
`<repo>-worktrees/<agent>`; named projects use
`<repo>-worktrees/<project>__<agent>`. Removing an agent leaves its workspace,
worktree, branch, and files intact for explicit cleanup or recovery.

## Messaging and budgets

Agent mail always identifies the sender from the managed session:

```bash
crew message <target> <text...>
crew message --no-prefix <target> <text...>
crew mail [<agent>] [--status STATUS] [-n N]
```

Every accepted message is first recorded as `queued`, including immutable
sender, target, and authorizing-edge GUID snapshots plus delivery options. When
the runtime is ready, Crew claims the row as `submitting` under the target lock
before creating a multiline inbox file or typing into tmux. Confirmed idle
submission ends as `delivered`; acceptance into a working Codex next-turn queue
ends as `runtime_queued`.

Delivery is at-most-once, not exactly-once. Only a proven pre-launch failure
where no tmux command ran returns to `queued`. Once tmux may have acted, an
unconfirmed attempt becomes `delivery_uncertain` and is never retried
automatically; if durable finalization itself is unavailable, `submitting` is
also non-retryable. Deleted identities and same-name replacements never inherit
old queued mail. Older safe queued mail stays ahead of newer mail; dashboard and
CLI flushes progress it headlessly. Rows still queued after one hour become
`failed`, notify the operator, and best-effort bounce to the original sender
identity. Custom runtimes remain queued when Crew cannot establish a safe idle
state. Multiline bodies are stored in full under the target's `.crew-inbox/`;
the prompt receives a one-line pointer.

Edge limits apply to the target's trailing-hour usage:

```bash
crew connect a b --max-turns 10 --token-cap 100000 --cost-cap 2.50
crew cap a b --max-turns 5 --token-cap 50000
```

Claude token and cost usage is read from its local transcripts; input, cache
creation, cache read, and output tokens all count. A complete all-zero usage
record is valid. Any in-window assistant record with missing, empty, partial,
non-integer, or negative usage fields makes both metrics unavailable instead of
silently contributing zero. Token totals remain available for an otherwise
valid record whose model is unknown, but its cost is unavailable because Crew
cannot price it. Sonnet 5 records through August 31, 2026 UTC use the introductory
$2 input / $10 output per-million-token rates; records beginning September 1 use
$3 / $15, with each record priced by its own timestamp. Codex and custom usage
meters are currently unavailable. If an edge configures a metric Crew cannot
measure, delivery fails closed with status `budget_unavailable`; it is never
treated as zero spend.

### Transforms

A human can attach a Python transform located under `var/transforms/`:

```bash
crew connect a b --transform var/transforms/redact.py
```

Crew sends the original body on stdin. Exit 0 plus non-empty stdout replaces
the message; empty output, nonzero exit, or timeout drops it with status
`filtered` and notifies the operator. A transform runs during acceptance and
never re-runs during queue flush. Once Crew durably records the transformed or
filtered result, a provider retry reuses it. A process crash after the script
runs but before that row is stored can run the script again, so transform
scripts must treat external side effects as at-least-once. Attaching or
changing one is human-only, and the path must name a regular, non-symlink file
inside `var/transforms/` (with no symlinked parent below that root). Crew
executes a stable anonymous snapshot so a pathname swap cannot change the
selected code. Transform stderr is never forwarded to senders or webhooks.
`redact.py`, `squeeze.py`, and `scrub.py` are included as examples.

### Inbound webhook nodes

Create a hook from the dashboard with **+ Hook** or from the CLI, optionally
give it a message template, and connect it to one or more agents:

```bash
crew webhook create github-issues \
  --description "GitHub issue events" \
  --template "New issue: {{ payload.issue.title }}"
crew webhook show github-issues
crew connect github-issues triage \
  --max-turns 10 --token-cap 50000 --cost-cap 1
```

Hook edges are always directed `hook → agent`; hooks cannot receive edges or
replies. `crew webhook list` omits secret URLs. `show`, `update`, `rotate`, and
`remove` disclose or mutate one hook and are available to a human or to the
active Foreman that created it. Clicking the hook card provides the same
controls to the local operator.

To make every hook in the current project temporarily reachable from the
Internet, run the foreground ingress in one operator shell:

```bash
brew install cloudflared  # once, on macOS
crew ingress run
```

Then use `crew webhook show github-issues` in another shell to copy its active
public URL. `crew ingress status` reports the current origin. Control-C removes
that origin and stops the exact tunnel child. A parent-death watchdog also
stops it if the foreground CLI is killed. The tunnel points through a fresh
private Unix socket to a separate hook-only gateway: dashboard, API, static,
and terminal paths are not implemented there and return `404`.

For example, a template can select fields from a JSON request:

```text
New issue: {{ payload.issue.title }}
Repository: {{ payload.repository.full_name }}
Event: {{ headers.x-github-event }}
```

Then call the URL returned by the dashboard:

```bash
curl -X POST "$CREW_HOOK_URL" \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: provider-delivery-123' \
  -d '{"issue":{"title":"Queue retries"},"repository":{"full_name":"acme/crew"}}'
```

Templates can read `payload.*`, non-credential lower-case `headers.*`, numeric
array indexes, and `raw`. Authorization, cookie, proxy-credential, and
`Set-Cookie` headers are never available to templates. A blank template uses
`payload.message`, then `payload.text`, then the complete payload. Missing
fields or malformed input reject the request before fan-out. Templates are
data, not executable code; a human-attached edge transform remains available
for code-level processing.

The public route accepts JSON, form-encoded, or UTF-8 text bodies and returns
`202` with one result per target after each message is durably queued. It does
not wait for agent runtimes. `Idempotency-Key`, `X-GitHub-Delivery`,
`X-Webhook-Id`, and `Webhook-Id` prevent duplicate fan-out; the stored receipt
contains only a hash of the provider key and freezes the rendered message plus
exact edge/endpoint identities. A retry can neither pick up a changed template
nor redirect an old payload through a replaced route. Edge rate/budget limits
and transforms apply normally. Unknown or malformed hook paths are rejected
before their declared body is read, infrastructure failures remain retryable,
and public per-route failures are generic; detailed diagnostics remain
operator-only. See [the webhook-node technical specification](docs/webhook-nodes.md)
for the complete behavior and deployment model.

## Governance and agent self-modification

Calls made inside a Crew-managed pane are attributed to that agent. Calls from
an ordinary operator shell are attributed to `human`. Every applied, refused,
pending, approved, or rejected graph edit is recorded in `crew audit`; rows
created by agents remain visibly unblessed until a human reviews them.

A plain agent may inspect the graph and mail, message authorized peers, note
its own node or an incident edge, publish its own activity line
(`crew activity "working on website…"` — shown on its graph card; peers read
all lines with `crew activity`, no mail needed), and lower a rate or budget
cap on an edge where it is an endpoint. A cap
raise—including changing a finite cap to `0` (unlimited)—becomes a pending
request. It cannot create or remove topology, grant authority, approve requests,
attach transforms, or use the human-only `crew kickoff` bypass.

One agent may be the foreman:

```bash
crew foreman planner
crew foreman planner --revoke
crew bless planner
crew bless --edge planner builder
crew bless --all
```

The foreman may spawn agents, create and configure webhook nodes, connect or
disconnect its own envelope, and bring its own agent children up or down. It is
limited to itself plus nodes carrying its immutable creator GUID, defaults to
at most 12 total agents, 12 owned webhooks, and four agent-authored spawns per
hour, and must give agent-created edges finite positive limits no higher than
30 messages/hour, 500,000 tokens/hour, and $5/hour. Configure those ceilings
with `CREW_MAX_AGENTS`, `CREW_MAX_WEBHOOKS_PER_FOREMAN`,
`CREW_SPAWN_RATE`, and the `AGENT_EDGE_*_CEILING` variables.

A Foreman cannot read, rotate, update, or remove a user-created hook or one
created by a different immutable Foreman GUID. Revoking or deleting the owner
does not interrupt its hook: the URL and existing routes remain live and become
human-managed. Reusing the old Foreman name does not transfer ownership.

A foreman cannot choose a child's custom workspace, repository, runtime, launch
command, or foreman flag. It cannot remove agents, bless changes, grant/revoke
foreman, approve/reject requests, revoke grants, or attach transforms. A request
to connect to a human-created node or obtain a file grant enters the pending
queue instead of taking effect.

```bash
crew pending
crew approve <guid-or-unique-prefix>
crew reject <guid-or-unique-prefix> --why "reason"
crew audit [-n N] [--refused] [--actor NAME]
crew note agent <name> "text"
crew note edge <source> <target> "text"
```

Approvals durably claim the stored request before replay and notify the
requester; rejection records the reason without applying the change. Per-request
locks allow only one approve/reject winner. A persistence, replay, or
finalization failure is surfaced and leaves a non-pending diagnostic state, so
an applied mutation cannot be replayed as if it were still awaiting review.

### File grants

```bash
crew grant <agent> <path> [--ro|--rw]
crew grants [<agent>]
crew revoke-grant <agent> <grant-name>
```

A grant requires an existing target and creates
`<agent-home>/refs/<name>` as a symlink, recording the path, mode, author, and
time in the agent identity and audit log. Crew refuses a symlinked stored home
or `refs/` directory, validates revoke names as one safe path component, and
serializes changes per agent. Filesystem, durable grant state, and identity
updates compensate one another on failure instead of reporting a partial grant.
`ro` and `rw` are declared intent, not filesystem permissions. Crew does not
mount, chmod, or sandbox the path, and an unrestricted shell may already be able
to reach it. Human grants apply immediately; a foreman's request is pending; a
plain agent's request is refused. Revocation is human-only.

## Dashboard

The dashboard currently supports:

- Viewing, panning, zooming, and arranging the current project's graph.
- Creating Claude, Codex, or custom agents, including optional worktrees and
  `--no-launch`-equivalent creation.
- Creating and configuring webhook nodes, copying or rotating their capability
  URLs, and connecting them to one or more agents.
- Creating, editing, blessing, and deleting directed or two-way edges,
  including conditions, actions, and rate/token/cost caps.
- Viewing runtime, identity, status, foreman, blessing, and peer information,
  plus each agent's self-set activity line on its card.
- Watching messages flow: an edge lights up for ~5s when a message is
  accepted onto it, and hovering an edge shows the latest message (direction,
  age, preview) alongside the edge's conditions.
- Starting a stopped or not-started runtime.
- Attaching a real xterm.js terminal to a Crew-owned tmux session, with a
  tab per tmux window and a "+" tab that opens a new shell in the session
  (the agent's own current window is never moved).
- Optionally routing agent terminals to a second browser window
  (header "⧉ 2nd window"; /?view=term) for dual-monitor setups — the graph
  window drives it over a same-origin BroadcastChannel.
- Granting/revoking foreman, blessing agent-authored rows, and resolving the
  pending-approval tray.
- Expanding a plain-language agent or edge description into reviewable fields.

### Frontend development

The dashboard UI is a React + [MUI](https://mui.com) app in `frontend/`,
built with Vite into `static/` (which is committed, so running Crew never
requires node):

```bash
cd frontend
npm install        # once
npm run build      # emits static/index.html + static/assets/*
npm test           # vitest contract suites (also driven by the python suite)
```

The graph canvas engine (`frontend/src/graphEngine.js`), terminal transport
(`frontend/src/term.js` + xterm.js from npm), and API client
(`frontend/src/api.js`) are framework-free modules hosted inside React-owned
skeletons; React + MUI own the chrome, forms, and modals. Element ids and
classes are stable contracts — the browser scripts in `tests/browser/` and the
vitest suites in `frontend/tests/` key off them.

### Graphs gallery (projects)

Every project is one graph. `/?view=graphs` (the "⌂ graphs" header button)
lists them all — name, description, agent count, live state — and opens any
of them: the gallery finds the dashboard process that owns that project or
spawns one on a free port, handing the browser its capability URL. Leaving a
graph stops nothing: agents live in tmux, not in a browser window. Creating a
graph from the gallery registers the project, pushes its schema, and seeds a
launched **foreman** whose identity makes it ask "Describe the system you
want to build" and then build the crew itself with the crew CLI (within the
usual foreman quotas). `crew project create <name> --description "..."` is
the CLI equivalent (without the foreman seed).

The dashboard does **not** currently include a kickoff or peer-message bar,
mail/audit viewer, notes, file-grant controls, transform controls for ordinary
agent edges, agent removal, or down/restart controls. Use the CLI for those
tasks. Hook-route create/edit is the narrow exception: it exposes the
human-only transform path alongside that route's limits. The CLI and dashboard
share the same MorphDB data, but their control surfaces are intentionally not
identical.

### Dashboard capability boundary

When `crew dashboard start`, `crew dashboard open`, or `crew init` starts a new
dashboard process, it generates a fresh random capability in
`var/dashboard-<port>.cap` with mode `0600`. Reusing an already-running process
reuses that process's capability. The printed URL uses `/#cap=...`; the fragment
is exchanged for an `HttpOnly`, `SameSite=Strict` cookie and removed from browser
history. Every control POST and the PTY stream requires that cookie. Read-only
graph snapshots and the pending list require it too. Static HTML/assets and
`/api/health` remain reachable on loopback so the browser can bootstrap. Every
authenticated control POST must also use `Content-Type: application/json` and
`X-Crew-CSRF: 1`; when a browser supplies `Origin`, it must exactly match this
dashboard's scheme and host. The shipped browser client supplies these headers.

`POST /hooks/<capability>` is the deliberate exception: the random 256-bit URL
segment authorizes only delivery into that one hook's outgoing edges. It grants
no graph, terminal, or operator access and does not use the dashboard cookie or
CSRF header. Rotate it if disclosed. For internet delivery, terminate TLS and
provider signature/IP policy at a proxy that forwards only `/hooks/*`.

Lifecycle commands verify the exact dashboard PID, app, port, and random
instance id. Start/open fail if the port belongs to another app or service and
do not report success until that exact child answers health checks. Stop waits
for the exact owned process to leave the port before deleting its capability
and PID metadata.

This boundary prevents an anonymous localhost request from being treated as a
human graph mutation. It is not a same-UID sandbox: an unsandboxed agent running
as the same OS user may be able to read the capability file, inspect browser or
process state, access the tmux socket, and read other user-owned files. Use
separate OS users or a real sandbox when hostile-agent isolation is required.

## CLI reference

```text
crew [--project P] init [--no-dashboard]
crew project create <name> [--description ...]
crew project list

crew spawn-agent <name> [--role ...] [--identity ...]
    [--home DIR | --repo REPO] [--runtime claude|codex|custom]
    [--launch-cmd CMD] [--no-launch] [--foreman]
crew remove-agent <name> [--keep-session]
crew up|down|restart <name>|--all
crew status | agents | edges | whoami

crew webhook create <name> [--description ...] [--template ...]
crew webhook list
crew webhook show <name>
crew webhook update <name> [--description ...] [--template ...]
crew webhook rotate <name>
crew webhook remove <name>
crew ingress run
crew ingress status

crew connect <A> <B> [--label ...] [--when ...] [--does ...]
    [--reply] [--undirected] [--when-back ...] [--does-back ...]
    [--reply-back] [--max-turns N] [--token-cap N] [--cost-cap X]
    [--transform FILE]
crew disconnect <A> <B>
crew cap <A> <B> [--max-turns N] [--token-cap N] [--cost-cap X]
crew peers [<agent>]

crew message [--no-prefix] <target> <text...>
crew kickoff <agent> <text...>
crew mail [<agent>] [--status STATUS] [-n N]

crew foreman <name> [--revoke]
crew bless <agent>
crew bless --edge <A> <B>
crew bless --all
crew activity [<text...>] [--agent NAME] [--clear]
crew harness [<agent>] [--json]
crew note agent <name> <text>
crew note edge <A> <B> <text>
crew pending
crew approve <guid>
crew reject <guid> [--why ...]
crew audit [-n N] [--refused] [--actor NAME]

crew grant <agent> <path> [--ro|--rw]
crew grants [<agent>]
crew revoke-grant <agent> <grant-name>

crew dashboard start|stop|status|open|logs
```

Run `crew <command> --help` for exact argument details. Put `--project` before
the subcommand.

## Operator notifications

Set `CREW_WEBHOOK_URL` to receive best-effort alerts:

```bash
export CREW_WEBHOOK_URL=https://ntfy.sh/your-topic
```

ntfy URLs receive plain text with a title. Other endpoints receive:

```json
{"event":"agent_down","agent":"builder","detail":"...","ts":1234567890}
```

Current event families include `agent_down`, `needs_input`, `message_expired`,
`message_failed`, `message_filtered`, and `graph_edit`. `message_expired` is
reserved for queue age-outs; other terminal queue failures use
`message_failed`. Webhook failures never block Crew. Down and needs-input alerts
are detected by a dashboard-owned background monitor; they do not depend on a
browser tab being open. The first observation after a dashboard restart
establishes a baseline without re-announcing agents that were already down.

## Architecture

```text
Internet webhook provider
        │ HTTPS /hooks/<capability>
        ▼
Cloudflare Quick Tunnel
        │ HTTP over a private per-run Unix socket
        ▼
Crew hook gateway (foreground, no dashboard/API routes)
        │ durable delivery
        ▼
MorphDB :8787

browser (React + MUI dashboard: graph + xterm.js)
        │ HTTP / SSE
        ▼
Crew dashboard :8788
  ├─ graph snapshot + operator control API
  ├─ loopback-compatible POST /hooks/* ingress
  ├─ authenticated PTY bridge to Crew-owned tmux sessions
  ├─ background queued-mail flusher
  ├─ MorphDB :8787  (nodes, edges, messages, webhook receipts, audits)
  └─ private tmux endpoint (one project-scoped session per agent)
```

The dashboard and CLI call the same Python modules and MorphDB app. Terminal
attachment is a real grouped `tmux attach` streamed over SSE; the browser sends
bytes and terminal size changes while tmux owns rendering and scrollback. The
server refuses to attach to sessions that are not registered to the current
Crew project, and a corrupt stored session field cannot redirect it away from
the canonical project-and-agent tmux session.

## Operational limitations

- Crew coordinates interactive agents; it does not automatically execute a
  workflow graph or verify that models obey natural-language conditions.
- Agent workspaces are ownership declarations, not OS sandboxes. Global Claude,
  Codex, shell, and same-user filesystem state may still be visible.
- A file grant's mode is audited intent, not an enforced mount permission.
- Custom runtime state remains `unknown`, and mail waits rather than assuming an
  unknown prompt is safe.
- Codex/custom usage metering is unavailable, so configured token or cost caps
  targeting those runtimes block delivery until a trustworthy meter exists.
- `crew kickoff` is human-only and is a direct, readiness-gated operator steer,
  not durable agent mail. Managed agents are refused and must use graph-gated
  `crew message`; the refused attempt is retained with mail status `blocked`.
  If the target is busy, retry; there is no dashboard kickoff control.
- Removing an agent preserves its workspace/worktree. Clean those files and Git
  branches explicitly after confirming they are no longer needed.
- A graph node name is an identity, not a label. Crew serializes its own creates,
  but if legacy or imported storage holds two nodes with one name, every
  name-authorized action fails closed until one is gone. Remove the extra row
  from the dashboard, which acts on the immutable GUID; the CLI resolves by name
  and is itself blocked while the name is ambiguous.
- The dashboard is a local operator tool. Its capability reduces accidental
  control-plane access but does not isolate mutually hostile processes running
  as the same OS user.

## Feature development records

User-visible capabilities have repository-owned
[feature records](docs/features/index.html). Each feature keeps its product
description, technical contract, architecture diagram, implementation lineage,
exact verification commands, and real screenshot or video evidence in one
reviewable `index.html`. The only companion files are sanitized proof media in
that feature's `assets/` directory.

Create one before implementation:

```bash
python3 scripts/new_feature.py <feature-id> \
  --title "Human-readable title" \
  --summary "One sentence describing the user outcome."
```

Before marking it verified, run:

```bash
python3 scripts/validate_feature_docs.py
```

The repository's
[feature-development skill](.claude/skills/feature-development/SKILL.md) and
[agent instructions](AGENTS.md) make this record part of the definition of
done.

## Tests

The full suite includes pure behavior tests, isolated MorphDB fixtures, live CLI
writes, and live dashboard API tests. Start MorphDB and a capability-enabled
dashboard first:

The discover suite is fully isolated from live data: it expects a dedicated QA
MorphDB at `127.0.0.1:18787` (the isolated-dashboard modules hard-pin it) and a
dashboard started against that instance, with the whole run pointed at both:

```bash
# QA backends (separate database — live data is never touched)
morphdb run --port 18787 --db ~/tmp/morphdb-test/data.sqlite3 &
MORPHDB_HOST=127.0.0.1:18787 CREW_PORT=18790 ./bin/crew init

MORPHDB_HOST=127.0.0.1:18787 CREW_PORT=18790 python3 -m unittest discover tests

# live write-path smoke runs against the REAL app (morphdb start; ./bin/crew init):
python3 tests/live_smoke.py
```

Both commands create namespaced test agents and clean them up. `live_smoke.py`
intentionally writes through the configured default `crew` app to catch live
schema drift; do not point it at data you are unwilling to test.

Browser workflows are executable checklists and must be run with browser
automation against the live dashboard:

```text
tests/browser/create-agent.md
tests/browser/runtime-selection.md
tests/browser/terminal-dock.md
tests/browser/terminal-window.md
tests/browser/connect-edge.md
tests/browser/dock-tabs.md
tests/browser/edge-messages.md
tests/browser/edit-edge.md
tests/browser/revive-agent.md
tests/browser/foreman-bless.md
tests/browser/graphs-gallery.md
tests/browser/pending-tray.md
tests/browser/one-blob-config.md
tests/browser/agent-activity.md
tests/browser/canvas-navigation.md
tests/browser/graph-node-readability.md
tests/browser/graph-pan-anywhere.md
tests/browser/react-migration.md
tests/browser/resilience-accessibility.md
tests/browser/webhook-nodes.md
tests/browser/subagent-badge.md
tests/browser/crew-settings.md
```

Every mutating script defines isolated fixtures, capability bootstrap, expected
results, and owned cleanup; read-only scripts state their exact preconditions.
The exhaustive behavior matrix is in `TEST_PLAN.md`.

## License

MIT © Felix Chen
