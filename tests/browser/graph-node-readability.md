# graph-node-readability — big node titles; role appears only on hover/focus

Node cards scan by NAME + status: the agent name renders large, and the
role/description line stays hidden until the pointer hovers the card (or it
receives keyboard focus, so the detail is not mouse-only).

Target: http://127.0.0.1:8788 (authorized tab), any graph with ≥1 agent.

1. Load the dashboard, wait for agent nodes.
2. Pick a `.cnode.agent`. EXPECT: computed font-size of its `.nm` row ≥ 15px
   (the headline), and its `.sub.role` element has `display: none`
   (offsetParent null) — the description is not shown at rest.
3. The status line (`.sub.state`) IS visible at rest — state stays glanceable.
4. Hover the card with the mouse. EXPECT: `.sub.role` appears as a floating
   TOOLTIP below the card (absolutely positioned, full text wrapped) showing
   the agent's role (or the italic "no role" placeholder) — and the card's own
   border box KEEPS ITS EXACT SIZE (getBoundingClientRect unchanged vs rest).
   The tooltip has pointer-events:none so it never blocks graph interaction,
   and there is no stacked native title tooltip on the card.
5. Move the pointer off the card. EXPECT: the tooltip hides again.
6. Tab to a card (keyboard focus). EXPECT: `.sub.role` visible while focused —
   parity for keyboard users.
7. Drag a card while hovering. EXPECT: drag/click behavior unchanged (click
   still opens the dock after the double-click window).
