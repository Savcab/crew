# crew

**Organize teams of long-running Claude Code agents as a graph you draw yourself.**

📖 **[Visual explainer → crew-ddq.pages.dev](https://crew-ddq.pages.dev)**

Each **agent** is one full, persistent Claude Code session living in its own tmux
session and its own home directory. You **connect** agents with **relationships**
you describe in plain language — what each side does, and *when* one should
message the other. Those relationships are the only sanctioned channels:
**an agent can message another agent only if you've drawn an edge between them.**

It's the generalization of the classic manager→workers setup: instead of one
hard-coded shape, you compose any team — a leads agent that hands qualified leads
to a builder, a builder that pings a sales agent when a demo is ready, a
reviewer that only talks to the two agents it reviews. You draw the graph; crew
enforces who may message whom, and how often.

```
   ┌────────┐   "when a qualified lead is found"   ┌─────────┐
   │ leads  │ ───────────────────────────────────▶ │ builder │
   └────────┘                                        └────┬────┘
                                                          │ "when a demo is ready"
                                                          ▼
                                                     ┌─────────┐
                                                     │  sales  │
                                                     └─────────┘
```

- **Glass dashboard** — see every agent as a node, every relationship as a
  labeled edge, and click any node to drop into its live terminal (a real
  `tmux attach` streamed to xterm.js — native scrollback, resize, the works).
- **Create an agent** from the graph (`+ Agent`) — it gets a home dir, a tmux
  session, and a launched Claude. Its identity is written into the home as
  `identity.md` (the full record) plus a managed `CLAUDE.md` that Claude
  auto-loads every session, so a fresh or restarted Claude knows who it is and who
  it may talk to without anything being typed in. The launch command is
  configurable (defaults to `claude --dangerously-skip-permissions` so agents run
  unattended).
- **Connect two agents** by dragging one's ● handle onto another, then describing the edge.
- **Gated agent-mail** — `crew message <peer>` delivers into the peer's prompt,
  but only along an edge you've drawn. No edge → hard block. Delivery is
  **reliable**: a message is logged, waits for the target to be idle before typing
  (so it won't interleave with a mid-turn generation or a dialog), and is retried
  by the dashboard if the target is busy — so handoffs aren't silently dropped.
- **Kick it off** — seed or steer any agent yourself from the dashboard's message
  bar or `crew kickoff <agent> "<task>"` (this is you talking to your own agent, so
  it isn't gated). That's how a crew starts moving.

Built on **[MorphDB](https://morphdb.pages.dev)** for the data (agents + edges)
and a tmux **PTY bridge** for the terminals. Pure Python stdlib server, no build
step, no runtime third-party deps.

---

## Concepts

| Thing | What it is |
|-------|-----------|
| **agent** | A node: one durable identity = one home directory = one tmux session running `claude`. Survives any single session — a restarted Claude re-reads `identity.md` to resume. |
| **edge** | A directed relationship you author, capturing **both sides**: a `condition` ("when should source message target?"), a `target_action` ("what does the target do on receipt?"), whether a reply is expected, and `max_turns` — an hourly **rate limit** (N messages/hour, 0 = unlimited) so a tight loop can't run away. It **also authorizes** messaging source→target (and is the only thing that does). `--undirected` makes it two-way. Both halves are rendered into each agent's identity. |
| **identity.md** | Written into each agent's home. States the agent's role, its workspace boundary, and the exact list of agents it may message (with the per-edge condition). The durable source of "who am I". A managed block in the home's **`CLAUDE.md`** mirrors the essentials so Claude auto-loads them at every session start. |
| **the gate** | `crew message A → B` is allowed **iff** an edge connects them in that direction and the edge's hourly `max_turns` isn't exhausted. Enforced at delivery, not as UI advice. |

**Two tiers of enforcement — know which is which.** The plain-language halves of
an edge (the `condition`, the `target_action`) are **advisory**: they're rendered
into `identity.md` / `CLAUDE.md` as standing instructions, and a model can ignore
instructions. What crew **enforces in code at delivery** is exactly two things:
edge existence (no edge → hard block) and the per-edge hourly rate limit
(`max_turns`). "When should A message B" is a request; whether A *can* message B,
and how often, is a guarantee.

**One agent per directory, and no nesting.** crew refuses to put an agent inside
another agent's home (or to share one), so no two agents are ever *assigned*
overlapping ground on disk.

**File grants — a sanctioned, audited exception to that boundary.** `crew grant
<agent> <path> [--ro|--rw]` (human-only; an agent's own attempt is queued for
your approval, never applied outright) symlinks `<path>` into the agent's home
at `refs/<name>` and records the grant on the agent (`crew grants [<agent>]` to
list, `crew revoke-grant <agent> <name>` to remove). **Know exactly what this
does and doesn't do:** the symlink and the recorded `mode` (`ro`/`rw`) are
*discoverability and declared intent* — they tell the agent (via `identity.md`/
`CLAUDE.md`) which outside paths it's authorized to touch, and audit every
grant/revoke. **Nothing about this is filesystem-enforced** — there is no
sandbox, `mode` is not a permission bit, and an agent's shell can already reach
anything it could reach before the grant. A grant is the sanctioned, audited
*exception* you've drawn to the default one-agent-one-directory boundary, not a
technical guarantee like the messaging gate is. (The `grants` list is typed —
today only `{"type": "path", ...}`; a future wave may reuse the same list for
`{"type": "port", ...}` grants.)

**Transform edges — code on the wire.** `crew connect A B --transform
var/transforms/<script>.py` attaches a script that runs **once, at delivery**,
for every message crossing that edge: crew pipes the body to it on stdin and,
if it exits 0 with nonempty stdout, that output **replaces** the body for the
rest of delivery (logged, typed into the pane — transformed). If it exits 0
with empty stdout, exits nonzero, or times out (`CREW_TRANSFORM_TIMEOUT`,
default 5s), the message is **dropped** instead — loud, not silent: logged
with status `filtered` (original body kept, viewable via `crew mail --status
filtered`), the sender told exactly why, and the operator notified. A queued
message's transform never re-runs on flush — it already ran once, at accept
time. Attaching or changing a transform is **human-only** (not even the
foreman flag covers it), and the script must live in `var/transforms/` (a
path outside it, or a missing file, is refused at attach time). Three example
scripts ship there: `redact.py` (strips common secret shapes), `squeeze.py`
(caps very long bodies), `scrub.py` (drops probable prompt-injection bodies).
Transform-author note: if your script spawns a child process, redirect the
child's stdout/stderr (e.g. to `DEVNULL`) — an inherited pipe held open by a
lingering child reads as a transform timeout even when your script finished.

**Durability.** `identity.md` + `CLAUDE.md` make a restarted agent resume *who it
is*. To resume *what it was doing*, each agent is told to keep a `progress.md` in
its home — identity is durable for free, in-flight work is durable if the agent
writes it down.

**Identity isolation (sharp edge).** A launched agent also loads your global
`~/.claude/` config — global memory, hooks, and skills. Those can overlay the
agent's *style* (e.g. a global persona), but they don't rewrite the agent's
standing instructions or touch the gate: the agent's role and workspace boundary
live in its own `CLAUDE.md` (which states it takes precedence), and exactly who it
may message is enforced by the delivery gate.
If you need fully deterministic agents, run them under a separate
`CLAUDE_CONFIG_DIR` via the per-agent launch command — note that config dir needs
its own Claude auth.

---

## Requirements

| Tool | Why |
|------|-----|
| `python3` ≥ 3.8 | the CLI + dashboard (stdlib only) |
| `tmux` | each agent is a tmux session; the dashboard streams panes |
| Claude Code CLI (`claude`) | the agents themselves |
| **MorphDB** (`pip install morphdb`, then `morphdb start`) | the data backend (agents + edges) |
| `git` *(optional)* | only for `--repo` (spawn an agent in a fresh worktree) |

MorphDB runs on `127.0.0.1:8787`; the crew dashboard runs on `127.0.0.1:8788`.
The dashboard manages **only** crew-spawned agents — it never lists, attaches to,
or resizes any other Claude session you're running.

---

## Quickstart

```bash
# 0. make sure the data backend is up
morphdb start

# 1. set up crew's schema + start the dashboard
./bin/crew init                     # → http://127.0.0.1:8788

# 2. create a couple of agents (each gets a home + tmux session + claude)
./bin/crew spawn-agent leads   --role "finds businesses with no website" --home ~/crew/leads
./bin/crew spawn-agent builder --role "builds demo sites"                --home ~/crew/builder

# 3. connect them — and say WHEN leads should message builder
./bin/crew connect leads builder --label "leads→builder" \
  --when "when a qualified lead with contact info is found"

# 4. open the dashboard, click a node to enter its terminal, watch them work
./bin/crew dashboard open
```

Inside the `leads` agent's session, when it has a lead:

```bash
crew message builder "Acme Plumbing, no site, owner@acme.com — please build a demo"
```

That lands in `builder`'s prompt. If `leads` tried to message an agent it isn't
connected to, crew refuses.

Put `bin/` on your `PATH` (or symlink `bin/crew`) so agents can call `crew`
directly.

---

## CLI

```
crew init [--no-dashboard]            set up MorphDB schema + start the dashboard
crew spawn-agent <name> [--role …] [--identity …] [--home DIR | --repo REPO] [--no-launch]
crew connect <A> <B> [--when "<cond>"] [--does "<target action>"] [--reply] [--max-turns N] [--undirected]
crew disconnect <A> <B>
crew message <target> <text…>         message a connected agent (GATED)
crew kickoff <agent> <text…>          seed/steer one of your own agents (ungated)
crew peers [<agent>]                  who an agent may message, and who may message it
crew status                           per-agent liveness + queued/failed mail counts
crew up | down | restart <name>|--all revive / stop / bounce agent sessions (records kept)
crew mail [<agent>] [--status …] [-n N]  the message log, newest first
crew agents | edges | whoami
crew remove-agent <name> [--keep-session]
crew grant <agent> <path> [--ro|--rw]  grant access to a path outside its home (GATED, human-only)
crew revoke-grant <agent> <name>      revoke a grant (human-only)
crew grants [<agent>]                 list file grants (read-only)
crew dashboard {start|stop|status|open|logs}
```

Everything the CLI does, the dashboard does too (and vice-versa) — they share the
same MorphDB data.

### Operator notifications

A crew's worst failures are silent — an agent dies overnight, a handoff expires
undelivered, a pane sits on a permission prompt. Point `CREW_WEBHOOK_URL` at a
webhook and crew POSTs those events to it (fire-and-forget; never blocks or
breaks delivery):

```bash
export CREW_WEBHOOK_URL=https://ntfy.sh/your-topic   # → phone push via the ntfy app
```

ntfy.sh URLs get a plain-text push (title = event); any other URL receives JSON
`{"event", "agent", "detail", "ts"}`. Events: `agent_down`, `needs_input`,
`message_expired`, `message_failed`. Unset = off.

---

## Architecture

```
 browser (xterm.js glass graph)
        │  HTTP/SSE
        ▼
 crew dashboard  :8788   ── crew/server/app.py (stdlib ThreadingHTTPServer)
   ├─ /api/graph/snapshot ─ reads agents+edges from MorphDB, joins live tmux status
   ├─ /api/agent/* /api/edge/* ─ crew.spawn / crew.graphstore (in-process)
   └─ /api/pty/* ─ real `tmux attach` in a PTY → streamed to xterm  (crew/server/ptyio.py)
        │
        ├── MorphDB :8787  ── agents + edges (one tenant app, key "crew")
        └── tmux            ── one session per agent (the live Claude)
```

- **Data** lives in MorphDB as two types — `agent` and `edge` (an edge is a
  first-class object with `source`/`target` relations, so it can carry the
  description + condition + direction). The messaging gate is a single
  index-backed relation query: `GET /objects/edge?source=<A>&target=<B>`.
- **Terminals** are real `tmux attach` clients in a PTY, streamed over SSE — tmux
  renders/scrolls/resizes natively, the browser just pipes bytes.

## Known limitations

- **Delivery while an agent is mid-generation.** `crew message` waits for the
  target to look idle and hold still for ~1.6s before typing, which covers steady
  streaming. In a rare (~5–10%) turn-boundary transient the target can momentarily
  look idle and get typed into anyway; Claude Code's own input layer buffers that
  text and submits it when the turn ends, so the message still arrives intact (not
  interleaved, not lost) — but it bypasses crew's queue and is logged `delivered`
  when it was really buffered. The guarantee against interleaving therefore leans
  partly on Claude Code's input buffering, not on crew's gate alone. A positive
  "idle" signal would close it but risks never-delivering if the UI string changes,
  so crew deliberately fails safe instead.
- **Identity isolation.** See the sharp-edge note above — a launched agent also
  loads your global `~/.claude/` config; crew identity asserts precedence but a
  global persona/style can still overlay the agent unless you isolate its config.
- **Side channels.** The gate covers `crew message` — the only **sanctioned**
  channel, and the only one with delivery, queueing, and logging. An agent with an
  unrestricted shell can still reach a peer around it: `tmux send-keys` into the
  peer's session, or writing files into the peer's home directory. No harness can
  hard-guarantee otherwise; if that matters, restrict the agents' shells rather
  than trusting the graph alone.

## Tests

```bash
python3 -m unittest tests.test_graphstore   # data layer + gate + home-nesting + status detection (needs MorphDB up)
```

## License

MIT © Felix Chen
