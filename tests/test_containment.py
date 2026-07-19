"""WAVE 2 tests: containment — envelope (connect/disconnect), spawn confinement
(agent count + hourly rate), finite-caps rule, downhill-only cap updates
(extended to foreman), the foreman-touch rule (up/down only own creations),
spawn.py's home/repo/launch_cmd confinement, and the `crew cap` / `crew note`
verbs.

Three layers, per SKILL.md:
  * unit — mostly a throwaway MorphDB app (`crewtest-containment-unit`); the
    agent-count and spawn-rate confinement tests get their OWN dedicated
    throwaway apps (crewtest-containment-count / -rate) so their exact counts
    can't be polluted by any other test in this file running in the same app.
  * live — a throwaway project ("w2test", its own MorphDB app "crew-w2test"),
    never touching the real 5-agent "crew" app. Core acceptance: a real
    foreman's real tmux pane runs `crew spawn-agent ... --home /tmp/evil` and
    is refused (home confinement), then spawns properly, connects with caps,
    and is refused connecting to a human-made node — all typed into the
    foreman's OWN pane via tmux send-keys, exactly as the agent would.
  * regression — the full suite (run separately: `python3 -m unittest
    discover tests`).

    python3 -m unittest tests.test_containment          (from the repo root)
    python3 -m unittest discover tests                   (full suite)
"""
import contextlib
import io
import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_APP = "crewtest-containment-unit"

from crew import cli, config, graphstore as gs, guard, schema, spawn  # noqa: E402


_orig_max_agents = None
_orig_spawn_rate = None
_CREW_APP_PATCHER = None


def setUpModule():
    # Re-pin at RUN time (see test_guard.py's comment — a module discovered
    # later that repins the env mid-run must not inherit a leaked pin from us,
    # nor should we inherit one from an earlier module).
    global _CREW_APP_PATCHER
    _CREW_APP_PATCHER = mock.patch.dict(os.environ, {"CREW_APP": TEST_APP})
    _CREW_APP_PATCHER.start()
    unittest.addModuleCleanup(_CREW_APP_PATCHER.stop)
    try:
        gs._req("DELETE", f"/app/{TEST_APP}", app=None)
    except gs.GraphError:
        pass
    schema.ensure_schema(TEST_APP)
    # Most classes below use an agent-actor spawn purely as FIXTURE setup for
    # envelope/finite-caps/cap/foreman-touch tests, not to exercise the
    # count/rate ceiling itself — raise both sky-high here so shared-app
    # fixtures never trip them. SpawnCountConfinementTests /
    # SpawnRateConfinementTests get their OWN throwaway apps AND patch these
    # back down to a small, exact number for their own tests.
    global _orig_max_agents, _orig_spawn_rate
    _orig_max_agents, _orig_spawn_rate = config.MAX_AGENTS, config.SPAWN_RATE
    config.MAX_AGENTS = 10_000
    config.SPAWN_RATE = 10_000


def tearDownModule():
    try:
        config.MAX_AGENTS, config.SPAWN_RATE = _orig_max_agents, _orig_spawn_rate
        try:
            gs._req("DELETE", f"/app/{TEST_APP}", app=None)
        except gs.GraphError:
            pass
    finally:
        _CREW_APP_PATCHER.stop()


def _audit_rows(actor=None, op=None):
    res = gs.list_objects("graph_edit", limit=1000, sort="created_at", order="desc")
    rows = (res or {}).get("objects", [])
    if actor is not None:
        rows = [r for r in rows if r.get("actor") == actor]
    if op is not None:
        rows = [r for r in rows if r.get("op") == op]
    return rows


def _foreman(name):
    # Module cases share one app; honor the product singleton by retiring the
    # preceding test's foreman before constructing this test's actor.
    for current in gs.list_agents():
        if current.get("can_edit_graph"):
            gs.set_foreman(current["_guid"], revoke=True, actor="human")
    return gs.create_agent(name, home=f"/tmp/crew_containtest/{name}",
                           can_edit_graph=True)


