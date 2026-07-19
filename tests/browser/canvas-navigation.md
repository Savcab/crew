# Browser script: Figma-feel canvas navigation (wheel zoom-to-cursor, pan, legacy flows)

Area D-browser-ui. Executed with browser tools (agent-browser/playwright) against
the REAL, already-running dashboard at **http://127.0.0.1:8788** (MorphDB app
`crew`). This exercises pure view-state (zoom/pan, stored in `localStorage`, not
MorphDB) plus a live regression check of the three legacy graph interactions, so
it needs its own throwaway agents only for the regression section.

## Safety (non-negotiable)

* Only ever create/touch agents named **`test_ba_canvasA`** and
  **`test_ba_canvasB`** for the regression section. Never touch `leads`,
  `builder`, `sales`, `AgentA`, `AgentB`, or any of their edges.
* Both agents' homes MUST be set explicitly under `/tmp/crew_tests/`.
* "Launch it now" MUST be unchecked for both.
* Don't type into either agent's terminal dock.
* Cleanup is mandatory, even on failure.
* Before finishing, reset the view: `localStorage.setItem('crew.view.v1', JSON.stringify({zoom:1,panX:0,panY:0}))`
  then reload, so the next person's view isn't left zoomed/panned oddly.

## Part 1 — wheel zoom-to-cursor

1. Navigate to `http://127.0.0.1:8788` (skip if already open).

2. Read the current `#zoomPct` text (baseline, expect `100%` on a fresh
   localStorage, but note whatever it actually is).

3. Take an accessibility snapshot / bounding box of `#cgraph` (the canvas
   viewport). Compute a point near its **top-left quadrant**, e.g.
   `(rect.left + rect.width*0.25, rect.top + rect.height*0.25)` — call this
   `P`. Pick a point that is NOT centered (center-anchored zoom and
   cursor-anchored zoom look identical at dead center, so the test must not
   use it).

4. Dispatch a synthetic `wheel` event at `P` with `deltaY: -240` (scroll
   "up"/zoom-in gesture), `ctrlKey: false`, `bubbles: true`, targeting the
   element at `P` (`document.elementFromPoint`). Use
   `mcp__plugin_playwright_playwright__browser_evaluate` to run:
   ```js
   () => {
     const r = document.getElementById('cgraph').getBoundingClientRect();
     const x = r.left + r.width * 0.25, y = r.top + r.height * 0.25;
     const el = document.elementFromPoint(x, y);
     const ev = new WheelEvent('wheel', { deltaY: -240, deltaX: 0, clientX: x, clientY: y, bubbles: true, cancelable: true, ctrlKey: false });
     el.dispatchEvent(ev);
     return { x, y, pct: document.getElementById('zoomPct').textContent };
   }
   ```
   **Expected:** the returned `pct` is greater than the Step-2 baseline (zoomed
   in) — a single tick should move it noticeably (at least a few percentage
   points with the `-240` delta).

5. Re-read the CSS transform on `.gcanvas` (`getComputedStyle(...).transform`
   or read `document.querySelector('.gcanvas').style.transform` directly) and
   confirm it is a `translate(...) scale(...)` whose scale factor matches
   `pct/100` (within rounding).

6. Confirm the point `P` stayed visually anchored: compute the WORLD
   coordinate under `P` before and after the zoom
   (`world = (screenP - cgraphRect.top/left - pan) / zoom` — subtract
   `#cgraph`'s OWN bounding-rect offset first, since the header above it
   means `#cgraph`'s top-left isn't the viewport origin; using the pan/zoom
   pulled from `localStorage.getItem('crew.view.v1')` before and after) —
   they should match within ~1px (rounding: `setZoom` rounds the zoom factor
   to 2 decimals, so expect a sub-pixel remainder, not an exact match). This
   is the actual "zoom to cursor, not center" assertion — a center-anchored
   implementation would move `P`'s world coordinate by much more than
   rounding noise.

7. Dispatch a second wheel event at the SAME point `P` with `deltaY: 240`
   (zoom out) and `ctrlKey: true` (simulating trackpad pinch-out).
   **Expected:** `pct` decreases back down; `P` again stays anchored (repeat
   the world-coordinate check from step 6). This confirms ctrlKey wheel
   (trackpad pinch) also zooms, using the same anchor rule.

