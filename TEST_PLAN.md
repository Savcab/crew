# Crew product contract and exhaustive test plan

Date: 2026-07-19

## User-visible contract

Crew is a local runtime for persistent coding-agent teams. An operator can create
Claude Code or Codex CLI agents, each in an isolated home directory or an explicit
Git worktree, and Crew gives each agent a dedicated tmux session, durable native
identity, and access to the `crew` CLI. The operator connects agents in a directed
graph, starts work with a kickoff, watches real terminals in the dashboard, and
inspects durable mail and audit history. Agents send their own messages when an
edge's natural-language condition applies. Crew does not execute a DAG or evaluate
conditions automatically. It enforces the graph direction, configured rate and
budget caps, transforms, containment rules, and human approval boundaries. A
foreman may modify only its governed envelope through the CLI; every applied,
pending, approved, rejected, or refused edit is observable.

## Completion rule

The product is ready only when all applicable layers are green:

1. Python unit/behavior tests.
2. Clean-room integration tests against an isolated MorphDB app.
3. At least one safe write through every changed path against the live `crew` app
   when schema or live-stack behavior changes.
4. Real CLI/tmux end-to-end tests, including one Claude Code agent and one Codex
   CLI agent.
5. Every browser script executed against the real dashboard with computer use,
   plus the missing terminal, responsive, accessibility, error, and runtime flows.
6. Full regression suite passes with no leaked agent records, tmux sessions,
   worktrees, homes, browser views, or test apps.
7. README, CLI help, dashboard copy, public explainer copy, and test instructions
   match the verified behavior.

No passing fixture substitutes for a failed live path. No known browser gap is
accepted as success. When the desired behavior cannot be provided safely, the UI
and documentation must say so plainly and the limitation must have a regression
test.

## Safety and test isolation

- Preserve the unrelated existing tmux sessions `manager` and `worker1` through
  `worker8`; Crew must never attach to, resize, message, or kill them.
- Prefer a per-run prefix such as `test_overnight_20260719_`; fixed-name fixtures
  are allowed only when setup aborts on any pre-existing record/session/home and
  cleanup revalidates an exact ownership receipt.
- Run most writes in a throwaway MorphDB app and delete only that exact app in a
  `finally`/tear-down path.
- Cross-process invariant tests start their own MorphDB process on an ephemeral
  port with a temporary SQLite file and lock directory; they never use the
  default/shared server, and remove locks only after every worker/server stops.
- Run the dashboard on isolated port `18788` with the throwaway app.
- Any required live-`crew` fixture uses a narrower prefix and is removed in the
  same test that creates it.
- Put temporary homes under a newly-created `/tmp/crew_tests_*` directory and
  remove only those exact paths.
- Worktree tests use a disposable Git repository. Never remove or reset a user
  worktree.
- Real-model prompts are non-destructive, bounded, and ask only for a short file or
  message round-trip inside the test home.
- Do not expose credentials, auth files, transcript contents, or webhook secrets in
  logs, reports, screenshots, or fixtures.

## Environment and setup validation

| ID | Behavior to prove | Evidence |
|---|---|---|
| SET-01 | Python >=3.10, tmux, MorphDB, Git, Claude Code, and Codex are detected with actionable failures for missing required tools. | CLI behavior tests plus clean-shell smoke. |
| SET-02 | `morphdb start` and `crew init` succeed from a clean install; init is idempotent and schema merge preserves data. | Isolated live app, run init twice, write/read after each. |
| SET-03 | Following the documented quickstart makes `crew` available inside every spawned agent, not only from the repo checkout. | Fresh shell and real tmux pane run `command -v crew`, `crew whoami`, and `crew peers`; the actual pane process receives the selected app/backend/root even when interactive shell startup files export conflicting values. |
| SET-04 | Dashboard start/status/open/logs/stop handle stale PID files, occupied/foreign/wrong-app ports, a child that never binds, a process that refuses to terminate, absent MorphDB, and repeated calls cleanly without false success or premature ownership-file deletion. | CLI integration plus browser recovery test. |
| SET-05 | A clean install does not require pre-existing `leads`, `builder`, `sales`, `AgentA`, or `AgentB` records. | Full suite against a new app. |
| SET-06 | Supported setup, default homes, ports, runtime choices, and test commands are correct everywhere they are documented. | Documentation assertions and manual quickstart. |

