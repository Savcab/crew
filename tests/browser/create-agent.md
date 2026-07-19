# Browser script: create an agent (`+ Agent` → `POST /api/agent/create`)

Area D-browser-ui. Executed with browser tools (agent-browser/playwright) against
the REAL, already-running dashboard at **http://127.0.0.1:8788** (MorphDB app
`crew` — the live app with agents `leads`, `builder`, `sales`, `AgentA`,
`AgentB`). This is not a throwaway fixture: the dashboard, MorphDB, and tmux on
the box are all real and shared with other agents/operators, so follow the
safety rules exactly. Other live test suites may run concurrently against the
same app (e.g. `tests/test_dashboard_api.py`, `tests/test_cli_live.py`), but
each is namespaced to its OWN sub-prefix (`test_dashapi_`, `test_cli_`, …) and
only ever sweeps that sub-prefix — verified in `tests/test_dashboard_api.py`
lines 15–19/94–101 and `tests/test_cli_live.py` lines 108–118 — so `test_ba_*`
agents from this script are never touched by a sibling suite's cleanup. If
`test_ba_create` disappears unexpectedly anyway, that's a real anomaly, not
expected concurrent-suite behavior; just re-run from step 1 with a clean slate.

## Known gap — confirmed live, read before running

The dashboard's `alive` / `live_status` fields (and therefore the "idle" vs
"session down" badge, and the dock's "▶ start session" button) are computed by
`crew/server/tmuxio.py`'s `list_claude_panes()` (lines 101–117) via
`_session_pane_map()` (lines 128–137), consumed in
`crew/server/app.py:133-139`. That path requires an ACTUAL `claude` process on
one of the session's pane ttys — a `--no-launch` agent's pane runs a bare
shell, so it never satisfies that, and the dashboard reports it as
`alive: false` / `live_status: "down"` **immediately after creation**, even
though its tmux session is genuinely up. (This is unrelated to
`tmuxio.claude_pane()`, used by `crew.mail` delivery and the PTY terminal
attach, which DOES fall back to a bare-shell pane — so opening the dock's
terminal still works fine.) The steps below are written to match this real,
observed behavior — do not treat a "session down" badge on a freshly-created
test agent as a sign step 8 failed.

## Safety (non-negotiable)

* Only ever create/touch an agent named **`test_ba_create`** (prefix `test_ba_`
  = "browser area, area D"). Never click, drag, or submit a form referencing
  `leads`, `builder`, `sales`, `AgentA`, or `AgentB`.
