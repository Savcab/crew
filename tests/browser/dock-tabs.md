# dock-tabs — terminal tabs: one per tmux window, "+" opens a typeable shell

The dock shows a tab bar (#dockTabs): one tab per tmux WINDOW of the docked
agent's session, plus a "+" (#dockTabAdd) that creates a new shell window in
the BASE session and switches the dock to it. Tab clicks switch only the
DOCK's grouped view — the agent's own current window never moves.

Target: http://127.0.0.1:8788 (authorized tab). Use a FIXTURE agent
(test_bt_* with a live tmux session, launch command like `exec sh`) — never
add/kill windows on real agents. Record the base session's current window
first: `tmux -S "$CREW_TMUX_SOCKET" display-message -p -t '<session>:' '#{window_id}'`.

1. Open the fixture's dock. EXPECT: #dockTabs renders one tab per window
   (fresh spawn = the runtime window, active) plus the "+" button. Tabs have
   role=tab and aria-selected mirrors the active state.
2. Click "+" (#dockTabAdd). EXPECT: a new tab appears (shell window name) and
   becomes ACTIVE; the terminal shows a fresh shell prompt.
3. Click into the terminal, type `echo tab-check-$((6*7))`, Enter. EXPECT:
   `tab-check-42` renders — the new tab is a real, typeable shell.
4. Click the first (runtime) tab. EXPECT: it becomes active and the runtime
   window's content is back on screen.
5. Check the BASE session: `display-message -p -t '<session>:' '#{window_id}'`.
   EXPECT: unchanged from the recorded value — the agent's own screen was
   never yanked to the new tab.
6. Close and reopen the dock. EXPECT: both tabs still listed (windows are
   durable in the session); tab bar clears while the dock is closed.
7. Cleanup: kill the created window
   (`tmux -S "$CREW_TMUX_SOCKET" kill-window -t '<session>:<new-window-id>'`),
   then remove the fixture agent + home as usual.