## Runtime compatibility matrix

Run every row once for Claude Code and once for Codex CLI unless explicitly noted.

| ID | Behavior to prove | Expected outcome |
|---|---|---|
| RUN-01 | Runtime selection at CLI and dashboard creation. | A first-class runtime choice is stored; the default remains backward compatible; custom launch commands remain possible and are labeled custom. |
| RUN-02 | Launch command and unattended mode. | The selected CLI starts in the agent's tmux pane without an unhandled trust/approval dialog; dangerous autonomy is clearly disclosed. |
| RUN-03 | Native durable identity. | Both get `identity.md`; Claude gets a managed `CLAUDE.md` block and Codex gets a managed `AGENTS.md` block. Existing user content is preserved and marker injection is defanged. |
| RUN-04 | Identity ingestion. | A fresh real session can answer its Crew name, role, home, and permitted peers without the operator typing that context. |
| RUN-05 | Process/pane discovery. | Dashboard and CLI locate the selected runtime even with split panes; bare-shell `--no-launch` is reported as session-up/not-started rather than session-down. |
| RUN-06 | Status detection. | Idle, working, needs-input, not-started, and down are not conflated. Unknown/custom runtimes degrade honestly instead of claiming certainty. |
| RUN-07 | Safe delivery while busy. | Mail never corrupts the active turn. Runtime-specific submission/queue keystrokes are used where necessary; the message ends as queued, delivered, `runtime_queued`, or non-retryable `submitting`/`delivery_uncertain` exactly according to durable submission evidence. |
| RUN-08 | Restart/resume. | down/up/restart relaunch the same configured runtime, preserve identity and existing native instruction content, and refresh the pane id. |
| RUN-09 | Usage budgets. | Supported runtime usage is measured correctly. Complete zero records remain valid; missing/empty/partial/non-integer/negative assistant usage poisons both metrics for the active window; unknown pricing poisons cost only. An unsupported/unavailable meter is surfaced as unavailable rather than silently treated as zero. |
| RUN-10 | Runtime-specific trust/config isolation. | Claude trust setup never mutates Codex config; Codex setup never mutates Claude trust; failures do not corrupt either config. |
| RUN-11 | Runtime-neutral UI/copy. | Dashboard, CLI, docs, window/status labels, and error text say agent/runtime where appropriate instead of hardcoding Claude. |
| RUN-12 | One-blob expansion. | Expansion has a documented provider/config path and a deterministic fallback if the configured provider is absent, slow, or malformed. |
| RUN-13 | Sandbox-compatible tmux isolation and upgrade. | Crew-managed sessions use a private, owner-only tmux server/socket that an allowed agent sandbox can reach without exposing or changing unrelated user tmux sessions. Every spawn, identity lookup, mail send, lifecycle action, status probe, and dashboard PTY uses the same explicit server even when `$TMUX` points elsewhere. Pre-upgrade sessions are detected only after canonical name, complete ownership context, and exact stored-pane validation; extra shell splits are allowed. Crew never claims it can atomically move a live tmux conversation between servers: `up` and `restart` refuse without touching it, and the operator explicitly accepts conversation teardown by running `down` before a later `up`. The `down` boundary revalidates exact ownership; if the later `up` fails, Crew preserves the durable row, home, and retriable down state. Collisions and foreign/default-server sessions remain untouched. |

## Agent and project lifecycle

