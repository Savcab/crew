# Public webhook ingress verification evidence

## Tested commit

`19c051ed9979442a03cb79057f5a9a4c905a67ac`

Content digest:
`c1e3ec5ebb920a27a9c47dca9777c62571a3f1dbf4ec1793d61127f914b38580`

The digest binds this evidence-only amendment to every declared implementation
and test blob in the clean candidate.

## Commands and results

The complete backend suite ran against an explicitly local MorphDB fixture. Its
process was detached from the evidence recorder's controlling TTY because Crew
intentionally distinguishes operator terminals from non-interactive automation.

```text
$ git rev-parse HEAD
19c051ed9979442a03cb79057f5a9a4c905a67ac

$ MORPHDB_HOST=127.0.0.1:18787 CREW_PORT=18790 \
    python3 -m unittest discover -s tests -q
Ran 1116 tests in 223.385s

OK (skipped=13)

$ cd frontend && npm test -- --run
Test Files  5 passed (5)
Tests       21 passed (21)

$ npm run build
✓ 516 modules transformed
✓ built in 130ms
```

The opt-in test used the installed official `cloudflared` binary, a private
local MorphDB process, one throwaway app, and a real Cloudflare Quick Tunnel.
It kept the temporary hostname and every webhook capability in process memory.

```text
$ CREW_RUN_PUBLIC_INGRESS_LIVE=1 \
    python3 -W error tests/test_public_ingress_live.py -v
public_origin_valid_trycloudflare=true
cli_surface=status_online show_public_url_true capability_redacted
initial_delivery=status_202 accepted_2 rejected_0 durable_messages_2
idempotent_replay=duplicate_true same_receipt_true durable_messages_2
rotation=old_404 new_202 durable_messages_4
control_surface=four_generic_404s
shutdown=state_removed_true endpoint_accepts_false
Ran 1 test in 38.576s

OK
RESULT: PASS · public HTTPS → durable fan-out → clean shutdown

$ python3 scripts/validate_feature_docs.py
feature docs valid: 3 dossier(s), template present
```

The full suite also includes the process-level watchdog proof: hard owner death
closes the lifetime pipe, terminates the exact tunnel child, and cannot retarget
a later run's fresh Unix socket. Independent code and test/spec audits found no
remaining P0–P2 blockers.

## Media evidence

- [Terminal verification](evidence/terminal-verification.png) is the primary
  full-suite proof. It shows the exact candidate SHA, commands, 1,116-test
  result, all 21 frontend tests, production build, and clean status.
- [Real public ingress E2E](evidence/public-ingress-e2e.mp4) is a 13.2-second,
  1600×900 H.264 recording with no audio. It replays at 4× the exact timestamped
  pseudo-TTY byte stream from the real 38.576-second Internet test.
- The repository-owned [HTML explainer](explainer.html) renders both files next
  to the architecture diagram. It is committed here and is not published to a
  separate artifact host.

Native Terminal automation was unavailable under the existing macOS
permissions. The recorder therefore captured the real command stream with
macOS `script(1)` and replayed those exact bytes in a browser terminal for
portable PNG/MP4 evidence. The outputs were not reconstructed; local filesystem
paths were redacted during capture.

## Safety and redaction

Verification used only a uniquely named throwaway app, synthetic agents,
synthetic messages, and a temporary Quick Tunnel. The live test deleted that
app, stopped its exact owned processes, removed active ingress state, and
confirmed the public endpoint no longer accepted the hook.

No customer data, webhook capability, full hook URL, temporary public hostname,
provider/request identifier, device hostname, personal filesystem path, or
credential appears in the committed HTML, screenshot, or video.
