# agent-activity — self-set status lines visible on the graph

`crew activity <text>` lets an agent publish a short "what I'm doing" line
(guard: only its OWN; humans may target any agent via --agent). The line
rides the graph snapshot onto the node card so the operator — and any peer
running `crew activity` — sees what everyone is doing without opening a
terminal or sending mail.

Target: http://127.0.0.1:8788 (authorized tab). Fixture agent test_bv_* (a
plain record is enough; no tmux needed), removed afterwards.

1. Create fixture `test_bv_activity` (direct graphstore create or dashboard
   + Agent with launch unchecked).
2. From an operator shell: `./bin/crew activity --agent test_bv_activity
   "working on outreach…"`. EXPECT: "activity set" confirmation.
3. Watch the graph (≤ one poll). EXPECT: the fixture's card shows an italic,
   accent-tinted `.sub.activity` line "working on outreach…" at rest (no
   hover needed); the card's aria-label includes the text.
4. `./bin/crew activity`. EXPECT: a listing with one line per agent; the
   fixture shows the text + freshness ("just now"/"Nm ago"); others show —.
5. Open the fixture's ⓘ identity card. EXPECT: an "activity" row with the text.
6. `./bin/crew activity --agent test_bv_activity --clear`. EXPECT: the card's
   activity line disappears on the next poll.
7. Guard check (agent scope): with a managed agent pane available, run
   `crew activity --agent <other-agent> "x"` FROM that pane. EXPECT: refused —
   an agent may set only its own activity (refusal lands in `crew audit
   --refused`).
8. Cleanup: remove the fixture.
