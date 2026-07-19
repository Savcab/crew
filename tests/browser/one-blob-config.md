# Browser script: one-blob LLM config (connect modal + create-agent modal)

Area D-browser-ui. Two halves: Part 1 exercises BLOB MODE's presence/manual-
fallback against the dashboard AS ALREADY RUNNING (no config change needed).
The target is the isolated QA dashboard on **http://127.0.0.1:18788** with the
same `MORPHDB_HOST` and `CREW_APP` exported in the invoking shell. Part 2 needs
that dashboard restarted with `CREW_EXPAND_CMD` pointed at the
stub fixture (`tests/fixtures/expand_stub.sh`) so "Generate" hits a
deterministic canned response instead of shelling out to the real `claude`
CLI — **restart the dashboard back to its normal config in Cleanup, always**,
even if a step fails.

## Safety (non-negotiable)

* Only ever create the agent named **`test_ba_blobagent`** (Part 2, step 10)
  and the edge test agents **`test_ba_blobsrc`** / **`test_ba_blobtgt`** (Part
  2, step 6). Never touch `leads`, `builder`, `sales`, `AgentA`, `AgentB`.
* Homes MUST be set explicitly under `/tmp/crew_tests/`. "Launch it now" MUST
  be unchecked.
* Restarting the dashboard affects the isolated QA process — verify port
  `18788`, `MORPHDB_HOST`, and `CREW_APP` first, and always restore it afterward
  (Cleanup step 1), even if the script fails partway.
* Cleanup is mandatory.

## Portable preflight (run once before Part 1)

From a terminal already inside this Crew checkout. This records the exact
pre-test expander environment and aborts if any fixed fixture is already in
use:
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
export CREW_DASH_COOKIE="$(mktemp /tmp/crew-browser-$CREW_PORT.XXXXXX.cookies)"
export CREW_QA_STATE="$(mktemp -d /tmp/crew-browser-$CREW_PORT.XXXXXX.state)"
if [ "${CREW_EXPAND_CMD+x}" = x ]; then
  export CREW_QA_HAD_CREW_EXPAND_CMD=1 CREW_QA_ORIG_CREW_EXPAND_CMD="$CREW_EXPAND_CMD"
else
  export CREW_QA_HAD_CREW_EXPAND_CMD=0 CREW_QA_ORIG_CREW_EXPAND_CMD=""
fi
if [ "${EXPAND_STUB_MODE+x}" = x ]; then
  export CREW_QA_HAD_EXPAND_STUB_MODE=1 CREW_QA_ORIG_EXPAND_STUB_MODE="$EXPAND_STUB_MODE"
else
  export CREW_QA_HAD_EXPAND_STUB_MODE=0 CREW_QA_ORIG_EXPAND_STUB_MODE=""
fi
crew_dashboard_auth() {
  export CREW_DASH_CAP="$(tr -d '\r\n' < "$CREW_REPO/var/dashboard-$CREW_PORT.cap")"
  python3 -c 'import json,os; print(json.dumps({"capability":os.environ["CREW_DASH_CAP"]}))' \
    | curl -fsS -c "$CREW_DASH_COOKIE" -H 'Content-Type: application/json' \
        --data-binary @- "$CREW_DASH_URL/api/auth/bootstrap" >/dev/null
  test "$(curl -fsS "$CREW_DASH_URL/api/health" | python3 -c 'import json,sys; print(json.load(sys.stdin)["app"])')" = "$CREW_APP" || {
    echo "dashboard app does not match CREW_APP after restart; aborting" >&2; return 2;
  }
}
crew_dashboard_snapshot() {
  curl -fsS -b "$CREW_DASH_COOKIE" "$CREW_DASH_URL/api/graph/snapshot"
}
crew_dashboard_auth
test "$(curl -fsS "$CREW_DASH_URL/api/health" | python3 -c 'import json,sys; print(json.load(sys.stdin)["app"])')" = "$CREW_APP" || {
  echo "dashboard app does not match CREW_APP; refusing cleanup/mutation" >&2; exit 2;
}
crew_qa_snapshot() { crew_dashboard_snapshot; }
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
crew_qa_assert_unused test_ba_blobagent /tmp/crew_tests/test_ba_blobagent
crew_qa_assert_unused test_ba_blobsrc /tmp/crew_tests/test_ba_blobsrc
crew_qa_assert_unused test_ba_blobtgt /tmp/crew_tests/test_ba_blobtgt
```
The UI steps provision all three agents; the bundled expander fixture supplies
all generated values deterministically.

## Part 1 — blob mode is the default entry point (no config change)

1. Navigate to `$CREW_DASH_URL/#cap=$CREW_DASH_CAP` using the preflight values
   (skip only when this browser context already has the operator cookie).

