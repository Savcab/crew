"""WAVE 3 tests: foreman UX + dashboard truth — `crew foreman`, `crew bless`,
`crew spawn-agent --foreman`, the identity "Graph powers" section, the
dashboard's bless/foreman endpoints + snapshot field exposure, and the
token_cap/cost_cap edge-create/update gap.

Three layers, per SKILL.md:
  * unit — a throwaway MorphDB app (`crewtest-foreman-unit`), registered in
    setUpModule and cascade-deleted in tearDownModule. A couple of tests spawn
    a real (but claude-less: --launch-cmd true --no-launch) tmux session via
    spawn.spawn_agent to exercise the --foreman flag's full wiring; those
    sessions are killed in tearDown.
  * live — a throwaway project ("w3test", its own MorphDB app "crew-w3test"),
    never touching the real 5-agent "crew" app. Exercises the real CLI +
    the real, already-running dashboard's new endpoints.
  * browser — tests/browser/foreman-bless.md, executed separately via
    playwright tools (not part of this file).

    python3 -m unittest tests.test_foreman        (from the repo root)
    python3 -m unittest discover tests              (full suite)
"""
import contextlib
import http.cookiejar
import json
import os
import subprocess
import sys
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

from operator_harness import pin_environment, run_operator

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_APP = "crewtest-foreman-unit"

from crew import cli, config, graphstore as gs, guard, identity, schema, spawn  # noqa: E402

_orig_max_agents = None
_orig_spawn_rate = None
_CREW_APP_PATCHER = None


def setUpModule():
    global _CREW_APP_PATCHER
    _CREW_APP_PATCHER = mock.patch.dict(os.environ, {"CREW_APP": TEST_APP})
    _CREW_APP_PATCHER.start()
    unittest.addModuleCleanup(_CREW_APP_PATCHER.stop)
    try:
        gs._req("DELETE", f"/app/{TEST_APP}", app=None)
    except gs.GraphError:
        pass
    schema.ensure_schema(TEST_APP)
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
    return gs.create_agent(name, home=f"/tmp/crew_foremantest/{name}",
                           can_edit_graph=True)


def _tmux(*args, timeout=10):
    p = subprocess.run(
        config.tmux_command(*args), env=config.tmux_environment(),
        capture_output=True, text=True, timeout=timeout)
    return p.returncode == 0, (p.stdout if p.returncode == 0 else p.stderr)


class _DedicatedAppCase(unittest.TestCase):
    """Base for tests whose assertions depend on the EXACT set of foremen in
    the whole app (the singleton rule scans every agent) — a shared app would
    let another test class's fixture foreman leak in and change the answer
    (this happened: see the report). setUp gives EACH TEST METHOD (not just
    each class) a freshly wiped app, same defensive pattern as
    test_containment.py's SpawnCountConfinementTests/SpawnRateConfinementTests."""
    APP = "crewtest-foreman-singleton"

    def setUp(self):
        pin_environment(self.addCleanup, {"CREW_APP": self.APP})
        try:
            gs._req("DELETE", f"/app/{self.APP}", app=None)
        except gs.GraphError:
            pass
        schema.ensure_schema(self.APP)

    def tearDown(self):
        try:
            gs._req("DELETE", f"/app/{self.APP}", app=None)
        except gs.GraphError:
            pass


# --------------------------------------------------------------------------- #
# unit — foreman singleton (guard.check op "foreman")
# --------------------------------------------------------------------------- #
class ForemanSingletonTests(_DedicatedAppCase):
    def test_second_foreman_refused_names_first(self):
        _foreman("fs_first")
        with self.assertRaises(gs.GraphError) as ctx:
            guard.check("human", "foreman", name="fs_second", revoke=False)
        self.assertIn("fs_first", str(ctx.exception))
        self.assertIn("--revoke", str(ctx.exception))
        rows = _audit_rows(actor="human", op="foreman")
        self.assertTrue(any(r.get("result") == "refused" for r in rows))

    def test_idempotent_same_name_allowed(self):
        _foreman("fs_same")
        guard.check("human", "foreman", name="fs_same", revoke=False)  # must not raise

    def test_revoke_bypasses_singleton(self):
        _foreman("fs_third")
        guard.check("human", "foreman", name="fs_third", revoke=True)  # must not raise

    def test_no_existing_foreman_allowed(self):
        gs.create_agent("fs_plain", home="/tmp/crew_foremantest/fs_plain")
        guard.check("human", "foreman", name="fs_plain", revoke=False)  # must not raise

    def test_agent_actor_refused_even_without_conflict(self):
        gs.create_agent("fs_agent_actor", home="/tmp/crew_foremantest/fs_agent_actor")
        with self.assertRaises(gs.GraphError) as ctx:
            guard.check("fs_agent_actor", "foreman", name="someone_else", revoke=False)
        self.assertIn("human", str(ctx.exception).lower())