class ImmutableOwnershipTests(unittest.TestCase):
    def test_schema_upgrade_backfills_unambiguous_legacy_creator_guids(self):
        foreman = _foreman("guid_migrate_f")
        self.addCleanup(
            gs.patch_object, "agent", foreman["_guid"],
            {"can_edit_graph": False})
        child = gs.create_agent(
            "guid_migrate_child",
            home="/tmp/crew_containtest/guid_migrate_child",
            actor=foreman["name"])
        peer = gs.create_agent(
            "guid_migrate_peer",
            home="/tmp/crew_containtest/guid_migrate_peer",
            actor=foreman["name"])
        edge = gs.create_edge(
            child["_guid"], peer["_guid"], actor=foreman["name"],
            max_turns=5, token_cap=1000, cost_cap=1.0)
        gs.patch_object("agent", foreman["_guid"], {"created_at": 100})
        gs.patch_object(
            "agent", child["_guid"], {
                "created_by_guid": "", "created_at": 200})
        gs.patch_object(
            "edge", edge["_guid"], {
                "created_by_guid": "", "created_at": 300})

        schema.ensure_schema(TEST_APP)

        self.assertEqual(
            gs.get_object(child["_guid"])["created_by_guid"],
            foreman["_guid"])
        self.assertEqual(
            gs.get_object(edge["_guid"])["created_by_guid"],
            foreman["_guid"])

    def test_schema_upgrade_does_not_bind_legacy_rows_to_newer_name_reuse(self):
        name = "guid_migrate_reused_f"
        original = _foreman(name)
        child = gs.create_agent(
            "guid_migrate_reused_child",
            home="/tmp/crew_containtest/guid_migrate_reused_child",
            actor=name)
        gs.patch_object("agent", child["_guid"], {
            "created_by_guid": "", "created_at": 100,
        })
        gs.delete_object("agent", original["_guid"])
        replacement = _foreman(name)
        self.addCleanup(
            gs.patch_object, "agent", replacement["_guid"],
            {"can_edit_graph": False})

        schema.ensure_schema(TEST_APP)

        self.assertEqual(
            gs.get_object(child["_guid"]).get("created_by_guid") or "", "")

    def test_agent_and_edge_ownership_stamp_creator_guid(self):
        foreman = _foreman("guid_owner_stamp_f")
        self.addCleanup(
            gs.patch_object, "agent", foreman["_guid"],
            {"can_edit_graph": False})
        child = gs.create_agent(
            "guid_owner_stamp_child",
            home="/tmp/crew_containtest/guid_owner_stamp_child",
            actor=foreman["name"])
        peer = gs.create_agent(
            "guid_owner_stamp_peer",
            home="/tmp/crew_containtest/guid_owner_stamp_peer",
            actor=foreman["name"])
        edge = gs.create_edge(
            child["_guid"], peer["_guid"], actor=foreman["name"],
            max_turns=5, token_cap=1000, cost_cap=1.0)
        self.assertEqual(child.get("created_by_guid"), foreman["_guid"])
        self.assertEqual(edge.get("created_by_guid"), foreman["_guid"])

    def test_recreated_foreman_name_does_not_inherit_children_or_edges(self):
        name = "guid_takeover_f"
        original = _foreman(name)
        child = gs.create_agent(
            "guid_takeover_child",
            home="/tmp/crew_containtest/guid_takeover_child", actor=name)
        peer = gs.create_agent(
            "guid_takeover_peer",
            home="/tmp/crew_containtest/guid_takeover_peer", actor=name)
        edge = gs.create_edge(
            child["_guid"], peer["_guid"], actor=name,
            max_turns=5, token_cap=1000, cost_cap=1.0)

        gs.delete_object("agent", original["_guid"])
        replacement = _foreman(name)
        self.addCleanup(
            gs.patch_object, "agent", replacement["_guid"],
            {"can_edit_graph": False})
        self.assertNotEqual(replacement["_guid"], original["_guid"])

        with self.assertRaisesRegex(gs.GraphError, "belongs|created|ask"):
            gs.update_agent(
                child["_guid"], role="hijacked", actor=replacement["name"])
        with self.assertRaisesRegex(gs.GraphError, "ask|drawn|envelope"):
            gs.delete_edge(edge["_guid"], actor=replacement["name"])
        self.assertNotEqual(gs.get_object(child["_guid"])["role"], "hijacked")
        self.assertEqual(gs.get_object(edge["_guid"])["_guid"], edge["_guid"])

    def test_pending_requester_guid_refuses_recreated_foreman_name(self):
        name = "guid_pending_f"
        original = _foreman(name)
        child = gs.create_agent(
            "guid_pending_child",
            home="/tmp/crew_containtest/guid_pending_child", actor=name)
        outsider = gs.create_agent(
            "guid_pending_human",
            home="/tmp/crew_containtest/guid_pending_human")
        with self.assertRaisesRegex(gs.GraphError, "queued"):
            gs.create_edge(
                child["_guid"], outsider["_guid"], actor=name,
                max_turns=5, token_cap=1000, cost_cap=1.0)
        rows = [row for row in _audit_rows(actor=name, op="connect")
                if row.get("result") == "pending"]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.get("actor_guid"), original["_guid"])

        gs.delete_object("agent", original["_guid"])
        replacement = _foreman(name)
        self.addCleanup(
            gs.patch_object, "agent", replacement["_guid"],
            {"can_edit_graph": False})
        self.assertNotEqual(replacement["_guid"], original["_guid"])
        with self.assertRaisesRegex(gs.GraphError, "requester|stale|identity"):
            guard.approve_pending(row["_guid"], actor="human")
        self.assertEqual(gs.get_object(row["_guid"])["result"], "pending")
        self.assertEqual(
            gs.edges_from_to(child["_guid"], outsider["_guid"]), [])


# --------------------------------------------------------------------------- #
# unit — envelope (connect)
# --------------------------------------------------------------------------- #
class EnvelopeConnectTests(unittest.TestCase):
    def test_connect_outside_envelope_to_human_node_queued_pending_not_refused(self):
        # WAVE 4: an out-of-envelope endpoint created_by "human" no longer
        # hard-refuses — it routes to the pending-approval queue instead (case
        # (a) of the wave-4 spec). See tests/test_pending.py for the full
        # pending-queue matrix (approve/reject/notice/CLI); this just confirms
        # the wave-2 envelope check's outcome for this exact case changed.
        f = _foreman("env_f1")
        outsider = gs.create_agent("env_outsider1", home="/tmp/crew_containtest/env_outsider1")
        with self.assertRaises(gs.GraphError) as ctx:
            gs.create_edge(f["_guid"], outsider["_guid"], actor="env_f1",
                          max_turns=5, token_cap=1000, cost_cap=1.0)
        self.assertIn("queued", str(ctx.exception).lower())
        rows = _audit_rows(actor="env_f1", op="connect")
        self.assertTrue(any(r.get("result") == "pending" for r in rows))
        self.assertFalse(any(r.get("result") == "refused" for r in rows))

    def test_connect_inside_envelope_with_finite_caps_applied_unblessed(self):
        f = _foreman("env_f2")
        kid = gs.create_agent("env_kid2", home="/tmp/crew_containtest/env_kid2", actor="env_f2")
        e = gs.create_edge(f["_guid"], kid["_guid"], actor="env_f2",
                          max_turns=5, token_cap=1000, cost_cap=1.0)
        self.assertFalse(e["blessed"])
        self.assertEqual(e["created_by"], "env_f2")
        self.assertEqual(e["max_turns"], 5)
        rows = _audit_rows(actor="env_f2", op="connect")
        self.assertTrue(any(r.get("result") == "applied" for r in rows))


