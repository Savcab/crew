# crew/harness — context

## What this area does
Reads what each agent's coding harness says it is working toward — the open
goals it is pursuing, plus how many subagents it is running — and normalizes
that into one shape no matter which harness produced it. Every reader goes to
the harness's OWN durable state on disk; nothing here drives a TUI or needs
the agent's cooperation. Callers are the `crew harness` command (crew/cli.py)
and the dashboard's graph poll (crew/server/app.py).

## Key files
- `__init__.py` — the package seam: the HARNESSES registry and probe() /
  probe_many(), the only entry points callers use.
- `base.py` — the Harness ABC, the HarnessState reading it produces, and the
  shared helpers clean_text() and read_only_db().
- `claude.py` — Claude Code: live session registry plus per-session task files.
- `codex.py` — Codex CLI: the thread-goal and thread sqlite stores.
- `hermes.py` — Hermes: the machine-wide sqlite kanban board.

## Invariants and gotchas
- Readers never write harness state. It belongs to a running program: open it
  read-only, briefly, and let go. All sqlite access goes through
  read_only_db() in `base.py` (`mode=ro` URI, 0.2s timeout). The timeout is
  short on purpose — this runs in the dashboard's poll loop, and one missed
  reading beats a stalled poll.
- probe() never raises. Unreadable, malformed, absent, or unsupported inputs
  all degrade to an empty reading carrying a `reason` string. Individual
  readers may raise freely; being shielded is the point. Note that
  `supported=False` (Crew has no reader for that runtime) is a different
  claim from a supported runtime with nothing open — the UI must not badge
  the first as "no goal".
- None is not 0 for `subagents`. None means "no reading available" (the store
  is absent, or the harness predates the concept); 0 is an honest claim that
  there are none. Codex returns None when the spawn-edge table is missing
  rather than reporting zero, and a subagent read that blows up is caught
  separately in base.state() so it cannot take the goals down with it.
- The base class models only what Crew SHOWS the user, never a full harness.
  Resist adding fields no card displays; the product framing lives in the
  dossier at `docs/features/harness-introspection/` — read it there.
- Readers return raw strings and base.state() does all cleaning and bounding
  (MAX_TEXT, MAX_GOALS). Do not truncate inside a subclass.
- Claude Code liveness is checked against processes actually running Claude
  Code, via one cached ps sweep — not bare pid liveness, because the registry
  keeps a file per session ever started and the OS recycles pids. If ps is
  unavailable the reader falls back to pid liveness rather than declaring
  every session dead. Sweeps cache for 3s; call claude.reset_caches() after
  changing the state dir (tests do).
- Codex bumps a numeric generation suffix (goals_<N>.sqlite) when it migrates
  a store's schema, so always read the newest generation, never a fixed name.
- Hermes is a per-user singleton, so its board is machine-wide and `home`
  deliberately does not filter the reading. Claude and Codex both join on
  home, and both compare realpath.
- Each reader takes its state directory from environment variables first
  (CREW_*_STATE_DIR, then the harness's own CLAUDE_CONFIG_DIR / CODEX_HOME /
  HERMES_HOME). That is how tests point the readers at fixtures.

## When to update this file
- A new harness reader lands: it needs a Harness subclass here, an entry in
  HARNESSES, and a matching adapter in crew.runtime — record it here.
- The base class gains or loses a reading (a field beside goals/subagents),
  or any invariant above stops being true.