| ID | Behavior to prove | Cases |
|---|---|---|
| AGT-01 | Create an agent by CLI, dashboard structured form, and one-paragraph form. Crew does not currently expose rename as a product control; the internal name-update boundary must still preserve uniqueness. | Minimal/default fields, every optional field, invalid/duplicate names, reserved operator/system authority and sender sentinel names rejected at every write boundary, long/special text, launch now/off, synchronized same-name create/create and internal rename/create processes leave exactly one identity. |
| AGT-02 | Home planning and containment. | Default `~/crew/<project>/<agent>`; explicit home; reject root, user home/ancestors, same/nested/parent overlap, symlink aliases, and unsafe names. |
| AGT-03 | Repository worktree. | `--repo` creates a separate worktree on the persistent named branch `crew/<project>/<agent>` from the correct default branch, records it, rejects non-repos/conflicts, and preserves dirty work on removal. UI and docs must name the branch behavior accurately. |
| AGT-04 | Project lifecycle and isolation. | create/list/select; `--project` ordering; app key; default-home subtree; session prefix; identical agent names in two projects; no record/session/layout leakage. |
| AGT-05 | Named-project identity anti-spoofing. | The live tmux session resolves to the plain agent name and mutable env vars cannot impersonate a peer. |
| AGT-06 | Status and lifecycle commands. | status, up/down/restart one and `--all`; missing name; idempotency; custom runtime; already-running session; missing ordinary home; missing recorded worktree must not be recreated as a plain directory; absent/crashing built-in executable must not be reported running; an unrelated same-named tmux session is never adopted, attached, resized, messaged, or reported alive. |
| AGT-07 | Removal. | Owned session kill succeeds before record/edge deletion; kill failure leaves the row retriable; `--keep-session` respected; home/worktree preserved; surviving neighbors' identities refresh; stale trust entry handled only for the relevant runtime; synchronized PATCH/delete cannot resurrect a blank agent, and incident-edge create/delete cannot leave an orphan edge. |
| AGT-08 | Existing tmux collision and foreign-session isolation. | Spawn refuses a collision; dashboard PTY refuses every non-Crew session and leaves it unchanged. |
| AGT-09 | Identity rewrite. | Connect/edit/delete/cap/grant/foreman changes rewrite both affected identities atomically and preserve user-authored native-instruction content; cross-operation locks refetch the latest row before publication, ambiguous committed writes reconcile, transient neighbor reads abort rather than omit authorization, and corrupt legacy edge values fail cleanly without a traceback. |
| AGT-10 | Upgrade compatibility. | Existing unambiguous name-owned foreman children/edges migrate to immutable creator GUIDs on init, without granting authority after a name is deleted and reused. |

## Graph authoring and visualization

| ID | Behavior to prove | Cases |
|---|---|---|
| GRF-01 | Directed edge creation. | Label, description, multiple forward conditions, receiver action, reply flag, all caps, transform, identity update. |
| GRF-02 | Two-way edge creation. | Independent reverse conditions/action/reply and authorization in both directions. |
| GRF-03 | Reply contract consistency. | A directed edge cannot promise an impossible reply; UI/CLI either require reverse authorization, make it two-way, or clearly reject the contradictory configuration. |
| GRF-04 | Edge edit, cap, note, bless, and delete. | CLI/API/UI surfaces persist correct fields; invalid values and unknown update/bless ids fail without phantom PATCH upserts; synchronized update/delete cannot resurrect a relation-less edge. |
| GRF-05 | Duplicate-edge semantics. | Ambiguous/overlapping authorizations are rejected. Synchronized directed/two-way create/create and authorization-expanding update/create processes accept exactly one conflicting operation; legacy duplicate rows fail closed at the delivery gate. |
| GRF-06 | Disconnect semantics. | Directed and two-way cases remove only the intended relation(s); CLI and browser wording match any difference. |
| GRF-07 | Canvas layout. | Force layout, drag pin, double-click unpin, persistence, empty state, graph refresh, large graph, long labels, overlapping pinned nodes settle without an endless animation loop, and project-scoped saved views. |
| GRF-08 | Canvas navigation. | Wheel/pinch cursor anchoring, pan, zoom buttons, keyboard shortcuts, fit, 5-300% clamps, node drag, edge drag, and label click do not conflict. |
| GRF-09 | Snapshot truthfulness. | Agent/edge counts, arrows, conditions, caps, runtime, foreman/blessed styling, and status match API/storage after every mutation. |
| GRF-10 | Expansion. | Agent and edge prose expansion, fenced JSON, malformed output, timeout, missing provider, exact fallback, and no unintended submission. |

## Messaging, delivery, and transforms

