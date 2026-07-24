# Webhook nodes verification evidence

## Tested commit

`fdd4deb7847f81ce131915b92ce6a325a10b7906`

Content digest:
`258e7e894e79c53bdf7df291c2aa8996c4882d2291122872ee7546544b86f231`

The digest binds this evidence-only amendment to the declared implementation
and test blobs in the tested candidate, even though the commit was later
amended to add this evidence.

## Commands and results

The full suite ran against the explicitly local MorphDB. The suite process was
detached from the evidence Terminal's controlling TTY because Crew
intentionally ignores `CREW_AGENT` compatibility hints in a real operator
terminal, while six live identity tests deliberately exercise the
non-interactive automation path.

```text
$ git rev-parse HEAD
fdd4deb7847f81ce131915b92ce6a325a10b7906

$ MORPHDB_HOST=127.0.0.1:18787 CREW_PORT=18790 \
    python3 -m unittest discover tests
Ran 1027 tests in 207.131s

OK (skipped=12)

$ python3 scripts/validate_feature_docs.py
Feature docs valid: 1 dossier(s), template present
```

The frontend contracts and production bundle also passed:

```text
$ cd frontend && npm test -- --run
Test Files  5 passed (5)
Tests       21 passed (21)

$ npm run build
production build passed
```

The real isolated-dashboard flow produced these sanitized assertions. The
script read the capability URLs from owner-only files; it never printed them.

```text
$ live webhook E2E (capability URL redacted)
initial POST: 202, accepted=1, rejected=0
same-key retry: duplicate=true across Content-Type change
durable fanout: exact body + sender/target/edge/request provenance matched
rotation: revoked URL=404; replacement URL=202
replacement retry: duplicate=true across Content-Type change
replacement durable fanout: exact body + provenance matched
RESULT: PASS — real dashboard graph, HTTP ingress, durable delivery, replay, rotation
```

An independent exact-SHA review found no P0–P2 issues. Focused webhook, mail,
graphstore, transform, dashboard security/process/API, CLI identity, graph
identity, containment, feature-doc, browser-contract, and docs-contract suites
also passed before the full run.

## Media evidence

- [Terminal verification](evidence/terminal-verification.png) is the primary
  proof. It shows the exact candidate SHA, actual full-suite command and result,
  and the decisive live HTTP, persistence, retry, and rotation assertions.
- [Real browser flow](evidence/browser-flow.mp4) shows the actual Crew UI
  creating a stopped target agent, creating the hook, drawing/configuring its
  directed route, and observing live graph delivery activity. It is supporting
  interaction evidence; the chapter annotation is not treated as a persisted
  message assertion.
- [Final graph topology](evidence/graph-delivery.png) shows the real
  source-only hook and stopped target agent joined by the configured edge.
- The self-contained, published
  [visual explainer](https://tools.ziphq.net/zip-dev-artifacts/felix.chen@ziphq.com/reports/crew-webhook-nodes-fdd4deb.html)
  embeds the same repository-owned media.

## Safety and redaction

The evidence used only the isolated MorphDB app
`crew-feature-hook-evidence-fdd4deb`, hook `test_fdd_hook_source`, agent
`test_fdd_hook_target`, and `/tmp/crew_tests/test_fdd_hook_target`. No customer
data or production app was used.

Webhook and dashboard capabilities were stored only in owner-readable files
and omitted from commands, logs shown in media, screenshots, video, committed
files, and the published artifact. A dashboard auth capability that appeared
during setup was invalidated immediately by restarting the isolated dashboard;
the replacement remained redacted.

After capture, the browser session and dashboard were stopped, the exact
isolated MorphDB app was deleted, and the standard test home was moved into the
task's recoverable temporary archive. Remaining bearer files are quarantined
in an owner-only temporary directory and are not part of the repository.
