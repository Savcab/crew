# subagent-badge — live subagents from the agent's own harness, on its card

An agent's card badges how many live subagents its coding harness is running
under it (`⑂ N sub`), read from the harness's own on-disk state on the SAME
poll that paints status. A missing reading is not a claim of none: only a real
count above zero badges, so "0 running" and "unreadable" both render nothing.

Target: http://127.0.0.1:8788 (authorized tab). Fixtures: a throwaway Hermes
state dir + one hermes-runtime agent `test_bv_sub_hermes`. Hermes is the
deterministic path — its delegation count is machine-wide and read straight
out of `state.db`, so no live Hermes process and no launched runtime are
needed. The dashboard must be RESTARTED with `CREW_HERMES_STATE_DIR` pointing
at the fixture dir, because the server process itself does the reading. Both
the dir and the agent are removed at the end.

1. Build the fixture state dir (any throwaway path, e.g.
   `D=~/tmp/crew-sub-fixture; mkdir -p "$D"`):
   `sqlite3 "$D/state.db" 'CREATE TABLE async_delegations (delegation_id TEXT
   PRIMARY KEY, state TEXT); INSERT INTO async_delegations VALUES
   ("d1","running"),("d2","finalizing"),("d3","done");'`
   EXPECT: `$D/state.db` exists. `running` and `finalizing` are the two live
   states; the `done` row is the control that must NOT be counted. No
   `kanban.db` is needed — the badge reads state.db alone.
2. Restart the dashboard so its process sees the fixture:
   `./bin/crew dashboard stop`, then
   `CREW_HERMES_STATE_DIR="$D" ./bin/crew dashboard start`.
   EXPECT: "dashboard started → http://127.0.0.1:8788". The stop is required —
   `start` against a live dashboard is a no-op and would keep the old env.
3. Create the fixture agent. The Create-agent modal offers only Claude Code /
   Codex CLI / Custom, so a hermes runtime comes from the CLI:
   `./bin/crew spawn-agent test_bv_sub_hermes --runtime hermes --no-launch`.
   EXPECT: created; its card appears on the graph with a `hermes` runtime badge.
4. Watch that card (≤ one poll). EXPECT: immediately after the runtime badge, a
   `.subagent-badge` pill reading exactly `⑂ 2 sub` — the two live delegations,
   not the `done` one. It is accent-tinted (blue text on a faint blue fill),
   visibly distinct from the dim, bordered runtime badge next to it.
5. Hover the badge. EXPECT: title tooltip
   `2 live subagents run by this agent's harness`.
6. Look at a claude-runtime agent in the same graph with no live background
   sessions. EXPECT: NO `.subagent-badge` anywhere on its card — its runtime
   badge is unchanged, and nothing renders for a 0 or absent reading.
7. Retire one delegation:
   `sqlite3 "$D/state.db" "UPDATE async_delegations SET state='done' WHERE
   delegation_id='d2';"`
   EXPECT: within one poll the fixture's badge reads `⑂ 1 sub` and its title
   goes singular: `1 live subagent run by this agent's harness`.
8. Retire the last one:
   `sqlite3 "$D/state.db" "UPDATE async_delegations SET state='done';"`
   EXPECT: the badge disappears entirely on the next poll (an honest zero shows
   nothing, exactly like an unreadable harness) — the card is otherwise
   unchanged: same runtime badge, same status line.
9. Cleanup: `./bin/crew remove-agent test_bv_sub_hermes`, `rm -rf "$D"`, then
   `./bin/crew dashboard stop && ./bin/crew dashboard start` to drop
   `CREW_HERMES_STATE_DIR` from the server's environment. EXPECT: the graph is
   back to baseline and no card shows a subagent badge from the fixture.
