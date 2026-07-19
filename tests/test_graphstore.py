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
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_APP = os.environ.get("CREW_TEST_APP", "crew_selftest")

from crew import config, graphstore as gs, identity, schema  # noqa: E402

_CREW_APP_PATCHER = None


def setUpModule():
    # Scope the selector to this module's execution. Import-time mutations leak
    # deleted tenants into later modules because discovery imports the suite
    # before it executes module cleanup.
    global _CREW_APP_PATCHER
    _CREW_APP_PATCHER = mock.patch.dict(os.environ, {"CREW_APP": TEST_APP})
    _CREW_APP_PATCHER.start()
    unittest.addModuleCleanup(_CREW_APP_PATCHER.stop)
    # Clean slate: drop a leftover test app from a prior crashed run, then create.
    try:
        gs._req("DELETE", f"/app/{TEST_APP}", app=None)
    except gs.GraphError:
        pass
    schema.ensure_schema(TEST_APP)


def tearDownModule():
    try:
        try:
            gs._req("DELETE", f"/app/{TEST_APP}", app=None)
        except gs.GraphError:
            pass
    finally:
        _CREW_APP_PATCHER.stop()


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
                       target_action="do the thing", reply_expected=True,
                       directed=False, max_turns=5)
        e = gs.edges_from_to(a["_guid"], b["_guid"])[0]
        self.assertEqual(e["target_action"], "do the thing")
        self.assertTrue(e["reply_expected"])
        self.assertEqual(int(e["max_turns"]), 5)

    def test_reply_expected_requires_a_two_way_edge(self):
        """A receiver cannot be told to reply across a one-way authorization.

        The graph contract must reject that contradictory state instead of
        rendering an instruction the mail gate will subsequently block.
        """
        a = gs.create_agent("reply_a", home="/tmp/crew_reply/a")
        b = gs.create_agent("reply_b", home="/tmp/crew_reply/b")
        with self.assertRaisesRegex(gs.GraphError, "reply.*two-way|two-way.*reply"):
            gs.create_edge(a["_guid"], b["_guid"], directed=True,
                           reply_expected=True)
        self.assertEqual(gs.edges_from_to(a["_guid"], b["_guid"]), [])

    def test_edge_update_validates_the_merged_reply_contract_atomically(self):
        a = gs.create_agent("reply_up_a", home="/tmp/crew_reply/up_a")
        b = gs.create_agent("reply_up_b", home="/tmp/crew_reply/up_b")
        edge = gs.create_edge(a["_guid"], b["_guid"], directed=True)
        with self.assertRaisesRegex(gs.GraphError, "reply.*two-way|two-way.*reply"):
            gs.update_edge(edge["_guid"], {"reply_expected": True})
        updated = gs.update_edge(edge["_guid"], {
            "directed": False, "reply_expected": True,
        })
        self.assertFalse(updated["directed"])
        self.assertTrue(updated["reply_expected"])
        with self.assertRaisesRegex(gs.GraphError, "reply.*two-way|two-way.*reply"):
            gs.update_edge(edge["_guid"], {"directed": True})

    def test_non_finite_cost_caps_are_rejected_before_create_or_update(self):
        a = gs.create_agent("finite_cap_a", home="/tmp/crew_finite_cap/a")
        b = gs.create_agent("finite_cap_b", home="/tmp/crew_finite_cap/b")
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(path="create", value=value), self.assertRaisesRegex(
                    gs.GraphError, "finite"):
                gs.create_edge(a["_guid"], b["_guid"], cost_cap=value)
        self.assertEqual(gs.edges_from_to(a["_guid"], b["_guid"]), [])

        edge = gs.create_edge(a["_guid"], b["_guid"], cost_cap=1.0)
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(path="update", value=value), self.assertRaisesRegex(
                    gs.GraphError, "finite"):
                gs.update_edge(edge["_guid"], {"cost_cap": value})
            self.assertEqual(float(gs.get_object(edge["_guid"])["cost_cap"]), 1.0)

    def test_non_finite_cost_caps_never_reach_the_persistence_writer(self):
        current = {
            "_guid": "edge_finite_boundary", "source": "agent_a",
            "target": "agent_b", "directed": True,
            "reply_expected": False, "back_reply": False, "cost_cap": 1.0,
        }
        with mock.patch.object(gs, "list_edges", return_value=[]), \
             mock.patch.object(gs, "create_object", return_value={}) as create:
            with self.assertRaisesRegex(gs.GraphError, "finite"):
                gs.create_edge("agent_a", "agent_b", cost_cap=float("nan"))
        create.assert_not_called()

        with mock.patch.object(gs, "get_object", return_value=current), \
             mock.patch.object(gs, "list_edges", return_value=[]), \
             mock.patch.object(gs, "patch_object", return_value=current) as patch:
            with self.assertRaisesRegex(gs.GraphError, "finite"):
                gs.update_edge(
                    "edge_finite_boundary", {"cost_cap": float("inf")})
        patch.assert_not_called()

    def test_negative_caps_are_rejected_before_the_persistence_writer(self):
        current = {
            "_guid": "edge_nonnegative_boundary", "source": "agent_a",
            "target": "agent_b", "directed": True,
            "reply_expected": False, "back_reply": False,
            "max_turns": 1, "token_cap": 1, "cost_cap": 1.0,
        }
        for field, value in (
                ("max_turns", -1), ("token_cap", -1), ("cost_cap", -0.01)):
            with self.subTest(path="create", field=field), \
                 mock.patch.object(gs, "list_edges", return_value=[]), \
                 mock.patch.object(gs, "create_object", return_value={}) as create, \
                 self.assertRaisesRegex(gs.GraphError, "zero|positive"):
                gs.create_edge("agent_a", "agent_b", **{field: value})
            create.assert_not_called()

            with self.subTest(path="update", field=field), \
                 mock.patch.object(gs, "get_object", return_value=current), \
                 mock.patch.object(gs, "list_edges", return_value=[]), \
                 mock.patch.object(gs, "patch_object", return_value=current) as patch, \
                 self.assertRaisesRegex(gs.GraphError, "zero|positive"):
                gs.update_edge(
                    "edge_nonnegative_boundary", {field: value})
            patch.assert_not_called()

    def test_boolean_caps_are_rejected_before_the_persistence_writer(self):
        """Python bool is an int subclass, but JSON true/false is not a cap."""
        for field in ("max_turns", "token_cap", "cost_cap"):
            for value in (True, False):
                with self.subTest(field=field, value=value), \
                     mock.patch.object(gs, "list_edges", return_value=[]), \
                     mock.patch.object(gs, "create_object") as create, \
                     self.assertRaisesRegex(gs.GraphError, "boolean|number"):
                    gs.create_edge(
                        "agent_a", "agent_b", **{field: value})
                create.assert_not_called()

    def test_null_empty_and_fractional_integer_caps_are_rejected(self):
        cases = (
            ("max_turns", None), ("token_cap", None), ("cost_cap", None),
            ("max_turns", ""), ("token_cap", ""), ("cost_cap", ""),
            ("max_turns", 1.5), ("token_cap", 1.5),
            ("max_turns", "1.5"), ("token_cap", "1.5"),
        )
        for field, value in cases:
            with self.subTest(field=field, value=value), \
                 mock.patch.object(gs, "list_edges", return_value=[]), \
                 mock.patch.object(gs, "create_object") as create, \
                 self.assertRaisesRegex(gs.GraphError, "number|integer"):
                gs.create_edge(
                    "agent_a", "agent_b", **{field: value})
            create.assert_not_called()

    def test_overlapping_authorization_edges_are_rejected(self):
        a = gs.create_agent("dupe_edge_a", home="/tmp/crew_dupe_edge/a")
        b = gs.create_agent("dupe_edge_b", home="/tmp/crew_dupe_edge/b")
        gs.create_edge(a["_guid"], b["_guid"], directed=True)
        with self.assertRaisesRegex(gs.GraphError, "already.*authoriz|overlap|duplicate"):
            gs.create_edge(a["_guid"], b["_guid"], directed=True)
        with self.assertRaisesRegex(gs.GraphError, "already.*authoriz|overlap|duplicate"):
            gs.create_edge(a["_guid"], b["_guid"], directed=False)
        # Opposite one-way links are distinct authorizations and remain valid.
        gs.create_edge(b["_guid"], a["_guid"], directed=True)
        self.assertEqual(len(gs.edges_touching(a["_guid"])), 2)

    def test_direction_update_cannot_create_overlapping_authorizations(self):
        a = gs.create_agent("dupe_up_a", home="/tmp/crew_dupe_up/a")
        b = gs.create_agent("dupe_up_b", home="/tmp/crew_dupe_up/b")
        first = gs.create_edge(a["_guid"], b["_guid"], directed=True)
        second = gs.create_edge(b["_guid"], a["_guid"], directed=True)
        with self.assertRaisesRegex(gs.GraphError, "already.*authoriz|overlap|duplicate"):
            gs.update_edge(first["_guid"], {"directed": False})
        self.assertTrue(gs.get_object(first["_guid"])["directed"])
        self.assertTrue(gs.get_object(second["_guid"])["directed"])

    def test_legacy_duplicate_authorizations_fail_closed(self):
        """Pre-invariant rows (or a cross-process race) must not be selected
        according to backend return order."""
        a = gs.create_agent("legacy_dupe_a", home="/tmp/crew_legacy_dupe/a")
        b = gs.create_agent("legacy_dupe_b", home="/tmp/crew_legacy_dupe/b")
        base = {
            "source": a["_guid"], "target": b["_guid"], "label": "legacy",
            "description": "", "conditions": [], "condition": "",
            "target_action": "", "reply_expected": False,
            "back_conditions": [], "back_action": "", "back_reply": False,
            "max_turns": 0, "token_cap": 0, "cost_cap": 0,
            "directed": True, "transform": "", "created_at": 1,
            "created_by": "human", "blessed": True,
        }
        gs.create_object("edge", dict(base))
        gs.create_object("edge", dict(base, label="legacy duplicate", created_at=2))
        with self.assertRaisesRegex(gs.GraphError, "ambiguous|multiple|duplicate"):
            gs.authorizing_edge("legacy_dupe_a", "legacy_dupe_b")
        with self.assertRaises(gs.GraphError):
            gs.can_message("legacy_dupe_a", "legacy_dupe_b")

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


