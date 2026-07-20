# graphs-gallery — every project is one graph; Figma-style gallery + open

/?view=graphs lists every known graph (project) as a card: name, description,
agent count, live-dashboard dot, "current" badge. Opening a card jumps to the
dashboard process that owns that graph (finding a live one or spawning a
sibling on a free port, capability handed over in the URL fragment). Agents
keep running when you leave a graph or close the page — they live in tmux,
not in a browser window. "+ New graph" creates the project AND seeds a
launched foreman whose identity makes it ask "Describe the system you want to
build" and then build the crew itself via the crew CLI.

Target: http://127.0.0.1:8788 (authorized tab). Fixture graph name
testgraph-* — cleanup at the end must stop its dashboard, remove the foreman
(+ session + home), DELETE its MorphDB app, and unregister the project.

1. On the dashboard, EXPECT a "⌂ graphs" button (#graphsHomeBtn) in the
   header; clicking it lands on /?view=graphs (title "crew — graphs").
2. The gallery reads as a HOME PAGE, not a canvas (Figma-style): flat lighter
   background, sans-serif type, a left sidebar (#graphs-side) with a working
   search box (#graphsSearch filters cards by name/title/description), and a
   card grid. Each card's top pane is an SVG THUMBNAIL of the actual graph
   shape (one mini-node rect per agent, one line per edge — counts match the
   card's "N agents · M edges" meta). The serving project's card has the
   "current" badge and a live dot.
2b. Graph names are free text: creating with a spaced title (e.g. "AWB on
   crew") derives a machine slug (AWB-on-crew) for the app key/tmux/paths;
   the card shows the TITLE.
3. Click "+ New graph" (#newGraphBtn), enter name testgraph-<x> + a
   description, Create. EXPECT: the project registers (card appears with the
   description), a foreman agent exists in it, and the browser navigates to
   the new graph's own dashboard (different port, capability in fragment).
4. The new graph renders exactly one node: "foreman" (⚑ badge). Its identity
   (ⓘ) contains the chat-to-build brief ("Describe the system you want to
   build"). With launch enabled, opening its terminal shows a live runtime
   ready to take the description.
5. Header on the new dashboard shows "/ <graph name>" and the ⌂ graphs
   button; clicking ⌂ shows the SAME gallery (any process can render it),
   with both graphs listed and the OTHER one still live-dotted.
6. Open the original graph from the gallery. EXPECT: back on port 8788 with
   the original agents — nothing stopped or restarted while away.
7. Cleanup per the fixture rules above; verify var/projects.json no longer
   lists the fixture and no fixture dashboard/cap file remains.