class EnvelopeDisconnectTests(unittest.TestCase):
    def test_disconnect_edge_not_created_by_foreman_refused(self):
        f = _foreman("disc_f1")
        kid = gs.create_agent("disc_kid1", home="/tmp/crew_containtest/disc_kid1", actor="disc_f1")
        e = gs.create_edge(f["_guid"], kid["_guid"], actor="human")
        with self.assertRaises(gs.GraphError):
            gs.delete_edge(e["_guid"], actor="disc_f1")

    def test_disconnect_own_edge_inside_envelope_ok(self):
        f = _foreman("disc_f2")
        kid = gs.create_agent("disc_kid2", home="/tmp/crew_containtest/disc_kid2", actor="disc_f2")
        e = gs.create_edge(f["_guid"], kid["_guid"], actor="disc_f2",
                          max_turns=5, token_cap=1000, cost_cap=1.0)
        gs.delete_edge(e["_guid"], actor="disc_f2")  # must not raise

    def test_cli_disconnect_mixed_ownership_is_all_or_nothing_in_both_orders(self):
        parser = cli.build_parser()
        for suffix, reverse_args in (("a", False), ("b", True)):
            with self.subTest(reverse_args=reverse_args):
                foreman_name = f"disc_batch_f_{suffix}"
                child_name = f"disc_batch_k_{suffix}"
                f = _foreman(foreman_name)
                kid = gs.create_agent(
                    child_name,
                    home=f"/tmp/crew_containtest/{child_name}",
                    actor=foreman_name)
                own = gs.create_edge(
                    f["_guid"], kid["_guid"], actor=foreman_name,
                    max_turns=5, token_cap=1000, cost_cap=1.0)
                human = gs.create_edge(kid["_guid"], f["_guid"], actor="human")
                names = ([child_name, foreman_name] if reverse_args
                         else [foreman_name, child_name])

                with mock.patch.object(cli, "_ACTOR", foreman_name), \
                     mock.patch.object(spawn, "rewrite_identity") as rewrite, \
                     self.assertRaises(gs.GraphError):
                    args = parser.parse_args(["disconnect", *names])
                    args.fn(args)

                remaining = {
                    e["_guid"]
                    for e in (gs.edges_from_to(f["_guid"], kid["_guid"])
                              + gs.edges_from_to(kid["_guid"], f["_guid"]))
                }
                self.assertEqual(remaining, {own["_guid"], human["_guid"]})
                rewrite.assert_not_called()
                rows = _audit_rows(actor=foreman_name, op="disconnect")
                self.assertTrue(any(r.get("result") == "refused" for r in rows))
                self.assertFalse(any(r.get("result") == "applied" for r in rows))

    def test_cli_disconnect_fully_owned_batch_deletes_both_then_rewrites(self):
        foreman_name = "disc_batch_ok_f"
        child_name = "disc_batch_ok_k"
        f = _foreman(foreman_name)
        kid = gs.create_agent(
            child_name, home=f"/tmp/crew_containtest/{child_name}",
            actor=foreman_name)
        for source, target in ((f, kid), (kid, f)):
            gs.create_edge(
                source["_guid"], target["_guid"], actor=foreman_name,
                max_turns=5, token_cap=1000, cost_cap=1.0)

        parser = cli.build_parser()
        with mock.patch.object(cli, "_ACTOR", foreman_name), \
             mock.patch.object(spawn, "rewrite_identity") as rewrite:
            args = parser.parse_args([
                "disconnect", foreman_name, child_name])
            self.assertEqual(args.fn(args), 0)

        self.assertEqual(gs.edges_from_to(f["_guid"], kid["_guid"]), [])
        self.assertEqual(gs.edges_from_to(kid["_guid"], f["_guid"]), [])
        self.assertEqual(rewrite.call_count, 2)


# --------------------------------------------------------------------------- #
# unit — finite-caps rule (connect by agent actor)
# --------------------------------------------------------------------------- #
class FiniteCapsTests(unittest.TestCase):
    def _pair(self, prefix):
        f = _foreman(f"{prefix}_f")
        kid = gs.create_agent(f"{prefix}_kid", home=f"/tmp/crew_containtest/{prefix}_kid",
                              actor=f"{prefix}_f")
        return f, kid

    def test_missing_caps_refused(self):
        f, kid = self._pair("fc_missing")
        with self.assertRaises(gs.GraphError) as ctx:
            gs.create_edge(f["_guid"], kid["_guid"], actor="fc_missing_f")
        self.assertIn("finite", str(ctx.exception))

    def test_zero_cap_refused(self):
        f, kid = self._pair("fc_zero")
        with self.assertRaises(gs.GraphError) as ctx:
            gs.create_edge(f["_guid"], kid["_guid"], actor="fc_zero_f",
                          max_turns=0, token_cap=1000, cost_cap=1.0)
        self.assertIn("finite", str(ctx.exception))

    def test_over_ceiling_cap_refused(self):
        f, kid = self._pair("fc_over")
        with self.assertRaises(gs.GraphError) as ctx:
            gs.create_edge(f["_guid"], kid["_guid"], actor="fc_over_f",
                          max_turns=100000, token_cap=1000, cost_cap=1.0)
        self.assertIn(str(config.AGENT_EDGE_MAX_TURNS_CEILING), str(ctx.exception))


