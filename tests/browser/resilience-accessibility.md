# Browser script: recovery, stale responses, errors, responsive layout, and accessibility

Run this against the isolated QA dashboard at `http://127.0.0.1:18788` only.
The invoking shell's `MORPHDB_HOST` and `CREW_APP` must identify the same
backend. Use the exact fixtures `test_ba_resilience_a` and
`test_ba_resilience_b`; launch neither model runtime.

## Preflight

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
crew_qa_assert_unused test_ba_resilience_a /tmp/crew_tests/test_ba_resilience_a
crew_qa_assert_unused test_ba_resilience_b /tmp/crew_tests/test_ba_resilience_b
```

Open `$CREW_DASH_URL/#cap=$CREW_DASH_CAP`, wait for the graph, select **refresh
off**, and clear browser console/network logs. Save the original fetch function:

```js
window.crewQaOriginalFetch = window.fetch.bind(window);
```

Restore it after every interception with
`window.fetch = window.crewQaOriginalFetch` and in cleanup even if a step fails.

## Malformed durable-row quarantine

Before exercising the ordinary UI, seed one deliberately incomplete row in
this isolated app only:

```sh
export CREW_QA_MALFORMED_GUID="test-ba-malformed-agent-$CREW_PORT"
python3 -c 'import os; from crew import graphstore as gs; gs._req("PATCH", "/objects/agent/" + os.environ["CREW_QA_MALFORMED_GUID"], {"can_edit_graph": False}, app=os.environ["CREW_APP"])'
```

1. Reload the authenticated dashboard and wait for one successful graph poll.
   **Expected:** the graph remains usable, every valid agent still appears, no
   blank/actionable agent card appears, and the browser console has no exception.
2. Inspect the authenticated snapshot:
   ```sh
   crew_qa_snapshot | python3 -c 'import json,os,sys; d=json.load(sys.stdin); assert d.get("ok") is True; assert os.environ["CREW_QA_MALFORMED_GUID"] not in {a.get("_guid") for a in d["agents"]}'
   ```
   **Expected:** the malformed GUID is quarantined from the operator/UI graph;
   sparse legacy rows with a valid GUID and name remain visible.

## Snapshot outage and unchanged-data recovery

1. Record the current agent/edge counts. Replace `window.fetch` with a wrapper
   that returns `new Response(JSON.stringify({ok:false,error:'forced QA outage'}),
   {status:503,headers:{'Content-Type':'application/json'}})` for exactly the
   next `/api/graph/snapshot`, delegating every other request.
2. Switch refresh to **1s**, wait for one request, then immediately switch it
   back **off**.
   **Expected:** the graph shows an accessible `backend unavailable` status
   containing `forced QA outage`; no uncaught exception occurs.
3. Restore fetch and switch to **1s** for one cycle, then **off**.
   **Expected:** the same unchanged backend snapshot repaints the graph and the
   original counts return. The error DOM must not remain stranded.

## Forced refresh behind a slow snapshot

4. Install a one-shot wrapper that starts a real snapshot request immediately
   but withholds its response behind `window.crewQaReleaseSnapshot`. Switch to
   **1s** until that request is captured, then **off**.
5. With the snapshot still withheld, create `test_ba_resilience_a` through the
   manual agent form, home `/tmp/crew_tests/test_ba_resilience_a`, runtime
   Claude, Launch unchecked.
   **Expected:** the real create POST returns success and the modal closes.
   Immediately run:
   ```sh
   crew_qa_capture_agent test_ba_resilience_a /tmp/crew_tests/test_ba_resilience_a
   ```
   Stop unless its exact `_guid` /
   stored-session receipt is captured.
6. Release the withheld pre-mutation snapshot and restore fetch.
   **Expected:** although refresh remains off, a second forced snapshot runs and
   the new node appears as **runtime not started**. A stale in-flight response
   may render briefly but cannot be the final state.

## Non-success mutation and invalid numeric data

7. Open **+ Agent** and fill a unique but deliberately unsaved second agent.
   Intercept exactly the next `/api/agent/create` with a JSON HTTP 500 response.
   Submit.
   **Expected:** an assertive error toast appears; the modal stays open with all
   fields intact and controls re-enabled; no success toast or node appears.
