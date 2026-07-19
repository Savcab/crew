"""WAVE 1 tests: crew.guard — the graph-editing permission gate + audit trail.

Two layers, per SKILL.md:
  * unit — a throwaway MorphDB app (`crewtest-guard-unit`), registered in
    setUpModule and cascade-deleted in tearDownModule.
  * live — real spawned agent panes in a throwaway PROJECT ("w1test", its own
    MorphDB app "crew-w1test"), never touching the real 5-agent "crew" app.
    Core acceptance: `crew connect` run FROM INSIDE an agent's own pane (via
    tmux send-keys, exactly as the agent itself would type it) is refused.

    python3 -m unittest tests.test_guard          (from the repo root)
    python3 -m unittest discover tests             (full suite)
"""
import os
import subprocess
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_APP = "crewtest-guard-unit"

from crew import config, graphstore as gs, guard, schema  # noqa: E402

_orig_max_agents = None
_orig_spawn_rate = None
_CREW_APP_PATCHER = None


def setUpModule():
    # Re-pin at RUN time (see test_mail.py's comment — a module discovered
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
    # This file's fixtures are almost all HUMAN-created (unrestricted), but a
    # couple of WAVE 2 ForemanActorTests spawn via the foreman actor to stay
    # inside the ENVELOPE — those would otherwise trip CREW_MAX_AGENTS purely
    # from this shared app's cumulative fixture count. This file isn't testing
    # the count ceiling itself (see tests/test_containment.py for that), so
    # raise it sky-high here.
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
    res = gs.list_objects("graph_edit", limit=500, sort="created_at", order="desc")
    rows = (res or {}).get("objects", [])
    if actor is not None:
        rows = [r for r in rows if r.get("actor") == actor]
    if op is not None:
        rows = [r for r in rows if r.get("op") == op]
    return rows


# --------------------------------------------------------------------------- #
# unit — human actor: unrestricted, and stamps created_by/blessed correctly
# --------------------------------------------------------------------------- #
class HumanActorTests(unittest.TestCase):
    def test_create_agent_applied_created_by_blessed_and_audit_row(self):
        a = gs.create_agent("gh_human_a", home="/tmp/crew_guardtest/gh_human_a",
                            actor="human")
        self.assertEqual(a["created_by"], "human")
        self.assertTrue(a["blessed"])
        rows = _audit_rows(actor="human", op="spawn")
        self.assertTrue(any(r.get("result") == "applied" for r in rows))

    def test_create_edge_applied_created_by_blessed_and_audit_row(self):
        a = gs.create_agent("gh_human_b1", home="/tmp/crew_guardtest/gh_human_b1")
        b = gs.create_agent("gh_human_b2", home="/tmp/crew_guardtest/gh_human_b2")
        e = gs.create_edge(a["_guid"], b["_guid"], actor="human")
        self.assertEqual(e["created_by"], "human")
        self.assertTrue(e["blessed"])
        rows = _audit_rows(actor="human", op="connect")
        self.assertTrue(any(r.get("result") == "applied" for r in rows))

    def test_default_actor_is_human(self):
        # explicit actor kwarg omitted -> defaults to "human" (keeps the pre-wave
        # 172 tests green, and every non-guard-aware caller stays unrestricted).
        a = gs.create_agent("gh_default_actor", home="/tmp/crew_guardtest/gh_default_actor")
        self.assertEqual(a["created_by"], "human")

    def test_human_can_remove_and_bless_ops(self):
        # human bypasses remove/bless unconditionally. "foreman" gained a
        # WAVE-3 singleton check (still human-only, but a GRANT now conflicts
        # with any other foreman already in this shared test app) — call with
        # revoke=True so this always succeeds regardless of fixtures other
        # test classes in this module may have already created.
        guard.check("human", "remove")
        guard.check("human", "bless")
        guard.check("human", "foreman", revoke=True)  # must not raise