# --------------------------------------------------------------------------- #
# unit — spawn confinement: agent count ceiling (dedicated app, exact count)
# --------------------------------------------------------------------------- #
class SpawnCountConfinementTests(unittest.TestCase):
    APP = "crewtest-containment-count"

    def setUp(self):
        self._prev = os.environ.get("CREW_APP")
        os.environ["CREW_APP"] = self.APP
        try:
            gs._req("DELETE", f"/app/{self.APP}", app=None)
        except gs.GraphError:
            pass
        schema.ensure_schema(self.APP)
        self._patch = mock.patch.object(config, "MAX_AGENTS", 12)
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def tearDown(self):
        try:
            gs._req("DELETE", f"/app/{self.APP}", app=None)
        except gs.GraphError:
            pass
        if self._prev is None:
            os.environ.pop("CREW_APP", None)
        else:
            os.environ["CREW_APP"] = self._prev

    def test_13th_agent_spawn_refused_after_seeding_12(self):
        f = _foreman("cnt_f")
        for i in range(11):
            gs.create_agent(f"cnt_seed_{i}", home=f"/tmp/crew_containtest/cnt_seed_{i}")
        self.assertEqual(len(gs.list_agents()), 12, "expected exactly 12 seeded agents")
        with self.assertRaises(gs.GraphError) as ctx:
            gs.create_agent("cnt_13th", home="/tmp/crew_containtest/cnt_13th", actor="cnt_f")
        self.assertIn(str(config.MAX_AGENTS), str(ctx.exception))
        rows = _audit_rows(actor="cnt_f", op="spawn")
        self.assertTrue(any(r.get("result") == "refused" for r in rows))


# --------------------------------------------------------------------------- #
# unit — spawn confinement: hourly rate (dedicated app)
# --------------------------------------------------------------------------- #
class SpawnRateConfinementTests(unittest.TestCase):
    APP = "crewtest-containment-rate"

    def setUp(self):
        self._prev = os.environ.get("CREW_APP")
        os.environ["CREW_APP"] = self.APP
        try:
            gs._req("DELETE", f"/app/{self.APP}", app=None)
        except gs.GraphError:
            pass
        schema.ensure_schema(self.APP)
        self._patch = mock.patch.object(config, "SPAWN_RATE", 4)
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def tearDown(self):
        try:
            gs._req("DELETE", f"/app/{self.APP}", app=None)
        except gs.GraphError:
            pass
        if self._prev is None:
            os.environ.pop("CREW_APP", None)
        else:
            os.environ["CREW_APP"] = self._prev

    def test_5th_agent_spawn_in_hour_refused_human_spawns_unlimited(self):
        f = _foreman("rate_f")
        for i in range(config.SPAWN_RATE):
            gs.create_agent(f"rate_kid_{i}", home=f"/tmp/crew_containtest/rate_kid_{i}",
                            actor="rate_f")
        with self.assertRaises(gs.GraphError) as ctx:
            gs.create_agent("rate_kid_over", home="/tmp/crew_containtest/rate_kid_over",
                            actor="rate_f")
        self.assertIn("hour", str(ctx.exception))
        # human spawns are NEVER rate-limited, even right after the agent-actor
        # window is exhausted
        h = gs.create_agent("rate_human_kid", home="/tmp/crew_containtest/rate_human_kid",
                            actor="human")
        self.assertEqual(h["created_by"], "human")

    def test_refused_spawns_dont_count_toward_rate_window(self):
        f = _foreman("rate_f2")
        for i in range(config.SPAWN_RATE):
            gs.create_agent(f"rate2_kid_{i}", home=f"/tmp/crew_containtest/rate2_kid_{i}",
                            actor="rate_f2")
        # two refused attempts in a row — if refusals counted toward the
        # window, the SECOND would look identical to the first, so this just
        # proves both refuse the same way (neither un-refuses the other)
        for j in range(2):
            with self.assertRaises(gs.GraphError):
                gs.create_agent(f"rate2_bad_{j}", home=f"/tmp/crew_containtest/rate2_bad_{j}",
                                actor="rate_f2")
        rows = _audit_rows(actor="rate_f2", op="spawn")
        refused = [r for r in rows if r.get("result") == "refused"]
        self.assertGreaterEqual(len(refused), 2)


