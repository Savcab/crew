# harness-badges — the goal badge read from the agent's harness

`crew harness` reads what an agent's coding harness already knows: the open
goals it is working toward. The graph snapshot carries that reading onto the
node card as a ◎ goal badge, so the operator sees the crew's standing work
without opening a terminal. Unlike `crew activity`, the agent publishes
nothing; Crew reads it.

Target: http://127.0.0.1:8788 (authorized tab). Fixture agent `test_hb_*` on
the **hermes** runtime — its goals are cards on the machine-wide Hermes kanban
(`~/.hermes/kanban.db`), which can be written and cleaned from the CLI without
driving any TUI. Every fixture card and the agent itself are removed at the
end; use a `test_hb_` title prefix so cleanup is unambiguous.

1. Confirm the dashboard is serving the built bundle:
   `./bin/crew dashboard status`. EXPECT: running, and the served
   `static/assets/index-*.js` matches the current build.
2. Create fixture `test_hb_badges`
   (`./bin/crew --project default spawn-agent test_hb_badges --runtime hermes
   --no-launch --home <fresh dir>`). EXPECT: the node appears on the graph
   with a `hermes` runtime pill.
3. With the kanban empty, watch the fixture's card (≤ one poll). EXPECT: no ◎
   badge and a NORMAL runtime pill — the harness is readable and reports "no
   goal". Contrast with any `custom`-runtime agent on the graph, whose pill is
   muted/dashed (`.harness-unknown`): "no reading" must not look like "no
   goal".
4. Create a card: `hermes kanban create "test_hb: first goal" --json`. Reload
   (≤ one poll). EXPECT: the fixture's card shows a green ◎ badge carrying
   the card title, on its own row under the name — not squeezed into the name
   line next to the runtime pill.
5. Hover the badge. EXPECT: the full reading in the native tooltip
   (`title="goal: …"`), clipped to one line in the pill itself; the card does
   not grow to fit long text.
6. Create a second card: `hermes kanban create "test_hb: second goal" --json`.
   Reload. EXPECT: the badge still shows one reading, now with a `+1` suffix —
   the suffix counts the open goals NOT shown, so two open goals read as
   "<shown goal> +1".
7. Archive both cards (`hermes kanban archive <id>` twice), then reload.
   EXPECT: the ◎ badge disappears on the next poll and the card goes back to
   no badge — with a normal runtime pill (this IS the "no goal" claim, unlike
   the muted "no reading" state).
8. Keyboard/AT check: focus the fixture's card and read its accessible name
   (`document.activeElement.getAttribute('aria-label')`). EXPECT: with an open
   goal, the goal reading is named in it, not only shown visually.
9. Cross-check the CLI against the card: `./bin/crew harness test_hb_badges`.
   EXPECT: the same goal text the badge shows, and with more than one open
   goal a `(+N more open)` suffix matching the badge's `+N`.
10. Cleanup: `./bin/crew remove-agent test_hb_badges`, and confirm the kanban
    holds no `test_hb` cards (`hermes kanban list --json`). EXPECT: the node
    and its badge leave the graph.