8. Restore fetch and close the modal. Create `test_ba_resilience_b` for real,
   Launch unchecked. Run:
   ```sh
   crew_qa_capture_agent test_ba_resilience_b /tmp/crew_tests/test_ba_resilience_b
   ```
   Stop unless its exact receipt is
   captured. Then drag-connect A to B.
9. In the edge form, enter `1.5` for the integer message cap and submit.
   **Expected:** backend validation is shown as an error, the modal remains open,
   and no edge is created. Repeat with `NaN`, `Infinity`, `-1`, and an empty
   explicitly edited cap. None may be coerced into `0`/unlimited. Clearing a
   field is incomplete input; enter the visible value `0` explicitly when the
   intended setting really is no cap.
10. Enter valid finite values (`max_turns=2`, `token_cap=1000`, `cost_cap=0.5`)
    and submit. **Expected:** the edge is created and exactly those values
    round-trip through the authenticated snapshot.

## Stale async modal responses

11. Intercept `/api/expand` with a deferred successful JSON response. Open
    **+ Agent**, enter prose, and click **Generate**. Before resolving it, close
    the modal and reopen a fresh Create-agent modal.
12. Resolve the old response with distinctive fields such as
    `name: "must_not_appear"`.
    **Expected:** the replacement modal remains blank/default; it does not crash,
    close, unlock incorrectly, or receive any old field.
13. Repeat with a deferred `/api/edge/update`: submit an edit, close the edge
    modal, open an identity or Create-agent modal, then resolve the old success.
    **Expected:** the replacement modal stays open and unchanged; a stale callback
    cannot close it or report the wrong operation as current.

## Responsive and accessibility pass

14. At desktop (1440×900), tablet (768×1024), and mobile (390×844), capture
    screenshots of the graph, Create-agent modal, edge modal, identity card,
    pending tray when available, and terminal dock. At each size assert:

    - no horizontal document overflow;
    - modal content scrolls without hiding its primary action;
    - dock controls wrap/fit and the terminal remains visible;
    - graph zoom controls remain reachable;
    - text is not clipped at 200% browser zoom.

15. Repeat the core flow keyboard-only: reach **+ Agent**, open manual fields,
    traverse every control, close with Escape, keyboard-activate both graph
    cards and their edge, open/close identity, and resize/close the dock.
    **Expected:** visible focus, logical order, trapped modal focus, and focus
    restoration to the opener.
16. Inspect the accessibility tree. **Expected:** one named dialog while open;
    every icon button has a name; cards/edges are operable; loading and success
    use polite status; failures use alert; the resize separator exposes value and
    orientation.
17. Emulate reduced motion and repeat open/close/zoom. **Expected:** operations
    remain functional without a transition-dependent delay.
18. Review console and network logs. **Expected:** no uncaught errors, CSP/mixed
    content failures, repeating PTY failures, unexplained 4xx/5xx, or overlapping
    snapshot backlog. Save screenshots and the final accessibility snapshot.

## Cleanup

```sh
crew_qa_cleanup_agent test_ba_resilience_a
crew_qa_cleanup_agent test_ba_resilience_b
python3 -c 'import os; from crew import graphstore as gs; gs._req("DELETE", "/objects/agent/" + os.environ["CREW_QA_MALFORMED_GUID"], app=os.environ["CREW_APP"])'
curl -fsS -b "$CREW_DASH_COOKIE" "$CREW_DASH_URL/api/graph/snapshot" \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); assert not [a for a in d["agents"] if a["name"].startswith("test_ba_resilience_")]'
rm -f "$CREW_DASH_COOKIE"
rmdir "$CREW_QA_STATE"
```

Each cleanup call first revalidates the live `_guid`, stored session, and
canonical home against its ownership receipt. It aborts on any mismatch; there
is no blind tmux or filesystem fallback.

In the browser, always restore `window.fetch = window.crewQaOriginalFetch`,
reset viewport/zoom/reduced-motion emulation, clear fixture-specific localStorage,
and confirm refresh is back at 1.5s.