# --------------------------------------------------------------------------- #
# unit — downhill-only cap updates, extended to a foreman
# --------------------------------------------------------------------------- #
class CapDownhillTests(unittest.TestCase):
    def _edge(self, prefix):
        f = _foreman(f"{prefix}_f")
        kid = gs.create_agent(f"{prefix}_kid", home=f"/tmp/crew_containtest/{prefix}_kid",
                              actor=f"{prefix}_f")
        e = gs.create_edge(f["_guid"], kid["_guid"], actor=f"{prefix}_f",
                           max_turns=10, token_cap=1000, cost_cap=1.0)
        return f, kid, e

    def test_lower_ok(self):
        f, kid, e = self._edge("cap_lower")
        out = gs.update_edge(e["_guid"], {"max_turns": 5}, actor="cap_lower_f")
        self.assertEqual(out.get("max_turns"), 5)

    def test_raise_queued_pending_not_refused(self):
        # WAVE 4: a cap raise no longer hard-refuses — it routes to the
        # pending-approval queue instead (case (b) of the wave-4 spec, ANY
        # agent including a foreman). See tests/test_pending.py for the full
        # matrix; this just confirms the wave-2 downhill-only rule's outcome
        # for a raise attempt changed.
        f, kid, e = self._edge("cap_raise")
        with self.assertRaises(gs.GraphError) as ctx:
            gs.update_edge(e["_guid"], {"max_turns": 20}, actor="cap_raise_f")
        self.assertIn("cap raise", str(ctx.exception).lower())
        refreshed = gs.get_object(e["_guid"])
        self.assertEqual(refreshed.get("max_turns"), 10)  # unchanged

    def test_raise_to_zero_also_queued_pending(self):
        f, kid, e = self._edge("cap_zero")
        with self.assertRaises(gs.GraphError) as ctx:
            gs.update_edge(e["_guid"], {"max_turns": 0}, actor="cap_zero_f")
        self.assertIn("cap raise", str(ctx.exception).lower())

    def test_non_finite_cost_cap_is_refused_not_treated_as_a_lowering(self):
        _f, _kid, edge = self._edge("cap_nonfinite")
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value), self.assertRaisesRegex(
                    gs.GraphError, "finite"):
                gs.update_edge(
                    edge["_guid"], {"cost_cap": value}, actor="cap_nonfinite_f")
            self.assertEqual(float(gs.get_object(edge["_guid"])["cost_cap"]), 1.0)
        rows = _audit_rows(actor="cap_nonfinite_f", op="update_edge")
        refused = [r for r in rows if r.get("result") == "refused"]
        pending = [r for r in rows if r.get("result") == "pending"]
        self.assertGreaterEqual(len(refused), 3)
        self.assertEqual(pending, [])

    def test_negative_caps_are_invalid_not_pending_or_applied(self):
        _f, _kid, edge = self._edge("cap_negative")
        for field, value in (
                ("max_turns", -1), ("token_cap", -1), ("cost_cap", -0.01)):
            with self.subTest(field=field), self.assertRaisesRegex(
                    gs.GraphError, "zero|positive"):
                gs.update_edge(
                    edge["_guid"], {field: value}, actor="cap_negative_f")
        rows = _audit_rows(actor="cap_negative_f", op="update_edge")
        self.assertGreaterEqual(
            len([r for r in rows if r.get("result") == "refused"]), 3)
        self.assertEqual([r for r in rows if r.get("result") == "pending"], [])


class ForemanInboundCapTests(unittest.TestCase):
    def test_foreman_cap_raise_on_own_inbound_edge_also_queued_pending(self):
        # WAVE 4: was a hard refusal ("agents may only LOWER caps..."); now
        # queued for approval like any other cap raise (case (b)).
        f = _foreman("inb_f")
        boss = gs.create_agent("inb_boss", home="/tmp/crew_containtest/inb_boss")  # human-made
        e = gs.create_edge(boss["_guid"], f["_guid"], actor="human", max_turns=5)
        with self.assertRaises(gs.GraphError) as ctx:
            gs.update_edge(e["_guid"], {"max_turns": 50}, actor="inb_f")
        self.assertIn("cap raise", str(ctx.exception).lower())


# --------------------------------------------------------------------------- #
# unit — foreman-touch rule: up/down only agents it created
# --------------------------------------------------------------------------- #
class ForemanTouchUpDownTests(unittest.TestCase):
    def test_foreman_cannot_up_human_created_agent(self):
        _foreman("touch_f1")
        gs.create_agent("touch_victim1", home="/tmp/crew_containtest/touch_victim1")
        with self.assertRaises(gs.GraphError):
            guard.check("touch_f1", "up", name="touch_victim1")

    def test_foreman_cannot_down_human_created_agent(self):
        _foreman("touch_f2")
        gs.create_agent("touch_victim2", home="/tmp/crew_containtest/touch_victim2")
        with self.assertRaises(gs.GraphError):
            guard.check("touch_f2", "down", name="touch_victim2")

    def test_foreman_can_up_down_own_created_agent(self):
        _foreman("touch_f3")
        gs.create_agent("touch_kid3", home="/tmp/crew_containtest/touch_kid3", actor="touch_f3")
        guard.check("touch_f3", "up", name="touch_kid3")     # must not raise
        guard.check("touch_f3", "down", name="touch_kid3")   # must not raise