# --------------------------------------------------------------------------- #
# unit — plain agent actor: default-deny for graph editing
# --------------------------------------------------------------------------- #
class AgentActorDefaultDenyTests(unittest.TestCase):
    def test_create_edge_refused_and_audit_row(self):
        a = gs.create_agent("ga_plain_a", home="/tmp/crew_guardtest/ga_plain_a")
        b = gs.create_agent("ga_plain_b", home="/tmp/crew_guardtest/ga_plain_b")
        with self.assertRaises(gs.GraphError) as ctx:
            gs.create_edge(a["_guid"], b["_guid"], actor="ga_plain_a")
        self.assertIn("foreman", str(ctx.exception))
        rows = _audit_rows(actor="ga_plain_a", op="connect")
        self.assertTrue(any(r.get("result") == "refused" for r in rows))

    def test_spawn_refused(self):
        gs.create_agent("ga_spawner", home="/tmp/crew_guardtest/ga_spawner")
        with self.assertRaises(gs.GraphError) as ctx:
            guard.check("ga_spawner", "spawn", name="ga_new_agent")
        self.assertIn("foreman", str(ctx.exception))

    def test_remove_refused_even_though_topology(self):
        gs.create_agent("ga_remover", home="/tmp/crew_guardtest/ga_remover")
        with self.assertRaises(gs.GraphError):
            guard.check("ga_remover", "remove", name="someone")

    def test_bless_refused(self):
        gs.create_agent("ga_blesser", home="/tmp/crew_guardtest/ga_blesser")
        with self.assertRaises(gs.GraphError):
            guard.check("ga_blesser", "bless")

    def test_unknown_actor_name_treated_as_non_foreman_agent(self):
        # a name that isn't even a registered agent must NOT be treated as human
        with self.assertRaises(gs.GraphError):
            guard.check("totally_unregistered_ghost", "spawn", name="x")

    def test_unknown_actor_note_is_refused(self):
        target = gs.create_agent(
            "ga_note_target", home="/tmp/crew_guardtest/ga_note_target")
        with self.assertRaises(gs.GraphError):
            guard.check(
                "nobody_registered_at_all", "note", on="agent", target=target)


