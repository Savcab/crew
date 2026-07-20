# terminal-window — opt-in second-window terminal (dual-monitor flow)

The header's "⧉ 2nd window" toggle (#termWinToggle, persisted in
localStorage crew.termwin.v1) routes agent clicks to a separate terminal
window at /?view=term instead of the local dock. The two windows talk over
BroadcastChannel('crew-term'); the terminal window is the named target
'crewTerm', so repeat opens reuse it.

Target: http://127.0.0.1:8788 (authorized tab; the term window shares the
origin cookie).

1. Click #termWinToggle. EXPECT: aria-pressed="true"; state survives reload.
2. Click an agent node. EXPECT: a new window/tab opens at /?view=term titled
   "crew — terminal"; the LOCAL dock stays closed. The term window shows that
   agent's dock full-window (resize handle hidden, dock tabs present,
   terminal streaming).
3. In the GRAPH window, click a different agent. EXPECT: the term window
   switches to it WITHOUT reloading (no page navigation — e.g. a marker set
   on its window object survives).
4. Close the term window, then click an agent in the graph window. EXPECT: a
   fresh term window is spawned (the graph window falls back to window.open
   after the term window's goodbye broadcast) and shows the agent.
5. Toggle #termWinToggle off, click an agent. EXPECT: the LOCAL dock opens
   again; no new window.
6. Two projects note: BroadcastChannel is per-origin, so dashboards on
   different ports can never cross-talk.
