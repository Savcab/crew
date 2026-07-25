# Browser script: webhook node → durable agent message

Execute with browser automation against the isolated QA dashboard at
**http://127.0.0.1:18788**. This procedure creates one stopped agent, one hook,
and one directed route; it never starts an agent runtime.

## Safety

- Only touch agent `test_ba_hook_target`, hook `test_ba_hook_source`, and the
  route between them.
- Keep the hook capability in owner-only temporary files. Never print it or
  include the URL field in screenshots or video.
- Cleanup is mandatory, including after a failed assertion.

## Portable preflight

Run from a terminal inside this checkout:

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
chmod 700 "$CREW_QA_STATE"
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
crew_qa_assert_hook_unused() {
  crew_qa_snapshot | python3 -c 'import json,sys; n=sys.argv[1]; d=json.load(sys.stdin); assert not [r for r in d["agents"]+d["webhooks"] if r.get("name")==n], f"existing node {n}; aborting"' "$1"
}
crew_qa_capture_hook() {
  local name="$1" receipt="$CREW_QA_STATE/$1.owner.json" secret="$CREW_QA_STATE/$1.url.secret"
  crew_qa_snapshot | python3 -c 'import json,sys,urllib.parse; n,app,base,receipt,secret=sys.argv[1:]; d=json.load(sys.stdin); rows=[h for h in d["webhooks"] if h.get("name")==n]; assert len(rows)==1; h=rows[0]; assert h.get("_guid"); u=h.get("public_url",""); u=base+u if u.startswith("/") else u; p=urllib.parse.urlparse(u); assert p.scheme=="http" and p.netloc=="127.0.0.1:18788" and p.path.startswith("/hooks/"); json.dump({"name":n,"guid":h["_guid"],"app":app},open(receipt,"x",encoding="utf-8")); open(secret,"x",encoding="utf-8").write(u)' "$name" "$CREW_APP" "$CREW_DASH_URL" "$receipt" "$secret"
  chmod 600 "$receipt" "$secret"
}
crew_qa_assert_owned_hook() {
  local receipt="$CREW_QA_STATE/$1.owner.json"
  test -s "$receipt" || { echo "missing hook ownership receipt" >&2; return 2; }
  crew_qa_snapshot | python3 -c 'import json,sys; r=json.load(open(sys.argv[1])); d=json.load(sys.stdin); assert d.get("workspace_key")==r["app"]; rows=[h for h in d["webhooks"] if h.get("name")==r["name"]]; assert len(rows)==1 and rows[0].get("_guid")==r["guid"]; print(r["guid"])' "$receipt"
}
crew_qa_cleanup_hook() {
  local name="$1" receipt="$CREW_QA_STATE/$1.owner.json" guid
  guid="$(crew_qa_assert_owned_hook "$name")" || return 2
  python3 -c 'import json,sys; print(json.dumps({"guid":sys.argv[1]}))' "$guid" | curl -fsS -b "$CREW_DASH_COOKIE" -H 'Content-Type: application/json' -H 'X-Crew-CSRF: 1' --data-binary @- "$CREW_DASH_URL/api/webhook/delete" | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d.get("ok") is True, d' || return 2
  crew_qa_snapshot | python3 -c 'import json,sys; n=sys.argv[1]; d=json.load(sys.stdin); assert not [h for h in d["webhooks"] if h.get("name")==n]' "$name" || return 2
  rm -f "$receipt" "$CREW_QA_STATE/$name.url.secret"
}
crew_qa_assert_unused test_ba_hook_target /tmp/crew_tests/test_ba_hook_target
crew_qa_assert_hook_unused test_ba_hook_source
```

Create the stopped target through the authenticated API, then capture its exact
record before browser mutations:

```sh
python3 -c 'import json; print(json.dumps({"name":"test_ba_hook_target","role":"triage incoming hooks","home":"/tmp/crew_tests/test_ba_hook_target","launch":False,"launch_cmd":"true"}))' \
  | curl -fsS -b "$CREW_DASH_COOKIE" -H 'Content-Type: application/json' -H 'X-Crew-CSRF: 1' --data-binary @- "$CREW_DASH_URL/api/agent/create" \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d.get("ok") is True, d'
crew_qa_capture_agent test_ba_hook_target /tmp/crew_tests/test_ba_hook_target
```

## Browser steps

1. Navigate to `$CREW_DASH_URL/#cap=$CREW_DASH_CAP`. Confirm the fragment
   disappears, `#cgraph` loads, and `test_ba_hook_target` reads
   **runtime not started**.
2. Click `#addWebhookBtn`. Confirm the modal title is **Create hook**.
3. Enter:
   - `#w-name`: `test_ba_hook_source`
   - `#w-description`: `browser webhook evidence`
   - `#w-template`: `Issue {{ payload.issue.title }} / {{ headers.x-provider-event }}`
4. Click `#w-go`. Confirm toast **created hook test_ba_hook_source**, then wait
   for `.cnode.agent.webhook[data-sess="test_ba_hook_source"]` to show
   **listening for POSTs**.