# --------------------------------------------------------------------------- #
# unit — agent actor editing an edge it's an endpoint of: narrow allowance
# --------------------------------------------------------------------------- #
class AgentEndpointEdgeUpdateTests(unittest.TestCase):
    def _edge(self, prefix):
        a = gs.create_agent(f"{prefix}_a", home=f"/tmp/crew_guardtest/{prefix}_a")
        b = gs.create_agent(f"{prefix}_b", home=f"/tmp/crew_guardtest/{prefix}_b")
        e = gs.create_edge(a["_guid"], b["_guid"], max_turns=10, token_cap=1000)
        return a, b, e

    def test_note_only_edge_update_allowed(self):
        a, b, e = self._edge("ga_edgeupd1")
        out = gs.update_edge(e["_guid"], {"notes": "heads up"}, actor="ga_edgeupd1_a")
        self.assertEqual(out.get("notes"), "heads up")
        rows = _audit_rows(actor="ga_edgeupd1_a", op="update_edge")
        self.assertTrue(any(r.get("result") == "applied" for r in rows))

    def test_label_description_conditions_target_action_allowed(self):
        a, b, e = self._edge("ga_edgeupd2")
        out = gs.update_edge(e["_guid"], {
            "label": "l", "description": "d", "conditions": ["when x"],
            "target_action": "ack",
        }, actor="ga_edgeupd2_a")
        self.assertEqual(out.get("label"), "l")

    def test_cap_raise_queued_pending_not_refused(self):
        # WAVE 4: a cap raise on an edge you're an endpoint of no longer
        # hard-refuses — it routes to the pending-approval queue instead
        # (case (b) of the wave-4 spec). See tests/test_pending.py for the
        # full matrix (approve/reject/notice/CLI).
        a, b, e = self._edge("ga_edgeupd3")
        with self.assertRaises(gs.GraphError) as ctx:
            gs.update_edge(e["_guid"], {"max_turns": 50}, actor="ga_edgeupd3_a")
        self.assertIn("cap raise", str(ctx.exception).lower())
        rows = _audit_rows(actor="ga_edgeupd3_a", op="update_edge")
        self.assertTrue(any(r.get("result") == "pending" for r in rows))
        self.assertFalse(any(r.get("result") == "refused" for r in rows))

    def test_cap_raise_cannot_queue_a_later_disallowed_field_by_key_order(self):
        a, b, e = self._edge("ga_edgeupd3_order")
        with self.assertRaises(gs.GraphError) as ctx:
            gs.update_edge(
                e["_guid"], {"max_turns": 50, "directed": False},
                actor=a["name"])
        self.assertIn("directed", str(ctx.exception))
        rows = _audit_rows(actor=a["name"], op="update_edge")
        self.assertFalse(any(r.get("result") == "pending" for r in rows), rows)
        self.assertTrue(any(r.get("result") == "refused" for r in rows), rows)
        self.assertTrue(gs.get_object(e["_guid"])["directed"])

    def test_cap_raise_cannot_queue_a_later_invalid_cap_by_key_order(self):
        a, b, e = self._edge("ga_edgeupd3_invalid")
        with self.assertRaises(gs.GraphError) as ctx:
            gs.update_edge(
                e["_guid"], {"max_turns": 50, "token_cap": "nan"},
                actor=a["name"])
        self.assertRegex(str(ctx.exception), r"integer|finite")
        rows = _audit_rows(actor=a["name"], op="update_edge")
        self.assertFalse(any(r.get("result") == "pending" for r in rows), rows)
        self.assertTrue(any(r.get("result") == "refused" for r in rows), rows)

    def test_cap_lower_allowed(self):
        a, b, e = self._edge("ga_edgeupd4")
        out = gs.update_edge(e["_guid"], {"max_turns": 2}, actor="ga_edgeupd4_a")
        self.assertEqual(out.get("max_turns"), 2)

    def test_cap_equal_allowed(self):
        a, b, e = self._edge("ga_edgeupd5")
        out = gs.update_edge(e["_guid"], {"max_turns": 10}, actor="ga_edgeupd5_a")
        self.assertEqual(out.get("max_turns"), 10)

    def test_non_endpoint_agent_refused(self):
        a, b, e = self._edge("ga_edgeupd6")
        gs.create_agent("ga_edgeupd6_outsider", home="/tmp/crew_guardtest/ga_edgeupd6_outsider")
        with self.assertRaises(gs.GraphError) as ctx:
            gs.update_edge(e["_guid"], {"notes": "x"}, actor="ga_edgeupd6_outsider")
        self.assertIn("endpoint", str(ctx.exception))

    def test_disallowed_field_refused(self):
        a, b, e = self._edge("ga_edgeupd7")
        with self.assertRaises(gs.GraphError):
            gs.update_edge(e["_guid"], {"directed": False}, actor="ga_edgeupd7_a")

    def test_target_side_endpoint_also_allowed(self):
        a, b, e = self._edge("ga_edgeupd8")
        out = gs.update_edge(e["_guid"], {"notes": "from target"}, actor="ga_edgeupd8_b")
        self.assertEqual(out.get("notes"), "from target")


