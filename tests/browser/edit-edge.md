# Browser script: edit + delete an edge (click edge → `POST /api/edge/update`|`/delete`)

Area D-browser-ui. Execute with browser tools against the isolated QA dashboard
on **http://127.0.0.1:18788**. The invoking shell's `MORPHDB_HOST` and
`CREW_APP` must name the same isolated backend.

## Safety (non-negotiable)

* Only ever create/touch agents named **`test_ba_editsrc`** and
  **`test_ba_edittgt`**, and only the edge between them. Never click an edge
  line/label belonging to the real graph (anything touching `leads`,
  `builder`, `sales`, `AgentA`, or `AgentB`) — inspect the pair header at the
  top of whatever modal opens (`<b>source</b> → <b>target</b>`) before typing
  anything, and close it (`#modalClose`) without saving if it isn't
  `test_ba_editsrc` / `test_ba_edittgt`.
* Both agents' homes MUST be set explicitly under `/tmp/crew_tests/`.
* "Launch it now" MUST be unchecked for both agents.
* Don't type into either agent's terminal dock.
* Cleanup is mandatory, even on failure.

## Portable preflight (run once before setup)

From a terminal already inside this Crew checkout. Abort rather than reclaiming
any same-named record, exact session, or home from an earlier/foreign run:
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
crew_qa_assert_unused test_ba_editsrc /tmp/crew_tests/test_ba_editsrc
crew_qa_assert_unused test_ba_edittgt /tmp/crew_tests/test_ba_edittgt
```
The setup below provisions both agents and its v1 edge from scratch.

## Expected no-launch state

A freshly created `launch:false` agent has a live tmux session and no runtime
process. Each node must say **runtime not started**. A **session down** badge at
this point is a failure.

## Setup — create two agents and connect them (v1 edge)

1. Navigate to `$CREW_DASH_URL/#cap=$CREW_DASH_CAP` using the preflight values
   (skip only when this browser context already has the operator cookie).

2. Create the two agents exactly as in `tests/browser/create-agent.md` steps
   2–9, using:
   * `test_ba_editsrc` — role `edit test source`, home
     `/tmp/crew_tests/test_ba_editsrc`, "Launch it now" unchecked.
   * `test_ba_edittgt` — role `edit test target`, home
     `/tmp/crew_tests/test_ba_edittgt`, "Launch it now" unchecked.
   **Expected:** two new node cards appear, both reading "runtime not started".

2b. Run:
    ```sh
    crew_qa_capture_agent test_ba_editsrc /tmp/crew_tests/test_ba_editsrc
    crew_qa_capture_agent test_ba_edittgt /tmp/crew_tests/test_ba_edittgt
    ```
    Both exact `_guid` /
    stored-session receipts must exist before creating or later deleting the
    edge.

3. Connect them exactly as in `tests/browser/connect-edge.md` steps 4–12,
   using these v1 values instead:
   * Label (`#e-label`): `edge v1`
   * First condition in `#e-when`: `when v1 happens`
   * "What should test_ba_edittgt do on receipt?" (`#e-does`): `do v1 thing`
   * `#e-reply` unchecked, `#e-undirected` unchecked, `#e-max` left `0`
   **Expected:** toast `connected test_ba_editsrc → test_ba_edittgt`; an edge
   line appears between the two nodes labeled `when v1 happens`.

## Steps — edit the edge

4. Click the edge's line (`.cedge`) or its label (`.cedge-label`) in the
   graph, between the `test_ba_editsrc` and `test_ba_edittgt` cards.
   **Expected:** a modal titled "Edit relationship" opens. Verify the pair
   header reads `test_ba_editsrc → test_ba_edittgt` before continuing. Field
   values should be pre-filled: `#e-label` = `edge v1`; the first row in
   `#e-when` = `when v1 happens`; `#e-does` = `do v1 thing`; `#e-undirected`
   unchecked; a "Delete" button (`#e-del`) and a "Save" button (`#e-save`)
   are both present.

5. Change the Label field (`#e-label`) to `edge v2`.