# --------------------------------------------------------------------------- #
# unit — spawn.py: home/repo/launch_cmd confinement for agent actors
# --------------------------------------------------------------------------- #
class SpawnHomeConfinementUnitTests(unittest.TestCase):
    def test_agent_actor_home_override_refused_with_audit(self):
        _foreman("sh_f1")
        with self.assertRaises(gs.GraphError) as ctx:
            spawn.spawn_agent("sh_kid1", home="/tmp/evil", actor="sh_f1")
        self.assertIn("--home", str(ctx.exception))
        rows = _audit_rows(actor="sh_f1", op="spawn")
        self.assertTrue(any(r.get("result") == "refused" for r in rows))

    def test_agent_actor_repo_override_refused(self):
        _foreman("sh_f2")
        with self.assertRaises(gs.GraphError) as ctx:
            spawn.spawn_agent("sh_kid2", repo="/tmp/somerepo", actor="sh_f2")
        self.assertIn("--repo", str(ctx.exception))

    def test_agent_actor_launch_cmd_override_refused(self):
        _foreman("sh_f3")
        with self.assertRaises(gs.GraphError) as ctx:
            spawn.spawn_agent("sh_kid3", launch_cmd="rm -rf /", actor="sh_f3")
        self.assertIn("--launch-cmd", str(ctx.exception))


# --------------------------------------------------------------------------- #
# unit — `crew note` / `crew cap` verbs
# --------------------------------------------------------------------------- #
class NotesVerbTests(unittest.TestCase):
    def test_set_agent_note_any_agent_allowed_on_itself(self):
        a = gs.create_agent("note_agent1", home="/tmp/crew_containtest/note_agent1")
        out = gs.set_agent_note(a["_guid"], "hello", actor="note_agent1")
        self.assertEqual(out.get("notes"), "hello")
        rows = _audit_rows(actor="note_agent1", op="note")
        self.assertTrue(any(r.get("result") == "applied" for r in rows))

    def test_set_edge_note_endpoint_agent_allowed(self):
        a = gs.create_agent("note_edge_a", home="/tmp/crew_containtest/note_edge_a")
        b = gs.create_agent("note_edge_b", home="/tmp/crew_containtest/note_edge_b")
        e = gs.create_edge(a["_guid"], b["_guid"], actor="human")
        out = gs.set_edge_note(e["_guid"], "note text", actor="note_edge_a")
        self.assertEqual(out.get("notes"), "note text")

    def test_set_agent_note_on_another_agent_is_refused_without_mutation(self):
        actor = gs.create_agent(
            "note_other_actor", home="/tmp/crew_containtest/note_other_actor")
        target = gs.create_agent(
            "note_other_target", home="/tmp/crew_containtest/note_other_target",
            notes="original")

        with self.assertRaises(gs.GraphError):
            gs.set_agent_note(target["_guid"], "forged", actor=actor["name"])

        self.assertEqual(gs.get_object(target["_guid"]).get("notes"), "original")
        rows = _audit_rows(actor=actor["name"], op="note")
        self.assertTrue(any(r.get("result") == "refused" for r in rows))
        self.assertFalse(any(r.get("result") == "applied" for r in rows))

    def test_set_edge_note_by_nonendpoint_is_refused_without_mutation(self):
        a = gs.create_agent(
            "note_far_a", home="/tmp/crew_containtest/note_far_a")
        b = gs.create_agent(
            "note_far_b", home="/tmp/crew_containtest/note_far_b")
        outsider = gs.create_agent(
            "note_far_out", home="/tmp/crew_containtest/note_far_out")
        edge = gs.create_edge(a["_guid"], b["_guid"], actor="human")
        gs.set_edge_note(edge["_guid"], "original", actor="human")

        with self.assertRaises(gs.GraphError):
            gs.set_edge_note(
                edge["_guid"], "forged", actor=outsider["name"])

        self.assertEqual(gs.get_object(edge["_guid"]).get("notes"), "original")
        rows = _audit_rows(actor=outsider["name"], op="note")
        self.assertTrue(any(r.get("result") == "refused" for r in rows))
        self.assertFalse(any(r.get("result") == "applied" for r in rows))

    def test_cli_note_agent_and_edge_dispatch(self):
        gs.create_agent("note_cli_a", home="/tmp/crew_containtest/note_cli_a")
        b = gs.create_agent("note_cli_b", home="/tmp/crew_containtest/note_cli_b")
        gs.create_edge(gs.get_agent_by_name("note_cli_a")["_guid"], b["_guid"], actor="human")
        p = cli.build_parser()
        with mock.patch.object(cli, "_ACTOR", "human"):
            args = p.parse_args(["note", "agent", "note_cli_a", "via cli"])
            self.assertEqual(args.fn(args), 0)
            args = p.parse_args(["note", "edge", "note_cli_a", "note_cli_b", "via cli edge"])
            self.assertEqual(args.fn(args), 0)
        refreshed = gs.get_agent_by_name("note_cli_a")
        self.assertEqual(refreshed.get("notes"), "via cli")