# --------------------------------------------------------------------------- #
# unit — foreman-flagged agent (can_edit_graph=True): topology pass-through
# --------------------------------------------------------------------------- #
class ForemanActorTests(unittest.TestCase):
    def _foreman(self, name):
        # This module shares one throwaway app and foreman is a singleton.
        # Retire a prior test fixture before creating the next test's actor.
        for existing in gs.list_agents():
            if existing.get("can_edit_graph"):
                gs.patch_object(
                    "agent", existing["_guid"], {"can_edit_graph": False})
        return gs.create_agent(name, home=f"/tmp/crew_guardtest/{name}",
                               can_edit_graph=True)

    def test_create_edge_allowed_blessed_false_created_by_agent(self):
        # WAVE 2: connect is now gated by the ENVELOPE rule (target must be a
        # foreman-owned agent, not just anyone) and the FINITE-CAPS RULE (all
        # three caps mandatory) — see tests/test_containment.py for the full
        # containment matrix; this test just confirms the wave-1 shape
        # (allowed, blessed=false, created_by=agent, audit row) still holds
        # once those wave-2 preconditions are met.
        f = self._foreman("gf_foreman1")
        b = gs.create_agent("gf_target1", home="/tmp/crew_guardtest/gf_target1",
                            actor="gf_foreman1")
        e = gs.create_edge(f["_guid"], b["_guid"], actor="gf_foreman1",
                           max_turns=5, token_cap=1000, cost_cap=1.0)
        self.assertFalse(e["blessed"])
        self.assertEqual(e["created_by"], "gf_foreman1")
        rows = _audit_rows(actor="gf_foreman1", op="connect")
        self.assertTrue(any(r.get("result") == "applied" for r in rows))

    def test_disconnect_allowed(self):
        # WAVE 2: disconnect additionally requires the EDGE itself was
        # created_by the foreman (the ENVELOPE rule) — see
        # tests/test_containment.py for the containment matrix.
        f = self._foreman("gf_foreman2")
        b = gs.create_agent("gf_target2", home="/tmp/crew_guardtest/gf_target2",
                            actor="gf_foreman2")
        e = gs.create_edge(f["_guid"], b["_guid"], actor="gf_foreman2",
                           max_turns=5, token_cap=1000, cost_cap=1.0)
        gs.delete_edge(e["_guid"], actor="gf_foreman2")  # must not raise

    def test_remove_refused(self):
        f = self._foreman("gf_foreman3")
        victim = gs.create_agent("gf_victim3", home="/tmp/crew_guardtest/gf_victim3")
        with self.assertRaises(gs.GraphError) as ctx:
            gs.delete_agent(victim["_guid"], actor="gf_foreman3")
        self.assertIn("human", str(ctx.exception).lower())

    def test_bless_refused(self):
        f = self._foreman("gf_foreman4")
        with self.assertRaises(gs.GraphError):
            guard.check("gf_foreman4", "bless")

    def test_foreman_op_refused(self):
        # a foreman cannot GRANT foreman powers to another agent
        f = self._foreman("gf_foreman5")
        with self.assertRaises(gs.GraphError):
            guard.check("gf_foreman5", "foreman", name="someone_else")

    def test_cannot_touch_protected_agent_fields(self):
        f = self._foreman("gf_foreman6")
        victim = gs.create_agent(
            "gf_victim6", home="/tmp/crew_guardtest/gf_victim6",
            actor="gf_foreman6")
        attempts = {
            "name": "gf_poisoned6",
            "home": "/tmp/crew_guardtest/evil-home",
            "session": "operators-session",
            "pane": "%9999",
            "worktree": "/tmp/crew_guardtest/operators-worktree",
            "status": "working",
            "runtime": "codex",
            "launch_cmd": "touch /tmp/must-not-run",
            "kind": "human",
            "can_edit_graph": True,
            "grants": [{"name": "forged", "path": "/", "mode": "rw"}],
        }
        before = gs.get_object(victim["_guid"])
        for field, value in attempts.items():
            with self.subTest(field=field), self.assertRaises(gs.GraphError):
                gs.update_agent(
                    victim["_guid"], actor="gf_foreman6", **{field: value})
        after = gs.get_object(victim["_guid"])
        for field in attempts:
            self.assertEqual(after.get(field), before.get(field), field)

    def test_can_update_non_protected_agent_fields(self):
        f = self._foreman("gf_foreman7")
        victim = gs.create_agent(
            "gf_victim7", home="/tmp/crew_guardtest/gf_victim7",
            actor="gf_foreman7")
        out = gs.update_agent(
            victim["_guid"], role="new role", identity="new identity",
            notes="new note", actor="gf_foreman7")
        self.assertEqual(out.get("role"), "new role")
        self.assertEqual(out.get("identity"), "new identity")
        self.assertEqual(out.get("notes"), "new note")

    def test_cannot_update_human_owned_or_other_branch_agent(self):
        f = self._foreman("gf_scope_f8")
        human_owned = gs.create_agent(
            "gf_scope_h8", home="/tmp/crew_guardtest/gf_scope_h8")
        other = self._foreman("gf_scope_other8")
        other_child = gs.create_agent(
            "gf_scope_child8", home="/tmp/crew_guardtest/gf_scope_child8",
            actor=other["name"])
        # Restore f as this scenario's sole current foreman after constructing
        # a durable child owned by a different (now-retired) foreman branch.
        gs.patch_object(
            "agent", other["_guid"], {"can_edit_graph": False})
        gs.patch_object("agent", f["_guid"], {"can_edit_graph": True})

        for target in (human_owned, other_child):
            with self.subTest(target=target["name"]), \
                 self.assertRaises(gs.GraphError):
                gs.update_agent(
                    target["_guid"], role="hijacked", actor=f["name"])
            self.assertNotEqual(
                gs.get_object(target["_guid"]).get("role"), "hijacked")

    def test_foreman_cannot_update_its_own_row(self):
        f = self._foreman("gf_scope_self9")
        with self.assertRaises(gs.GraphError):
            gs.update_agent(f["_guid"], role="self rewritten", actor=f["name"])
        self.assertNotEqual(gs.get_object(f["_guid"]).get("role"), "self rewritten")

    def test_internal_runtime_and_grant_helpers_are_field_limited(self):
        child = gs.create_agent(
            "gf_internal10", home="/tmp/crew_guardtest/gf_internal10")
        runtime = gs.update_agent_runtime_state(
            child["_guid"], pane="%internal", status="idle")
        self.assertEqual(runtime.get("pane"), "%internal")
        self.assertEqual(runtime.get("status"), "idle")

        grants = [{"name": "docs", "path": "/tmp/docs", "mode": "ro"}]
        granted = gs.update_agent_grants(child["_guid"], grants)
        self.assertEqual(granted.get("grants"), grants)