| ID | Behavior to prove | Cases |
|---|---|---|
| MSG-01 | Sender identity and anti-spoofing. | Real default/named-project pane, foreign pane, no tmux, stale/forged `TMUX_PANE`, actual controlling-tty/pane mismatch, env fallback, conflicting env values, reserved authority/sender sentinels, and unknown caller. The actual controlling Crew pane wins; mutable env cannot claim operator authority or impersonate a peer. |
| MSG-02 | Hard topology gate. | Authorized direction succeeds; reverse, disconnected, self, empty, and unknown target fail loudly and log the right status. |
| MSG-03 | Submission format. | Provenance prefix, `--no-prefix`, forged-prefix defanging, Unicode, quotes, control characters, and long/multiline bodies. |
| MSG-04 | Multiline inbox. | Full body is stored once under the target's private inbox; prompt gets a safe pointer; filename collision and unusable-home fallback work. |
| MSG-05 | Live delivery. | Idle target receives exactly one complete submitted message and the durable row changes to delivered only after typing succeeds. |
| MSG-06 | Busy/down queue. | Down, unavailable, or busy non-Codex targets queue; a working Codex target may accept the message into its next-turn queue as `runtime_queued`; FIFO has a durable same-second tie-break, uncertain head/backlog reads never permit overtaking, one target with more than a scan page of blocked rows cannot starve healthy targets, and recovery flushes oldest first. |
| MSG-07 | Retry/expiry/bounce. | Dashboard flusher and inline flush work; target deletion fails rows; one-hour expiry is once-only, batched, observable, and bounces/notifies. |
| MSG-08 | Concurrent sends. | Per-target lock prevents interleaving and stale locks recover; fail-after-commit message creation reconciles by idempotency key; double-submit does not duplicate delivery; disconnect linearizes with acceptance so no send can be accepted after authorization revocation returns. |
| MSG-09 | Rate cap. | Boundary count, hourly rollover, reverse direction, filtered/blocked exclusion, zero=unlimited, limits above a 2,000-row page remain enforceable, replacement names/edges do not inherit old GUID usage, and refusal output/log. |
| MSG-10 | Token/cost budgets. | Exact threshold, overage, unknown model/runtime, missing/malformed transcripts, cache tokens, and direction-specific usage. |
| MSG-11 | Operator kickoff. | Idle delivery, busy/down behavior, missing target/text, runtime-specific safety, and UI/docs accurately reflect whether kickoff is durable. |
| MSG-12 | Mail inspection. | status and agent filters, limit, newest-first display, age formatting, all documented statuses, and no body/provenance loss. |
| MSG-13 | Transform attachment security. | Human only; realpath must stay in transforms directory; missing/empty/outside path rejected. |
| MSG-14 | Transform execution. | Pass-through replacement, multiline output, empty/nonzero/timeout filtering, stderr reason clipping, exactly once across queue flush, log and notification. |
| MSG-15 | Shipped transforms. | redact known fake key shapes without leaking; squeeze boundary behavior; scrub likely prompt injections without false positives on clean examples. |
| MSG-16 | True cross-runtime handoff. | A real Claude agent and Codex agent exchange a bounded task and reply through a two-way edge; identities, mail rows, and terminal output agree. |

## Governance, self-editing, approvals, and grants

| ID | Behavior to prove | Cases |
|---|---|---|
| GOV-01 | Human operations. | Human may perform every graph/lifecycle operation and rows are blessed with complete audit records. |
| GOV-02 | Plain-agent boundary. | May note itself/incident edge and lower incident caps; spawn/connect/remove/bless/foreman/grant/revoke/approve/reject and non-incident edits refuse. |
| GOV-03 | Foreman singleton. | Grant/revoke/idempotency, second foreman refusal, identity badge/powers, quota display, and human-only enforcement. |
| GOV-04 | Foreman envelope. | Spawn child; connect/disconnect inside envelope; start/stop owned child; touching human/foreign node becomes pending or refused exactly as specified. |
| GOV-05 | Finite-cap rule. | Agent-created edge requires all three finite positive caps within ceilings; zero/unlimited/over-ceiling fail before any write. |
| GOV-06 | Quotas. | Max agents and shared hourly spawn rate at exact boundaries; refused attempts do not consume quota. |
| GOV-07 | Cap direction. | Lower/equal apply directly; every raise including raise-to-zero becomes pending; no premature mutation. |
| GOV-08 | Pending lifecycle. | Required/fail-closed persistence; complete stored args; immutable requester/target GUIDs; unique/ambiguous prefix; durable applying claim; per-app/GUID approve/reject serialization with one winner across processes; approve replay once; mutation/finalization failure becomes non-pending and non-replayable; tampered stored operations/fields cannot expand the originally queueable shape; reject with reason; terminal states immutable; requester notice; `applying`/`approval_failed` remain visible with recovery guidance in CLI/browser. |
| GOV-09 | Blessing and audit. | Agent-created records remain visibly unblessed until human review; bless one/edge/all; audit filters and every result state. |
| GOV-10 | Real self-modification E2E. | From its actual pane, a foreman uses bare `crew` to spawn an agent and connect it with finite caps, receives a pending result across its envelope, and observes human approval/rejection. |
| GOV-11 | Operator authority boundary. | An agent cannot gain human authority by curling the loopback dashboard API or by detaching from its Crew pane and spoofing/unsetting inherited identity markers; missing/mismatched managed context fails closed. If a fully stripped same-OS-user process is fundamentally indistinguishable from the operator, docs state that residual boundary explicitly. |
| GNT-01 | Grant/revoke. | Existing-target requirement; ro/rw record; symlink; identity text; audit; collision suffix; own-home refusal; symlinked stored home/refs refusal; safe single-component revoke name; missing symlink; per-agent serialization; graph/link/identity rollback on every injected failure; final-section removal. |
| GNT-02 | Grant governance. | Human applies, foreman pending then approve/reject, plain agent refuses, list is read-only/no gate, revoke human-only. |
| GNT-03 | Honesty. | UI/docs state ro/rw is recorded intent rather than filesystem enforcement. |

