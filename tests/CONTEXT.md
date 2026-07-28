# tests — context

## What this area does
The whole regression surface for Crew. It holds stdlib `unittest` behavior
tests, tests that write through a real MorphDB app, live CLI/tmux end-to-end
tests, contract tests that keep README/TEST_PLAN/dossiers honest, and markdown
browser procedures that a human or a computer-use agent executes against a
running dashboard. `TEST_PLAN.md` is the exhaustive behavior matrix and the
isolation rulebook; the README "Tests" section has the exact run commands.

## Key files
- `operator_harness.py` — strips inherited Crew pane/tenant env so a CLI
  subprocess behaves like an operator shell; used by every test that shells out.
- `live_smoke.py` — rerunnable live write-path check against the real crew app;
  deliberately not named test*.py, so discovery skips it. Run it by hand.
- `browser/` — one markdown procedure per dashboard workflow, executed with
  browser automation rather than by the Python suite.
- `fixtures/expand_stub.sh` — fake claude -p expander for the expansion tests;
  EXPAND_STUB_MODE picks ok / fail / timeout / badjson.
- `test_context_docs.py` — the area registry and the mechanical half of the
  CONTEXT.md contract (see AGENTS.md, "Context docs").
- `test_browser_contracts.py` — asserts the mutating browser procedures keep
  their port pin, ownership receipt, and cleanup steps.
- `test_docs_contract.py` — keeps README, TEST_PLAN.md, and the shipped browser
  inventory in agreement.
- `test_feature_docs.py` — validates the feature dossiers under docs/features/.

## Invariants and gotchas
- There is no `tests/__init__.py`, no conftest, and no pytest. Discovery
  (`python3 -m unittest discover tests`) puts this directory on `sys.path`,
  which is the only reason `from operator_harness import ...` resolves. Running
  a single module as `python3 -m unittest tests.test_foreman` from the repo root
  breaks that import — cd into `tests/` first, or use `discover -k`.
- Each module does its own `sys.path.insert(0, ROOT)` to import `crew`. Tests
  are stdlib-only; adding a third-party test dependency breaks the clean-room
  install promise in TEST_PLAN.md.
- The discover suite expects the QA MorphDB at `127.0.0.1:18787` with the
  dashboard on `CREW_PORT=18790`. Some modules hard-pin `18787` internally, so a
  run pointed at another host tests a backend it did not set up instead of
  failing. Browser procedures pin `18788` instead — `test_browser_contracts.py`
  enforces that, and the two port sets must not be merged.
- Live gating is deliberately not uniform. `CREW_LIVE_TESTS` defaults to `"1"`
  (live CLI/tmux tests run unless you set it to `0`), while
  `CREW_RUN_HARNESS_LIVE` and `CREW_RUN_PUBLIC_INGRESS_LIVE` default to off and
  must be set to `1`. A "passing" run may have skipped whole live classes.
- App isolation has two shapes: per-process names like `crewtest-<area>-<pid>`,
  and fixed `crewtest-<area>-unit` names whose `setUpModule` DELETEs the whole
  app and re-runs `ensure_schema`. That delete is destructive by design and safe
  only because the name is namespaced — nothing here may target the default
  `crew` app except `live_smoke.py`, which writes to it on purpose to catch
  schema drift.
- Modules that pin `CREW_APP` must do it in `setUpModule`, not at import time.
  Discovery imports every module before running any of them, so an import-time
  pin leaks into whichever module runs next (see the comment in
  `test_guard.py`). The same applies to any config value a module mutates: pair
  it with a `tearDownModule` restore.
- Fixtures that touch tmux or real homes carry an ownership receipt and
  revalidate it before cleanup. Never kill a same-named tmux session on the name
  alone; TEST_PLAN.md "Safety and test isolation" is the full rule set.
- A few live-touching tests fail from machine state rather than from your diff
  (a foreman flag already held in the real crew app, an ingress tunnel running
  while webhook URL tests patch static config). Re-run against a stashed tree
  before treating a red test as a regression.

## When to update this file
- A new shared helper, fixture, or env gate appears under `tests/` — anything a
  second module imports or that switches a whole class of tests on or off.
- The way the suite is run or isolated changes: discovery layout, the pinned QA
  ports and app-name scheme, or the live gating defaults.