# --------------------------------------------------------------------------- #
# unit — audit() never raises, even when the graphstore write itself fails
# --------------------------------------------------------------------------- #
class AuditNeverRaisesTests(unittest.TestCase):
    def test_audit_swallows_graphstore_error(self):
        class FakeGS:
            class GraphError(Exception):
                pass

            @staticmethod
            def create_object(*a, **kw):
                raise FakeGS.GraphError("MorphDB is down")

        # Patch the module-level lazy import target so guard.audit's internal
        # `from . import graphstore as gs` resolves to our broken fake.
        import sys as _sys
        real = _sys.modules.get("crew.graphstore")
        try:
            _sys.modules["crew.graphstore"] = FakeGS
            guard.audit("someone", "connect", {"x": 1}, "applied")  # must not raise
        finally:
            if real is not None:
                _sys.modules["crew.graphstore"] = real

    def test_audit_with_unserializable_args_does_not_raise(self):
        class Unserializable:
            pass
        guard.audit("someone", "connect", {"obj": Unserializable()}, "applied")


# --------------------------------------------------------------------------- #
# live — real spawned agent pane, throwaway project "w1test"
# --------------------------------------------------------------------------- #
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CREW_BIN = os.path.join(ROOT, "bin", "crew")
HOME_BASE = "/tmp/crew_tests_guard"
PROJECT = "w1test"
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


@unittest.skipUnless(os.environ.get("CREW_LIVE_TESTS", "1") == "1",
                     "set CREW_LIVE_TESTS=0 to skip live pane tests")