## Dashboard, terminal, API, and usability

| ID | Behavior to prove | Cases |
|---|---|---|
| UI-01 | Initial/empty/loading/error/recovery states. | No app/schema, MorphDB down/up, dashboard restart, empty graph, populated graph, slow poll. |
| UI-02 | Refresh control. | 1s/1.5s/3s/off, no overlapping requests, state preserved, counts/status fresh after resume. |
| UI-03 | Agent modal. | Keyboard/focus, structured/prose switch, defaults match backend, runtime and repo/home semantics, validation, double-submit protection, errors, success. |
| UI-04 | Edge modal. | Drag open, every forward/reverse field, add/remove condition, directed toggle, expansion, validation, edit/delete/bless, double-submit. |
| UI-05 | Identity card. | Full role/home/runtime/status/blessed/foreman/grants/incoming/outgoing/caps content, long-text overflow, controls and fresh state. |
| UI-06 | Pending tray. | Badge/count, summaries, approve/reject/reason, stale/terminal row handling, focus and error states. |
| UI-07 | Real terminal transport. | SSE attach, binary/base64 stream, keyboard input, paste, Unicode, resize, scrollback, selection, split/window navigation, reconnect, close; a false/failed initial resize closes its allocated view before returning an error; every target passes exact live Crew ownership validation. |
| UI-08 | Dock controls. | Previous/next wrap, maximize/restore, drag resize limits/persistence, close, Ctrl+Esc detach, focus handoff, custom-crash/start visibility; dock and in-flight start are keyed by immutable GUID and close rather than adopting a same-name replacement. |
| UI-09 | Multiple viewers. | Newest view wins without resize tug-of-war; stale grouped tmux views are reaped. |
| UI-10 | API validation/security. | Unknown paths, malformed/non-object JSON, missing fields, non-string text fields, malformed condition arrays/members, invalid numbers/booleans, max body, static traversal, canonical PTY target scope even with a corrupt stored session, clean JSON errors; snapshot/pending/PTY/control require the capability cookie; health/static bootstrap remains public; initial-load and same-document `#cap` changes exchange once and erase the fragment; authenticated writes require JSON plus the CSRF header and reject a foreign Origin. |
| UI-11 | Responsive and accessibility. | Desktop/tablet/mobile layout, keyboard-only operation, focus order/visibility, labels/names, modal trap/escape, contrast, reduced motion, zoomed text. |
| UI-12 | Console/network health. | No uncaught errors, failed requests, CSP/mixed-content issues, or unexplained 4xx/5xx after each workflow. |
| UI-13 | Honest surface parity. | Docs list which tasks are dashboard, CLI, or both. No claim that a removed message bar, project switcher, grant UI, or other nonexistent control exists. |

## Notifications, persistence, and failure recovery

