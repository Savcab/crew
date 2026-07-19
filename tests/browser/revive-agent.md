# Browser script: revive a down agent (dock's "▶ start session" → `POST /api/agent/start`)

Area D-browser-ui. Executed with browser tools (agent-browser/playwright) against
the REAL, already-running dashboard at **http://127.0.0.1:8788** (MorphDB app
`crew`). Creates one throwaway agent, kills its session to simulate a crash,
then revives it from the dashboard — see safety rules below.

## Safety (non-negotiable) — read before running

* Only ever create/touch/kill the tmux session for **`test_ba_revive`**. Never
  run `tmux kill-session` against `leads`, `builder`, `sales`, `AgentA`, or
  `AgentB` (or any session you did not personally create in step 2 below).
* `POST /api/agent/start` (what the "▶ start session" button calls) has **no
  launch=false option** — reviving an agent ALWAYS types its stored
  `launch_cmd` into the pane. To exercise this without booting a real
  `claude`, this agent is created with an inert launch command (`true`) and
  is NEVER actually launched until the revive step runs it — so the only
  thing that ever gets typed into its pane is the harmless shell builtin
  `true`, not `claude --dangerously-skip-permissions`.
* "Launch it now" MUST be unchecked at creation time regardless (so nothing
  runs before the deliberate revive step).
* Killing the session in step 4 is done from a terminal (the `Bash` tool),
  **not** by typing into the dashboard's xterm dock — this script never types
  into the terminal pane.
* Cleanup is mandatory, even on failure.

## Known gap — confirmed live, changes how this whole test must be verified

Confirmed by direct API + tmux testing before this script was finalized:
because this agent's pane never runs an actual `claude` process (by design —
that's what makes reviving it with `launch_cmd: "true"` safe), the dashboard's
`alive`/`live_status` fields (`crew/server/tmuxio.py:101-137` +
`crew/server/app.py:133-139`, which require a real `claude` process on the
session's pane tty) read `alive: false` / `live_status: "down"` for it
**permanently — before the kill in step 4, and still after a successful
revive in step 7.** Concretely, observed live:

| moment                                  | tmux session really up? | dashboard `alive` |
|------------------------------------------|:---:|:---:|
| right after creation (step 2)             | yes | **false** |
| after `tmux kill-session` (step 4)        | no  | false |
| after clicking "▶ start session" (step 7) | yes | **false** |

So the graph's "idle"/"session down" badge and the dock's `#dockMeta` text
CANNOT be used to tell "genuinely down" apart from "revived and running
fine" for this agent — both read "session down" throughout. This also means
`#dockStart`'s visibility (`dock.js`, keyed purely off `alive`) will show
"▶ start session" again the next time the dock re-opens for this agent (e.g.
if you cycle away and back), even though the revive in step 7 worked — don't
mistake that for a second failure. Steps 5, 6, 9, and 10 below verify the
REAL state via `tmux` / the POST response bodies instead of the graph's
status field, which is the only reliable signal for this test setup.

## Setup — create the test agent with an inert launch command

1. Navigate to `http://127.0.0.1:8788` (skip if already open).

2. Create the agent using the same procedure as
   `tests/browser/create-agent.md` steps 2–9, with:
   * Name (`#a-name`): `test_ba_revive`
   * What does it do? (`#a-role`): `revive test agent`
   * Home folder (`#a-home`, under Advanced): `/tmp/crew_tests/test_ba_revive`
   * Launch command (`#a-launch-cmd`, under Advanced): `true`
   * "Launch it now" (`#a-launch`): **unchecked**
   **Expected:** toast `creating test_ba_revive…`; a new node card appears
   for `test_ba_revive` reading status "session down" — per the Known gap
   above, this is immediate and expected, NOT a sign the create failed.

3. Confirm the agent is genuinely up despite the badge: from a terminal,
   `tmux has-session -t test_ba_revive`.
   **Expected:** exit code 0 (session exists). Optionally cross-check
   `curl -s http://127.0.0.1:8788/api/graph/snapshot` for `test_ba_revive`:
   expect `launch_cmd == "true"` and (per the Known gap) `alive == false`.

## Steps — kill the session for real

4. From a terminal (not the browser): `tmux kill-session -t test_ba_revive`.
   **Expected:** the command exits 0 (the session existed and is now gone).

5. Confirm it's really gone: `tmux has-session -t test_ba_revive`.
   **Expected:** non-zero exit / "can't find session test_ba_revive". (Do
   NOT rely on the dashboard's node-card status here — per the Known gap it
   already said "session down" before this step and will say the same thing
   after; it gives no signal either way.)

## Steps — revive it from the dock

6. Click the `test_ba_revive` node to open its dock (or reuse the dock if
   already open on this agent from step 2/3).
   **Expected:** the dock (`#dock`) opens; `#dockMeta` ends in "session
   down"; the "▶ start session" button (`#dockStart`) is VISIBLE and enabled
   (it has been visible since step 2 — see Known gap).

7. Click "▶ start session" (`#dockStart`).
   **Expected:** a toast reads `starting test_ba_revive…`; the button
   immediately hides (`style.display == 'none'`); `#dockMeta` updates to
   include "starting…". This hide/toast behavior is unconditional on the
   POST returning `ok: true` — it does not depend on the (permanently false)
   `alive` field, so it IS a valid signal that the request succeeded.

8. Wait 2–3 seconds for `spawn.start_session` to finish (it runs
   synchronously server-side: recreate the tmux session, rewrite
   identity.md/CLAUDE.md, type the launch command).

9. Confirm the revive actually worked via a terminal, NOT the dashboard
   badge:
   ```
   tmux has-session -t test_ba_revive
   tmux list-panes -t test_ba_revive:claude -F '#{pane_current_command}'
   ```
   **Expected:** `has-session` exits 0 (a NEW session was created); the pane
   is sitting at a shell (e.g. `zsh`/`bash`), confirming the inert
   `launch_cmd` (`true`) ran and returned to a prompt — no real `claude`
   process was ever started.

10. Cross-check via the API: `curl -s http://127.0.0.1:8788/api/graph/snapshot`
    for `test_ba_revive`.
    **Expected (per the Known gap):** `alive` STILL reads `false` and
    `live_status` STILL reads `"down"`, even though step 9 just proved the
    session is genuinely up and the revive worked. This is the expected,
    already-documented outcome for this test setup, not a new failure — the
    graph/dock status fields simply cannot represent this agent as anything
    but "down".

## Cleanup (always run, even if a step above failed)

1. `cd /Users/felix/Desktop/learn_ai/crew && ./bin/crew remove-agent test_ba_revive`
   — removes the record and kills whatever session currently exists for it
   (whether the original or the revived one).
2. Fallback for any leftover session: `tmux kill-session -t test_ba_revive 2>/dev/null || true`.
3. `rm -rf /tmp/crew_tests/test_ba_revive`.
4. Confirm via the API:
   ```
   curl -s http://127.0.0.1:8788/api/graph/snapshot \
     | python3 -c 'import json,sys; d=json.load(sys.stdin); print([a["name"] for a in d["agents"] if a["name"]=="test_ba_revive"])'
   ```
   **Expected:** `[]`.
5. Confirm no stray tmux session remains: `tmux has-session -t test_ba_revive`.
   **Expected:** non-zero exit / "can't find session test_ba_revive".