class LiveGuardPaneTests(unittest.TestCase):
    """The core acceptance test: `crew connect` run FROM INSIDE a spawned agent's
    own tmux pane (via tmux send-keys, exactly as the agent would type it) is
    refused by the guard, and a refused graph_edit audit row is written. Runs in
    an isolated project (its own MorphDB app) so the real 5-agent 'crew' app is
    never touched. Full cleanup in finally regardless of outcome."""

    def setUp(self):
        self.a = "test_w1_a"
        self.b = "test_w1_b"
        self.home_a = os.path.join(HOME_BASE, self.a)
        self.home_b = os.path.join(HOME_BASE, self.b)

    def tearDown(self):
        for n in (self.a, self.b):
            try:
                _run(["remove-agent", n], timeout=15)
            except Exception:
                pass
            _tmux("kill-session", "-t", f"{PROJECT}__{n}")
        try:
            gs._req("DELETE", f"/app/{PROJECT_APP}", app=None)
        except gs.GraphError:
            pass
        # drop the project registration so it doesn't linger in var/projects.json
        try:
            names = [n for n in config.list_known_projects() if n != PROJECT]
            os.makedirs(config.VAR, exist_ok=True)
            import json as _json
            with open(config._projects_file(), "w") as f:
                _json.dump([n for n in names if n != config.DEFAULT_PROJECT], f)
        except OSError:
            pass

    def test_agent_pane_connect_refused_human_cli_connect_applies(self):
        rc, out, err = _run(["project", "create", PROJECT])
        self.assertEqual(rc, 0, f"project create failed: {out!r} {err!r}")

        rc, out, err = _run(["spawn-agent", self.a, "--home", self.home_a,
                             "--launch-cmd", "true", "--no-launch"])
        self.assertEqual(rc, 0, f"spawn a failed: {out!r} {err!r}")
        rc, out, err = _run(["spawn-agent", self.b, "--home", self.home_b,
                             "--launch-cmd", "true", "--no-launch"])
        self.assertEqual(rc, 0, f"spawn b failed: {out!r} {err!r}")

        session = f"{PROJECT}__{self.a}"
        ok, out = _tmux("has-session", "-t", session)
        self.assertTrue(ok, f"expected a real tmux session for {self.a}: {out}")
        ok, panes = _tmux(
            "list-panes", "-t", f"={session}", "-F", "#{pane_id}")
        self.assertTrue(ok and panes.strip(), panes)
        pane = panes.strip().splitlines()[0]

        # type `crew connect <a> <b>` INTO the agent's own pane, exactly as the
        # agent itself would run it — this is what proves the gate is enforced
        # from a real agent pane, not just from a python call.
        cmd = (f"CREW_PROJECT={PROJECT} {sys.executable} {CREW_BIN} connect "
              f"{self.a} {self.b}; echo GUARD_TEST_RC=$?\n")
        ok, err = _tmux("send-keys", "-t", pane, "-l", cmd.rstrip("\n"))
        self.assertTrue(ok, err)
        ok, err = _tmux("send-keys", "-t", pane, "Enter")
        self.assertTrue(ok, err)

        # Poll for the ACTUAL result code, not just the (already-visible-as-typed)
        # "GUARD_TEST_RC=$?" echo text — that literal substring is on the pane
        # the instant the command is typed, before it has even run.
        pane_text = ""
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            ok, pane_text = _tmux("capture-pane", "-t", pane, "-p")
            if ok and ("GUARD_TEST_RC=0" in pane_text or "GUARD_TEST_RC=1" in pane_text):
                break
            time.sleep(0.5)
        self.assertIn("GUARD_TEST_RC=1", pane_text, pane_text)
        self.assertIn("foreman", pane_text, pane_text)

        # a refusal audit row must exist for this actor/op
        os.environ["CREW_APP"] = PROJECT_APP
        try:
            rows = _audit_rows(actor=self.a, op="connect")
        finally:
            os.environ["CREW_APP"] = TEST_APP
        self.assertTrue(any(r.get("result") == "refused" for r in rows), rows)

        # the human CLI (no agent pane identity) connecting the SAME two agents
        # in the same project applies fine.
        rc, out, err = _run(["connect", self.a, self.b, "--when", "test"])
        self.assertEqual(rc, 0, f"human connect failed: {out!r} {err!r}")
        self.assertIn(f"connected {self.a} -> {self.b}", out)


# --------------------------------------------------------------------------- #
# live — `crew audit` command
# --------------------------------------------------------------------------- #
@unittest.skipUnless(os.environ.get("CREW_LIVE_TESTS", "1") == "1",
                     "set CREW_LIVE_TESTS=0 to skip live pane tests")
class AuditCommandLiveTests(unittest.TestCase):
    def setUp(self):
        os.environ["CREW_APP"] = TEST_APP

    def test_audit_lists_rows(self):
        a = gs.create_agent("gaud_a", home="/tmp/crew_guardtest/gaud_a")
        b = gs.create_agent("gaud_b", home="/tmp/crew_guardtest/gaud_b")
        try:
            gs.create_edge(a["_guid"], b["_guid"], actor="gaud_a")
        except gs.GraphError:
            pass  # expected refusal — the audit row is what we're checking for
        env = dict(os.environ)
        p = subprocess.run([sys.executable, CREW_BIN, "audit", "-n", "50"],
                           cwd=ROOT, env=env, capture_output=True, text=True, timeout=15)
        self.assertEqual(p.returncode, 0, f"crew audit failed: {p.stdout!r} {p.stderr!r}")
        self.assertIn("gaud_a", p.stdout)

    def test_audit_refused_filter(self):
        env = dict(os.environ)
        p = subprocess.run([sys.executable, CREW_BIN, "audit", "--refused", "-n", "50"],
                           cwd=ROOT, env=env, capture_output=True, text=True, timeout=15)
        self.assertEqual(p.returncode, 0, f"crew audit --refused failed: {p.stdout!r} {p.stderr!r}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