| ID | Behavior to prove | Cases |
|---|---|---|
| OPS-01 | Webhook formats. | ntfy text/title and generic JSON for every implemented event; invalid URL/network failure never breaks core work. |
| OPS-02 | Status notifications. | down/needs-input transitions notify once per immutable-GUID transition and are monitored as documented even if no browser is polling; same-name replacements seed fresh state without inheriting false alerts. |
| OPS-03 | Graph/mail notifications. | filtered, expired, failed, and pending graph-edit events use accurate documented names and details. |
| OPS-04 | Restart persistence. | MorphDB records, identities, audit/mail history, homes/worktrees, runtime selection, and graph survive service/dashboard/session restart. |
| OPS-05 | Corruption/failure. | Missing/corrupt project registry, malformed message/pending rows, pending-create/claim/replay/finalize write failures, fail-after-commit POST/PATCH/DELETE responses, partial tmux failure, stale pane id, MorphDB timeout, and schema drift fail loudly and recover safely without replaying an already-applied mutation or emitting duplicate bounce/notification side effects. |
| OPS-06 | Graph history boundary. | Product copy does not promise n8n-style executable workflows, checkpoint replay, graph version rollback, or automatic condition evaluation that Crew does not provide. |

## Browser scripts to execute and repair

All eleven scripts are required. Their setup must target the isolated backend,
authenticate API cross-checks, use project-scoped canvas keys, and reject the
historical false-down liveness behavior:

Every mutating procedure must pin port `18788`, verify the non-operator app and
default project, and abort when a fixed-name record, exact tmux session, or home
already exists. After creation it records an ownership receipt containing the
exact agent `_guid`, stored session, canonical home, and app. Cleanup must
revalidate that receipt and use the authenticated app before removing anything;
it may not blindly kill a same-named session or recursively delete an unmarked
home. `terminal-dock.md` is independently executable, provisions both terminal
states itself, and treats the preexisting `_ngview_*` set as an immutable
baseline.

1. `tests/browser/create-agent.md`
2. `tests/browser/connect-edge.md`
3. `tests/browser/edit-edge.md`
4. `tests/browser/revive-agent.md`
5. `tests/browser/foreman-bless.md`
6. `tests/browser/pending-tray.md`
7. `tests/browser/one-blob-config.md`
8. `tests/browser/canvas-navigation.md`
9. `tests/browser/runtime-selection.md`
10. `tests/browser/terminal-dock.md`
11. `tests/browser/resilience-accessibility.md`

Also execute and record durable scenarios for:

1. Messaging/queue/mail inspection and operator kickoff behavior.
2. Project isolation/worktree wording and view-state isolation.
3. Governance API boundary and a real foreman CLI flow.
4. Any additional error/recovery, responsive-layout, or accessibility case
   discovered while executing the resilience script.

Every browser script must use a dynamically resolved repo root, an isolated test
app/port, either per-run names or receipt-guarded fixed-name fixtures, screenshots
for failures and fixes, console checks after interactions, and mandatory cleanup
in a `finally` path.

## Execution order

1. Record environment, service, process, port, and unrelated-session baselines.
2. Make the test harness clean-room safe; write red tests for each confirmed gap.
3. Run the current full Python suite and isolated live smoke to establish the
   baseline without changing production behavior.
4. Fix setup and in-agent CLI availability first, since every real E2E depends on
   them.
5. Implement/fix runtime adapters and prove Claude and Codex separately.
6. Fix lifecycle, identity, status, mail safety, project spoofing, and reply/edge
   semantics with targeted red-green tests.
7. Fix governance, dashboard API authority, approvals, and grants.
8. Run every CLI/live workflow and the real Claude-to-Codex handoff.
9. Execute all browser scripts with computer use, fix each reproducible issue,
   and repeat until the browser baseline is green.
10. Verify mobile/accessibility/console/error states and public explainer links.
11. Update docs and copy only to behavior proven in the preceding steps.
12. Run targeted tests, affected tests, `python3 -m unittest discover tests`, live
    smoke, all browser scripts, and final cleanup/leak checks.

## Final evidence to report

- Exact setup and day-to-day usage commands.
- Tests run, counts, durations, and all pass/fail results.
- Initial and final QA health scores.
- Each bug's reproduction, regression test, fix, and verification evidence.
- Claude and Codex runtime results separately.
- Clean-room install and live-write results.
- Browser screenshots for initial state, each issue, and each verified fix.
- Remaining limitations only when they are technically unavoidable and accurately
  represented in the product.
