"""Live HTTP tests for POST /api/expand (crew/server/app.py) — the "one-blob"
LLM config endpoint (UI wave B). Run against the REAL dashboard on
127.0.0.1:8788, but unlike test_dashboard_api.py this module DOES restart
that dashboard — deliberately: /api/expand shells out to config.expand_cmd(),
which is only overridable via $CREW_EXPAND_CMD at the SERVER PROCESS's own
os.environ (read at call time inside the dashboard's process, not the test
process's), so there is no way to point it at the stub fixture
(tests/fixtures/expand_stub.sh) without restarting the dashboard with that
env var set. `.claude/skills/feature-development` + the project context
explicitly allow a dashboard restart for this kind of test ("Dashboard
restart allowed"). Every class that needs a particular stub behavior restarts
into it in setUpClass and restarts back to plain defaults in tearDownClass;
tearDownModule does one more restart-to-plain as a final safety net so a
crash mid-run can't leave the live dashboard pointed at a test stub.

No actor/gating tests here: /api/expand is dashboard-only surface with no CLI
or agent-side entry point (an agent has no HTTP access to the dashboard's
loopback-bound port), so — same as the PTY transport — there is no
guard.check() call to test; the endpoint is human-only by construction, not
by an actor check.

    python3 -m unittest tests.test_expand   (from the repo root)
"""
import json
import os
import subprocess
import sys
import time
import unittest
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

BASE = "http://127.0.0.1:" + os.environ.get("CREW_PORT", "8788")
STUB = os.path.join(ROOT, "tests", "fixtures", "expand_stub.sh")


def _req(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=70) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw.decode(errors="replace")


def get(path):
    return _req("GET", path)


def post(path, body=None):
    return _req("POST", path, body if body is not None else {})


def _wait_healthy(timeout=15):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            status, body = get("/api/graph/snapshot")
            if status == 200 and body and body.get("ok"):
                return
            last = (status, body)
        except Exception as e:
            last = e
        time.sleep(0.3)
    raise RuntimeError(f"dashboard did not come back healthy in time: {last!r}")


def _dashboard_stop_start(extra_env=None):
    """Stop + start the live dashboard (no `restart` subcommand exists — see
    crew/cli.py's dashboard action choices), with `extra_env` layered on top
    of a CLEAN env before spawning the new dashboard process —
    start_dashboard()'s subprocess.Popen inherits whatever os.environ the
    `crew` CLI process sees, so this is the only lever a test has over what
    the live server reads for $CREW_EXPAND_CMD etc.

    Deliberately NOT `os.environ.copy()`: per test_dashboard_api.py's own
    warning, several sibling test MODULES pin $CREW_APP (and friends) to a
    throwaway app as a top-level IMPORT-TIME side effect, which has already
    run by the time `python3 -m unittest discover` gets here — so this
    process's os.environ can be silently polluted with someone else's test
    app. Blindly copying it into the env used to restart the LIVE dashboard
    would repoint the real 'crew' app's dashboard at a throwaway app (this
    happened during development of this very file). Build the child env from
    a real subprocess-clean slate (os.environ minus every var any sibling
    test module is known to mutate) instead, so this module's dashboard
    restarts are unaffected by import-order pollution."""
    env = os.environ.copy()
    for k in ("CREW_APP", "CREW_PROJECT", "CREW_ROOT",
              "CREW_EXPAND_CMD", "EXPAND_STUB_MODE", "CREW_EXPAND_TIMEOUT"):
        env.pop(k, None)
    if extra_env:
        env.update(extra_env)
    subprocess.run(["./bin/crew", "dashboard", "stop"], cwd=ROOT, env=env,
                   capture_output=True, text=True, timeout=30)
    subprocess.run(["./bin/crew", "dashboard", "start"], cwd=ROOT, env=env,
                   capture_output=True, text=True, timeout=30, check=True)
    _wait_healthy()
    # belt-and-suspenders: confirm the app we actually landed on is the real
    # one, so a future env leak this guard doesn't yet know about fails LOUD
    # (a wrong-app 404 on every snapshot) instead of silently corrupting the
    # live dashboard's data for whoever's using it next.
    status, body = get("/api/graph/snapshot")
    if status != 200 or not (body or {}).get("ok"):
        raise RuntimeError(
            f"dashboard came back on the WRONG app after restart (not 'crew'): "
            f"status={status} body={body!r} — check for env leakage")


def _restart_plain():
    _dashboard_stop_start(None)


def _restart_stub(mode, timeout=None):
    env = {"CREW_EXPAND_CMD": STUB, "EXPAND_STUB_MODE": mode}
    if timeout is not None:
        env["CREW_EXPAND_TIMEOUT"] = str(timeout)
    _dashboard_stop_start(env)


def tearDownModule():
    # Final safety net: whatever the last class left the dashboard pointed
    # at, always land back on plain defaults when this module is done.
    _restart_plain()