5. In the preflight terminal run `crew_qa_capture_hook test_ba_hook_source`.
   Stop if it fails; the secret URL is now in an owner-only file and has not
   been printed.
6. Drag the hook card's `.conn-handle` onto
   `.cnode.agent[data-sess="test_ba_hook_target"]`. Confirm the modal title is
   **Route hook to agent** and the pair is
   `test_ba_hook_source → test_ba_hook_target`.
7. Enter `browser hook route` in `#e-label` and
   `triage the rendered issue` in `#e-does`, then click `#e-go`.
   Confirm toast **routed test_ba_hook_source → test_ba_hook_target** and a
   one-way edge appears.

## Invoke and prove durable delivery

The command below never echoes the capability:

```sh
CREW_QA_HOOK_URL="$(< "$CREW_QA_STATE/test_ba_hook_source.url.secret")"
curl -fsS -o "$CREW_QA_STATE/first-response.json" \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: browser-hook-delivery-1' \
  -H 'X-Provider-Event: issues.opened' \
  --data-binary '{"issue":{"title":"Queue retries"}}' \
  "$CREW_QA_HOOK_URL"
python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); assert d["ok"] is True and d["accepted"]==1 and d["rejected"]==0 and len(d["deliveries"])==1, d; print("public POST: accepted=1 rejected=0")' "$CREW_QA_STATE/first-response.json"
CREW_APP="$CREW_APP" python3 - "$CREW_QA_STATE/first-response.json" <<'PY'
import json
import sys
from crew import graphstore as gs
response = json.load(open(sys.argv[1], encoding="utf-8"))
message = gs.get_object(response["deliveries"][0]["message_guid"])
assert message["sender"] == "test_ba_hook_source", message
assert message["target"] == "test_ba_hook_target", message
assert message["body"] == "Issue Queue retries / issues.opened", message
assert message.get("edge_guid") and message.get("sender_guid"), message
print("durable message: body matched; edge and sender provenance present")
PY
```

8. Return to the browser and wait for the next graph poll. Hover the route
   cable or its label. Confirm its tooltip includes:
   `last message just now`, `test_ba_hook_source → test_ba_hook_target`, and
   `Issue Queue retries / issues.opened`.
9. Capture the graph with the hook, stopped target, one-way route, and latest
   message tooltip. Do not open the hook modal during evidence capture because
   its URL field is a secret.

## Rotate and prove revocation

10. Click the hook card, click `#w-rotate`, and accept the confirmation dialog.
    Confirm toast **hook URL rotated**, then close the modal before any capture.
11. Refresh the owner-only secret without printing either URL:

```sh
mv "$CREW_QA_STATE/test_ba_hook_source.url.secret" "$CREW_QA_STATE/old.url.secret"
crew_qa_snapshot | python3 -c 'import json,sys; n,base,destination=sys.argv[1:]; d=json.load(sys.stdin); h=next(h for h in d["webhooks"] if h.get("name")==n); u=h["public_url"]; open(destination,"x",encoding="utf-8").write(base+u if u.startswith("/") else u)' test_ba_hook_source "$CREW_DASH_URL" "$CREW_QA_STATE/test_ba_hook_source.url.secret"
chmod 600 "$CREW_QA_STATE/test_ba_hook_source.url.secret"
test "$(< "$CREW_QA_STATE/old.url.secret")" != "$(< "$CREW_QA_STATE/test_ba_hook_source.url.secret")"
old_status="$(curl -sS -o "$CREW_QA_STATE/old-response.json" -w '%{http_code}' -H 'Content-Type: application/json' --data-binary '{}' "$(< "$CREW_QA_STATE/old.url.secret")")"
test "$old_status" = "404"
new_status="$(curl -sS -o "$CREW_QA_STATE/new-response.json" -w '%{http_code}' -H 'Content-Type: application/json' -H 'Idempotency-Key: browser-hook-delivery-2' -H 'X-Provider-Event: rotation.test' --data-binary '{"issue":{"title":"Rotated URL works"}}' "$(< "$CREW_QA_STATE/test_ba_hook_source.url.secret")")"
test "$new_status" = "202"
python3 -c 'import json,sys; old,new=map(json.load,map(open,sys.argv[1:])); assert old=={"ok":False,"error":"webhook not found"}, old; assert new["accepted"]==1 and new["rejected"]==0, new; print("rotation: old=404 new=202")' "$CREW_QA_STATE/old-response.json" "$CREW_QA_STATE/new-response.json"
```

## Cleanup

Always run:

```sh
crew_qa_cleanup_hook test_ba_hook_source
crew_qa_cleanup_agent test_ba_hook_target
rm -f "$CREW_DASH_COOKIE" "$CREW_QA_STATE/old.url.secret" \
  "$CREW_QA_STATE/first-response.json" "$CREW_QA_STATE/old-response.json" \
  "$CREW_QA_STATE/new-response.json"
rmdir "$CREW_QA_STATE"
```

Confirm the graph snapshot contains neither fixture and no edge mentioning
either name.
