# Browser script: create an agent (`+ Agent` → `POST /api/agent/create`)

Area D-browser-ui. Execute with browser tools against the isolated QA dashboard
on **http://127.0.0.1:18788**. `MORPHDB_HOST` and `CREW_APP` in the invoking
shell must identify that same isolated backend; never run this procedure against
an operator's normal project. A `--no-launch` agent has a live tmux session but
no runtime process. The UI must represent that state as **runtime not started**,
not as a dead session.

## Safety (non-negotiable)

* Only ever create/touch an agent named **`test_ba_create`** (prefix `test_ba_`
  = "browser area, area D"). Never click, drag, or submit a form referencing
  `leads`, `builder`, `sales`, `AgentA`, or `AgentB`.
* Its home folder MUST be set explicitly to `/tmp/crew_tests/test_ba_create`;
  never rely on the configured Crew-root default for a throwaway browser test.
* The "Launch it now" checkbox is **checked by default** in the form — it MUST
  be unchecked before submitting, or this test boots a real `claude` process.
  This is the one thing to double- and triple-check before clicking Create.
* Do not type anything into the agent's terminal dock (xterm pane) at any
  point in this script — this script only exercises the create-agent FORM and
  the resulting graph/dock chrome, not the terminal transport.
* Cleanup at the end is mandatory — run it even if an earlier step failed or
  produced an unexpected result.

## Portable preflight (run once before step 1)

From a terminal already inside this Crew checkout, resolve its path and reserve
the fixture only if no record, exact default-project session, or home already
exists. An interrupted earlier run is an explicit abort, never an invitation to
delete an unknown same-named session:
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
  crew_qa_snapshot | python3 -c 'import json,os,sys; n,h,app=sys.argv[1:]; d=json.load(sys.stdin); rows=[a for a in d["agents"] if a.get("name")==n]; assert len(rows)==1; a=rows[0]; assert a.get("_guid"); assert a.get("session")==n; assert os.path.realpath(a.get("home",""))==os.path.realpath(h); json.dump({"name":n,"guid":a["_guid"],"session":a["session"],"home":os.path.realpath(h),"home_arg":h,"app":app},sys.stdout)' "$name" "$home" "$CREW_APP" > "$receipt" || return 2
  python3 -c 'import json,pathlib,sys; r=json.load(open(sys.argv[1])); p=pathlib.Path(r["home_arg"]); assert p.is_dir() and not p.is_symlink(); (p/".crew-browser-owner").open("x",encoding="utf-8").write(r["guid"])' "$receipt"
}
crew_qa_assert_owned_agent() {
  local receipt="$CREW_QA_STATE/$1.owner.json"
  test -s "$receipt" || { echo "missing ownership receipt for $1; refusing cleanup" >&2; return 2; }
  crew_qa_snapshot | python3 -c 'import json,os,sys; r=json.load(open(sys.argv[1])); d=json.load(sys.stdin); assert d.get("workspace_key")==r["app"]; rows=[a for a in d["agents"] if a.get("name")==r["name"]]; assert len(rows)==1; a=rows[0]; assert a.get("_guid")==r["guid"]; assert a.get("session")==r["session"]; assert os.path.realpath(a.get("home",""))==r["home"]; print(r["session"])' "$receipt"
}
crew_qa_cleanup_agent() {
  local name="$1" receipt="$CREW_QA_STATE/$1.owner.json" session
  session="$(crew_qa_assert_owned_agent "$name")" || return 2
  python3 -c 'import json,sys; print(json.dumps({"name":sys.argv[1]}))' "$name" | curl -fsS -b "$CREW_DASH_COOKIE" -H 'Content-Type: application/json' -H 'X-Crew-CSRF: 1' --data-binary @- "$CREW_DASH_URL/api/agent/remove" | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d.get("ok") is True, d' || return 2
  ! tmux has-session -t "=$session" 2>/dev/null || { echo "owned session still exists; preserving home and receipt" >&2; return 2; }
  crew_qa_snapshot | python3 -c 'import json,sys; n=sys.argv[1]; d=json.load(sys.stdin); assert not [a for a in d["agents"] if a.get("name")==n]' "$name" || return 2
  python3 -c 'import json,pathlib,shutil,sys; r=json.load(open(sys.argv[1])); p=pathlib.Path(r["home_arg"]); root=pathlib.Path("/tmp/crew_tests").resolve(); assert p.parent.resolve()==root and not p.is_symlink(); assert (p/".crew-browser-owner").read_text(encoding="utf-8")==r["guid"]; shutil.rmtree(p)' "$receipt" || return 2
  rm -f "$receipt"
}
crew_qa_assert_unused test_ba_create /tmp/crew_tests/test_ba_create
```
This script creates its own agent in the UI; no pre-existing graph nodes are
required.

## Steps

1. Navigate to `$CREW_DASH_URL/#cap=$CREW_DASH_CAP` using the values from the
   preflight (the fragment bootstraps the operator cookie and then disappears).
   **Expected:** the page loads (`<title>crew</title>`); the header reads
   "crew"; the graph canvas (`#cgraph`) renders. On an empty install, its empty
   state and create control are visible; on an established install, existing
   cards may render. No particular seed agent is required. Do not interact
   with any pre-existing card.

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
   showing the role text from step 4. Its status reads **"runtime not
   started"**: the managed tmux session exists, but Claude was deliberately
   not launched.

