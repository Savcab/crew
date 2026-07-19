# Browser script: revive a down agent (dock's "▶ start runtime" → `POST /api/agent/start`)

Area D-browser-ui. Execute with browser tools against the isolated QA dashboard
on **http://127.0.0.1:18788**. The invoking shell's `MORPHDB_HOST` and
`CREW_APP` must name the same isolated backend. This creates one throwaway
custom-runtime agent, kills its session, and revives it from the dashboard.

## Safety (non-negotiable) — read before running

* Only ever create/touch/kill the tmux session for **`test_ba_revive`**. Never
  run `tmux kill-session` against `leads`, `builder`, `sales`, `AgentA`, or
  `AgentB` (or any session you did not personally create in step 2 below).
* `POST /api/agent/start` always runs the stored launch command. Select the
  **custom** runtime and use `exec sh`, which is a harmless persistent
  interactive process and can be detected honestly without starting a model.
* "Launch it now" MUST be unchecked at creation time regardless (so nothing
  runs before the deliberate revive step).
* Killing the session in step 4 is done from a terminal (the `Bash` tool),
  **not** by typing into the dashboard's xterm dock — this script never types
  into the terminal pane.
* Cleanup is mandatory, even on failure.

## Portable preflight (run once before setup)

From a terminal already inside this Crew checkout. A same-named record, exact
session, or home is a hard abort; this procedure never reclaims it:
```sh
export CREW_REPO="$(git rev-parse --show-toplevel)"
cd "$CREW_REPO"
test -n "$MORPHDB_HOST" && test -n "$CREW_APP" || {
  echo "set MORPHDB_HOST and CREW_APP to the isolated QA backend" >&2; exit 2;
}
export CREW_PORT="${CREW_PORT:-18788}"
test "$CREW_PORT" = "18788" || { echo "this procedure requires isolated port 18788" >&2; exit 2; }
test "${CREW_PROJECT:-default}" = "default" || { echo "this procedure requires CREW_PROJECT=default" >&2; exit 2; }
test "$CREW_APP" != "crew" || { echo "refusing the operator/default app" >&2; exit 2; }
export CREW_DASH_URL="http://127.0.0.1:$CREW_PORT"
export CREW_DASH_CAP="$(tr -d '\r\n' < "$CREW_REPO/var/dashboard-$CREW_PORT.cap")"
export CREW_DASH_COOKIE="$(mktemp /tmp/crew-browser-$CREW_PORT.XXXXXX.cookies)"
export CREW_QA_STATE="$(mktemp -d /tmp/crew-browser-$CREW_PORT.XXXXXX.state)"
python3 -c 'import json,os; print(json.dumps({"capability":os.environ["CREW_DASH_CAP"]}))' \
  | curl -fsS -c "$CREW_DASH_COOKIE" -H 'Content-Type: application/json' \
      --data-binary @- "$CREW_DASH_URL/api/auth/bootstrap" >/dev/null
test "$(curl -fsS "$CREW_DASH_URL/api/health" | python3 -c 'import json,sys; print(json.load(sys.stdin)["app"])')" = "$CREW_APP" || {
  echo "dashboard app does not match CREW_APP; refusing cleanup/mutation" >&2; exit 2;
}
crew_qa_snapshot() { curl -fsS -b "$CREW_DASH_COOKIE" "$CREW_DASH_URL/api/graph/snapshot"; }
crew_qa_assert_unused() {
  local name="$1" home="$2"
  crew_qa_snapshot | python3 -c 'import json,sys; n=sys.argv[1]; d=json.load(sys.stdin); assert not [a for a in d["agents"] if a.get("name")==n], f"existing agent {n}; aborting"' "$name" || return 2
  ! tmux has-session -t "=$name" 2>/dev/null || { echo "existing exact tmux session $name; aborting" >&2; return 2; }
  { [ ! -e "$home" ] && [ ! -L "$home" ]; } || { echo "existing home $home; aborting" >&2; return 2; }
}
crew_qa_capture_agent() {
  local name="$1" home="$2" receipt="$CREW_QA_STATE/$1.owner.json"
  crew_qa_snapshot | python3 -c 'import json,os,sys; n,h,app=sys.argv[1:]; d=json.load(sys.stdin); rows=[a for a in d["agents"] if a.get("name")==n]; assert len(rows)==1; a=rows[0]; assert a.get("_guid") and a.get("session")==n; assert os.path.realpath(a.get("home",""))==os.path.realpath(h); json.dump({"name":n,"guid":a["_guid"],"session":a["session"],"home":os.path.realpath(h),"home_arg":h,"app":app},sys.stdout)' "$name" "$home" "$CREW_APP" > "$receipt" || return 2
  python3 -c 'import json,pathlib,sys; r=json.load(open(sys.argv[1])); p=pathlib.Path(r["home_arg"]); assert p.is_dir() and not p.is_symlink(); (p/".crew-browser-owner").open("x",encoding="utf-8").write(r["guid"])' "$receipt"
}
crew_qa_assert_owned_agent() {
  local receipt="$CREW_QA_STATE/$1.owner.json"
  test -s "$receipt" || { echo "missing ownership receipt for $1; refusing cleanup" >&2; return 2; }
  crew_qa_snapshot | python3 -c 'import json,os,sys; r=json.load(open(sys.argv[1])); d=json.load(sys.stdin); assert d.get("workspace_key")==r["app"]; rows=[a for a in d["agents"] if a.get("name")==r["name"]]; assert len(rows)==1; a=rows[0]; assert a.get("_guid")==r["guid"] and a.get("session")==r["session"]; assert os.path.realpath(a.get("home",""))==r["home"]; print(r["session"])' "$receipt"
}
crew_qa_cleanup_agent() {
  local name="$1" receipt="$CREW_QA_STATE/$1.owner.json" session
  session="$(crew_qa_assert_owned_agent "$name")" || return 2
  python3 -c 'import json,sys; print(json.dumps({"name":sys.argv[1]}))' "$name" | curl -fsS -b "$CREW_DASH_COOKIE" -H 'Content-Type: application/json' -H 'X-Crew-CSRF: 1' --data-binary @- "$CREW_DASH_URL/api/agent/remove" | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d.get("ok") is True, d' || return 2
  ! tmux has-session -t "=$session" 2>/dev/null || { echo "owned session still exists; preserving home and receipt" >&2; return 2; }
  crew_qa_snapshot | python3 -c 'import json,sys; n=sys.argv[1]; d=json.load(sys.stdin); assert not [a for a in d["agents"] if a.get("name")==n]' "$name" || return 2
  python3 -c 'import json,pathlib,shutil,sys; r=json.load(open(sys.argv[1])); p=pathlib.Path(r["home_arg"]); assert p.parent.resolve()==pathlib.Path("/tmp/crew_tests").resolve() and not p.is_symlink(); assert (p/".crew-browser-owner").read_text(encoding="utf-8")==r["guid"]; shutil.rmtree(p)' "$receipt" || return 2
  rm -f "$receipt"
}
crew_qa_assert_unused test_ba_revive /tmp/crew_tests/test_ba_revive
```
The setup below provisions the agent with a safe launch command; it does not
depend on any existing graph node.