2. Click `#addAgentBtn` (+ Agent).
   **Expected:** the "Create agent" modal opens in BLOB MODE: a single
   textarea labelled "Describe this agent in plain words" (`#a-blob`), a
   "Generate" button (`#a-generate`), and a "fill manually instead" link
   (`#a-manual-link`) are visible; the full manual form (`#a-name`, `#a-role`,
   etc.) is INSIDE a collapsed `<details>` fold (not expanded/visible by
   default).

3. Click `#a-manual-link`.
   **Expected:** the blob textarea/Generate/link block is hidden; the
   `<details>` fold auto-expands showing the full manual form (`#a-name`
   etc.) with no prefill.

4. Close the modal (Escape). Do not use arbitrary pre-existing nodes; the next
   step creates this script's two owned fixtures so the flow also works on an
   empty install.

5. Create `test_ba_blobsrc` (home `/tmp/crew_tests/test_ba_blobsrc`, launch
   unchecked) and `test_ba_blobtgt` (home `/tmp/crew_tests/test_ba_blobtgt`,
   launch unchecked) via the manual fold (same procedure as
   `tests/browser/create-agent.md`).

5b. Capture both exact records before using them:
    ```sh
    crew_qa_capture_agent test_ba_blobsrc /tmp/crew_tests/test_ba_blobsrc
    crew_qa_capture_agent test_ba_blobtgt /tmp/crew_tests/test_ba_blobtgt
    ```
    Stop unless both `_guid` / stored-session receipts were written.

6. Drag-connect `test_ba_blobsrc`'s `.conn-handle` onto `test_ba_blobtgt`.
   **Expected:** "Describe the relationship" modal opens in BLOB MODE: a
   textarea `#e-blob` ("Describe this relationship in plain words"), a
   `#e-generate` button, and `#e-manual-link`; the full manual form
   (`#e-label`, `#e-when`, `#e-does`, etc.) is inside a collapsed
   `<details>` fold.

7. Click `#e-manual-link`.
   **Expected:** blob block hides, fold expands showing the full manual
   form empty (unchanged from the pre-blob-mode form).

8. Close the modal without submitting (Escape).

## Part 2 — restart with the stub expander, exercise Generate + fallback

9. Restart the dashboard pointed at the stub:
   ```sh
   cd "$CREW_REPO"
   export CREW_EXPAND_CMD="tests/fixtures/expand_stub.sh" EXPAND_STUB_MODE=ok
   ./bin/crew dashboard stop && ./bin/crew dashboard start
   crew_dashboard_auth
   ```
   Wait for `crew_dashboard_snapshot` to return
   `"ok": true` before proceeding (a few seconds).