9b. In the preflight terminal run:
    ```sh
    crew_qa_capture_agent test_ba_create /tmp/crew_tests/test_ba_create
    ```
    **Expected:** it succeeds and writes an ownership receipt containing this
    run's exact `_guid`, stored session, canonical home, and app. Stop if it
    fails; cleanup must never infer ownership from the fixture name alone.

10. Click the `test_ba_create` node to open its terminal dock.
    **Expected:** the dock (`#dock`) gains class `show`; `#dockName` reads
    `test_ba_create`; `#dockMeta` ends in "runtime not started"; the **start
    runtime** button (`#dockStart`) is visible. The terminal attaches to the
    existing bare-shell session rather than treating it as dead. Do not click
    start and do not type into the terminal in this form-focused script.

11. Click "ⓘ identity" (`#dockIdentity`).
    **Expected:** an identity card modal opens titled `test_ba_create —
    identity`; the "role" row shows the role text from step 4; the "home" row
    shows `/tmp/crew_tests/test_ba_create` (it may be resolved to
    `/private/tmp/...` on macOS — that's expected, `/tmp` is a symlink there);
    "status" shows "runtime not started"; both "talks to →" and "hears from ←" lists show "no one yet" (no
    edges exist for this fresh agent).

12. Close the identity modal (`#modalClose`), then close the dock
    (`#dockClose`).

13. Cross-check with the authenticated API directly:
    `curl -fsS -b "$CREW_DASH_COOKIE" "$CREW_DASH_URL/api/graph/snapshot"`.
    **Expected:** the `agents` array contains one object with `name ==
    "test_ba_create"`, `home` ending in `/crew_tests/test_ba_create`, and
    `launch_cmd == "claude --dangerously-skip-permissions"` (the default —
    stored but never executed, since `launch: false` was sent),
    `session_alive == true`, `runtime_alive == false`, and `live_status ==
    "not_started"`. The legacy `alive` field remains false because it means
    runtime-process liveness. Also confirm `tmux has-session -t
    test_ba_create` exits 0.

## Cleanup (always run, even if a step above failed)

1. From the same preflight terminal run
   ```sh
   crew_qa_cleanup_agent test_ba_create
   ```
   It first compares the live record's
   exact `_guid`, stored default-project session, and canonical home to the
   ownership receipt. Crew then removes only that verified record/session; the
   marked home is removed only after both record and exact session are gone.
   If capture never succeeded, or any value changed, it aborts and preserves
   everything for diagnosis—there is intentionally no direct tmux fallback.
2. Confirm cleanup via the API:
   ```sh
   curl -fsS -b "$CREW_DASH_COOKIE" "$CREW_DASH_URL/api/graph/snapshot" \
     | python3 -c 'import json,sys; d=json.load(sys.stdin); print([a["name"] for a in d["agents"] if a["name"]=="test_ba_create"])'
   ```
   **Expected:** `[]`.
3. Confirm no stray tmux session remains: `tmux has-session -t =test_ba_create`.
   **Expected:** non-zero exit / "can't find session test_ba_create".
4. Remove the temporary cookie jar and empty receipt directory:
   `rm -f "$CREW_DASH_COOKIE"; rmdir "$CREW_QA_STATE"`.
