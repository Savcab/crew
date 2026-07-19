# Browser script: pending-approval tray (badge, list, approve, reject)

Execute with browser tools against the isolated QA dashboard on
**http://127.0.0.1:18788**. `MORPHDB_HOST` and `CREW_APP` in the invoking shell
must name the same isolated backend; a dashboard serves whichever project/app
its process was started with. This creates fixtures without launching a model
runtime, then exercises badge, list, approve, and reject-with-reason end to end.

## Safety (non-negotiable)

* Only ever create/touch agents named **`test_w4ui_foreman`** and
  **`test_w4ui_human`**, and any edge/pending-row between them. Never touch
  `leads`, `builder`, `sales`, `AgentA`, or `AgentB`.
* Both agents' homes MUST be set explicitly under `/tmp/crew_tests/`.
* Fixtures are created with `launch:false` via direct API/Python calls — this
  script is about the pending-tray UI, not agent creation.
* Cleanup is mandatory, even on failure. Resolve any still-pending fixture
  request as rejected, then remove agents, sessions, homes, and edges.
* Resolved `graph_edit` rows are durable audit history and must not be deleted.
  Approved/rejected rows from this script intentionally remain as evidence of
  the decisions that were made.

## Portable preflight (run once before setup)

From a terminal already inside this Crew checkout. Fixed names are reserved
only if no row, exact default-project session, home, or unresolved request for
the fixture actor already exists:
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
crew_qa_assert_unused test_w4ui_foreman /tmp/crew_tests/test_w4ui_foreman
crew_qa_assert_unused test_w4ui_human /tmp/crew_tests/test_w4ui_human
python3 -c 'from crew import graphstore as gs; rows=gs.list_objects("graph_edit",result="pending",limit=1000)["objects"]; assert not [r for r in rows if r.get("actor")=="test_w4ui_foreman"], "existing fixture pending row; aborting"'
```
The preflight never rejects another run's request or deletes another run's
session. An interrupted fixture is an explicit abort. The setup below creates
both agents and the pending rows it needs.

## Setup — create fixtures + the FIRST pending request (real spawn + graphstore)

1. From the repo root:
   ```sh
   cd "$CREW_REPO" && python3 - <<'PY'
from crew import graphstore as gs, spawn
f = spawn.spawn_agent('test_w4ui_foreman', home='/tmp/crew_tests/test_w4ui_foreman',
                      launch=False, launch_cmd='true', runtime='custom', foreman=True)
h = spawn.spawn_agent('test_w4ui_human', home='/tmp/crew_tests/test_w4ui_human',
                      launch=False, launch_cmd='true', runtime='custom')
try:
    gs.create_edge(f['_guid'], h['_guid'], actor='test_w4ui_foreman',
                   max_turns=5, token_cap=1000, cost_cap=1.0)
except gs.GraphError as e:
    print('pending as expected:', e)
PY
   ```
   **Expected:** prints a line starting `pending as expected:` containing
   `queued` and `crew pending` — the foreman's request to connect to the
   human-made node was queued, NOT applied.

   Then run:
   ```sh
   crew_qa_capture_agent test_w4ui_foreman /tmp/crew_tests/test_w4ui_foreman
   crew_qa_capture_agent test_w4ui_human /tmp/crew_tests/test_w4ui_human
   ```
   **Expected:** both exact `_guid` / stored-session receipts are captured
   before the tray mutates anything.

## Steps — badge + tray list

2. Navigate to `$CREW_DASH_URL/#cap=$CREW_DASH_CAP` using the preflight values
   (or reload if this browser context already has the operator cookie). Wait
   ~2s for a poll cycle.

3. Take an accessibility snapshot of the header.
   **Expected:** a pending button (`#pendingBtn`) is visible with a badge
   (`#pendingBadge`) reading `1`.

4. Click `#pendingBtn`.
   **Expected:** the modal opens (title containing "Pending"); it lists one
   row whose text includes `test_w4ui_foreman`, the op (`connect`), and a
   human-readable summary naming both `test_w4ui_foreman` and
   `test_w4ui_human`. An "approve" and a "reject" control are present on the
   row.

## Steps — approve

5. Click the row's approve control.
   **Expected:** a non-error toast appears (e.g. containing "approved"); the
   modal closes.

6. Wait ~2s (poll cycle), then check the graph.
   **Expected:** via `browser_evaluate` or a fresh
   `GET /api/graph/snapshot`, an edge now exists
   `test_w4ui_foreman -> test_w4ui_human`. Since it's foreman-authored (not
   human), it must render **unblessed** — dashed, amber stroke — confirm the
   same way `foreman-bless.md` step 5 does:
   ```js
   [...document.querySelectorAll('.cedge')].map(l => ({
     dash: l.getAttribute('stroke-dasharray'), stroke: l.getAttribute('stroke')
   }))
   ```
   at least one entry has a non-null `dash` and amber (`#d29922`) `stroke`.

7. Confirm via Python that the resolved graph_edit row reads `approved`:
   ```sh
   cd "$CREW_REPO" && python3 - <<'PY'
from crew import graphstore as gs
rows = gs.list_objects('graph_edit', actor='test_w4ui_foreman', op='connect',
                       sort='created_at', order='desc', limit=5)['objects']
print([r['result'] for r in rows])
PY
   ```
   **Expected:** the newest row's result is `approved`.

