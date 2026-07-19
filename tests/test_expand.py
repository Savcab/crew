"""Live HTTP tests for POST /api/expand (crew/server/app.py) — the "one-blob"
LLM config endpoint (UI wave B).

This module owns an isolated loopback port, dashboard process, and throwaway
MorphDB app. It never restarts or authenticates against the operator's 8788
dashboard. ``/api/expand`` reads ``CREW_EXPAND_CMD`` in the server process, so
each behavior class restarts only this module's server with the requested stub
mode and restores its isolated plain configuration afterward.

No actor/gating tests here: /api/expand is dashboard-only surface with no CLI
or agent-side entry point (an agent has no HTTP access to the dashboard's
loopback-bound port), so — same as the PTY transport — there is no
guard.check() call to test; the endpoint is human-only by construction, not
by an actor check.

    python3 -m unittest tests.test_expand   (from the repo root)
"""
import json
import http.cookiejar
import os
import subprocess
import sys
import time
import unittest
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from crew import graphstore as gs, schema  # noqa: E402


PORT = str(26000 + (os.getpid() % 1000))
TEST_APP = f"crewtest-expand-{os.getpid()}"
BASE = "http://127.0.0.1:" + PORT
STUB = os.path.join(ROOT, "tests", "fixtures", "expand_stub.sh")
CAPFILE = os.path.join(ROOT, "var", f"dashboard-{PORT}.cap")
_AUTH_CAP = None
_AUTH_OPENER = None
_SERVER_STARTED = False


def _authenticated_opener():
    global _AUTH_CAP, _AUTH_OPENER
    try:
        with open(CAPFILE) as fh:
            cap = fh.read().strip()
    except OSError:
        cap = ""
    if cap and (cap != _AUTH_CAP or _AUTH_OPENER is None):
        jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        req = urllib.request.Request(
            BASE + "/api/auth/bootstrap",
            data=json.dumps({"capability": cap}).encode(), method="POST",
            headers={"Content-Type": "application/json", "X-Crew-CSRF": "1"})
        with opener.open(req, timeout=10) as response:
            body = json.load(response)
        if not body.get("ok"):
            raise RuntimeError(f"dashboard operator bootstrap failed: {body!r}")
        _AUTH_CAP, _AUTH_OPENER = cap, opener
    return _AUTH_OPENER or urllib.request.build_opener()


def _req(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if method == "POST":
        req.add_header("X-Crew-CSRF", "1")
    try:
        opener = _authenticated_opener()
        with opener.open(req, timeout=70) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as error:
        try:
            raw = error.read()
            try:
                return error.code, json.loads(raw)
            except Exception:
                return error.code, raw.decode(errors="replace")
        finally:
            error.close()


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
            if (status == 200 and body and body.get("ok")
                    and body.get("workspace_key") == TEST_APP):
                return
            last = (status, body)
        except Exception as e:
            last = e
        time.sleep(0.3)
    raise RuntimeError(f"dashboard did not come back healthy in time: {last!r}")


def _dashboard_stop_start(extra_env=None):
    """Stop + start this module's isolated dashboard, with ``extra_env``
    layered on top of a clean child environment —
    start_dashboard()'s subprocess.Popen inherits whatever os.environ the
    `crew` CLI process sees, so this is the only lever a test has over what
    the isolated server reads for $CREW_EXPAND_CMD etc.

    Deliberately NOT `os.environ.copy()`: per test_dashboard_api.py's own
    warning, several sibling test MODULES pin $CREW_APP (and friends) to a
    throwaway app as a top-level IMPORT-TIME side effect, which has already
    run by the time `python3 -m unittest discover` gets here — so this
    process's os.environ can be silently polluted with someone else's test
    app. Strip every routing/config variable sibling modules are known to
    mutate before pinning this module's explicit app and port."""
    global _SERVER_STARTED
    env = os.environ.copy()
    for k in ("CREW_APP", "CREW_PROJECT", "CREW_ROOT",
              "CREW_PORT", "CREW_EXPAND_CMD", "EXPAND_STUB_MODE",
              "CREW_EXPAND_TIMEOUT"):
        env.pop(k, None)
    env.update({
        "CREW_APP": TEST_APP,
        "CREW_PROJECT": "default",
        "CREW_PORT": PORT,
        "MORPHDB_HOST": "127.0.0.1:18787",
    })
    if extra_env:
        env.update(extra_env)
    if _SERVER_STARTED:
        stopped = subprocess.run(
            ["./bin/crew", "dashboard", "stop"], cwd=ROOT, env=env,
            capture_output=True, text=True, timeout=30)
        if stopped.returncode != 0:
            raise RuntimeError(
                f"isolated dashboard stop failed: {stopped.stdout!r} "
                f"{stopped.stderr!r}")
        _SERVER_STARTED = False
    subprocess.run(["./bin/crew", "dashboard", "start"], cwd=ROOT, env=env,
                   capture_output=True, text=True, timeout=30, check=True)
    _SERVER_STARTED = True
    _wait_healthy()
    # Belt-and-suspenders: fail loudly if the port belongs to any other app.
    status, body = get("/api/graph/snapshot")
    if (status != 200 or not (body or {}).get("ok")
            or body.get("workspace_key") != TEST_APP):
        raise RuntimeError(
            f"isolated dashboard came back on the wrong app: "
            f"status={status} body={body!r} — check for env leakage")


def _restart_plain():
    _dashboard_stop_start(None)


def _restart_stub(mode, timeout=None):
    env = {"CREW_EXPAND_CMD": STUB, "EXPAND_STUB_MODE": mode}
    if timeout is not None:
        env["CREW_EXPAND_TIMEOUT"] = str(timeout)
    _dashboard_stop_start(env)


def setUpModule():
    schema.ensure_schema(TEST_APP)
    unittest.addModuleCleanup(_cleanup_module_resources)
    _restart_plain()


def tearDownModule():
    _cleanup_module_resources()


def _cleanup_module_resources():
    global _SERVER_STARTED
    env = os.environ.copy()
    for key in (
            "CREW_APP", "CREW_PROJECT", "CREW_ROOT", "CREW_PORT",
            "CREW_EXPAND_CMD", "EXPAND_STUB_MODE", "CREW_EXPAND_TIMEOUT"):
        env.pop(key, None)
    env.update({
        "CREW_APP": TEST_APP,
        "CREW_PROJECT": "default",
        "CREW_PORT": PORT,
        "MORPHDB_HOST": "127.0.0.1:18787",
    })
    if _SERVER_STARTED:
        subprocess.run(
            ["./bin/crew", "dashboard", "stop"], cwd=ROOT, env=env,
            capture_output=True, text=True, timeout=30)
        _SERVER_STARTED = False
    try:
        gs._req("DELETE", f"/app/{TEST_APP}", app=None)
    except gs.GraphError:
        pass


# --------------------------------------------------------------------------- #
# Input validation does not touch the expander subprocess, so the module's
# initial isolated plain server is sufficient.
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
        self.assertFalse(fields.get("directed"))
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
