# environments — prepare an agent's workspace before its runtime starts

The settings page gains an Environments tab: named, operator-defined setup
routines (prereq + commands) that run inside an agent's home before its
harness launches, plus two built-ins — a fresh git worktree and a Graphite
branch stacked off main. Environments are picked per agent at creation (or a
crew-wide default applies), and defining them is human-only: their commands
run as the operator on every spawn that uses them.

Target: http://127.0.0.1:8788 (authorized tab). The store
(var/environments.json) is SHARED with the live install: this script only
creates names prefixed `test_bv_env_`, restores the default it found, and
removes everything it made. CLI checks run from the repo root.

1. Open `⚙ settings` → click the Environments tab
   (`#settings-tab-environments`).
   EXPECT: cards for the two built-ins — `worktree` and `graphite-stack` —
   each marked built-in with its description and (for graphite-stack) its
   prereq `gt --version` and command list; NO delete control on either. A
   `Default environment` select (`#env-default`) reads `none`.
2. In the add form (`#env-new`): name `test_bv_env_touch`, commands textarea
   one line `touch env-ran.txt`, click Add (`#env-add`).
   EXPECT: a `test_bv_env_touch` card appears (not built-in, with a delete
   control); the default select now offers it.
3. Try to add a second environment named `worktree`.
   EXPECT: refused with an error naming the built-in collision; the form
   keeps its values; no duplicate card.
4. From the repo root:
   `./bin/crew spawn-agent test_bv_env_agent --env test_bv_env_touch --no-launch`.
   EXPECT: spawn succeeds; the agent's home contains `env-ran.txt` (the
   setup command really ran in the workspace before any runtime start), and
   `./bin/crew env list` shows `test_bv_env_touch`. The graph shows the new
   agent's card.
5. Set `#env-default` to `test_bv_env_touch`.
   EXPECT: saved; reloading the page shows the default still selected.
6. Open the Create-agent modal (`+ Agent`).
   EXPECT: an Environment select (`#a-environment`) listing none, worktree,
   graphite-stack, and test_bv_env_touch. Close without creating.
7. Set `#env-default` back to `none`, then delete the
   `test_bv_env_touch` card (`#env-remove-test_bv_env_touch`).
   EXPECT: the card disappears; built-ins remain; the default select no
   longer offers the removed name.
8. Cleanup: `./bin/crew remove-agent test_bv_env_agent`; then
   `./bin/crew env list`.
   EXPECT: only the two built-ins, default none — the store holds nothing
   this script created.
