# Browser script: one-blob LLM config (connect modal + create-agent modal)

Area D-browser-ui. Two halves: Part 1 exercises BLOB MODE's presence/manual-
fallback against the dashboard AS ALREADY RUNNING (no config change needed).
Part 2 needs the dashboard restarted with `CREW_EXPAND_CMD` pointed at the
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
* Restarting the dashboard affects the SHARED live process — do this only for
  this script's Part 2, and ALWAYS restart back to normal config afterward
  (Cleanup step 1), even if the script fails partway.
* Cleanup is mandatory.

## Part 1 — blob mode is the default entry point (no config change)

1. Navigate to `http://127.0.0.1:8788` (skip if already open).

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

4. Close the modal (Escape). Drag-connect two arbitrary existing nodes (or
   click any node's `.conn-handle` and release over another node — reuse
   `test_ba_blobsrc`/`test_ba_blobtgt` created in Part 2 step 6 if convenient,
   or any two live nodes, cancelling the connect afterward without saving) —
   actually, simplest: open the connect modal by creating two throwaway
   agents first is not required for Part 1; instead SKIP directly to
   confirming the connect modal's blob-mode markup via its OWN check in step
   5 using the two agents from Part 2 step 6 (create them now, ahead of
   order, if Part 2 hasn't run yet — otherwise reuse).

5. Create `test_ba_blobsrc` (home `/tmp/crew_tests/test_ba_blobsrc`, launch
   unchecked) and `test_ba_blobtgt` (home `/tmp/crew_tests/test_ba_blobtgt`,
   launch unchecked) via the manual fold (same procedure as
   `tests/browser/create-agent.md`).

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
   ```
   cd /Users/felix/Desktop/learn_ai/crew
   CREW_EXPAND_CMD="tests/fixtures/expand_stub.sh" EXPAND_STUB_MODE=ok ./bin/crew dashboard stop && CREW_EXPAND_CMD="tests/fixtures/expand_stub.sh" EXPAND_STUB_MODE=ok ./bin/crew dashboard start
   ```
   Wait for `curl -s http://127.0.0.1:8788/api/graph/snapshot` to return
   `"ok": true` before proceeding (a few seconds).

10. Reload `http://127.0.0.1:8788`. Click `#addAgentBtn`. Type
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

12. Cross-check via API:
    `curl -s http://127.0.0.1:8788/api/graph/snapshot | python3 -c "import json,sys; d=json.load(sys.stdin); a=[x for x in d['agents'] if x['name']=='test_ba_blobagent'][0]; print(a['role'], '|', a['identity'])"`
    **Expected:** prints `stub role from fixture | stub identity from
    fixture`.

13. Open the connect modal again: drag-connect `test_ba_blobsrc` →
    `test_ba_blobtgt` (from Part 1 step 6; if that edge doesn't exist this is
    still just opening the modal — no edge yet). Type
    `src sends qualified leads to tgt` into `#e-blob`. Click `#e-generate`.
    **Expected:** fold auto-expands; `#e-label` == `stub label`; the first
    row of `#e-when` (`.cl-input`) == `when stub fires`; `#e-does` == `stub
    action`; `#e-reply` is CHECKED; `#e-undirected` is UNCHECKED (stub's
    `directed: true`); `#e-max` == `5`.

14. Click `#e-go` to save.
    **Expected:** toast `connected test_ba_blobsrc → test_ba_blobtgt`;
    cross-check via API that the new edge has `label == "stub label"`,
    `conditions == ["when stub fires"]`, `target_action == "stub action"`,
    `reply_expected == true`, `directed == true`, `max_turns == 5`.

15. Restart the dashboard pointed at a FAILING stub:
    ```
    CREW_EXPAND_CMD="tests/fixtures/expand_stub.sh" EXPAND_STUB_MODE=fail ./bin/crew dashboard stop && CREW_EXPAND_CMD="tests/fixtures/expand_stub.sh" EXPAND_STUB_MODE=fail ./bin/crew dashboard start
    ```
    Wait for the snapshot endpoint to come back healthy, then reload.

16. Click `#addAgentBtn`. Type `some raw freeform description of a new agent`
    into `#a-blob`. Click `#a-generate`.
    **Expected:** fold auto-expands; `#a-role` contains the VERBATIM text
    `some raw freeform description of a new agent` (the fallback path — no
    crash, no silently-empty field); a toast or inline message indicates the
    generation fell back (wording not asserted exactly, but must not read as
    success). Close without submitting.

17. Open the connect modal (`test_ba_blobsrc` → `test_ba_blobtgt` again is
    fine, or any two nodes — don't save). Type `raw edge description text`
    into `#e-blob`. Click `#e-generate`.
    **Expected:** fold auto-expands; the first `#e-when` row (`.cl-input`)
    contains the VERBATIM text `raw edge description text` (fallback stuffs
    the text into `conditions`). Close without submitting.

## Cleanup (always run, even if a step above failed)

1. **Restart the dashboard back to normal config first** (most important —
   do this even if everything else above failed):
   ```
   cd /Users/felix/Desktop/learn_ai/crew
   unset CREW_EXPAND_CMD EXPAND_STUB_MODE
   ./bin/crew dashboard stop && ./bin/crew dashboard start
   ```
   Confirm `curl -s http://127.0.0.1:8788/api/graph/snapshot` returns
   `"ok": true`.
2. `./bin/crew remove-agent test_ba_blobagent`
3. `./bin/crew remove-agent test_ba_blobsrc` (cascades its edge)
4. `./bin/crew remove-agent test_ba_blobtgt`
5. Fallback for any leftover session:
   ```
   tmux kill-session -t test_ba_blobagent 2>/dev/null || true
   tmux kill-session -t test_ba_blobsrc 2>/dev/null || true
   tmux kill-session -t test_ba_blobtgt 2>/dev/null || true
   ```
6. `rm -rf /tmp/crew_tests/test_ba_blobagent /tmp/crew_tests/test_ba_blobsrc /tmp/crew_tests/test_ba_blobtgt`
7. Confirm via the API that all three are gone:
   ```
   curl -s http://127.0.0.1:8788/api/graph/snapshot | python3 -c '
   import json, sys
   d = json.load(sys.stdin)
   names = {a["name"] for a in d["agents"]}
   print("agents left:", [n for n in names if n.startswith("test_ba_blob")])'
   ```
   **Expected:** empty list (`[]`).
