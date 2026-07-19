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
import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_APP = "crewtest-containment-unit"
os.environ["CREW_APP"] = TEST_APP

from crew import cli, config, graphstore as gs, guard, schema, spawn  # noqa: E402


_orig_max_agents = None
_orig_spawn_rate = None


def setUpModule():
    # Re-pin at RUN time (see test_guard.py's comment — a module discovered
    # later that repins the env mid-run must not inherit a leaked pin from us,
    # nor should we inherit one from an earlier module).
    os.environ["CREW_APP"] = TEST_APP
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
    config.MAX_AGENTS, config.SPAWN_RATE = _orig_max_agents, _orig_spawn_rate
    try:
        gs._req("DELETE", f"/app/{TEST_APP}", app=None)
    except gs.GraphError:
        pass


def _audit_rows(actor=None, op=None):
    res = gs.list_objects("graph_edit", limit=1000, sort="created_at", order="desc")
    rows = (res or {}).get("objects", [])
    if actor is not None:
        rows = [r for r in rows if r.get("actor") == actor]
    if op is not None:
        rows = [r for r in rows if r.get("op") == op]
    return rows


def _foreman(name):
    return gs.create_agent(name, home=f"/tmp/crew_containtest/{name}",
                           can_edit_graph=True)


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
    p = subprocess.run(["tmux", *args], capture_output=True, text=True, timeout=timeout)
    return p.returncode == 0, (p.stdout if p.returncode == 0 else p.stderr)


def _pane_run(session, cmd, marker, timeout=20):
    """Type `cmd` into session's claude pane exactly as an agent would, then poll
    for the completion marker's exit code (mirrors test_guard.py's
    LiveGuardPaneTests polling pattern: the literal echo text is visible the
    instant it's typed, so we must wait for MARKER=<rc> to actually appear)."""
    full = f"{cmd}; echo {marker}=$?"
    ok, err = _tmux("send-keys", "-t", f"{session}:claude", "-l", full)
    assert ok, err
    ok, err = _tmux("send-keys", "-t", f"{session}:claude", "Enter")
    assert ok, err
    deadline = time.monotonic() + timeout
    pane_text = ""
    while time.monotonic() < deadline:
        ok, pane_text = _tmux("capture-pane", "-t", f"{session}:claude", "-p", "-S", "-200")
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
                             "--launch-cmd", "true", "--no-launch"])
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

        # 4. F connect to a human-made node -> queued for approval (WAVE 4:
        #    was a hard refusal; see tests/test_pending.py for the full
        #    approve/reject/notice matrix — this just confirms the real-pane
        #    outcome for this exact envelope case changed).
        cmd = f"{sys.executable} {CREW_BIN} connect {self.f} {self.human_made}"
        rc, pane_text = _pane_run(session, cmd, "W2_ENVELOPE_RC")
        self.assertEqual(rc, 1, pane_text)
        self.assertIn("queued", pane_text.lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