# --------------------------------------------------------------------------- #
# unit — `crew foreman` CLI verb
# --------------------------------------------------------------------------- #
class ForemanCliTests(_DedicatedAppCase):
    APP = "crewtest-foreman-cli"

    def test_cli_grants_and_revokes(self):
        a = gs.create_agent("fc_a", home="/tmp/crew_foremantest/fc_a")
        p = cli.build_parser()
        with mock.patch.object(cli, "_ACTOR", "human"):
            args = p.parse_args(["foreman", "fc_a"])
            self.assertEqual(args.fn(args), 0)
            refreshed = gs.get_agent_by_name("fc_a")
            self.assertTrue(refreshed["can_edit_graph"])

            args = p.parse_args(["foreman", "fc_a", "--revoke"])
            self.assertEqual(args.fn(args), 0)
            refreshed = gs.get_agent_by_name("fc_a")
            self.assertFalse(refreshed["can_edit_graph"])

    def test_cli_singleton_refused_names_first(self):
        gs.create_agent("fc_first", home="/tmp/crew_foremantest/fc_first",
                        can_edit_graph=True)
        gs.create_agent("fc_second", home="/tmp/crew_foremantest/fc_second")
        p = cli.build_parser()
        with mock.patch.object(cli, "_ACTOR", "human"):
            args = p.parse_args(["foreman", "fc_second"])
            with self.assertRaises(gs.GraphError) as ctx:
                # cmd_foreman raises via guard.check -> cli.main catches it,
                # but calling args.fn directly (bypassing main's try/except)
                # surfaces the GraphError so we can assert its message.
                args.fn(args)
        self.assertIn("fc_first", str(ctx.exception))

    def test_cli_rewrites_identity_md_on_disk(self):
        home = "/tmp/crew_foremantest/fc_identity"
        a = gs.create_agent("fc_identity", home=home)
        p = cli.build_parser()
        with mock.patch.object(cli, "_ACTOR", "human"):
            args = p.parse_args(["foreman", "fc_identity"])
            self.assertEqual(args.fn(args), 0)
        path = os.path.join(home, config.IDENTITY_FILE)
        with open(path) as f:
            text = f.read()
        self.assertIn("## Graph powers", text)
        self.assertIn(guard.FOREMAN_ENVELOPE_SENTENCE, text)