class WorkingStatusDetection(unittest.TestCase):
    """detect_status must recognize Claude Code v2.1.185's 'working' UI, which does
    NOT always print 'esc to interrupt' and rotates non-'-ing' spinner words — the
    bug that let messages be typed mid-generation."""

    def setUp(self):
        from crew.server import tmuxio
        self.detect = tmuxio.detect_status

    def test_spinner_word_with_ellipsis(self):
        self.assertEqual(self.detect("\n✽ Booping…\n"), "working")
        self.assertEqual(self.detect("\n✻ Cogitating…\n"), "working")
        self.assertEqual(self.detect("\n· Churning…\n"), "working")

    def test_elapsed_time_status(self):
        self.assertEqual(self.detect("✻ Booping… (2s · thinking with high effort)"), "working")
        self.assertEqual(self.detect("(15s · 1.2k tokens · esc to interrupt)"), "working")

    def test_legacy_interrupt_hint(self):
        self.assertEqual(self.detect("doing things… esc to interrupt"), "working")

    def test_idle_prompt(self):
        self.assertEqual(self.detect("───────\n❯ \n───────\n  ? for shortcuts"), "idle")

    def test_needs_input_menu(self):
        self.assertEqual(self.detect("Do you want to proceed?\n❯ 1. Yes\n  2. No"), "needs_input")


if __name__ == "__main__":
    unittest.main(verbosity=2)
