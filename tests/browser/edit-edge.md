# Browser script: edit + delete an edge (click edge → `POST /api/edge/update`|`/delete`)

Area D-browser-ui. Executed with browser tools (agent-browser/playwright) against
the REAL, already-running dashboard at **http://127.0.0.1:8788** (MorphDB app
`crew`). Creates two throwaway agents + one edge to edit — see safety rules.

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

## Known gap — confirmed live, read before running

A freshly-created `--no-launch` agent's node card shows status **"session
down"** (grey dot), not "idle", from the moment it's created — even though its
tmux session is genuinely up (the dashboard's `alive`/`live_status` fields
require an actual `claude` process on the session's pane; see
`crew/server/tmuxio.py:101-137` + `crew/server/app.py:133-139`, and
`tests/browser/create-agent.md`'s "Known gap" for the full detail). It has no
effect on the edit/delete flow below.

## Setup — create two agents and connect them (v1 edge)

1. Navigate to `http://127.0.0.1:8788` (skip if already open).

2. Create the two agents exactly as in `tests/browser/create-agent.md` steps
   2–9, using:
   * `test_ba_editsrc` — role `edit test source`, home
     `/tmp/crew_tests/test_ba_editsrc`, "Launch it now" unchecked.
   * `test_ba_edittgt` — role `edit test target`, home
     `/tmp/crew_tests/test_ba_edittgt`, "Launch it now" unchecked.
   **Expected:** two new node cards appear, both reading "session down"
   (Known gap above — expected, not a failure).

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
    `curl -s http://127.0.0.1:8788/api/graph/snapshot`.
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
    `curl -s http://127.0.0.1:8788/api/graph/snapshot`.
    **Expected:** no edge in the `edges` array has `source_name ==
    "test_ba_editsrc"` and `target_name == "test_ba_edittgt"` (or vice versa).

## Cleanup (always run, even if a step above failed)

1. `cd /Users/felix/Desktop/learn_ai/crew && ./bin/crew remove-agent test_ba_editsrc`
2. `./bin/crew remove-agent test_ba_edittgt`
3. Fallback for any leftover session:
   ```
   tmux kill-session -t test_ba_editsrc 2>/dev/null || true
   tmux kill-session -t test_ba_edittgt 2>/dev/null || true
   ```
4. `rm -rf /tmp/crew_tests/test_ba_editsrc /tmp/crew_tests/test_ba_edittgt`
5. Confirm via the API that both agents are gone (same pattern as
   `tests/browser/connect-edge.md` cleanup step 5, substituting
   `test_ba_edit` for `test_ba_edge`).