## Expected state transitions

| moment | session | runtime | UI status |
|---|---|---|---|
| created with Launch unchecked | up | absent | runtime not started |
| after exact test-session kill | down | absent | session down |
| after **start runtime** | up | `sh` alive | state unknown |

Any other transition is a failure. Custom interactive state is intentionally
`unknown`; Crew must not claim that an arbitrary custom TUI is idle.

## Setup — create the test agent with a safe custom runtime

1. Navigate to `$CREW_DASH_URL/#cap=$CREW_DASH_CAP` using the preflight values
   (skip only when this browser context already has the operator cookie).

2. Create the agent using the same procedure as
   `tests/browser/create-agent.md` steps 2–9, with:
   * Name (`#a-name`): `test_ba_revive`
   * What does it do? (`#a-role`): `revive test agent`
   * Home folder (`#a-home`, under Advanced): `/tmp/crew_tests/test_ba_revive`
   * Runtime (`#a-runtime`): `Custom command`
   * Launch command (`#a-launch-cmd`, under Advanced): `exec sh`
   * "Launch it now" (`#a-launch`): **unchecked**
   **Expected:** toast `creating test_ba_revive…`; a new node card appears
   for `test_ba_revive` reading status "runtime not started".

3. Confirm the agent is genuinely up despite the badge: from a terminal,
   `tmux has-session -t test_ba_revive`.
   **Expected:** exit code 0 (session exists). Optionally cross-check
   `curl -fsS -b "$CREW_DASH_COOKIE" "$CREW_DASH_URL/api/graph/snapshot"`
   for `test_ba_revive`: expect `launch_cmd == "exec sh"`, `runtime ==
   "custom"`, `session_alive == true`, `runtime_alive == false`, and
   `live_status == "not_started"`.