8. Dispatch a wheel event with `deltaX: 120, deltaY: 0` (a horizontal
   two-finger swipe) at any point on the canvas.
   **Expected:** `pct` changes (down, since positive delta = zoom out by this
   script's convention) — confirms two-finger scroll also zooms rather than
   panning, per Felix's explicit "scrolling should zoom" ask.

9. Confirm the zoom is clamped: dispatch ~30 zoom-in wheel ticks in a row
   (`deltaY: -500` each) at the canvas center, then read `#zoomPct`.
   **Expected:** `pct` never exceeds `300%`. Then dispatch ~30 zoom-out ticks
   (`deltaY: 500` each); **expected:** `pct` never drops below `5%`.

## Part 2 — background-drag pan

10. Reset the view: run
    `localStorage.setItem('crew.view.v1', JSON.stringify({zoom:1,panX:0,panY:0}))`
    then reload `http://127.0.0.1:8788` and wait for the graph to render.

11. Find an EMPTY point on `#cgraph` (not over any `.cnode` card — use a
    corner of the canvas bounding box, away from node positions visible in an
    accessibility snapshot).

12. Use `mcp__plugin_playwright_playwright__browser_drag` (or manual
    mousedown/mousemove/mouseup via `browser_evaluate`/dispatch) from that
    empty point to a point ~150px right and ~80px down.
    **Expected:** `localStorage.getItem('crew.view.v1')`'s `panX`/`panY`
    change by approximately that delta (the whole graph visibly shifted, not
    an individual node).

13. Take a snapshot and confirm a node CARD did not move relative to the
    OTHER nodes (i.e. this was a view pan, not a node drag) — cross-check
    that `crew.pos.v1` in localStorage (per-node saved positions) is
    unchanged by this drag.

## Part 3 — legacy flows still work (regression, needs live agents)

14. Reset the view again (per step 10) and reload.

15. Create agent `test_ba_canvasA` (home `/tmp/crew_tests/test_ba_canvasA`,
    launch unchecked) and `test_ba_canvasB` (home
    `/tmp/crew_tests/test_ba_canvasB`, launch unchecked) — same procedure as
    `tests/browser/create-agent.md` steps 2–9.

16. **Node drag still works:** take a fresh snapshot, drag
    `test_ba_canvasA`'s card body (NOT its `.conn-handle`) by ~100px.
    **Expected:** the card visibly moves and stays where dropped (pinned);
    `crew.pos.v1` in localStorage now has an entry for `test_ba_canvasA`.

17. **Drag-connect still opens the modal:** drag from `test_ba_canvasA`'s
    `.conn-handle` onto `test_ba_canvasB`'s card.
    **Expected:** the "Describe the relationship" modal opens with the pair
    header `test_ba_canvasA → test_ba_canvasB`. Close the modal without
    submitting (Escape or the × button) — this script doesn't need a real
    edge.

18. **Edge label click still opens edit modal:** (skip if step 17 was closed
    without creating an edge — instead, quickly fill the Label field with
    `canvas regression` and click Connect to create one edge for this check,
    then) click the edge's on-canvas label.
    **Expected:** the "Edit relationship" modal opens for that edge.
    Close it.

19. **Zoom-to-fit button still works:** click `#zoomFit`.
    **Expected:** `#zoomPct` changes to frame both nodes (no JS error in
    console — check `mcp__plugin_playwright_playwright__browser_console_messages`).

20. **Ctrl/Cmd+0 reset still works:** focus the page body (click empty
    canvas first to ensure focus isn't in a text field), dispatch a keydown
    for `0` with `ctrlKey: true` (or `metaKey: true` on mac).
    **Expected:** the view re-fits (same effect as `#zoomFit` — zoomToFit is
    what Ctrl/Cmd+0 calls).

21. **Ctrl/Cmd +/- still work:** dispatch keydown `=` with `ctrlKey:true`,
    confirm `#zoomPct` increases; dispatch keydown `-` with `ctrlKey:true`,
    confirm it decreases.

## Cleanup (always run, even if a step above failed)

1. `cd /Users/felix/Desktop/learn_ai/crew && ./bin/crew remove-agent test_ba_canvasA`
2. `./bin/crew remove-agent test_ba_canvasB`
3. Fallback: `tmux kill-session -t test_ba_canvasA 2>/dev/null || true` and
   same for `test_ba_canvasB`.
4. `rm -rf /tmp/crew_tests/test_ba_canvasA /tmp/crew_tests/test_ba_canvasB`
5. Reset the view: run in the page
   `localStorage.setItem('crew.view.v1', JSON.stringify({zoom:1,panX:0,panY:0}))`
   and reload, so the shared dashboard isn't left zoomed oddly for the next
   person.
6. Confirm via the API that both agents are gone:
   ```
   curl -s http://127.0.0.1:8788/api/graph/snapshot | python3 -c '
   import json, sys
   d = json.load(sys.stdin)
   names = {a["name"] for a in d["agents"]}
   print("agents left:", [n for n in names if n.startswith("test_ba_canvas")])'
   ```
   **Expected:** empty list (`[]`).
