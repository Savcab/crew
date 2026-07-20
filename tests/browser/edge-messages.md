# edge-messages — cables light up when a message flows; hover shows the latest

The graph answers "who is talking to whom about what": when a message is
accepted onto an edge, that edge lights up (class `talking`: bright blue,
thicker, glow) for ~5 seconds and then fades on its own. Hovering an edge
(the line or its label) shows a tooltip with the edge contract PLUS the
latest message that flowed: actual direction (matters on two-way edges),
freshness ("just now"/"Nm ago"), non-delivered status in parens, and a
preview (≤140 chars). Refused sends (blocked/ratelimited/budget*/filtered)
never light the cable or appear as "latest" — they were never authorized.

Target: http://127.0.0.1:8788 (authorized tab). Fixtures: two test_bm_*
agents + an edge between them; message rows injected directly via
graphstore.create_message with the edge's guid (status "delivered") — never
send real mail through live agents. Full cleanup: message rows, edge,
agents, homes.

1. Create the fixture pair + edge. EXPECT: edge renders with the normal
   stroke and its tooltip shows only the contract (no "last message" line).
2. Inject a delivered message row on the edge, then watch ≤1 poll. EXPECT:
   the edge gains `.talking` (computed stroke ≈ rgb(121,192,255), width
   3.5px) within ~2s.
3. Keep watching. EXPECT: the glow clears itself ≤5s after the message's
   created_at, with no further data change needed.
4. Hover the edge line or label. EXPECT: tooltip ends with
   `last message <age> — <from> → <to>: <preview>`.
5. Inject a message with status "queued". EXPECT: tooltip shows "(queued)"
   after the age.
6. Inject a message with status "blocked". EXPECT: no glow, and the latest
   line still shows the previous ACCEPTED message.
7. Cleanup and verify the live graph is back to baseline.