3b. Run:
    ```sh
    crew_qa_capture_agent test_ba_revive /tmp/crew_tests/test_ba_revive
    ```
    **Expected:** an ownership receipt records
    the exact `_guid` and stored session `test_ba_revive`. Do not continue to
    the destructive session step if capture fails.

## Steps — kill the session for real

4. From the same terminal, revalidate the receipt and the live tmux ownership
   environment immediately before the one deliberate kill:
   ```sh
   CREW_QA_SESSION="$(crew_qa_assert_owned_agent test_ba_revive)"
   test "$CREW_QA_SESSION" = "test_ba_revive"
   test "$(tmux show-environment -t "=$CREW_QA_SESSION" CREW_PROJECT)" = "CREW_PROJECT=default"
   test "$(tmux show-environment -t "=$CREW_QA_SESSION" CREW_AGENT)" = "CREW_AGENT=test_ba_revive"
   test "$(tmux show-environment -t "=$CREW_QA_SESSION" CREW_APP)" = "CREW_APP=$CREW_APP"
   test "$(tmux show-environment -t "=$CREW_QA_SESSION" MORPHDB_HOST)" = "MORPHDB_HOST=$MORPHDB_HOST"
   tmux kill-session -t "=$CREW_QA_SESSION"
   ```
   **Expected:** every check and the exact-target kill exit 0. Any mismatch
   aborts before tmux is mutated.

5. Confirm it's really gone: `tmux has-session -t test_ba_revive`.
   **Expected:** non-zero exit / "can't find session test_ba_revive". The node
   card transitions to "session down" within one refresh.

## Steps — revive it from the dock

6. Click the `test_ba_revive` node to open its dock (or reuse the dock if
   already open on this agent from step 2/3).
   **Expected:** the dock (`#dock`) opens; `#dockMeta` ends in "session
   down"; the **start runtime** button (`#dockStart`) is visible and enabled.

7. Click "▶ start runtime" (`#dockStart`).
   **Expected:** a toast reads `starting test_ba_revive…`; the button
   immediately hides (`style.display == 'none'`); `#dockMeta` updates to
   include "starting…". If the request fails, an error toast appears and the
   button is restored; a success must not be inferred from optimistic chrome.

8. Wait 2–3 seconds for `spawn.start_session` to finish (it runs
   synchronously server-side: recreate the tmux session, rewrite `identity.md`,
   and type the launch command).

9. Confirm the revive actually worked via a terminal, NOT the dashboard
   badge:
   ```
   tmux has-session -t test_ba_revive
   tmux list-panes -t test_ba_revive:agent -F '#{pane_current_command}'
   ```
   **Expected:** `has-session` exits 0 (a NEW session was created); the pane
   current command is `sh`, confirming the safe custom runtime is alive.

10. Cross-check via the authenticated API:
    `curl -fsS -b "$CREW_DASH_COOKIE" "$CREW_DASH_URL/api/graph/snapshot"`
    for `test_ba_revive`.
    **Expected:** `session_alive == true`, `runtime_alive == true`, legacy
    `alive == true`, and `live_status == "unknown"`. The node and dock say
    "state unknown", and the start button stays hidden after refresh/reopen.

## Cleanup (always run, even if a step above failed)

1. Run:
   ```sh
   crew_qa_cleanup_agent test_ba_revive
   ```
   It compares the live `_guid`,
   stored session, and canonical home to the receipt before removing the row
   and currently revived owned session. A mismatch aborts; there is no direct
   fallback kill.
2. Confirm via the API:
   ```sh
   curl -fsS -b "$CREW_DASH_COOKIE" "$CREW_DASH_URL/api/graph/snapshot" \
     | python3 -c 'import json,sys; d=json.load(sys.stdin); print([a["name"] for a in d["agents"] if a["name"]=="test_ba_revive"])'
   ```
   **Expected:** `[]`.
3. Confirm no stray tmux session remains: `tmux has-session -t =test_ba_revive`.
   **Expected:** non-zero exit / "can't find session test_ba_revive".
4. Remove the temporary cookie jar and empty receipt directory:
   `rm -f "$CREW_DASH_COOKIE"; rmdir "$CREW_QA_STATE"`.
