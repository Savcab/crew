# crew-settings — a full settings page with selectable harness launch commands

The dashboard header's `⚙ settings` button navigates to a full settings page
(`/?view=settings`) with a left sidebar of tabs (first tab: Harnesses). Each
coding harness's startup command is a SELECT of curated choices — never a
typable shell field. Choosing a non-default option stores it in
`var/settings.json`; choosing the default clears the override. The next
spawn/revive uses the stored command — no dashboard restart. An env override,
when present, is declared in the row's hint and keeps winning.

Target: http://127.0.0.1:8788 (authorized tab). The store is SHARED with the
live install, so this script only ever changes `hermes_launch_cmd` (no hermes
agent is spawned while it runs) and ends by restoring the default. CLI checks
run from the repo root.

1. In the header, click `⚙ settings` (`#settingsBtn`).
   EXPECT: a full-page navigation to `/?view=settings` — not a popup. A left
   sidebar (`#settings-nav`) shows a "Harnesses" tab (active) and a way back
   to the graph; the main pane is titled Settings · Harnesses.
2. Inspect the three controls `setting-claude_launch_cmd`,
   `setting-codex_launch_cmd`, `setting-hermes_launch_cmd`.
   EXPECT: each is a SELECT (no free-text input anywhere on the page). Claude
   offers 3 options (Unattended default / Ask for permissions / Unattended
   continue last session), Codex 2 (Unattended default / Sandboxed with
   approvals), Hermes 3 (Default / Auto-approve (yolo) / Continue last
   session). Each shows its default choice selected and a `default` hint
   (unless an env override is active, which the hint declares instead).
3. Select `Auto-approve (yolo) — hermes --yolo` in the hermes select and
   click Save (`#settings-save`).
   EXPECT: a saved confirmation; no navigation away.
4. Reload `/?view=settings`.
   EXPECT: the hermes select shows the yolo choice selected with hint
   `stored override`; claude and codex still show defaults with hint
   `default`.
5. Cross-check the CLI: `./bin/crew settings list`.
   EXPECT: `hermes_launch_cmd (settings): hermes --yolo` while the claude and
   codex lines still show `(default)`.
6. Select the first option (`Default — hermes`) in the hermes select and
   Save, then reload the page.
   EXPECT: hermes back to the default choice with hint `default`, and the CLI
   shows all three keys `(default)` — clearing happened by choosing the
   default, no override remains.
7. Try to cheat: `./bin/crew settings set hermes_launch_cmd 'hermes --tui'`.
   EXPECT: a clean refusal listing the valid choice commands — arbitrary
   shell lines are not storable from any surface.
8. Cleanup check: `./bin/crew settings list` shows all three keys
   `(default)` — the store holds nothing this script created.
