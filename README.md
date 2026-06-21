# crew

**Organize teams of long-running Claude Code agents as a graph you draw yourself.**

📖 **[Visual explainer → crew-ddq.pages.dev](https://crew-ddq.pages.dev)**

Each **agent** is one full, persistent Claude Code session living in its own tmux
session and its own home directory. You **connect** agents with **relationships**
you describe in plain language — what each side does, and *when* one should
message the other. Those relationships are the only channels that exist:
**an agent can message another agent only if you've drawn an edge between them.**

It's the generalization of the classic manager→workers setup: instead of one
hard-coded shape, you compose any team — a leads agent that hands qualified leads
to a builder, a builder that pings a sales agent when a demo is ready, a
reviewer that only talks to the two agents it reviews. You draw the graph; crew
enforces it.

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
  session, a launched Claude, and an `identity.md` that tells it who it is and
  who it may talk to.
- **Connect two agents** by dragging one's ● handle onto another, then describing the edge.
- **Gated agent-mail** — `crew message <peer>` delivers into the peer's prompt,
  but only along an edge you've drawn. No edge → hard block.

Built on **[MorphDB](https://morphdb.pages.dev)** for the data (agents + edges)
and a tmux **PTY bridge** for the terminals. Pure Python stdlib server, no build
step, no runtime third-party deps.

---

## Concepts

| Thing | What it is |
|-------|-----------|
| **agent** | A node: one durable identity = one home directory = one tmux session running `claude`. Survives any single session — a restarted Claude re-reads `identity.md` to resume. |
| **edge** | A directed relationship you author: `label`, a natural-language `description` (what each side does), and a `condition` ("when should source message target?"). It **also authorizes** messaging source→target. `--undirected` makes it two-way. |
| **identity.md** | Written into each agent's home. States the agent's role, its workspace boundary, and the exact list of agents it may message (with the per-edge condition). The durable source of "who am I". |
| **the gate** | `crew message A → B` is allowed **iff** an edge connects them in that direction. Enforced at delivery, not as UI advice. |

**One agent per directory, and no nesting.** crew refuses to put an agent inside
another agent's home (or to share one), so two agents' work can never overlap on
disk.

---

## Requirements

| Tool | Why |
|------|-----|
| `python3` ≥ 3.8 | the CLI + dashboard (stdlib only) |
| `tmux` | each agent is a tmux session; the dashboard streams panes |
| Claude Code CLI (`claude`) | the agents themselves |
| **MorphDB** (`pip install morphdb`, then `morphdb start`) | the data backend (agents + edges) |
| `git` *(optional)* | only for `--repo` (spawn an agent in a fresh worktree) |
| `gh` CLI *(optional)* | native PR render in the side panel |

MorphDB runs on `127.0.0.1:8787`; the crew dashboard runs on `127.0.0.1:8788`.

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
crew connect <A> <B> [--label …] [--desc …] [--when "<condition>"] [--undirected]
crew disconnect <A> <B>
crew message <target> <text…>         message a connected agent (GATED)
crew agents | edges | whoami
crew remove-agent <name> [--keep-session]
crew dashboard {start|stop|status|open|logs}
```

Everything the CLI does, the dashboard does too (and vice-versa) — they share the
same MorphDB data.

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

## Tests

```bash
python3 -m unittest tests.test_graphstore   # data layer + gate + home-nesting (needs MorphDB up)
```

## License

MIT © Felix Chen