* Its home folder MUST be set explicitly to `/tmp/crew_tests/test_ba_create`
  (the Home folder field defaults to `./<name>` relative to the dashboard
  server's cwd if left blank — never leave it blank for a test agent).
* The "Launch it now" checkbox is **checked by default** in the form — it MUST
  be unchecked before submitting, or this test boots a real `claude` process.
  This is the one thing to double- and triple-check before clicking Create.
* Do not type anything into the agent's terminal dock (xterm pane) at any
  point in this script — this script only exercises the create-agent FORM and
  the resulting graph/dock chrome, not the terminal transport.
* Cleanup at the end is mandatory — run it even if an earlier step failed or
  produced an unexpected result.

## Steps

1. Navigate to `http://127.0.0.1:8788`.
   **Expected:** the page loads (`<title>crew</title>`); the header reads
   "crew"; the graph canvas (`#cgraph`) renders node cards for the existing
   real agents (at least `leads`, `builder`, `sales`, `AgentA`, `AgentB` should
   be visible somewhere on the canvas — pan/zoom-to-fit if needed to confirm).
   Do not interact with any of them.

2. Confirm the "+ Agent" button (`#addAgentBtn`, top-right of the graph header)
   is visible, then click it.
   **Expected:** the modal (`#cmodal`) gains class `show`; `#modalTitle` reads
   "Create agent"; the body opens in BLOB MODE (UI wave B): a "Describe this
   agent in plain words" textarea (`#a-blob`), a "Generate" button
   (`#a-generate`), and a "fill manually instead" link (`#a-manual-link`) —
   the manual "Name"/"What does it do?" fields are inside a collapsed
   `<details class="f-adv" id="a-form-fold">` fold, not directly visible.

2b. Click `#a-manual-link` (this script exercises the manual path, not
    Generate — see `tests/browser/one-blob-config.md` for the blob-mode
    flow).
    **Expected:** the blob textarea/Generate/link block hides; the
    `#a-form-fold` `<details>` auto-expands, revealing "Name"/"What does it
    do?" (still empty) plus the rest of the Advanced fields below them.

3. Type `test_ba_create` into the Name field (`#a-name`).

4. Type `browser test agent — safe to delete` into the "What does it do?"
   field (`#a-role`).

5. Confirm the fold (`#a-form-fold`) is already open (step 2b left it open) —
   "Identity / mission", "Home folder", "Start on a copy of a repo", "Launch
   command", and a "Launch it now" checkbox should already be visible. If it's
   somehow collapsed, click its `<summary>` to expand it.

6. Type `/tmp/crew_tests/test_ba_create` into the Home folder field (`#a-home`).

7. Confirm the "Launch it now" checkbox (`#a-launch`) is currently CHECKED
   (the default), then **uncheck it**.
   **Expected:** the checkbox is now unchecked. Do not proceed to step 8 if it
   is still checked.

8. Click "Create agent" (`#a-go`).
   **Expected:** a toast appears reading `creating test_ba_create…` (element
   `#toast` gains class `show`, and does NOT gain class `err`); the modal
   closes (`#cmodal` loses class `show`).

9. Wait for the graph to refresh (poll interval is ~1.5s; wait up to 5s, or
   force it by reloading the page).
   **Expected:** a new node card appears in the graph for `test_ba_create`
   showing the role text from step 4. Per the Known gap above, its status
   reads **"session down"** with a grey status dot — even though
   `--no-launch`/`launch:false` did open a real (bare-shell) tmux session,
   only the `claude` launch was skipped, and the dashboard cannot see a
   non-`claude` pane as alive.

10. Click the `test_ba_create` node to open its terminal dock.
    **Expected:** the dock (`#dock`) gains class `show`; `#dockName` reads
    `test_ba_create`; `#dockMeta` ends in "session down"; the "▶ start
    session" button (`#dockStart`) is VISIBLE (its `style.display` is not
    `'none'`) — per the Known gap, not because the session is actually down.
    Do NOT click "▶ start session" and do NOT click into the terminal pane
    itself or type anything there (clicking start here would needlessly kill
    and recreate a perfectly healthy session).

11. Click "ⓘ identity" (`#dockIdentity`).
    **Expected:** an identity card modal opens titled `test_ba_create —
    identity`; the "role" row shows the role text from step 4; the "home" row
    shows `/tmp/crew_tests/test_ba_create` (it may be resolved to
    `/private/tmp/...` on macOS — that's expected, `/tmp` is a symlink there);
    "status" shows "session down" (same Known-gap value shown everywhere
    else); both "talks to →" and "hears from ←" lists show "no one yet" (no
    edges exist for this fresh agent).

12. Close the identity modal (`#modalClose`), then close the dock
    (`#dockClose`).

13. Cross-check with the API directly (curl, not the browser):
    `curl -s http://127.0.0.1:8788/api/graph/snapshot`.
    **Expected:** the `agents` array contains one object with `name ==
    "test_ba_create"`, `home` ending in `/crew_tests/test_ba_create`, and
    `launch_cmd == "claude --dangerously-skip-permissions"` (the default —
    stored but never executed, since `launch: false` was sent). Per the Known
    gap, `alive == false` and `live_status == "down"` here too — do not read
    this as the create having failed; also confirm directly with
    `tmux has-session -t test_ba_create` (expect exit 0) that the session is
    in fact up.

## Cleanup (always run, even if a step above failed)

1. From a terminal (not the browser dock):
   ```
   cd /Users/felix/Desktop/learn_ai/crew && ./bin/crew remove-agent test_ba_create
   ```
   This deletes the MorphDB agent record (+ any edges touching it) and kills
   the `test_ba_create` tmux session.
2. If that reports "no such agent" (e.g. the create step failed partway,
   after the tmux session opened but before the record was written), kill any
   leftover session directly instead:
   ```
   tmux kill-session -t test_ba_create 2>/dev/null || true
   ```
3. Remove the home directory: `rm -rf /tmp/crew_tests/test_ba_create`.
4. Confirm cleanup via the API:
   ```
   curl -s http://127.0.0.1:8788/api/graph/snapshot \
     | python3 -c 'import json,sys; d=json.load(sys.stdin); print([a["name"] for a in d["agents"] if a["name"]=="test_ba_create"])'
   ```
   **Expected:** `[]`.
5. Confirm no stray tmux session remains: `tmux has-session -t test_ba_create`.
   **Expected:** non-zero exit / "can't find session test_ba_create".