# --------------------------------------------------------------------------- #
# unit — `crew bless`
# --------------------------------------------------------------------------- #
class BlessTests(unittest.TestCase):
    def setUp(self):
        # This class shares the module app across test methods, while the
        # product permits only one live foreman. Retire the previous method's
        # fixture before creating the next one.
        for current in gs.list_agents():
            if current.get("can_edit_graph"):
                gs.set_foreman(current["_guid"], revoke=True, actor="human")

    def test_bless_missing_edge_refuses_without_creating_a_phantom(self):
        fake_guid = "bl_missing_edge_guid"
        try:
            with self.assertRaises(gs.GraphError) as ctx:
                gs.bless_edge(fake_guid, actor="human")
            self.assertRegex(str(ctx.exception).lower(), r"no (?:such|object)")
            with self.assertRaises(gs.GraphError):
                gs.get_object(fake_guid)
        finally:
            # MorphDB PATCH is an upsert.  Keep the RED witness recoverable on
            # old implementations that accidentally materialize this GUID.
            try:
                gs.delete_object("edge", fake_guid)
            except gs.GraphError:
                pass

    def test_bless_agent_flips_unblessed_only(self):
        f = _foreman("bl_f1")
        kid = gs.create_agent("bl_kid1", home="/tmp/crew_foremantest/bl_kid1", actor="bl_f1")
        self.assertFalse(kid["blessed"])
        p = cli.build_parser()
        with mock.patch.object(cli, "_ACTOR", "human"):
            args = p.parse_args(["bless", "bl_kid1"])
            self.assertEqual(args.fn(args), 0)
        self.assertTrue(gs.get_agent_by_name("bl_kid1")["blessed"])
        rows = _audit_rows(actor="human", op="bless")
        self.assertTrue(any(r.get("result") == "applied" for r in rows))

    def test_bless_edge(self):
        f = _foreman("bl_f2")
        kid = gs.create_agent("bl_kid2", home="/tmp/crew_foremantest/bl_kid2", actor="bl_f2")
        e = gs.create_edge(f["_guid"], kid["_guid"], actor="bl_f2",
                           max_turns=5, token_cap=1000, cost_cap=1.0)
        self.assertFalse(e["blessed"])
        p = cli.build_parser()
        with mock.patch.object(cli, "_ACTOR", "human"):
            args = p.parse_args(["bless", "--edge", "bl_f2", "bl_kid2"])
            self.assertEqual(args.fn(args), 0)
        edges = gs.edges_from_to(f["_guid"], kid["_guid"])
        self.assertTrue(edges[0]["blessed"])

    def test_bless_all_covers_every_unblessed_agent_and_edge(self):
        f = _foreman("bl_f3")
        kid1 = gs.create_agent("bl_kid3a", home="/tmp/crew_foremantest/bl_kid3a", actor="bl_f3")
        kid2 = gs.create_agent("bl_kid3b", home="/tmp/crew_foremantest/bl_kid3b", actor="bl_f3")
        e1 = gs.create_edge(f["_guid"], kid1["_guid"], actor="bl_f3",
                            max_turns=5, token_cap=1000, cost_cap=1.0)
        p = cli.build_parser()
        with mock.patch.object(cli, "_ACTOR", "human"):
            args = p.parse_args(["bless", "--all"])
            self.assertEqual(args.fn(args), 0)
        self.assertTrue(gs.get_agent_by_name("bl_kid3a")["blessed"])
        self.assertTrue(gs.get_agent_by_name("bl_kid3b")["blessed"])
        edges = gs.edges_from_to(f["_guid"], kid1["_guid"])
        self.assertTrue(edges[0]["blessed"])
        # human-created rows were already blessed at creation — --all must not
        # error re-touching them (they're simply excluded from the sweep)
        self.assertTrue(f["blessed"] is False or f["blessed"] is True)  # sanity: field exists

    def test_bless_refused_for_agent_actor(self):
        gs.create_agent("bl_denied", home="/tmp/crew_foremantest/bl_denied")
        with self.assertRaises(gs.GraphError):
            gs.bless_agent(gs.get_agent_by_name("bl_denied")["_guid"], actor="bl_denied")


# --------------------------------------------------------------------------- #
# unit — `crew spawn-agent --foreman`
# --------------------------------------------------------------------------- #
class SpawnForemanFlagTests(_DedicatedAppCase):
    # dedicated app — this class's singleton assertions must not see any
    # foreman fixture another test class (e.g. BlessTests) created.
    APP = "crewtest-foreman-spawnflag"

    def tearDown(self):
        for n in ("sf_human_ok",):
            _tmux("kill-session", "-t", n)
        super().tearDown()

    def test_agent_actor_foreman_flag_refused(self):
        _foreman("sf_existing")
        gs.create_agent("sf_spawner", home="/tmp/crew_foremantest/sf_spawner",
                        actor="sf_existing")
        with self.assertRaises(gs.GraphError) as ctx:
            spawn.spawn_agent("sf_kid", actor="sf_existing", foreman=True)
        self.assertIn("--foreman", str(ctx.exception))
        rows = _audit_rows(actor="sf_existing", op="spawn")
        self.assertTrue(any(r.get("result") == "refused" for r in rows))

    def test_human_foreman_flag_ok_singleton_applies(self):
        agent = spawn.spawn_agent(
            "sf_human_ok", home="/tmp/crew_foremantest/sf_human_ok",
            launch=False, launch_cmd="true", actor="human", foreman=True)
        self.assertTrue(agent["can_edit_graph"])
        # a second human --foreman spawn while sf_human_ok still holds it is refused
        with self.assertRaises(gs.GraphError) as ctx:
            spawn.spawn_agent("sf_human_second", home="/tmp/crew_foremantest/sf_human_second",
                              launch=False, launch_cmd="true", actor="human", foreman=True)
        self.assertIn("sf_human_ok", str(ctx.exception))


