# Browser script: Figma-feel canvas navigation (wheel zoom-to-cursor, pan, legacy flows)

Area D-browser-ui. Execute with browser tools against the isolated QA dashboard
on **http://127.0.0.1:18788**. `MORPHDB_HOST` and `CREW_APP` in the invoking
shell must name the same isolated backend. This exercises pure view-state
(zoom/pan, stored in project-scoped `localStorage`, not
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
* Before finishing, reset the active workspace's view using `crewQaKeys.view`
  then reload, so the next person's view isn't left zoomed/panned oddly.

## Portable preflight (run once before Part 1)

From a terminal already inside this Crew checkout. The fixed fixtures are used
only when no same-named row, exact default-project session, or home exists:
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
crew_qa_assert_unused test_ba_canvasA /tmp/crew_tests/test_ba_canvasA
crew_qa_assert_unused test_ba_canvasB /tmp/crew_tests/test_ba_canvasB
```
Parts 1–2 require no graph fixture; Part 3 provisions its own two agents.

## Part 1 — wheel zoom-to-cursor

1. Navigate to `$CREW_DASH_URL/#cap=$CREW_DASH_CAP` using the preflight values
   (skip only when this browser context already has the operator cookie).

   Establish the exact project-scoped storage keys in the page:
   ```js
   async () => {
     const snap = await (await fetch('/api/graph/snapshot')).json();
     const suffix = snap.workspace_key === 'crew' ? '' : `.${encodeURIComponent(snap.workspace_key)}`;
     window.crewQaKeys = { view: `crew.view.v1${suffix}`, pos: `crew.pos.v1${suffix}` };
     return { workspace: snap.workspace_key, ...window.crewQaKeys };
   }
   ```
   **Expected:** `workspace` equals `$CREW_APP`; a non-default QA app has a
   suffixed key rather than reusing the default project's view.
   Re-run this short key-discovery snippet after every full page reload before
   reading or writing `crewQaKeys` again.

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
   the browser tool's page-evaluation facility to run:
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
   pulled from `localStorage.getItem(crewQaKeys.view)` before and after) —
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
    `localStorage.setItem(crewQaKeys.view, JSON.stringify({zoom:1,panX:0,panY:0}))`
    then reload `$CREW_DASH_URL` and wait for the graph to render.

11. Find an EMPTY point on `#cgraph` (not over any `.cnode` card — use a
    corner of the canvas bounding box, away from node positions visible in an
    accessibility snapshot).

12. Use the browser tool's drag operation (or manual
    mousedown/mousemove/mouseup via page evaluation/dispatch) from that
    empty point to a point ~150px right and ~80px down.
    **Expected:** `localStorage.getItem(crewQaKeys.view)`'s `panX`/`panY`
    change by approximately that delta (the whole graph visibly shifted, not
    an individual node).

13. Take a snapshot and confirm a node CARD did not move relative to the
    OTHER nodes (i.e. this was a view pan, not a node drag) — cross-check
    that `localStorage.getItem(crewQaKeys.pos)` (per-node saved positions) is
    unchanged by this drag.

## Part 3 — legacy flows still work (regression, needs live agents)

14. Reset the view again (per step 10) and reload.

15. Create agent `test_ba_canvasA` (home `/tmp/crew_tests/test_ba_canvasA`,
    launch unchecked) and `test_ba_canvasB` (home
    `/tmp/crew_tests/test_ba_canvasB`, launch unchecked) — same procedure as
    `tests/browser/create-agent.md` steps 2–9.

15b. Run:
     ```sh
     crew_qa_capture_agent test_ba_canvasA /tmp/crew_tests/test_ba_canvasA
     crew_qa_capture_agent test_ba_canvasB /tmp/crew_tests/test_ba_canvasB
     ```
     Stop unless both exact
     `_guid` / stored-session receipts were captured.

16. **Node drag still works:** take a fresh snapshot, drag
    `test_ba_canvasA`'s card body (NOT its `.conn-handle`) by ~100px.
    **Expected:** the card visibly moves and stays where dropped (pinned);
    `localStorage.getItem(crewQaKeys.pos)` now has an entry for `test_ba_canvasA`.

17. **Drag-connect still opens the modal:** drag from `test_ba_canvasA`'s
    `.conn-handle` onto `test_ba_canvasB`'s card.
    **Expected:** the "Describe the relationship" modal opens with the pair
    header `test_ba_canvasA → test_ba_canvasB`.

    **Create the regression edge** while that modal remains open: enter
    `canvas regression` as the label and `when canvas regression runs` as the
    first condition, leave reply/two-way unchecked, then click Connect.
    **Expected:** one directed edge between the two owned fixtures renders.

18. **Edge label click still opens edit modal:** click the created edge's
    on-canvas label.
    **Expected:** the "Edit relationship" modal opens for that edge.
    Close it.

19. **Zoom-to-fit button still works:** click `#zoomFit`.
    **Expected:** `#zoomPct` changes to frame both nodes (no JS error in
    console — check the browser tool's console log).

20. **Ctrl/Cmd+0 reset still works:** focus the page body (click empty
    canvas first to ensure focus isn't in a text field), dispatch a keydown
    for `0` with `ctrlKey: true` (or `metaKey: true` on mac).
    **Expected:** the view re-fits (same effect as `#zoomFit` — zoomToFit is
    what Ctrl/Cmd+0 calls).

21. **Ctrl/Cmd +/- still work:** dispatch keydown `=` with `ctrlKey:true`,
    confirm `#zoomPct` increases; dispatch keydown `-` with `ctrlKey:true`,
    confirm it decreases.

## Cleanup (always run, even if a step above failed)

1. Run `crew_qa_cleanup_agent test_ba_canvasA` (cascades the owned edge),
   then `crew_qa_cleanup_agent test_ba_canvasB`:
   ```sh
   crew_qa_cleanup_agent test_ba_canvasA
   crew_qa_cleanup_agent test_ba_canvasB
   ```
   Exact `_guid`, stored-session,
   and home receipts are required; a mismatch aborts without a direct tmux
   fallback.
2. Reset the view: run in the page
   `localStorage.setItem(crewQaKeys.view, JSON.stringify({zoom:1,panX:0,panY:0}))`
   and reload, so the shared dashboard isn't left zoomed oddly for the next
   person.
3. Confirm via the API that both agents are gone:
   ```sh
   curl -fsS -b "$CREW_DASH_COOKIE" "$CREW_DASH_URL/api/graph/snapshot" | python3 -c 'import json,sys; d=json.load(sys.stdin); names={a["name"] for a in d["agents"]}; print("agents left:",[n for n in names if n.startswith("test_ba_canvas")])'
   ```
   **Expected:** empty list (`[]`).
4. Remove the temporary cookie jar and empty receipt directory:
   `rm -f "$CREW_DASH_COOKIE"; rmdir "$CREW_QA_STATE"`.
