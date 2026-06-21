"""Milestone-1 tests: the MorphDB-backed agent graph + the messaging gate +
home-dir uniqueness + identity rendering.

These run against a LIVE MorphDB on $MORPHDB_HOST (default 127.0.0.1:8787) using
a throwaway app key, which is registered in setUp and cascade-deleted in
tearDown — the real `crew` app and the other tenants are never touched.

    python3 -m unittest tests.test_graphstore   (from the repo root)
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Point the whole stack at an isolated app BEFORE importing the modules that read
# it (config.current_app reads the env live, so this is enough).
TEST_APP = "crew_selftest"
os.environ["CREW_APP"] = TEST_APP

from crew import config, graphstore as gs, identity, schema  # noqa: E402


def setUpModule():
    # Clean slate: drop a leftover test app from a prior crashed run, then create.
    try:
        gs._req("DELETE", f"/app/{TEST_APP}", app=None)
    except gs.GraphError:
        pass
    schema.ensure_schema(TEST_APP)


def tearDownModule():
    try:
        gs._req("DELETE", f"/app/{TEST_APP}", app=None)
    except gs.GraphError:
        pass


class AgentCrud(unittest.TestCase):
    def test_create_and_get_by_name(self):
        a = gs.create_agent("leads", role="finds leads", home="/tmp/crew_x/leads")
        self.assertEqual(a["name"], "leads")
        self.assertEqual(a["role"], "finds leads")
        got = gs.get_agent_by_name("leads")
        self.assertEqual(got["_guid"], a["_guid"])

    def test_bad_name_rejected(self):
        with self.assertRaises(gs.GraphError):
            gs.create_agent("bad name.with/dots")

    def test_duplicate_name_rejected(self):
        gs.create_agent("dupe", home="/tmp/crew_x/dupe")
        with self.assertRaises(gs.GraphError):
            gs.create_agent("dupe", home="/tmp/crew_x/dupe2")

    def test_update_status(self):
        a = gs.create_agent("upd", home="/tmp/crew_x/upd")
        gs.update_agent(a["_guid"], status="working")
        self.assertEqual(gs.get_agent_by_name("upd")["status"], "working")


class HomeUniqueness(unittest.TestCase):
    def test_same_and_nested_conflict_sibling_ok(self):
        gs.create_agent("h1", home="/tmp/crewhomes/app")
        agents = gs.list_agents()
        # exact same dir conflicts
        self.assertIsNotNone(gs.home_conflict("/tmp/crewhomes/app", agents))
        # a child dir conflicts (would live inside h1's tree)
        self.assertIsNotNone(gs.home_conflict("/tmp/crewhomes/app/sub", agents))
        # a parent dir conflicts (h1 would live inside it)
        self.assertIsNotNone(gs.home_conflict("/tmp/crewhomes", agents))
        # a sibling is fine
        self.assertIsNone(gs.home_conflict("/tmp/crewhomes/app2", agents))
        # a lookalike prefix is NOT nesting (/app vs /app2)
        self.assertIsNone(gs.home_conflict("/tmp/crewhomes/app-other", agents))


class MessagingGate(unittest.TestCase):
    def _pair(self, n1, n2):
        a = gs.create_agent(n1, home=f"/tmp/crew_g/{n1}")
        b = gs.create_agent(n2, home=f"/tmp/crew_g/{n2}")
        return a, b

    def test_directed_edge_one_way(self):
        a, b = self._pair("d_a", "d_b")
        gs.create_edge(a["_guid"], b["_guid"], label="a->b",
                       condition="when you have a lead", directed=True)
        self.assertTrue(gs.can_message("d_a", "d_b"))   # along the edge
        self.assertFalse(gs.can_message("d_b", "d_a"))  # against a DIRECTED edge

    def test_undirected_edge_both_ways(self):
        a, b = self._pair("u_a", "u_b")
        gs.create_edge(a["_guid"], b["_guid"], directed=False)
        self.assertTrue(gs.can_message("u_a", "u_b"))
        self.assertTrue(gs.can_message("u_b", "u_a"))

    def test_unconnected_blocked(self):
        self._pair("x_a", "x_b")
        self.assertFalse(gs.can_message("x_a", "x_b"))

    def test_unknown_agent_blocked(self):
        gs.create_agent("solo", home="/tmp/crew_g/solo")
        self.assertFalse(gs.can_message("solo", "ghost"))
        self.assertFalse(gs.can_message("ghost", "solo"))

    def test_no_self_edge(self):
        a = gs.create_agent("selfish", home="/tmp/crew_g/selfish")
        with self.assertRaises(gs.GraphError):
            gs.create_edge(a["_guid"], a["_guid"])

    def test_messageable_targets_directed_and_undirected(self):
        a = gs.create_agent("m_a", home="/tmp/crew_m/a")
        b = gs.create_agent("m_b", home="/tmp/crew_m/b")
        c = gs.create_agent("m_c", home="/tmp/crew_m/c")
        gs.create_edge(a["_guid"], b["_guid"], directed=True)   # a may msg b
        gs.create_edge(c["_guid"], a["_guid"], directed=False)  # a<->c may msg
        targets = {g for g, _ in gs.messageable_targets(a["_guid"])}
        self.assertEqual(targets, {b["_guid"], c["_guid"]})

    def test_delete_agent_cascades_edges(self):
        a, b = self._pair("del_a", "del_b")
        gs.create_edge(a["_guid"], b["_guid"], directed=False)
        gs.delete_agent(a["_guid"])
        self.assertFalse(gs.can_message("del_b", "del_a"))
        self.assertEqual(gs.edges_touching(b["_guid"]), [])


class EdgeContractAndMessageLog(unittest.TestCase):
    """The enriched (two-sided) edge + the durable message log that makes delivery
    observable and powers the flusher + max_turns."""

    def test_edge_stores_receiver_contract(self):
        a = gs.create_agent("ec_a", home="/tmp/crew_ec/a")
        b = gs.create_agent("ec_b", home="/tmp/crew_ec/b")
        gs.create_edge(a["_guid"], b["_guid"], condition="when ready",
                       target_action="do the thing", reply_expected=True, max_turns=5)
        e = gs.edges_from_to(a["_guid"], b["_guid"])[0]
        self.assertEqual(e["target_action"], "do the thing")
        self.assertTrue(e["reply_expected"])
        self.assertEqual(int(e["max_turns"]), 5)

    def test_incoming_edges(self):
        a = gs.create_agent("in_a", home="/tmp/crew_in/a")
        b = gs.create_agent("in_b", home="/tmp/crew_in/b")
        gs.create_edge(a["_guid"], b["_guid"], directed=True)
        inc = gs.incoming_edges(b["_guid"])
        self.assertEqual([g for g, _ in inc], [a["_guid"]])
        self.assertEqual(gs.incoming_edges(a["_guid"]), [])  # directed: a has none

    def test_message_log_lifecycle(self):
        m = gs.create_message("ml_a", "ml_b", "hi", status="queued")
        self.assertEqual(m["status"], "queued")
        queued = [x for x in gs.list_messages(status="queued") if x["_guid"] == m["_guid"]]
        self.assertEqual(len(queued), 1)
        gs.mark_message(m["_guid"], "delivered", delivered=True)
        again = gs.get_object(m["_guid"])
        self.assertEqual(again["status"], "delivered")
        self.assertGreater(int(again["delivered_at"]), 0)

    def test_recent_message_count(self):
        gs.create_message("rc_a", "rc_b", "1")
        gs.create_message("rc_a", "rc_b", "2")
        self.assertEqual(gs.recent_message_count("rc_a", "rc_b", 0), 2)
        # a far-future floor excludes them
        self.assertEqual(gs.recent_message_count("rc_a", "rc_b", 9999999999), 0)


class IdentityRender(unittest.TestCase):
    def test_lists_neighbors_and_condition(self):
        agent = {"name": "leads", "role": "finds leads",
                 "identity": "I hunt for businesses with no website.",
                 "home": "/tmp/crew_id/leads"}
        nb = ({"name": "builder", "role": "builds sites"},
              {"condition": "when a qualified lead is found",
               "description": "leads hands builder the lead to build a demo"})
        md = identity.render_identity_md(agent, [nb])
        self.assertIn("# Identity: leads", md)
        self.assertIn("builder", md)
        self.assertIn("when a qualified lead is found", md)
        self.assertIn("/tmp/crew_id/leads", md)
        self.assertIn("crew message", md)

    def test_no_neighbors_states_isolation(self):
        md = identity.render_identity_md({"name": "lonely", "home": "/x"}, [])
        self.assertIn("no one to message", md.lower())

    def test_renders_both_sides_of_relationship(self):
        agent = {"name": "builder", "role": "builds sites", "home": "/tmp/crew_id/builder"}
        outgoing = ({"name": "sales", "role": "books calls"},
                    {"condition": "when a demo is ready", "reply_expected": True,
                     "max_turns": 3})
        incoming = ({"name": "leads", "role": "finds leads"},
                    {"target_action": "build a one-page demo and reply with the URL",
                     "reply_expected": True})
        md = identity.render_identity_md(agent, [outgoing], [incoming])
        # outgoing trigger + reply + turn cap
        self.assertIn("when a demo is ready", md)
        self.assertIn("they will reply", md)
        self.assertIn("3 message", md)
        # incoming receiver-obligation (the half that used to be missing)
        self.assertIn("When these agents message you", md)
        self.assertIn("build a one-page demo and reply with the URL", md)
        self.assertIn("progress.md", md)   # durable work-state guidance

    def test_spawn_context_points_at_file(self):
        ctx = identity.render_spawn_context(
            {"name": "leads", "home": "/tmp/crew_id/leads"}, [])
        self.assertIn("identity.md", ctx)
        self.assertIn("leads", ctx)


class ClaudeMdNativeIdentity(unittest.TestCase):
    """CLAUDE.md is the NATIVE hand-off — claude auto-loads it every session start,
    so identity arrives with zero send-keys race. It must carry the load-bearing
    facts and must never clobber a user's own CLAUDE.md content."""

    def test_renders_core_identity_and_peers(self):
        agent = {"name": "leads", "role": "finds leads",
                 "identity": "I hunt businesses with no website.",
                 "home": "/tmp/crew_cm/leads"}
        nb = ({"name": "builder", "role": "builds sites"},
              {"condition": "when a lead is qualified",
               "description": "leads hands builder the lead"})
        md = identity.render_claude_md(agent, [nb])
        self.assertIn("Crew agent: leads", md)
        self.assertIn("builder", md)
        self.assertIn("when a lead is qualified", md)
        self.assertIn("/tmp/crew_cm/leads", md)
        self.assertIn("crew message", md)
        self.assertIn("identity.md", md)   # points at the full record

    def test_no_neighbors_states_isolation(self):
        md = identity.render_claude_md({"name": "solo", "home": "/x"}, [])
        self.assertIn("no one to message", md.lower())

    def test_merge_replaces_block_preserves_user_content(self):
        user = "# My project notes\nUse tabs, not spaces.\n"
        first = identity._merge_managed_block(user, "BLOCK ONE")
        # user content survives, managed block present
        self.assertIn("My project notes", first)
        self.assertIn("BLOCK ONE", first)
        self.assertIn(identity.CREW_BLOCK_BEGIN, first)
        # re-rendering swaps ONLY the managed block; user notes stay, no dup block
        second = identity._merge_managed_block(first, "BLOCK TWO")
        self.assertIn("My project notes", second)
        self.assertIn("BLOCK TWO", second)
        self.assertNotIn("BLOCK ONE", second)
        self.assertEqual(second.count(identity.CREW_BLOCK_BEGIN), 1)

    def test_write_claude_md_roundtrip(self):
        import tempfile
        d = tempfile.mkdtemp(prefix="crew_cm_")
        agent = {"name": "w", "role": "r", "home": d}
        path = identity.write_claude_md(d, identity.render_claude_md(agent, []))
        self.assertTrue(path.endswith("CLAUDE.md"))
        with open(path) as f:
            body = f.read()
        self.assertIn("Crew agent: w", body)
        self.assertIn(identity.CREW_BLOCK_BEGIN, body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
