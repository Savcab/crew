# Browser script: foreman badge + unblessed dashed styling + bless + edge budgets

WAVE 3. Executed with browser tools (playwright) against the REAL, already-running
dashboard at **http://127.0.0.1:8788** (MorphDB app `crew`). Creates throwaway
fixtures via the API directly (launch:false — never boots a real claude), then
exercises the graph + modal UI for the parts that only exist client-side
(dashed/amber unblessed styling, the foreman badge, the bless button, the
foreman toggle, the edge budget fields).

## Safety (non-negotiable)

* Only ever create/touch agents named **`test_w3ui_foreman`** and
  **`test_w3ui_kid`**, and the edge between them. Never touch `leads`,
  `builder`, `sales`, `AgentA`, or `AgentB`.
* Both agents' homes MUST be set explicitly under `/tmp/crew_tests/`.
* Fixtures are created with `launch:false` via direct API calls (step 1), NOT
  through the create-agent modal — this script is about the graph/modal UI for
  foreman + bless, not agent creation.
* Cleanup is mandatory, even on failure.

## Setup — create fixtures via the API (launch:false)

1. Via `curl` (or `browser_network_request`), create the foreman + its
   spawned kid and connect them, all UNBLESSED (agent-actor authored):
   ```
   curl -s -X POST http://127.0.0.1:8788/api/agent/create -H 'Content-Type: application/json' \
     -d '{"name":"test_w3ui_foreman","home":"/tmp/crew_tests/test_w3ui_foreman","launch":false,"launch_cmd":"true"}'
   curl -s -X POST http://127.0.0.1:8788/api/agent/foreman -H 'Content-Type: application/json' \
     -d '{"name":"test_w3ui_foreman"}'
   ```
   **Expected:** both calls return `{"ok": true, ...}`.

   Now, from a Python one-liner (or the CLI: `./bin/crew spawn-agent
   test_w3ui_kid --no-launch --launch-cmd true`), spawn `test_w3ui_kid` AS
   THE FOREMAN ACTOR so it's created unblessed and lands inside the
   foreman's envelope, then connect them (also as the foreman actor, so the
   edge is unblessed too):
   ```
   cd /Users/felix/Desktop/learn_ai/crew && python3 -c "
   from crew import graphstore as gs
   f = gs.get_agent_by_name('test_w3ui_foreman')
   kid = gs.create_agent('test_w3ui_kid', home='/tmp/crew_tests/test_w3ui_kid', actor='test_w3ui_foreman')
   gs.create_edge(f['_guid'], kid['_guid'], actor='test_w3ui_foreman', max_turns=5, token_cap=1000, cost_cap=1.0)
   print('kid blessed:', kid['blessed'])
   "
   ```
   **Expected:** prints `kid blessed: False`.

## Steps — graph visuals

2. Navigate to `http://127.0.0.1:8788` (or reload if already open). Wait ~2s
   for a poll cycle.

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
   button (`#id-bless`) — the row does NOT just say "yes".

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
    button.

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
    `curl -s http://127.0.0.1:8788/api/graph/snapshot`.
    **Expected:** the edge between `test_w3ui_foreman` and `test_w3ui_kid`
    shows `token_cap == 500` and `cost_cap == 0.25`.

## Cleanup (always run, even if a step above failed)

1. `cd /Users/felix/Desktop/learn_ai/crew && ./bin/crew remove-agent test_w3ui_kid`
2. `./bin/crew remove-agent test_w3ui_foreman`
3. Fallback for any leftover session:
   ```
   tmux kill-session -t test_w3ui_foreman 2>/dev/null || true
   tmux kill-session -t test_w3ui_kid 2>/dev/null || true
   ```
4. `rm -rf /tmp/crew_tests/test_w3ui_foreman /tmp/crew_tests/test_w3ui_kid`
5. Confirm via the API that both agents and the edge are gone:
   ```
   curl -s http://127.0.0.1:8788/api/graph/snapshot | python3 -c '
   import json, sys
   d = json.load(sys.stdin)
   names = {a["name"] for a in d["agents"]}
   print("agents left:", [n for n in names if n.startswith("test_w3ui_")])
   print("edges left:", [e["_guid"] for e in d["edges"]
                          if e.get("source_name","").startswith("test_w3ui_")
                          or e.get("target_name","").startswith("test_w3ui_")])'
   ```
   **Expected:** both lists print empty (`[]`).