class CapCliTests(unittest.TestCase):
    def test_cli_cap_updates_edge_and_prints_change(self):
        a = gs.create_agent("cap_cli_a", home="/tmp/crew_containtest/cap_cli_a")
        b = gs.create_agent("cap_cli_b", home="/tmp/crew_containtest/cap_cli_b")
        gs.create_edge(a["_guid"], b["_guid"], actor="human", max_turns=10)
        p = cli.build_parser()
        with mock.patch.object(cli, "_ACTOR", "human"):
            args = p.parse_args(["cap", "cap_cli_a", "cap_cli_b", "--max-turns", "3"])
            self.assertEqual(args.fn(args), 0)
        edges = gs.edges_from_to(a["_guid"], b["_guid"])
        self.assertEqual(edges[0].get("max_turns"), 3)

    def test_cli_cost_cap_parser_rejects_non_finite_values_but_keeps_zero(self):
        parser = cli.build_parser()
        for value in ("nan", "inf", "-inf"):
            with contextlib.redirect_stderr(io.StringIO()):
                with self.subTest(value=value), self.assertRaises(SystemExit):
                    parser.parse_args([
                        "cap", "cap_cli_a", "cap_cli_b", f"--cost-cap={value}"])
                with self.subTest(command="connect", value=value), \
                     self.assertRaises(SystemExit):
                    parser.parse_args([
                        "connect", "cap_cli_a", "cap_cli_b", f"--cost-cap={value}"])
        args = parser.parse_args([
            "cap", "cap_cli_a", "cap_cli_b", "--cost-cap", "0"])
        self.assertEqual(args.cost_cap, 0.0)

    def test_cli_rejects_negative_caps_but_keeps_zero(self):
        parser = cli.build_parser()
        for option, value in (
                ("--max-turns", "-1"), ("--token-cap", "-1"),
                ("--cost-cap", "-0.01")):
            with contextlib.redirect_stderr(io.StringIO()):
                for command in ("cap", "connect"):
                    with self.subTest(command=command, option=option), \
                         self.assertRaises(SystemExit):
                        parser.parse_args([
                            command, "cap_cli_a", "cap_cli_b", f"{option}={value}"])
        args = parser.parse_args([
            "connect", "cap_cli_a", "cap_cli_b", "--max-turns", "0",
            "--token-cap", "0", "--cost-cap", "0"])
        self.assertEqual(
            (args.max_turns, args.token_cap, args.cost_cap), (0, 0, 0.0))


# --------------------------------------------------------------------------- #
# live — throwaway project "w2test", a real foreman pane
# --------------------------------------------------------------------------- #
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CREW_BIN = os.path.join(ROOT, "bin", "crew")
HOME_BASE = "/tmp/crew_tests_containment"
PROJECT = "w2test"
PROJECT_APP = f"crew-{PROJECT}"


