# Browser script: pending-approval tray (badge, list, approve, reject)

WAVE 4. Executed with browser tools (playwright) against the REAL, already-running
dashboard at **http://127.0.0.1:8788** (MorphDB app `crew` — the dashboard only
ever serves the DEFAULT project, so this script uses the real "crew" app with
`test_w4ui_*` fixtures, never a throwaway project). Creates fixtures via a direct
Python call (mirrors `foreman-bless.md`'s setup step, launch:false — never boots a
real claude), then exercises the pending tray UI end to end: badge count, the
list modal, approve, and reject(+reason).

## Safety (non-negotiable)

* Only ever create/touch agents named **`test_w4ui_foreman`** and
  **`test_w4ui_human`**, and any edge/pending-row between them. Never touch
  `leads`, `builder`, `sales`, `AgentA`, or `AgentB`.
* Both agents' homes MUST be set explicitly under `/tmp/crew_tests/`.
* Fixtures are created with `launch:false` via direct API/Python calls — this
  script is about the pending-tray UI, not agent creation.
* Cleanup is mandatory, even on failure (agents, sessions, homes, AND any
  leftover pending/graph_edit rows this run created).

## Setup — create fixtures + the FIRST pending request (Python, direct graphstore)

1. From the repo root:
   ```
   cd /Users/felix/Desktop/learn_ai/crew && python3 -c "
   from crew import graphstore as gs
   f = gs.create_agent('test_w4ui_foreman', home='/tmp/crew_tests/test_w4ui_foreman',
                        launch_cmd='true', can_edit_graph=True)
   h = gs.create_agent('test_w4ui_human', home='/tmp/crew_tests/test_w4ui_human',
                        launch_cmd='true')
   try:
       gs.create_edge(f['_guid'], h['_guid'], actor='test_w4ui_foreman',
                       max_turns=5, token_cap=1000, cost_cap=1.0)
   except gs.GraphError as e:
       print('pending as expected:', e)
   "
   ```
   **Expected:** prints a line starting `pending as expected:` containing
   `queued` and `crew pending` — the foreman's request to connect to the
   human-made node was queued, NOT applied.

## Steps — badge + tray list

2. Navigate to `http://127.0.0.1:8788` (or reload if already open). Wait ~2s
   for a poll cycle.

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
   ```
   cd /Users/felix/Desktop/learn_ai/crew && python3 -c "
   from crew import graphstore as gs
   rows = gs.list_objects('graph_edit', actor='test_w4ui_foreman', op='connect',
                           sort='created_at', order='desc', limit=5)['objects']
   print([r['result'] for r in rows])
   "
   ```
   **Expected:** the newest row's result is `approved`.

8. Confirm the requester got a notice (best-effort mail queued from the
   reserved `crew` sender):
   ```
   cd /Users/felix/Desktop/learn_ai/crew && python3 -c "
   from crew import graphstore as gs
   msgs = gs.list_objects('message', target='test_w4ui_foreman', sort='created_at',
                           order='desc', limit=10)['objects']
   print([(m['sender'], m['body']) for m in msgs if m['sender'] == 'crew'])
   "
   ```
   **Expected:** at least one `('crew', ...)` entry whose body contains
   `approved`.

## Steps — a second request, then reject with a reason

9. Issue a SECOND pending request — this time a cap raise on the just-
   approved edge (case (b) from the wave-4 spec, any agent raising a cap on
   an edge it's an endpoint of):
   ```
   cd /Users/felix/Desktop/learn_ai/crew && python3 -c "
   from crew import graphstore as gs
   f = gs.get_agent_by_name('test_w4ui_foreman')
   h = gs.get_agent_by_name('test_w4ui_human')
   e = gs.edges_from_to(f['_guid'], h['_guid'])[0]
   try:
       gs.update_edge(e['_guid'], {'max_turns': 50}, actor='test_w4ui_foreman')
   except gs.GraphError as ex:
       print('pending as expected:', ex)
   "
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
    ```
    cd /Users/felix/Desktop/learn_ai/crew && python3 -c "
    from crew import graphstore as gs
    f = gs.get_agent_by_name('test_w4ui_foreman')
    h = gs.get_agent_by_name('test_w4ui_human')
    e = gs.edges_from_to(f['_guid'], h['_guid'])[0]
    print('max_turns:', e['max_turns'])
    rows = gs.list_objects('graph_edit', actor='test_w4ui_foreman', op='update_edge',
                            sort='created_at', order='desc', limit=5)['objects']
    print(rows[0]['result'], rows[0].get('reason'))
    "
    ```
    **Expected:** `max_turns: 5` (unchanged), and `rejected not needed`.

13. Reload the dashboard.
    **Expected:** `#pendingBtn` is hidden again (badge back to 0 — no
    pending rows left).

## Cleanup (always run, even if a step above failed)

1. `cd /Users/felix/Desktop/learn_ai/crew && ./bin/crew remove-agent test_w4ui_human`
2. `./bin/crew remove-agent test_w4ui_foreman`
3. Fallback for any leftover session:
   ```
   tmux kill-session -t test_w4ui_foreman 2>/dev/null || true
   tmux kill-session -t test_w4ui_human 2>/dev/null || true
   ```
4. `rm -rf /tmp/crew_tests/test_w4ui_foreman /tmp/crew_tests/test_w4ui_human`
5. Confirm via the API that both agents and the edge are gone:
   ```
   curl -s http://127.0.0.1:8788/api/graph/snapshot | python3 -c '
   import json, sys
   d = json.load(sys.stdin)
   names = {a["name"] for a in d["agents"]}
   print("agents left:", [n for n in names if n.startswith("test_w4ui_")])
   print("edges left:", [e["_guid"] for e in d["edges"]
                          if e.get("source_name","").startswith("test_w4ui_")
                          or e.get("target_name","").startswith("test_w4ui_")])
   print("pending_count:", d.get("pending_count"))'
   ```
   **Expected:** both lists print empty (`[]`); `pending_count` reflects only
   OTHER suites' in-flight rows, not ours (it may be non-zero from a
   concurrent run, but must not include our `test_w4ui_*` rows — checked in
   step 5 above already resolving both of ours to approved/rejected).
