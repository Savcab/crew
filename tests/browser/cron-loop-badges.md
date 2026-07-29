# cron-loop-badges — cron loops from the agent's own harness, on its card

An agent's card badges how many cron loops its coding harness has scheduled
under it (`⟳ N cron`), read from the harness's own durable state on the same
poll that paints status. A missing reading is not a claim of none: only a
real count above zero badges, so "0 scheduled" and "no cron concept" (Codex)
both render nothing.

Target: http://127.0.0.1:8788 (authorized tab). Fixtures: a throwaway Hermes
state dir + one hermes-runtime agent `test_bv_cron_hermes`. Hermes is the
deterministic path — its jobs live in `cron/jobs.json` under the state dir,
bound to agent homes by each job's `workdir`. The dashboard must be
RESTARTED with `CREW_HERMES_STATE_DIR` pointing at the fixture dir, because
the server process does the reading. Both the dir and the agent are removed
at the end.

1. Create the fixture agent first (its home is the workdir the jobs bind
   to): `./bin/crew spawn-agent test_bv_cron_hermes --runtime hermes
   --no-launch`. EXPECT: created; note its home path H (printed).
2. Build the fixture state dir (any throwaway path, e.g.
   `D=~/tmp/crew-cron-fixture; mkdir -p "$D/cron"`) and write
   `$D/cron/jobs.json`:
   `{"jobs": [
     {"id": "j1", "name": "a", "enabled": true,  "workdir": "<H>"},
     {"id": "j2", "name": "b", "enabled": true,  "workdir": "<H>"},
     {"id": "j3", "name": "c", "enabled": false, "workdir": "<H>"},
     {"id": "j4", "name": "d", "enabled": true,  "workdir": null}]}`
   EXPECT: two jobs are live AND bound to H; the disabled one and the
   workdir-less one are the controls that must NOT be counted.
3. Restart the dashboard so its process sees the fixture:
   `./bin/crew dashboard stop`, then
   `CREW_HERMES_STATE_DIR="$D" ./bin/crew dashboard start`.
   EXPECT: "dashboard started" — the stop is required; start against a live
   dashboard is a no-op that would keep the old env.
4. Watch the `test_bv_cron_hermes` card (≤ one poll). EXPECT: a
   `.cron-badge` pill reading exactly `⟳ 2 cron` — amber-tinted, visibly
   distinct from the blue subagent badge and the dim runtime badge.
5. Hover it. EXPECT: title tooltip
   `2 cron loops scheduled by this agent's harness`.
6. Look at a claude-runtime agent with no `<home>/.claude/scheduled_tasks.json`
   and any codex-runtime agent. EXPECT: NO `.cron-badge` on either — an
   honest zero and a no-reading render identically as nothing.
7. Retire one job: edit `$D/cron/jobs.json` setting j2's `enabled` to false.
   EXPECT: within one poll the badge reads `⟳ 1 cron` and the tooltip goes
   singular.
8. Empty the jobs array. EXPECT: the badge disappears on the next poll; the
   card is otherwise unchanged.
9. Cleanup: `./bin/crew remove-agent test_bv_cron_hermes`, `rm -rf "$D"`,
   then `./bin/crew dashboard stop && ./bin/crew dashboard start` to drop
   `CREW_HERMES_STATE_DIR`. EXPECT: baseline graph, no cron badge anywhere
   from the fixture.