8. Confirm the requester got a notice (best-effort mail queued from the
   reserved `crew` sender):
   ```sh
   cd "$CREW_REPO" && python3 - <<'PY'
from crew import graphstore as gs
msgs = gs.list_objects('message', target='test_w4ui_foreman', sort='created_at',
                       order='desc', limit=10)['objects']
print([(m['sender'], m['body']) for m in msgs if m['sender'] == 'crew'])
PY
   ```
   **Expected:** at least one `('crew', ...)` entry whose body contains
   `approved`.

## Steps — a second request, then reject with a reason

9. Issue a SECOND pending request — this time a cap raise on the just-
   approved edge (case (b) from the wave-4 spec, any agent raising a cap on
   an edge it's an endpoint of):
   ```sh
   cd "$CREW_REPO" && python3 - <<'PY'
from crew import graphstore as gs
f = gs.get_agent_by_name('test_w4ui_foreman')
h = gs.get_agent_by_name('test_w4ui_human')
e = gs.edges_from_to(f['_guid'], h['_guid'])[0]
try:
    gs.update_edge(e['_guid'], {'max_turns': 50}, actor='test_w4ui_foreman')
except gs.GraphError as ex:
    print('pending as expected:', ex)
PY
   ```
   **Expected:** prints `pending as expected: ...cap raise... queued...`.

10. Reload the dashboard (or wait ~2s for the poll). `#pendingBadge` reads
    `1` again. Click `#pendingBtn`.
    **Expected:** the modal lists one row for the `update_edge` op, summary
    mentioning the cap field/value.

11. Click the row's reject control; when prompted for a reason, supply
    `not needed`.
    **Expected:** a non-error toast appears (e.g. containing "rejected");
    the modal closes.

12. Confirm the cap was NOT changed and the row reads `rejected` with the
    reason stored:
    ```sh
    cd "$CREW_REPO" && python3 - <<'PY'
from crew import graphstore as gs
f = gs.get_agent_by_name('test_w4ui_foreman')
h = gs.get_agent_by_name('test_w4ui_human')
e = gs.edges_from_to(f['_guid'], h['_guid'])[0]
print('max_turns:', e['max_turns'])
rows = gs.list_objects('graph_edit', actor='test_w4ui_foreman', op='update_edge',
                       sort='created_at', order='desc', limit=5)['objects']
print(rows[0]['result'], rows[0].get('reason'))
PY
    ```
    **Expected:** `max_turns: 5` (unchanged), and `rejected not needed`.

13. Reload the dashboard.
    **Expected:** `#pendingBtn` is hidden again (badge back to 0 — no
    pending rows left).

## Cleanup (always run, even if a step above failed)

1. Resolve only this fixture actor's still-pending requests through the normal
   rejection path. This leaves approved/rejected `graph_edit` rows intact as
   durable audit history; those rows must not be deleted:
   ```sh
   cd "$CREW_REPO" && python3 - <<'PY'
from crew import graphstore as gs, guard
rows = gs.list_objects('graph_edit', result='pending', sort='created_at',
                       order='desc', limit=1000)['objects']
mine = [r for r in rows if r.get('actor') == 'test_w4ui_foreman']
for row in mine:
    guard.reject_pending(row['_guid'], reason='browser test cleanup', actor='human')
left = gs.list_objects('graph_edit', result='pending', sort='created_at',
                       order='desc', limit=1000)['objects']
assert not [r for r in left if r.get('actor') == 'test_w4ui_foreman']
print('resolved pending rows:', len(mine))
PY
   ```
2. Run `crew_qa_cleanup_agent test_w4ui_human`, then
   `crew_qa_cleanup_agent test_w4ui_foreman`:
   ```sh
   crew_qa_cleanup_agent test_w4ui_human
   crew_qa_cleanup_agent test_w4ui_foreman
   ```
   Exact `_guid`, stored session,
   and home receipts are required; any mismatch aborts without a direct tmux
   fallback.
3. Confirm via the API that both agents and the edge are gone:
   ```sh
   curl -fsS -b "$CREW_DASH_COOKIE" "$CREW_DASH_URL/api/graph/snapshot" | python3 -c 'import json,sys; d=json.load(sys.stdin); names={a["name"] for a in d["agents"]}; print("agents left:",[n for n in names if n.startswith("test_w4ui_")]); print("edges left:",[e["_guid"] for e in d["edges"] if e.get("source_name","").startswith("test_w4ui_") or e.get("target_name","").startswith("test_w4ui_")]); print("pending_count:",d.get("pending_count"))'
   ```
   **Expected:** both lists print empty (`[]`); `pending_count` reflects only
   OTHER suites' in-flight rows, not ours (it may be non-zero from a
   concurrent run, but must not include our `test_w4ui_*` rows — checked in
   step 5 above already resolving both of ours to approved/rejected).
4. Remove the temporary cookie jar and empty receipt directory:
   `rm -f "$CREW_DASH_COOKIE"; rmdir "$CREW_QA_STATE"`.
