# Browser script: connect two agents (drag-to-connect → `POST /api/edge/create`)

Area D-browser-ui. Execute with browser tools against the isolated QA dashboard
on **http://127.0.0.1:18788**. The invoking shell's `MORPHDB_HOST` and
`CREW_APP` must name the same isolated backend.

## Safety (non-negotiable)

* Only ever create/touch agents named **`test_ba_edgesrc`** and
  **`test_ba_edgetgt`**. Never drag a connect-handle onto (or off of) `leads`,
  `builder`, `sales`, `AgentA`, or `AgentB`, and never edit/delete an edge that
  doesn't have both endpoints named `test_ba_edge*`.
* Both agents' homes MUST be set explicitly under `/tmp/crew_tests/` (never
  leave the Home folder field blank).
* "Launch it now" MUST be unchecked for both agents before submitting.
* Don't type into either agent's terminal dock.
* Cleanup is mandatory, even on failure.

## Portable preflight (run once before setup)

From a terminal already inside this Crew checkout. Static fixture names are
allowed only because this preflight aborts if any same-named record, exact
default-project session, or home already exists:
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
crew_qa_assert_unused test_ba_edgesrc /tmp/crew_tests/test_ba_edgesrc
crew_qa_assert_unused test_ba_edgetgt /tmp/crew_tests/test_ba_edgetgt
```
The setup below then provisions both agents and their edge from scratch.

## Expected no-launch state

A freshly created `launch:false` agent has a live tmux session and no runtime
process. Each node must say **runtime not started**. A **session down** badge at
this point is a failure.

## Setup — create the two test agents

1. Navigate to `$CREW_DASH_URL/#cap=$CREW_DASH_CAP` using the preflight values
   (skip only when this browser context already has the operator cookie).

2. Create the source agent using the same procedure as
   `tests/browser/create-agent.md` steps 2–9, with:
   * Name (`#a-name`): `test_ba_edgesrc`
   * What does it do? (`#a-role`): `edge test source`
   * Home folder (`#a-home`, under Advanced): `/tmp/crew_tests/test_ba_edgesrc`
   * "Launch it now" (`#a-launch`): **unchecked**
   **Expected:** toast `creating test_ba_edgesrc…`; a new node card appears
   for it reading "runtime not started".

3. Repeat step 2 for the target agent:
   * Name: `test_ba_edgetgt`
   * What does it do?: `edge test target`
   * Home folder: `/tmp/crew_tests/test_ba_edgetgt`
   * "Launch it now": **unchecked**
   **Expected:** toast `creating test_ba_edgetgt…`; a second new "runtime not
   started" node card appears. No edge exists between the two yet.

3b. Capture both exact records before any graph mutation or cleanup:
    ```sh
    crew_qa_capture_agent test_ba_edgesrc /tmp/crew_tests/test_ba_edgesrc
    crew_qa_capture_agent test_ba_edgetgt /tmp/crew_tests/test_ba_edgetgt
    ```
    **Expected:** both commands write receipts containing the exact `_guid`
    and stored session. Stop on either failure.

## Steps — drag-connect + describe the relationship

4. Take a fresh accessibility snapshot of the graph (node positions drift
   continuously under the force-directed layout, so get current coordinates
   immediately before the drag rather than reusing an older snapshot).

5. Within `test_ba_edgesrc`'s node card (`.cnode.agent[data-sess="test_ba_edgesrc"]`),
   locate the small "●" connect handle (`.conn-handle`, tooltip "drag onto
   another agent to connect").

6. Drag from that handle to anywhere on `test_ba_edgetgt`'s node card
   (`.cnode.agent[data-sess="test_ba_edgetgt"]`) and release.
   **Expected:** a modal titled "Describe the relationship" opens; the pair
   header reads `test_ba_edgesrc → test_ba_edgetgt` (arrow `#e-arrow` shows
   "→", not "↔", since nothing has been marked two-way yet).

7. Type `browser test edge` into the Label field (`#e-label`).

8. In the "When should test_ba_edgesrc message test_ba_edgetgt?" section
   (`#e-when`), type `when a browser test fires` into the first condition row
   (`.cl-input`).

9. Type `acknowledge the test message` into "What should test_ba_edgetgt do on
   receipt?" (`#e-does`, a textarea).

10. Leave "test_ba_edgetgt should reply back" (`#e-reply`) UNCHECKED, and
    leave "Two-way — both can message each other" (`#e-undirected`) UNCHECKED
    — this exercises a one-way (directed) edge.

11. Leave "Limit messages per hour" (`#e-max`) at its default `0`.

12. Click "Connect" (`#e-go`).
    **Expected:** a toast reads `connected test_ba_edgesrc → test_ba_edgetgt`
    (not an `err` toast); the modal closes.

13. Wait for the graph to refresh (~2s).
    **Expected:** a new edge line now runs between the two nodes with a single
    arrowhead pointing at `test_ba_edgetgt`; its on-canvas label
    (`.cedge-label`) shows the text `when a browser test fires`.

14. Click the `test_ba_edgesrc` node to dock it, then click "ⓘ identity"
    (`#dockIdentity`).
    **Expected:** the identity card's "talks to →" list contains an entry for
    `test_ba_edgetgt` with the condition text `when a browser test fires`; its
    "hears from ←" list still reads "no one yet".

15. Close that identity card and dock, then click the `test_ba_edgetgt` node
    and open ITS identity card.
    **Expected:** "hears from ←" contains an entry for `test_ba_edgesrc` with
    "you: acknowledge the test message"; "talks to →" still reads "no one
    yet" (the edge is one-way, so `test_ba_edgetgt` may not message back).

16. Cross-check with the API directly:
    `curl -fsS -b "$CREW_DASH_COOKIE" "$CREW_DASH_URL/api/graph/snapshot"`.
    **Expected:** the `edges` array contains an object with
    `source_name == "test_ba_edgesrc"`, `target_name == "test_ba_edgetgt"`,
    `directed == true`, `conditions == ["when a browser test fires"]`,
    `target_action == "acknowledge the test message"`,
    `reply_expected == false`, `max_turns == 0`.

## Cleanup (always run, even if a step above failed)

1. From the same preflight shell run `crew_qa_cleanup_agent test_ba_edgesrc`
   (this cascades its owned edge), then
   `crew_qa_cleanup_agent test_ba_edgetgt`:
   ```sh
   crew_qa_cleanup_agent test_ba_edgesrc
   crew_qa_cleanup_agent test_ba_edgetgt
   ```
   Each helper requires the live
   `_guid`, stored session, and home to match its receipt before Crew may kill
   the exact managed session. Missing/mismatched receipts abort; there is no
   direct tmux fallback.
2. Confirm via the API that both agents AND the edge are gone:
   ```sh
   curl -fsS -b "$CREW_DASH_COOKIE" "$CREW_DASH_URL/api/graph/snapshot" | python3 -c 'import json,sys; d=json.load(sys.stdin); names={a["name"] for a in d["agents"]}; print("agents left:",[n for n in names if n.startswith("test_ba_edge")]); print("edges left:",[e["_guid"] for e in d["edges"] if e.get("source_name","").startswith("test_ba_edge") or e.get("target_name","").startswith("test_ba_edge")])'
   ```
   **Expected:** both lists print empty (`[]`).
3. Remove the temporary cookie jar and empty receipt directory:
   `rm -f "$CREW_DASH_COOKIE"; rmdir "$CREW_QA_STATE"`.