10. Navigate to the refreshed `$CREW_DASH_URL/#cap=$CREW_DASH_CAP` (the restart
    generated a new capability). Click `#addAgentBtn`. Type
    `handles onboarding emails for new customers` into `#a-blob`. Click
    `#a-generate`.
    **Expected:** a brief spinner/disabled state on `#a-generate`, then the
    `<details>` fold auto-expands and `#a-name` == `stubagent`, `#a-role` ==
    `stub role from fixture`, `#a-identity` == `stub identity from fixture`
    (the stub's canned "ok" response — see `tests/fixtures/expand_stub.sh`).

11. Change `#a-name` to `test_ba_blobagent` (keep the generated role/identity)
    and set the Home folder (`#a-home`, in the now-open fold) to
    `/tmp/crew_tests/test_ba_blobagent`; leave "Launch it now" unchecked.
    Click `#a-go`.
    **Expected:** toast `creating test_ba_blobagent…`; new node card appears.

11b. Run:
     ```sh
     crew_qa_capture_agent test_ba_blobagent /tmp/crew_tests/test_ba_blobagent
     ```
     Stop unless its exact `_guid` /
     stored-session receipt was written.

12. Cross-check via API:
    `crew_dashboard_snapshot | python3 -c "import json,sys; d=json.load(sys.stdin); a=[x for x in d['agents'] if x['name']=='test_ba_blobagent'][0]; print(a['role'], '|', a['identity'])"`
    **Expected:** prints `stub role from fixture | stub identity from
    fixture`.

13. Open the connect modal again: drag-connect `test_ba_blobsrc` →
    `test_ba_blobtgt` (from Part 1 step 6; if that edge doesn't exist this is
    still just opening the modal — no edge yet). Type
    `src sends qualified leads to tgt` into `#e-blob`. Click `#e-generate`.
    **Expected:** fold auto-expands; `#e-label` == `stub label`; the first
    row of `#e-when` (`.cl-input`) == `when stub fires`; `#e-does` == `stub
    action`; `#e-reply` is CHECKED; `#e-undirected` is CHECKED (a required
    reply must have a two-way authorization); `#e-max` == `5`.

14. Click `#e-go` to save.
    **Expected:** toast `connected test_ba_blobsrc → test_ba_blobtgt`;
    cross-check via API that the new edge has `label == "stub label"`,
    `conditions == ["when stub fires"]`, `target_action == "stub action"`,
    `reply_expected == true`, `directed == false`, `max_turns == 5`.

15. Restart the dashboard pointed at a FAILING stub:
    ```sh
    export CREW_EXPAND_CMD="tests/fixtures/expand_stub.sh" EXPAND_STUB_MODE=fail
    ./bin/crew dashboard stop && ./bin/crew dashboard start
    crew_dashboard_auth
    ```
    Wait for the snapshot endpoint to come back healthy, then navigate to the
    refreshed `$CREW_DASH_URL/#cap=$CREW_DASH_CAP`.

16. Click `#addAgentBtn`. Type `some raw freeform description of a new agent`
    into `#a-blob`. Click `#a-generate`.
    **Expected:** fold auto-expands; `#a-role` contains the VERBATIM text
    `some raw freeform description of a new agent` (the fallback path — no
    crash, no silently-empty field); a toast or inline message indicates the
    generation fell back (wording not asserted exactly, but must not read as
    success). Close without submitting.

17. Open the connect modal for the owned pair (`test_ba_blobsrc` →
    `test_ba_blobtgt`; do not use any other nodes and do not save). Type
    `raw edge description text`
    into `#e-blob`. Click `#e-generate`.
    **Expected:** fold auto-expands; the first `#e-when` row (`.cl-input`)
    contains the VERBATIM text `raw edge description text` (fallback stuffs
    the text into `conditions`). Close without submitting.

## Cleanup (always run, even if a step above failed)

1. **Restart the dashboard back to normal config first** (most important —
   do this even if everything else above failed):
   ```sh
   cd "$CREW_REPO"
   if [ "$CREW_QA_HAD_CREW_EXPAND_CMD" = 1 ]; then
     export CREW_EXPAND_CMD="$CREW_QA_ORIG_CREW_EXPAND_CMD"
   else
     unset CREW_EXPAND_CMD
   fi
   if [ "$CREW_QA_HAD_EXPAND_STUB_MODE" = 1 ]; then
     export EXPAND_STUB_MODE="$CREW_QA_ORIG_EXPAND_STUB_MODE"
   else
     unset EXPAND_STUB_MODE
   fi
   ./bin/crew dashboard stop && ./bin/crew dashboard start
   crew_dashboard_auth
   ```
   Confirm `crew_dashboard_snapshot` returns
   `"ok": true`.
2. Run `crew_qa_cleanup_agent test_ba_blobagent`,
   `crew_qa_cleanup_agent test_ba_blobsrc` (cascades its edge), and
   `crew_qa_cleanup_agent test_ba_blobtgt`:
   ```sh
   crew_qa_cleanup_agent test_ba_blobagent
   crew_qa_cleanup_agent test_ba_blobsrc
   crew_qa_cleanup_agent test_ba_blobtgt
   ```
   Each requires the exact `_guid`,
   stored-session, and home receipt; no direct tmux fallback is permitted.
3. Confirm via the API that all three are gone:
   ```sh
   crew_dashboard_snapshot | python3 -c 'import json,sys; d=json.load(sys.stdin); names={a["name"] for a in d["agents"]}; print("agents left:",[n for n in names if n.startswith("test_ba_blob")])'
   ```
   **Expected:** empty list (`[]`).
4. Remove the temporary cookie jar and empty receipt directory:
   `rm -f "$CREW_DASH_COOKIE"; rmdir "$CREW_QA_STATE"`.