# --------------------------------------------------------------------------- #
# unit — identity.py: "## Graph powers" section
# --------------------------------------------------------------------------- #
class GraphPowersIdentityTests(unittest.TestCase):
    QUOTA = {"agents_used": 3, "max_agents": 12, "spawns_this_hour": 1,
            "spawn_rate": 4, "max_turns_ceiling": 30, "token_cap_ceiling": 500000,
            "cost_cap_ceiling": 5.0}

    def test_present_when_can_edit_graph(self):
        agent = {"name": "gp_agent", "home": "/tmp/x", "can_edit_graph": True}
        text = identity.render_identity_md(agent, [], quota=self.QUOTA)
        self.assertIn("## Graph powers", text)
        self.assertIn(guard.FOREMAN_ENVELOPE_SENTENCE, text)
        self.assertIn("3/12", text)
        self.assertIn("1/4", text)
        self.assertIn("spawn-agent", text)
        self.assertIn("unblessed", text)

    def test_absent_when_not_can_edit_graph(self):
        agent = {"name": "gp_agent2", "home": "/tmp/x", "can_edit_graph": False}
        text = identity.render_identity_md(agent, [], quota=self.QUOTA)
        self.assertNotIn("## Graph powers", text)

    def test_claude_md_also_carries_it(self):
        agent = {"name": "gp_agent3", "home": "/tmp/x", "can_edit_graph": True}
        block = identity.render_claude_md(agent, [], quota=self.QUOTA)
        self.assertIn("## Graph powers", block)
        self.assertIn(guard.FOREMAN_ENVELOPE_SENTENCE, block)

    def test_quota_state_helper_shape(self):
        gs.create_agent("gp_quota_agent", home="/tmp/crew_foremantest/gp_quota_agent")
        q = guard.quota_state()
        for k in ("agents_used", "max_agents", "spawns_this_hour", "spawn_rate",
                 "max_turns_ceiling", "token_cap_ceiling", "cost_cap_ceiling"):
            self.assertIn(k, q)
        self.assertGreaterEqual(q["agents_used"], 1)


# --------------------------------------------------------------------------- #
# live — throwaway project "w3test": CLI rewrites identity.md on disk
# --------------------------------------------------------------------------- #
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CREW_BIN = os.path.join(ROOT, "bin", "crew")
HOME_BASE = "/tmp/crew_tests_foreman"
PROJECT = "w3test"
PROJECT_APP = f"crew-{PROJECT}"