def _run(args, env_extra=None, timeout=30):
    env = dict(os.environ)
    env.pop("CREW_APP", None)
    env["CREW_PROJECT"] = PROJECT
    if env_extra:
        env.update(env_extra)
    p = subprocess.run([sys.executable, CREW_BIN, *args], cwd=ROOT, env=env,
                       capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


def _tmux(*args, timeout=10):
    p = subprocess.run(
        config.tmux_command(*args), env=config.tmux_environment(),
        capture_output=True, text=True, timeout=timeout)
    return p.returncode == 0, (p.stdout if p.returncode == 0 else p.stderr)


def _pane_run(session, cmd, marker, timeout=20):
    """Type `cmd` into session's claude pane exactly as an agent would, then poll
    for the completion marker's exit code (mirrors test_guard.py's
    LiveGuardPaneTests polling pattern: the literal echo text is visible the
    instant it's typed, so we must wait for MARKER=<rc> to actually appear)."""
    ok, panes = _tmux(
        "list-panes", "-t", f"={session}", "-F", "#{pane_id}")
    assert ok and panes.strip(), panes
    pane = panes.strip().splitlines()[0]
    full = f"{cmd}; echo {marker}=$?"
    ok, err = _tmux("send-keys", "-t", pane, "-l", full)
    assert ok, err
    ok, err = _tmux("send-keys", "-t", pane, "Enter")
    assert ok, err
    deadline = time.monotonic() + timeout
    pane_text = ""
    while time.monotonic() < deadline:
        ok, pane_text = _tmux(
            "capture-pane", "-t", pane, "-p", "-S", "-200")
        if f"{marker}=0" in pane_text:
            return 0, pane_text
        if f"{marker}=1" in pane_text:
            return 1, pane_text
        time.sleep(0.5)
    return None, pane_text


@contextlib.contextmanager
def _pinned_app(app):
    """Pin $CREW_APP to `app` for a few direct graphstore calls, then restore
    whatever it was before — NEVER just pop() it. This same test PROCESS also
    runs test_containment's own unit-test classes (module-wide pinned to
    TEST_APP) plus every other test_*.py module's fixtures in a `discover`
    run, all sharing this one mutable os.environ; a bare pop() here would fall
    the NEXT direct graphstore call in this process back to the DEFAULT
    "crew" app — the real, 5-agent app this whole file must never touch. (This
    exact bug leaked 8 fixtures into the real app during this feature's own
    development — see the wave-2 report.)"""
    prev = os.environ.get("CREW_APP")
    os.environ["CREW_APP"] = app
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop("CREW_APP", None)
        else:
            os.environ["CREW_APP"] = prev


@unittest.skipUnless(os.environ.get("CREW_LIVE_TESTS", "1") == "1",
                     "set CREW_LIVE_TESTS=0 to skip live pane tests")
class LiveContainmentTests(unittest.TestCase):
    def setUp(self):
        self.f = "test_w2_f"
        self.kid = "test_w2_kid"
        self.human_made = "test_w2_humanmade"
        self.home_f = os.path.join(HOME_BASE, self.f)
        self.home_human_made = os.path.join(HOME_BASE, self.human_made)

    def tearDown(self):
        for n in (self.kid, self.f, self.human_made):
            try:
                _run(["remove-agent", n], timeout=15)
            except Exception:
                pass
            _tmux("kill-session", "-t", f"{PROJECT}__{n}")
        try:
            gs._req("DELETE", f"/app/{PROJECT_APP}", app=None)
        except gs.GraphError:
            pass
        try:
            names = [n for n in config.list_known_projects() if n != PROJECT]
            os.makedirs(config.VAR, exist_ok=True)
            import json as _json
            with open(config._projects_file(), "w") as fh:
                _json.dump([n for n in names if n != config.DEFAULT_PROJECT], fh)
        except OSError:
            pass

    def test_foreman_containment_end_to_end(self):
        rc, out, err = _run(["project", "create", PROJECT])
        self.assertEqual(rc, 0, f"project create failed: {out!r} {err!r}")

        rc, out, err = _run(["spawn-agent", self.f, "--home", self.home_f,
                             "--runtime", "claude", "--launch-cmd", "true",
                             "--no-launch"])
        self.assertEqual(rc, 0, f"spawn F failed: {out!r} {err!r}")

        rc, out, err = _run(["spawn-agent", self.human_made, "--home",
                             self.home_human_made, "--launch-cmd", "true",
                             "--no-launch"])
        self.assertEqual(rc, 0, f"spawn human-made failed: {out!r} {err!r}")

        # make F a foreman — direct update_agent, actor=human. There is no
        # `crew foreman` verb yet (a later wave), so this is the wave-1-blessed
        # way to grant the flag.
        with _pinned_app(PROJECT_APP):
            f_agent = gs.get_agent_by_name(self.f)
            gs.update_agent(f_agent["_guid"], can_edit_graph=True, actor="human")

        session = f"{PROJECT}__{self.f}"
        ok, out = _tmux("has-session", "-t", session)
        self.assertTrue(ok, f"expected a real tmux session for {self.f}: {out}")

        # A foreman still may not annotate somebody else's node. This uses the
        # real pane/CLI identity path, not an injected actor test helper.
        cmd = (f"{sys.executable} {CREW_BIN} note agent {self.human_made} "
               "'must not land'")
        rc, pane_text = _pane_run(session, cmd, "W2_NOTE_OTHER_RC")
        self.assertEqual(rc, 1, pane_text)
        with _pinned_app(PROJECT_APP):
            self.assertEqual(
                gs.get_agent_by_name(self.human_made).get("notes") or "", "")

        # 1. F's pane: --home is refused (home confinement), audited
        cmd = (f"{sys.executable} {CREW_BIN} spawn-agent {self.kid} "
              f"--home /tmp/evil --no-launch")
        rc, pane_text = _pane_run(session, cmd, "W2_HOME_RC")
        self.assertEqual(rc, 1, pane_text)
        self.assertIn("--home", pane_text)

        with _pinned_app(PROJECT_APP):
            rows = _audit_rows(actor=self.f, op="spawn")
        self.assertTrue(any(r.get("result") == "refused" for r in rows), rows)

        # 2. F's pane: a plain spawn (no --home) succeeds; home lands under
        #    crew_root()/w2test/<name>
        cmd = f"{sys.executable} {CREW_BIN} spawn-agent {self.kid} --no-launch"
        rc, pane_text = _pane_run(session, cmd, "W2_SPAWN_RC")
        self.assertEqual(rc, 0, pane_text)

        with _pinned_app(PROJECT_APP):
            kid_agent = gs.get_agent_by_name(self.kid)
        self.assertIsNotNone(kid_agent)
        expected_root = os.path.realpath(os.path.join(config.crew_root(), PROJECT, self.kid))
        self.assertEqual(gs.normalize_home(kid_agent["home"]), gs.normalize_home(expected_root))

        # 3. F connects F -> kid with finite caps -> applied, unblessed
        cmd = (f"{sys.executable} {CREW_BIN} connect {self.f} {self.kid} "
              f"--max-turns 5 --token-cap 1000 --cost-cap 1.0")
        rc, pane_text = _pane_run(session, cmd, "W2_CONNECT_RC")
        self.assertEqual(rc, 0, pane_text)

        with _pinned_app(PROJECT_APP):
            f_guid = gs.get_agent_by_name(self.f)["_guid"]
            kid_guid = gs.get_agent_by_name(self.kid)["_guid"]
            edges = gs.edges_from_to(f_guid, kid_guid)
        self.assertTrue(edges, "expected F -> kid edge to exist")
        self.assertFalse(edges[0].get("blessed"))

        # A human-owned reverse edge makes the two-direction disconnect batch
        # ineligible for F. The permitted F->kid row must not disappear before
        # the reverse row is checked.
        rc, out, err = _run(["connect", self.kid, self.f])
        self.assertEqual(rc, 0, out + err)
        cmd = f"{sys.executable} {CREW_BIN} disconnect {self.f} {self.kid}"
        rc, pane_text = _pane_run(session, cmd, "W2_DISCONNECT_BATCH_RC")
        self.assertEqual(rc, 1, pane_text)
        with _pinned_app(PROJECT_APP):
            self.assertEqual(len(gs.edges_from_to(f_guid, kid_guid)), 1)
            self.assertEqual(len(gs.edges_from_to(kid_guid, f_guid)), 1)

        # 4. F connect to a human-made node -> queued for approval (WAVE 4:
        #    was a hard refusal; see tests/test_pending.py for the full
        #    approve/reject/notice matrix — this just confirms the real-pane
        #    outcome for this exact envelope case changed).
        cmd = (f"{sys.executable} {CREW_BIN} connect {self.f} "
               f"{self.human_made} --max-turns 5 --token-cap 1000 "
               f"--cost-cap 1.0")
        rc, pane_text = _pane_run(session, cmd, "W2_ENVELOPE_RC")
        self.assertEqual(rc, 1, pane_text)
        self.assertIn("queued", pane_text.lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