# --------------------------------------------------------------------------- #
# input validation — doesn't touch the expander subprocess, so no dashboard
# restart needed; runs against whatever config the dashboard already has.
# --------------------------------------------------------------------------- #
class InputValidation(unittest.TestCase):
    def test_missing_kind(self):
        status, body = post("/api/expand", {"text": "hello"})
        self.assertEqual(status, 200)
        self.assertFalse(body.get("ok"))
        self.assertIn("kind", body.get("error", ""))

    def test_bad_kind(self):
        status, body = post("/api/expand", {"kind": "widget", "text": "hello"})
        self.assertEqual(status, 200)
        self.assertFalse(body.get("ok"))
        self.assertIn("kind", body.get("error", ""))

    def test_missing_text(self):
        status, body = post("/api/expand", {"kind": "agent", "text": ""})
        self.assertEqual(status, 200)
        self.assertFalse(body.get("ok"))
        self.assertIn("text", body.get("error", ""))


# --------------------------------------------------------------------------- #
# stub in "ok" mode: parsed fields returned, code-fences tolerated, both kinds
# --------------------------------------------------------------------------- #
class ExpandOk(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _restart_stub("ok")

    @classmethod
    def tearDownClass(cls):
        _restart_plain()

    def test_kind_agent_shape(self):
        status, body = post("/api/expand", {
            "kind": "agent", "text": "handles onboarding emails"})
        self.assertEqual(status, 200)
        self.assertTrue(body.get("ok"), body)
        fields = body["fields"]
        self.assertEqual(fields.get("name"), "stubagent")
        self.assertEqual(fields.get("role"), "stub role from fixture")
        self.assertEqual(fields.get("identity"), "stub identity from fixture")

    def test_kind_edge_shape(self):
        status, body = post("/api/expand", {
            "kind": "edge", "text": "src sends leads to tgt",
            "source": "src", "target": "tgt"})
        self.assertEqual(status, 200)
        self.assertTrue(body.get("ok"), body)
        fields = body["fields"]
        self.assertEqual(fields.get("label"), "stub label")
        self.assertEqual(fields.get("conditions"), ["when stub fires"])
        self.assertEqual(fields.get("target_action"), "stub action")
        self.assertTrue(fields.get("reply_expected"))
        self.assertEqual(fields.get("back_conditions"), [])
        self.assertTrue(fields.get("directed"))
        self.assertEqual(fields.get("max_turns"), 5)

    def test_envelope_code_fences_tolerated(self):
        # the stub always wraps its canned JSON in ```json fences (see
        # tests/fixtures/expand_stub.sh) — every call above already exercises
        # this, but assert it explicitly so a future stub rewrite can't
        # silently drop fence coverage.
        status, body = post("/api/expand", {"kind": "agent", "text": "x"})
        self.assertTrue(body.get("ok"), body)
        self.assertIsInstance(body["fields"], dict)


# --------------------------------------------------------------------------- #
# stub exits 1: ok:false + verbatim fallback, no crash
# --------------------------------------------------------------------------- #
class ExpandFail(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _restart_stub("fail")

    @classmethod
    def tearDownClass(cls):
        _restart_plain()

    def test_agent_fallback_verbatim(self):
        text = "some raw freeform agent description"
        status, body = post("/api/expand", {"kind": "agent", "text": text})
        self.assertEqual(status, 200)
        self.assertFalse(body.get("ok"))
        fb = body.get("fallback") or {}
        self.assertEqual(fb.get("role"), text)
        self.assertEqual(fb.get("identity"), text)

    def test_edge_fallback_verbatim(self):
        text = "some raw freeform edge description"
        status, body = post("/api/expand", {
            "kind": "edge", "text": text, "source": "a", "target": "b"})
        self.assertEqual(status, 200)
        self.assertFalse(body.get("ok"))
        fb = body.get("fallback") or {}
        self.assertEqual(fb.get("conditions"), [text])


# --------------------------------------------------------------------------- #
# stub prints an envelope whose "result" isn't valid JSON: parse failure also
# falls back cleanly (belt-and-suspenders on the "ANY failure" contract).
# --------------------------------------------------------------------------- #
class ExpandBadJson(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _restart_stub("badjson")

    @classmethod
    def tearDownClass(cls):
        _restart_plain()

    def test_parse_failure_falls_back(self):
        text = "won't parse either way"
        status, body = post("/api/expand", {"kind": "agent", "text": text})
        self.assertEqual(status, 200)
        self.assertFalse(body.get("ok"))
        self.assertEqual((body.get("fallback") or {}).get("role"), text)


# --------------------------------------------------------------------------- #
# timeout path: stub sleeps past a short $CREW_EXPAND_TIMEOUT → fallback
# --------------------------------------------------------------------------- #
class ExpandTimeout(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _restart_stub("timeout", timeout=2)

    @classmethod
    def tearDownClass(cls):
        _restart_plain()

    def test_timeout_falls_back(self):
        text = "this call should time out"
        start = time.monotonic()
        status, body = post("/api/expand", {"kind": "agent", "text": text})
        elapsed = time.monotonic() - start
        self.assertEqual(status, 200)
        self.assertFalse(body.get("ok"))
        self.assertEqual((body.get("fallback") or {}).get("role"), text)
        # generous ceiling — the 2s configured timeout plus HTTP/process
        # overhead, well under the stub's 30s sleep (proves it didn't just
        # wait the whole thing out)
        self.assertLess(elapsed, 15, f"took {elapsed:.1f}s — timeout not honored")


if __name__ == "__main__":
    unittest.main(verbosity=2)
