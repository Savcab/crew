# Foreman-controlled webhook nodes verification evidence

## Tested commit

`71720cfd4080f08a35f633bd3811a1a4a2231b2d`

Content digest:
`ae02f66fa3c70365600fab00368e707189c8821a8eaf89e6647f7c48a0bb2832`

The digest binds this evidence-only amendment to every declared implementation
and test blob in the tested candidate.

## Commands and results

```text
$ git rev-parse HEAD
71720cfd4080f08a35f633bd3811a1a4a2231b2d

$ MORPHDB_HOST=127.0.0.1:18787 CREW_PORT=18790 \
    python3 -m unittest discover tests
Ran 1047 tests in 217.123s

OK (skipped=12)

$ cd frontend && npm test -- --run
Test Files  5 passed (5)
Tests       21 passed (21)

$ npm run build
production build passed (516 modules transformed)

$ MORPHDB_HOST=127.0.0.1:18787 \
    python3 tests/test_foreman_webhooks.py -v
test_cli_list_pages_past_one_thousand_indexed_webhooks ... ok
test_owned_hook_envelope_ignores_saturated_canvas_page ... ok
Ran 16 tests

OK

$ python3 scripts/validate_feature_docs.py
feature docs valid: 2 dossier(s), template present
```

The real isolated Foreman flow used detached `bin/crew` subprocesses with
`CREW_AGENT` resolving to a live registered Foreman. Captured output redacted
every URL and capability:

```text
$ MORPHDB_HOST=127.0.0.1:18787 python3 foreman_demo.py
create: real detached bin/crew returned POST [REDACTED]
ownership: immutable Foreman GUID recorded; hook starts unblessed
list: owner visible; capability token/hash/URL absent
show: owned URL + template returned through guarded read [REDACTED]
route: source-only hook → owned agent with finite caps
delivery: accepted=1; retry reused one durable receipt/message
provenance: exact body + hook/edge/target GUIDs matched
rotation: old capability revoked before replacement returned [REDACTED]
plain agent: guarded show refused with no secret disclosure
owner lifecycle: revoke/delete denied management; hook stayed live
name reuse: replacement GUID could not inherit the hook
audit: applied/refused decisions present; secrets recursively absent
RESULT: PASS — real Foreman CLI, durable delivery, rotation, orphan safety
```

The focused suite additionally proved a spawned-process race cannot claim the
last quota slot twice, all read/update/rotate/remove wait races revalidate the
immutable actor at the mutation lock, a 1,000-row graph page cannot hide owned
hooks from the exact indexed count, fieldless updates cannot become unaudited
secret reads, and nested audit mapping keys/cycles/hostile objects cannot leak
secrets. It also proves Foreman-owned hooks remain connectable behind a
saturated 1,000-row canvas page and that indexed webhook listing exhausts a
second page.

## Media evidence

- [Terminal verification](evidence/terminal-verification.png) is the primary
  proof. It shows the exact candidate SHA, actual full-suite and frontend
  results, and the decisive real CLI assertions.
- [Foreman CLI video](evidence/foreman-cli-demo.mp4) records the same native
  Terminal flow from the exact candidate. It is H.264, 1920×980, 20.4 seconds,
  and contains no audio.
- The self-contained
  [published explainer](https://tools.ziphq.net/zip-dev-artifacts/felix.chen@ziphq.com/reports/crew-foreman-webhook-control-71720cf.html)
  embeds these repository-owned artifacts.

## Safety and redaction

Verification used only the throwaway MorphDB app
`crew-feature-foreman-evidence-71720cf` and synthetic `evidence_*` graph
fixtures. The script captured secret-bearing CLI output in memory, printed only
sanitized assertions, and deleted that exact app in `finally`.

No customer data, dashboard capability, webhook token, token hash, template
secret, or public URL appears in the committed media, logs shown in the media,
or published artifact. The native Terminal window contains only the tested
commit, commands, aggregate results, and sanitized assertions.
