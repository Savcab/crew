# Browser script: foreman badge + unblessed dashed styling + bless + edge budgets

Execute with browser tools against the isolated QA dashboard on
**http://127.0.0.1:18788**. `MORPHDB_HOST` and `CREW_APP` in the invoking shell
must name the same isolated backend. This creates throwaway fixtures through the
authenticated API and real spawn path (`launch:false` — never boots a model
runtime), then
exercises the graph + modal UI for the parts that only exist client-side
(dashed/amber unblessed styling, the foreman badge, the bless button, the
foreman toggle, the edge budget fields).

## Safety (non-negotiable)

* Only ever create/touch agents named **`test_w3ui_foreman`** and
  **`test_w3ui_kid`**, and the edge between them. Never touch `leads`,
  `builder`, `sales`, `AgentA`, or `AgentB`.
* Both agents' homes MUST be under `/tmp/crew_tests/`. The human-created
  foreman uses `/tmp/crew_tests/test_w3ui_foreman`; the agent-authored child
  must use its confined default `/tmp/crew_tests/default/test_w3ui_kid`.
* Fixtures are created with `launch:false` via the API/real spawn path (step 1),
  NOT through the create-agent modal — this script is about the graph/modal UI
  for foreman + bless, not agent creation.
* Cleanup is mandatory, even on failure.

## Portable preflight (run once before setup)

From a terminal already inside this Crew checkout. The fixed fixture names are
reserved only when no record, exact default-project session, or home exists:
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
  python3 -c 'import json,pathlib,shutil,sys; r=json.load(open(sys.argv[1])); p=pathlib.Path(r["home_arg"]); root=pathlib.Path("/tmp/crew_tests").resolve(); assert root in p.resolve().parents and p.resolve()!=root and not p.is_symlink(); assert (p/".crew-browser-owner").read_text(encoding="utf-8")==r["guid"]; shutil.rmtree(p)' "$receipt" || return 2
  rm -f "$receipt"
}
crew_qa_assert_unused test_w3ui_foreman /tmp/crew_tests/test_w3ui_foreman
crew_qa_assert_unused test_w3ui_kid /tmp/crew_tests/default/test_w3ui_kid
```
The API/Python setup below then provisions every row the script asserts.

## Setup — create fixtures via the API (launch:false)

1. Via `curl` (or `browser_network_request`), create the foreman + its
   spawned kid and connect them, all UNBLESSED (agent-actor authored):
   ```
   curl -fsS -b "$CREW_DASH_COOKIE" -X POST "$CREW_DASH_URL/api/agent/create" -H 'Content-Type: application/json' -H 'X-Crew-CSRF: 1' \
     -d '{"name":"test_w3ui_foreman","home":"/tmp/crew_tests/test_w3ui_foreman","launch":false,"launch_cmd":"true"}'
   curl -fsS -b "$CREW_DASH_COOKIE" -X POST "$CREW_DASH_URL/api/agent/foreman" -H 'Content-Type: application/json' -H 'X-Crew-CSRF: 1' \
     -d '{"name":"test_w3ui_foreman"}'
   ```
   **Expected:** both calls return `{"ok": true, ...}`.

   Now use the real spawn path with `test_w3ui_foreman` AS THE FOREMAN ACTOR,
   so `test_w3ui_kid` is created unblessed and lands inside the
   foreman's envelope, then connect them (also as the foreman actor, so the
   edge is unblessed too):
   ```sh
   cd "$CREW_REPO" && CREW_ROOT=/tmp/crew_tests python3 - <<'PY'
from crew import graphstore as gs, spawn
f = gs.get_agent_by_name('test_w3ui_foreman')
kid = spawn.spawn_agent('test_w3ui_kid',
                        agent_identity='Review untrusted <markup> safely', launch=False,
                        actor='test_w3ui_foreman')
gs.patch_object('agent', kid['_guid'], {'notes': 'UI fixture notes'})
gs.update_agent_grants(kid['_guid'], [
    {'name': 'shared-docs', 'path': '/tmp/crew_tests/shared-reference', 'mode': 'ro'}])
gs.create_edge(f['_guid'], kid['_guid'], actor='test_w3ui_foreman',
               max_turns=5, token_cap=1000, cost_cap=1.0)
print('kid blessed:', kid['blessed'])
PY
   ```
   **Expected:** prints `kid blessed: False`.

   Immediately capture both live rows before continuing:
   ```sh
   crew_qa_capture_agent test_w3ui_foreman /tmp/crew_tests/test_w3ui_foreman
   crew_qa_capture_agent test_w3ui_kid /tmp/crew_tests/default/test_w3ui_kid
   ```
   **Expected:** both receipts bind the exact `_guid` and stored session.

## Steps — graph visuals

2. Navigate to `$CREW_DASH_URL/#cap=$CREW_DASH_CAP` using the preflight values
   (or reload if this browser context already has the cookie). Wait ~2s for a
   poll cycle.

