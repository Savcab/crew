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
        self.assertIn("no connections", md.lower())

    def test_spawn_context_points_at_file(self):
        ctx = identity.render_spawn_context(
            {"name": "leads", "home": "/tmp/crew_id/leads"}, [])
        self.assertIn("identity.md", ctx)
        self.assertIn("leads", ctx)


if __name__ == "__main__":
    unittest.main(verbosity=2)