6. Clear the existing condition row in `#e-when` and retype it as
   `when v2 happens`.

7. Click "+ add another condition" (the `.cl-add` button for `#e-when`) and
   type `or when urgent` into the newly added row.

8. Change "What should test_ba_edittgt do on receipt?" (`#e-does`) to
   `do v2 thing`.

9. CHECK "test_ba_edittgt should reply back" (`#e-reply`).

10. CHECK "Two-way — both can message each other" (`#e-undirected`).
    **Expected:** the back-direction section (`#e-back-wrap`) becomes
    enabled — it loses class `disabled` and its inputs/buttons become
    enabled — and the pair arrow (`#e-arrow`) flips from "→" to "↔".

11. In the now-enabled "When should test_ba_edittgt message test_ba_editsrc?"
    section (`#e-when-back`), type `when v2 back-condition` into its first
    row.

12. Type `do v2 back thing` into "What should test_ba_editsrc do on receipt?"
    (`#e-does-back`). Leave "test_ba_editsrc should reply back"
    (`#e-reply-back`) UNCHECKED (to confirm it defaults/stays false when not
    touched).

13. Set "Limit messages per hour" (`#e-max`) to `5`.

14. Click "Save" (`#e-save`).
    **Expected:** a toast reads `edge updated` (not an `err` toast); the
    modal closes.

15. Wait for the graph to refresh (~2s).
    **Expected:** the edge now renders with arrowheads at BOTH ends (two-way);
    its on-canvas label shows the forward conditions (`when v2 happens`,
    `or when urgent`) and the back condition prefixed `↩`
    (`↩ when v2 back-condition`).

16. Cross-check with the API directly:
    `curl -fsS -b "$CREW_DASH_COOKIE" "$CREW_DASH_URL/api/graph/snapshot"`.
    **Expected:** the edge object has `label == "edge v2"`,
    `conditions == ["when v2 happens", "or when urgent"]`,
    `target_action == "do v2 thing"`, `reply_expected == true`,
    `back_conditions == ["when v2 back-condition"]`,
    `back_action == "do v2 back thing"`, `back_reply == false`,
    `max_turns == 5`, `directed == false`.

17. Re-open the same edge (click its line/label again) to verify the saved
    edit round-tripped into the form.
    **Expected:** the modal reopens pre-filled with `edge v2`, both
    conditions, `do v2 thing`, `#e-reply` CHECKED, `#e-undirected` CHECKED,
    the back condition/action from steps 11–12, and `#e-max` = `5`.

## Steps — delete the edge

18. With the edit modal still open (from step 17), click "Delete"
    (`#e-del`).
    **Expected:** a toast reads `edge deleted`; the modal closes.

19. Wait for the graph to refresh.
    **Expected:** the edge line/label between `test_ba_editsrc` and
    `test_ba_edittgt` is gone; both node cards remain.

20. Cross-check with the API:
    `curl -fsS -b "$CREW_DASH_COOKIE" "$CREW_DASH_URL/api/graph/snapshot"`.
    **Expected:** no edge in the `edges` array has `source_name ==
    "test_ba_editsrc"` and `target_name == "test_ba_edittgt"` (or vice versa).

## Cleanup (always run, even if a step above failed)

1. Run `crew_qa_cleanup_agent test_ba_editsrc`, then
   `crew_qa_cleanup_agent test_ba_edittgt`:
   ```sh
   crew_qa_cleanup_agent test_ba_editsrc
   crew_qa_cleanup_agent test_ba_edittgt
   ```
   Each revalidates the current row's
   exact `_guid`, stored session, and canonical home before Crew removal. A
   mismatch aborts without a direct tmux or filesystem fallback.
2. Confirm via the API that both agents are gone (same pattern as
   `tests/browser/connect-edge.md` cleanup step 5, substituting
   `test_ba_edit` for `test_ba_edge`).
3. Remove the temporary cookie jar and empty receipt directory:
   `rm -f "$CREW_DASH_COOKIE"; rmdir "$CREW_QA_STATE"`.
