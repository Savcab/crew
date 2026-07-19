# Browser script: terminal dock transport, focus, resize, and cleanup

Execute against the isolated QA dashboard at **http://127.0.0.1:18788**. This
procedure is self-contained: it authenticates, provisions one running custom
shell and one owned down-session fixture, records exact ownership receipts,
exercises the dock, and removes only those verified fixtures in `finally`.

## Safety and independently executable preflight

Run this block in a terminal inside the Crew checkout. The fixed fixture names
are permitted only because any existing row, exact default-project tmux
session, or home causes an abort. Never reclaim an interrupted fixture by name.

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
crew_qa_assert_unused test_ba_terminal_up /tmp/crew_tests/test_ba_terminal_up
crew_qa_assert_unused test_ba_terminal_down /tmp/crew_tests/test_ba_terminal_down
export CREW_QA_VIEW_BASELINE="$(tmux list-sessions -F '#{session_name}' 2>/dev/null | LC_ALL=C sort | grep '^_ngview_' || true)"
```

## Owned fixture setup and API assertions

1. Create only the two custom-runtime fixtures through the authenticated API:

   ```sh
   curl -fsS -b "$CREW_DASH_COOKIE" -H 'Content-Type: application/json' -H 'X-Crew-CSRF: 1' \
     --data-binary '{"name":"test_ba_terminal_up","role":"PTY running fixture","home":"/tmp/crew_tests/test_ba_terminal_up","runtime":"custom","launch_cmd":"exec sh","launch":true}' \
     "$CREW_DASH_URL/api/agent/create" | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d.get("ok") is True, d'
   curl -fsS -b "$CREW_DASH_COOKIE" -H 'Content-Type: application/json' -H 'X-Crew-CSRF: 1' \
     --data-binary '{"name":"test_ba_terminal_down","role":"PTY down fixture","home":"/tmp/crew_tests/test_ba_terminal_down","runtime":"custom","launch_cmd":"exec sh","launch":false}' \
     "$CREW_DASH_URL/api/agent/create" | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d.get("ok") is True, d'
   crew_qa_capture_agent test_ba_terminal_up /tmp/crew_tests/test_ba_terminal_up
   crew_qa_capture_agent test_ba_terminal_down /tmp/crew_tests/test_ba_terminal_down
   ```

   **Expected:** both receipts contain the newly created exact `_guid`, stored
   session, canonical home, and app. The running fixture reaches
   `runtime_alive == true`; the no-launch fixture is initially
   `session_alive == true`, `runtime_alive == false`, `live_status ==
   "not_started"`.

2. Turn only the owned no-launch fixture into the down-session case. Recheck
   its receipt plus tmux ownership immediately before the exact-target kill:

   ```sh
   CREW_QA_SESSION="$(crew_qa_assert_owned_agent test_ba_terminal_down)"
   test "$CREW_QA_SESSION" = "test_ba_terminal_down"
   test "$(tmux show-environment -t "=$CREW_QA_SESSION" CREW_PROJECT)" = "CREW_PROJECT=default"
   test "$(tmux show-environment -t "=$CREW_QA_SESSION" CREW_AGENT)" = "CREW_AGENT=test_ba_terminal_down"
   test "$(tmux show-environment -t "=$CREW_QA_SESSION" CREW_APP)" = "CREW_APP=$CREW_APP"
   test "$(tmux show-environment -t "=$CREW_QA_SESSION" MORPHDB_HOST)" = "MORPHDB_HOST=$MORPHDB_HOST"
   tmux kill-session -t "=$CREW_QA_SESSION"
   crew_qa_snapshot | python3 -c 'import json,sys; d=json.load(sys.stdin); by={a["name"]:a for a in d["agents"]}; up=by["test_ba_terminal_up"]; down=by["test_ba_terminal_down"]; assert up["session_alive"] and up["runtime_alive"]; assert not down["session_alive"] and down["live_status"]=="down"'
   ```

3. Navigate to `$CREW_DASH_URL/#cap=$CREW_DASH_CAP`, wait for both exact cards,
   select **refresh off**, and clear the browser tool's console and network
   logs. Do not interact with any other graph card.

## Down-session behavior

4. Keyboard-activate `test_ba_terminal_down`'s graph card.
   **Expected:** the dock opens, names that exact agent, shows **start runtime**,
   and does not issue a repeating `/api/pty/stream` request/404 loop.
5. Press `Tab` through the dock controls.
   **Expected:** identity, start, previous, next, maximize, close, and the resize
   separator all expose meaningful accessible names/roles.
6. Focus the resize separator and press `ArrowUp`, then `ArrowDown`.
   **Expected:** the dock grows and shrinks respectively; no console error occurs.
7. Close the dock.
   **Expected:** focus returns to the exact graph card that opened it.

## Running terminal behavior

8. Keyboard-activate `test_ba_terminal_up` and click inside its terminal.
   **Expected:** the live-keyboard affordance appears and terminal input has focus.
9. Type `printf 'crew-pty-utf8: héllo 世界\n'` and press Enter.
   **Expected:** the exact UTF-8 output line renders once, in order, with a real
   line ending and no missing or rearranged characters.
10. Rapidly maximize/restore the dock and drag its resize separator.
    **Expected:** the xterm grid refits, the prompt remains usable, and the newest
    size wins without oscillation or duplicate output.
11. Press `Ctrl+Esc`.
    **Expected:** terminal live focus detaches and focus lands on **close**;
    normal dashboard shortcuts work again.
12. Re-focus the terminal, then quickly cycle previous/next several times.
    **Expected:** only the newest agent's output appears; late bytes from an
    older stream never paint into the current terminal.
13. Close and reopen the dock five times, pausing at least one heartbeat after
    the final close.
    **Expected:** no duplicate listeners and no newly created `_ngview_*`
    session remains after the final close.
14. Capture normal/narrow dock screenshots and an accessibility snapshot.
    Confirm the browser tool's console has no errors and the network log has no
    repeating failed PTY stream/input/resize request.

## Finally — mandatory ownership-safe cleanup

Run this block even if a browser assertion fails. It intentionally refuses to
clean an agent whose current `_guid`, stored session, or home differs from its
receipt. There is no direct tmux or unmarked-home fallback.

```sh
crew_qa_cleanup_agent test_ba_terminal_down
crew_qa_cleanup_agent test_ba_terminal_up
crew_qa_snapshot | python3 -c 'import json,sys; d=json.load(sys.stdin); assert not [a for a in d["agents"] if a.get("name") in {"test_ba_terminal_up","test_ba_terminal_down"}]'
CREW_QA_VIEW_AFTER="$(tmux list-sessions -F '#{session_name}' 2>/dev/null | LC_ALL=C sort | grep '^_ngview_' || true)"
test "$CREW_QA_VIEW_AFTER" = "$CREW_QA_VIEW_BASELINE" || {
  echo "new grouped PTY view leaked; do not kill an unidentified view" >&2; exit 2;
}
rm -f "$CREW_DASH_COOKIE"
rmdir "$CREW_QA_STATE"
```

Restore refresh to 1.5s and reset viewport/zoom in the browser. **Expected:**
the API contains neither fixture, both exact base sessions are absent, the
preexisting `_ngview_*` baseline is unchanged, and no other tmux session was
touched.
