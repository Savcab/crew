# Browser script: connect two agents (drag-to-connect → `POST /api/edge/create`)

Area D-browser-ui. Executed with browser tools (agent-browser/playwright) against
the REAL, already-running dashboard at **http://127.0.0.1:8788** (MorphDB app
`crew`). Creates two throwaway agents to connect — see safety rules below.

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

## Known gap — confirmed live, read before running

A freshly-created `--no-launch` agent's node card shows status **"session
down"** (grey dot), not "idle", from the moment it's created — even though its
tmux session is genuinely up. This is a real dashboard limitation (the
`alive`/`live_status` fields require an actual `claude` process on the
session's pane, per `crew/server/tmuxio.py:101-137` +
`crew/server/app.py:133-139`), not a sign anything in this script failed. See
`tests/browser/create-agent.md`'s "Known gap" section for the full detail. It
has no effect on the connect/edge flow below — you can drag-connect two
"session down" nodes exactly as you would two "idle" ones.

## Setup — create the two test agents

1. Navigate to `http://127.0.0.1:8788` (skip if already open).

2. Create the source agent using the same procedure as
   `tests/browser/create-agent.md` steps 2–9, with:
   * Name (`#a-name`): `test_ba_edgesrc`
   * What does it do? (`#a-role`): `edge test source`
   * Home folder (`#a-home`, under Advanced): `/tmp/crew_tests/test_ba_edgesrc`
   * "Launch it now" (`#a-launch`): **unchecked**
   **Expected:** toast `creating test_ba_edgesrc…`; a new node card appears
   for it reading "session down" (see Known gap above — this is expected, not
   a failure).

3. Repeat step 2 for the target agent:
   * Name: `test_ba_edgetgt`
   * What does it do?: `edge test target`
   * Home folder: `/tmp/crew_tests/test_ba_edgetgt`
   * "Launch it now": **unchecked**
   **Expected:** toast `creating test_ba_edgetgt…`; a second new "session
   down" node card appears (Known gap, as above). No edge exists between the
   two yet.

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
    `curl -s http://127.0.0.1:8788/api/graph/snapshot`.
    **Expected:** the `edges` array contains an object with
    `source_name == "test_ba_edgesrc"`, `target_name == "test_ba_edgetgt"`,
    `directed == true`, `conditions == ["when a browser test fires"]`,
    `target_action == "acknowledge the test message"`,
    `reply_expected == false`, `max_turns == 0`.

## Cleanup (always run, even if a step above failed)

1. `cd /Users/felix/Desktop/learn_ai/crew && ./bin/crew remove-agent test_ba_edgesrc`
   — this also drops any edge touching it (agent deletion cascades edges).
2. `./bin/crew remove-agent test_ba_edgetgt`
3. Fallback for any leftover session (e.g. a step failed before the record
   existed):
   ```
   tmux kill-session -t test_ba_edgesrc 2>/dev/null || true
   tmux kill-session -t test_ba_edgetgt 2>/dev/null || true
   ```
4. `rm -rf /tmp/crew_tests/test_ba_edgesrc /tmp/crew_tests/test_ba_edgetgt`
5. Confirm via the API that both agents AND the edge are gone:
   ```
   curl -s http://127.0.0.1:8788/api/graph/snapshot | python3 -c '
   import json, sys
   d = json.load(sys.stdin)
   names = {a["name"] for a in d["agents"]}
   print("agents left:", [n for n in names if n.startswith("test_ba_edge")])
   print("edges left:", [e["_guid"] for e in d["edges"]
                          if e.get("source_name","").startswith("test_ba_edge")
                          or e.get("target_name","").startswith("test_ba_edge")])'
   ```
   **Expected:** both lists print empty (`[]`).