def _run(args, env_extra=None, timeout=30):
    environment = {"CREW_PROJECT": PROJECT}
    if env_extra:
        environment.update(env_extra)
    p = run_operator(
        [sys.executable, CREW_BIN, *args], cwd=ROOT, env_extra=environment,
        capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


@contextlib.contextmanager
def _pinned_app(app):
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
class LiveForemanCliTests(unittest.TestCase):
    def setUp(self):
        self.a = "test_w3_a"
        self.home_a = os.path.join(HOME_BASE, self.a)

    def tearDown(self):
        for n in (self.a,):
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
            with open(config._projects_file(), "w") as fh:
                json.dump([n for n in names if n != config.DEFAULT_PROJECT], fh)
        except OSError:
            pass

    def test_foreman_designation_rewrites_identity_md_on_disk(self):
        rc, out, err = _run(["project", "create", PROJECT])
        self.assertEqual(rc, 0, f"project create failed: {out!r} {err!r}")

        rc, out, err = _run(["spawn-agent", self.a, "--home", self.home_a,
                             "--launch-cmd", "true", "--no-launch"])
        self.assertEqual(rc, 0, f"spawn failed: {out!r} {err!r}")

        rc, out, err = _run(["foreman", self.a])
        self.assertEqual(rc, 0, f"crew foreman failed: {out!r} {err!r}")

        path = os.path.join(self.home_a, config.IDENTITY_FILE)
        with open(path) as f:
            text = f.read()
        self.assertIn("## Graph powers", text)
        self.assertIn("you may wire only agents you created, plus yourself", text)

        with _pinned_app(PROJECT_APP):
            refreshed = gs.get_agent_by_name(self.a)
        self.assertTrue(refreshed["can_edit_graph"])

        # revoke works too
        rc, out, err = _run(["foreman", self.a, "--revoke"])
        self.assertEqual(rc, 0, f"crew foreman --revoke failed: {out!r} {err!r}")
        with _pinned_app(PROJECT_APP):
            refreshed = gs.get_agent_by_name(self.a)
        self.assertFalse(refreshed["can_edit_graph"])


# --------------------------------------------------------------------------- #
# live — dashboard endpoints (real, already-running dashboard on :8788, app "crew")
# --------------------------------------------------------------------------- #
BASE = "http://127.0.0.1:" + os.environ.get("CREW_PORT", "8788")
CAPFILE = os.path.join(ROOT, "var", f"dashboard-{os.environ.get('CREW_PORT', '8788')}.cap")
_AUTH_CAP = None
_AUTH_OPENER = None
REAL_AGENTS = {"leads", "builder", "sales", "AgentA", "AgentB"}
NAME_PREFIX = "test_w3dash_"
RUN_ID = str(int(time.time()))


def _dashboard_opener():
    global _AUTH_CAP, _AUTH_OPENER
    try:
        with open(CAPFILE) as fh:
            cap = fh.read().strip()
    except OSError:
        cap = ""
    if cap and (cap != _AUTH_CAP or _AUTH_OPENER is None):
        jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        request = urllib.request.Request(
            BASE + "/api/auth/bootstrap",
            data=json.dumps({"capability": cap}).encode(), method="POST",
            headers={"Content-Type": "application/json", "X-Crew-CSRF": "1"})
        with opener.open(request, timeout=10) as response:
            result = json.load(response)
        if not result.get("ok"):
            raise RuntimeError(f"dashboard operator bootstrap failed: {result!r}")
        _AUTH_CAP, _AUTH_OPENER = cap, opener
    return _AUTH_OPENER or urllib.request.build_opener()


def _http(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if method == "POST":
        req.add_header("X-Crew-CSRF", "1")
    try:
        opener = _dashboard_opener()
        with opener.open(req, timeout=10) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw.decode(errors="replace")
    except urllib.error.URLError:
        return None, None


def _get(path):
    return _http("GET", path)


def _post(path, body=None):
    return _http("POST", path, body if body is not None else {})


def _assert_test_name(name):
    if not name or not name.startswith(NAME_PREFIX) or name in REAL_AGENTS:
        raise AssertionError(f"refusing to operate on non-{NAME_PREFIX} agent {name!r}")
    return name


def _remove_dash_agent(name):
    _assert_test_name(name)
    try:
        _post("/api/agent/remove", {"name": name})
    except Exception:
        pass


def _dashboard_reachable():
    status, _ = _get("/api/graph/snapshot")
    return status == 200


@unittest.skipUnless(os.environ.get("CREW_LIVE_TESTS", "1") == "1",
                     "set CREW_LIVE_TESTS=0 to skip live dashboard tests")
@unittest.skipUnless(_dashboard_reachable(),
                     "real dashboard not reachable on :8788 — start it first")
class LiveDashboardForemanTests(unittest.TestCase):
    def setUp(self):
        self.src = _assert_test_name(f"{NAME_PREFIX}src_{RUN_ID}")
        self.tgt = _assert_test_name(f"{NAME_PREFIX}tgt_{RUN_ID}")
        for n in (self.src, self.tgt):
            status, body = _post("/api/agent/create", {
                "name": n, "home": f"/tmp/crew_tests/{n}", "launch": False,
                "launch_cmd": "true"})
            self.assertTrue(body.get("ok"), f"setup failed for {n}: {body}")

    def tearDown(self):
        _remove_dash_agent(self.src)
        _remove_dash_agent(self.tgt)

    def test_snapshot_exposes_new_agent_and_edge_fields(self):
        status, body = _get("/api/graph/snapshot")
        self.assertEqual(status, 200)
        by_name = {a["name"]: a for a in body["agents"]}
        a = by_name[self.src]
        for k in ("kind", "can_edit_graph", "blessed", "created_by"):
            self.assertIn(k, a, f"agent snapshot missing {k!r}")

        status, body = _post("/api/edge/create", {
            "source": self.src, "target": self.tgt, "token_cap": 2000, "cost_cap": 0.5})
        self.assertTrue(body.get("ok"), body)
        edge = body["edge"]
        self.assertEqual(int(edge.get("token_cap")), 2000)
        self.assertEqual(float(edge.get("cost_cap")), 0.5)

        status, body = _get("/api/graph/snapshot")
        found = next(e for e in body["edges"] if e["_guid"] == edge["_guid"])
        for k in ("blessed", "created_by", "token_cap", "cost_cap"):
            self.assertIn(k, found, f"edge snapshot missing {k!r}")
        self.assertEqual(int(found["token_cap"]), 2000)

    def test_edge_update_persists_token_and_cost_cap(self):
        status, body = _post("/api/edge/create", {
            "source": self.src, "target": self.tgt, "token_cap": 5000, "cost_cap": 1.0})
        self.assertTrue(body.get("ok"), body)
        guid = body["edge"]["_guid"]
        status, body = _post("/api/edge/update", {
            "guid": guid, "token_cap": 100, "cost_cap": 0.1})
        self.assertTrue(body.get("ok"), body)
        self.assertEqual(int(body["edge"]["token_cap"]), 100)
        self.assertEqual(float(body["edge"]["cost_cap"]), 0.1)

    def test_bless_endpoints_flip_rows(self):
        # this agent is human-created via the dashboard -> already blessed by
        # default, so bless it via a foreman-created row instead: use a direct
        # graphstore call pinned to the live "crew" app to make an unblessed
        # fixture, then flip it through the HTTP endpoint.
        prev = os.environ.get("CREW_APP")
        os.environ["CREW_APP"] = "crew"
        try:
            src_agent = gs.get_agent_by_name(self.src)
            foreman_name = f"{NAME_PREFIX}foreman_{RUN_ID}"
            foreman = gs.create_agent(foreman_name, home=f"/tmp/crew_tests/{foreman_name}",
                                      can_edit_graph=True)
            kid = gs.create_agent(f"{NAME_PREFIX}kid_{RUN_ID}",
                                  home=f"/tmp/crew_tests/{NAME_PREFIX}kid_{RUN_ID}",
                                  actor=foreman_name)
            self.assertFalse(kid["blessed"])
            edge = gs.create_edge(foreman["_guid"], kid["_guid"], actor=foreman_name,
                                  max_turns=5, token_cap=1000, cost_cap=1.0)
            self.assertFalse(edge["blessed"])
        finally:
            if prev is None:
                os.environ.pop("CREW_APP", None)
            else:
                os.environ["CREW_APP"] = prev

        try:
            status, body = _post("/api/agent/bless", {"name": kid["name"]})
            self.assertEqual(status, 200)
            self.assertTrue(body.get("ok"), body)
            status, body = _post("/api/edge/bless", {"guid": edge["_guid"]})
            self.assertEqual(status, 200)
            self.assertTrue(body.get("ok"), body)

            os.environ["CREW_APP"] = "crew"
            self.assertTrue(gs.get_agent_by_name(kid["name"])["blessed"])
            self.assertTrue(gs.get_object(edge["_guid"])["blessed"])
        finally:
            os.environ["CREW_APP"] = "crew"
            for n in (kid["name"], foreman_name):
                _remove_dash_agent_unsafe(n)
            if prev is None:
                os.environ.pop("CREW_APP", None)
            else:
                os.environ["CREW_APP"] = prev


def _remove_dash_agent_unsafe(name):
    """Cleanup for fixtures created directly via gs.create_agent (not the
    NAME_PREFIX-guarded dashboard helper above) — still namespaced to
    test_w3dash_, checked explicitly here rather than via _assert_test_name."""
    if not name.startswith(NAME_PREFIX):
        return
    try:
        _post("/api/agent/remove", {"name": name})
    except Exception:
        pass


if __name__ == "__main__":
    unittest.main(verbosity=2)
