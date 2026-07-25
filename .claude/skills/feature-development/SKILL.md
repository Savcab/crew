---
name: feature-development
description: Use when implementing any feature, bugfix, wave, or refactor in the crew project — before writing any implementation code, and again before claiming the work is done.
---

# Feature Development (crew)

## Overview

No feature is "implemented" until tests you wrote BEFORE implementing prove it against the LIVE system. Passing on a throwaway fixture is not passing.

**Violating the letter of this process is violating the spirit of it.**

## The process

1. **State the contract.** One paragraph: what user-visible behavior changes. If unclear, ask Felix first.
2. **Create the feature dossier.** For user-visible work, run
   `python3 scripts/new_feature.py <feature-id> --title "..." --summary "..."`
   before implementation. Fill in goals, non-goals, user flow, architecture,
   public surface, security boundaries, and planned verification. Bug fixes that
   only restore an already-documented contract may update the existing dossier.
3. **Write the test list BEFORE implementing.** Enumerate every check that must pass for the feature to count as done, across all three layers:
   - **Unit/behavior**: `tests/test_*.py` (unittest, stdlib). In-process fakes fine for logic.
   - **Live integration**: exercise the real path — real MorphDB app `crew` (schema pushed!), real CLI (`./bin/crew …`), real dashboard endpoint via curl. Every WRITE path the feature touches gets one live write.
   - **Browser (when UI is involved)**: a plain-text test script in `tests/browser/<feature>.md` — numbered steps + expected outcomes — executed with browser tools (agent-browser/playwright) against http://127.0.0.1:8788.
4. **Implement.**
5. **Capture primary evidence.** Record screenshots or video of the actual tested
   candidate commit, commands or browser actions, and decisive results. Never
   use a staged mockup as live proof. Redact capabilities, credentials, customer
   data, and private transcripts.
6. **Finish the dossier.** Keep `README.md`, `spec.md`, `evidence.md`,
   `explainer.html`, and `feature.json` aligned with the implementation. Store
   primary media under the dossier, include its SHA-256 in `feature.json`, and
   set status to `verified` only after the evidence exists. For a feature pull
   request, keep everything in one commit: set the delivery commit to `self`,
   record the tested candidate SHA, compute `verification.content_sha256` with
   `python3 scripts/validate_feature_docs.py --print-content-digest
   <feature-id>`, and amend the evidence into the candidate commit. Do not change
   declared code or test files during that amend. Declare every non-dossier path
   changed by the feature commit in `code_paths` or `test_paths`.
7. **Run everything**: the new tests, the FULL existing suite
   (`python3 -m unittest discover tests`), the browser scripts, and
   `python3 scripts/validate_feature_docs.py`. Regressions are your bug
   regardless of who wrote the broken code.
8. **Done = all green and documented.** Do not report done, do not end the task,
   with any test failing, unrun, or missing its dossier proof. If blocked, say
   "NOT done, blocked on X" — never soften it.

## Schema-drift rule (learned the hard way)

Any change touching `schema.py` fields must include a live write-path check against the running MorphDB app — on 2026-07-18 edge-creation was broken for a full day because tests pushed schema onto throwaway apps while the live app's schema was stale, and all smoke checks were read-only.

## Rationalization table

| Excuse | Reality |
|---|---|
| "29 tests pass" | On a fixture. The live app had drifted schema. Run one live write. |
| "Read-only smoke (status/edges/mail) worked" | Read paths don't exercise creates/updates. Smoke the write. |
| "It compiles / verifier approved" | Today's bug compiled and passed review. Only executed tests count. |
| "Browser test is overkill for a small UI change" | The broken Connect button was a "small UI flow". One script, two minutes. |
| "I'll add tests after it works" | Tests-after test what it does, not what it should do. List first. |
| "Existing suite is someone else's concern" | A regression you didn't run is a regression you shipped. |

## Red flags — STOP, go back to step 2

- Implementation diff exists but no test list was written
- About to say "done"/"shipped"/"verified" with an unrun layer
- New schema field + no live write in the checks
- UI change + no `tests/browser/` script
- User-visible change + no `docs/features/<feature-id>/feature.json`
- Verified dossier + no real screenshot/video showing the tested revision
- "The subagent already verified it"

## Layout

```
tests/test_<area>.py        unit/behavior (unittest, stdlib only)
tests/browser/<feature>.md  plain-text browser scripts (numbered steps + expected)
tests/live_smoke.py         live write-path checks, safe to rerun (cleans up after itself)
docs/features/<feature-id>/ durable spec, explainer, and verification evidence
```

## Live-fixture hygiene (learned 2026-07-18: 72 leaked agents polluted the real app)

- Prefer a throwaway MorphDB app (`crewtest-<area>`, deleted via `DELETE /app/<key>` in tearDown) over the real `crew` app whenever the test doesn't need the live install.
- Fixtures that MUST live in the real app: name them `test_<area>_…` with YOUR area's sub-prefix, and clean up ONLY your own sub-prefix — a bare `test_*` sweep deletes concurrent tests' fixtures mid-run (this race happened).
- Cleanup runs in `finally`/tearDown so a crash can't leak; never rely on "the next run will sweep it".