3. Take an accessibility snapshot of the graph.
   **Expected:** `test_w3ui_foreman`'s node card shows a foreman badge (text
   containing "foreman", e.g. "⚑ foreman") next to its name.

4. Inspect `test_w3ui_kid`'s node card
   (`.cnode.agent[data-sess="test_w3ui_kid"]`).
   **Expected:** it carries the `unblessed` CSS class (dashed, amber-tinted
   border) — confirm via `browser_evaluate`:
   ```js
   document.querySelector('.cnode.agent[data-sess="test_w3ui_kid"]').classList.contains('unblessed')
   ```
   returns `true`.

5. Inspect the edge line between the two nodes.
   **Expected:** via `browser_evaluate`, the `<line class="cedge ...">`
   connecting them has `stroke-dasharray` set (non-empty) and `stroke`
   `#d29922` (amber) — confirm with something like:
   ```js
   [...document.querySelectorAll('.cedge')].map(l => ({
     dash: l.getAttribute('stroke-dasharray'), stroke: l.getAttribute('stroke')
   }))
   ```
   at least one entry has a non-null `dash` and amber `stroke`.

## Steps — bless via the identity panel

6. Click the `test_w3ui_kid` node to dock it, then click "ⓘ identity"
   (`#dockIdentity`).
   **Expected:** the identity card shows a "blessed" row with a **bless**
   button (`#id-bless`) — the row does NOT just say "yes". It also shows the
   identity/mission text `Review untrusted <markup> safely` as literal text
   (no injected element), notes `UI fixture notes`, and a read-only file grant
   `refs/shared-docs → /tmp/crew_tests/shared-reference (ro)` with the
   recorded-intent honesty label.

7. Click the bless button.
   **Expected:** a toast reads something containing `blessed` (not an `err`
   toast); the modal closes.

8. Reload the graph (wait ~2s) and re-open `test_w3ui_kid`'s identity card.
   **Expected:** the "blessed" row now reads "yes" (no bless button); on the
   graph itself, `test_w3ui_kid`'s node card no longer carries the
   `unblessed` class (confirm via the same `browser_evaluate` snippet as step
   4 — now `false`).

## Steps — foreman toggle

9. Click the `test_w3ui_kid` node to dock it (if not already), open its
   identity card again.
   **Expected:** a "foreman" row with a button offering to make it foreman
   (e.g. "make foreman") — since `test_w3ui_foreman` already holds the flag,
   clicking it is expected to be REFUSED by the singleton rule.

10. Click the foreman toggle button on `test_w3ui_kid`'s card.
    **Expected:** an `err` toast appears mentioning `test_w3ui_foreman` (the
    current holder) — the singleton rule refusing a second foreman.

11. Open `test_w3ui_foreman`'s own identity card.
    **Expected:** its "foreman" row reads "yes" with a "revoke foreman"
    button. Its "talks to →" entry for `test_w3ui_kid` shows all three
    enforced edge caps: `5 msg/hr`, `1,000 tok/hr`, and `$1/hr`.

## Steps — edge budget fields

12. Click the edge label/line between the two nodes to open "Edit
    relationship".
    **Expected:** the modal shows a "token budget/hr" field and a "$
    budget/hr" field, pre-filled with `1000` and `1` respectively (the
    values set in the setup step).

13. Change the token budget field to `500` and the $ budget field to `0.25`,
    then click Save.
    **Expected:** a toast reads `edge updated` (not `err`); the modal
    closes.

14. Cross-check via the API:
    `curl -fsS -b "$CREW_DASH_COOKIE" "$CREW_DASH_URL/api/graph/snapshot"`.
    **Expected:** the edge between `test_w3ui_foreman` and `test_w3ui_kid`
    shows `token_cap == 500` and `cost_cap == 0.25`.

## Cleanup (always run, even if a step above failed)

1. Run `crew_qa_cleanup_agent test_w3ui_kid`, then
   `crew_qa_cleanup_agent test_w3ui_foreman`:
   ```sh
   crew_qa_cleanup_agent test_w3ui_kid
   crew_qa_cleanup_agent test_w3ui_foreman
   ```
   Each compares exact `_guid`,
   stored session, and canonical home to its receipt before Crew removal; a
   mismatch aborts without a direct tmux fallback.
2. Confirm via the API that both agents and the edge are gone:
   ```sh
   curl -fsS -b "$CREW_DASH_COOKIE" "$CREW_DASH_URL/api/graph/snapshot" | python3 -c 'import json,sys; d=json.load(sys.stdin); names={a["name"] for a in d["agents"]}; print("agents left:",[n for n in names if n.startswith("test_w3ui_")]); print("edges left:",[e["_guid"] for e in d["edges"] if e.get("source_name","").startswith("test_w3ui_") or e.get("target_name","").startswith("test_w3ui_")])'
   ```
   **Expected:** both lists print empty (`[]`).
3. Remove the temporary curl cookie jar and empty receipt directory:
   `rm -f "$CREW_DASH_COOKIE"; rmdir "$CREW_QA_STATE"`.
