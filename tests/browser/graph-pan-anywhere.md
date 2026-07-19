# graph-pan-anywhere — pan works in the whole graph area; drags never stick

Regression for: pan mousedown listener lived on the transformed `.gcanvas`
element, so at zoom < 1 (or panned view) parts of the visible `#cgraph` area
had no listener and drag did nothing — worst with the dock open (graph strip
shrinks to ~34%). Also: xterm stops mouseup propagation, so releasing a drag
over the terminal left the drag stuck to the cursor.

Target: http://127.0.0.1:8788

1. Load the dashboard. Wait for agent nodes to render in the graph.
2. In the page console, set the view zoomed out and reload:
   `localStorage.setItem('crew.view.v1', JSON.stringify({zoom:0.5, panX:0, panY:0}))`
   then reload. EXPECT: zoom indicator reads 50%.
3. Click an agent node. EXPECT: the bottom dock opens with its terminal.
4. Read `document.querySelector('.gcanvas').style.transform` (call it T0).
5. Press the left mouse button on an EMPTY spot in the LOWER-RIGHT region of
   the visible graph area (just above the dock, away from any node/edge), drag
   ~100px, release. EXPECT: `.gcanvas` transform changed from T0 (the view
   panned). This is the previously-dead zone.
6. Drag an agent node and RELEASE the mouse over the dock terminal. Then move
   the mouse (no buttons) back over the graph. EXPECT: the node does NOT
   follow the cursor (drag ended at release).
7. Reset zoom to 100% (Ctrl/Cmd+0 or zoom controls), drag on empty canvas.
   EXPECT: view pans.
8. Click (no drag) an agent node. EXPECT: dock opens on that agent (click
   still means open, not pan).
