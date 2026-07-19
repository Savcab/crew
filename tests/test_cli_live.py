"""Live write-path tests for the `crew` CLI — Area B-cli-live.

These invoke the REAL `./bin/crew` binary as a subprocess against the REAL,
persistent MorphDB app "crew" (the live app with agents leads/builder/sales/
AgentA/AgentB) — NOT a throwaway app — because the schema-drift regression this
matrix flags (SKILL.md's explicit lesson) can only be caught by writing against
that real, possibly-stale app. Every agent this file creates is named with a
"test_" prefix and lives under /tmp/crew_tests/; every one is removed (agent
record + tmux session) in tearDown, even on failure.

Absolute rules obeyed throughout:
  * every spawn passes --no-launch (never boots a real claude);
  * `crew up`/`crew down`/`crew restart` are only ever exercised against our own
    test_-prefixed agents, and NEVER with --all (which would touch every real
    agent's session too);
  * the real agents' names (leads, builder, sales, AgentA, AgentB) are never
    referenced by any operation that mutates state.

Run:
    python3 -m unittest tests.test_cli_live -v          (from the repo root)
    python3 -m unittest discover tests                  (full suite)
"""
import os
import subprocess
import sys
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CREW_BIN = os.path.join(ROOT, "bin", "crew")
HOME_BASE = "/tmp/crew_tests"

# Real agent names this file must NEVER create, remove, connect, or otherwise
# mutate — a guard used by _agent_name() below, not just documentation.
REAL_AGENTS = {"leads", "builder", "sales", "AgentA", "AgentB"}

sys.path.insert(0, ROOT)
from crew import graphstore as gs  # noqa: E402  (only used for setUpModule cleanup)


def _run(args, env_extra=None, timeout=30):
    """Run `./bin/crew <args>` as a real subprocess against the live app.

    Builds the env explicitly (a copy of the current environment with CREW_APP
    stripped so it always resolves to the default 'crew' app regardless of what
    any other test module did to os.environ in-process) so this is safe to run
    standalone or as part of the full suite in any order."""
    env = dict(os.environ)
    env.pop("CREW_APP", None)  # → config.DEFAULT_APP ("crew"), the real live app
    if env_extra:
        env.update(env_extra)
    p = subprocess.run([sys.executable, CREW_BIN, *args], cwd=ROOT, env=env,
                       capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


def _agent_name(suffix):
    name = f"test_cli_{suffix}_{int(time.time() * 1000) % 1000000}_{os.getpid()}"
    assert name not in REAL_AGENTS  # sanity: a generated test_ name can never collide
    return name


def _tmux_has_session(name):
    ok = subprocess.run(["tmux", "has-session", "-t", name],
                        capture_output=True, timeout=5).returncode == 0
    return ok


def _wait_no_queued(target, timeout=20.0, poll=1.0):
    """Poll until `target`'s queued backlog is empty, or timeout.

    `crew connect` queues a "your connections changed" notice (mail.deliver
    with sender='crew') to both ends via rewrite_identity(notify=True) — an
    unavoidable side effect of connecting two agents. If a live dashboard
    happens to be running against this same app, its OWN background flusher
    (a separate process, ~4s tick) may race our test's inline flush for the
    per-target typing lock and win, so a fixed sleep can't make this
    deterministic — poll instead. Returns (drained: bool, last `crew mail`
    output) so a caller can show it on failure."""
    deadline = time.monotonic() + timeout
    out = ""
    while time.monotonic() < deadline:
        rc, out, err = _run(["mail", target, "--status", "queued", "-n", "5"])
        if rc == 0 and out.strip() == "(no messages)":
            return True, out
        time.sleep(poll)
    return False, out


def _remove_test_agent(name, keep_session=False):
    """Best-effort cleanup: never raises, so it's safe inside a finally block even
    if the agent was already removed by the test itself."""
    assert name.startswith("test_") and name not in REAL_AGENTS
    args = ["remove-agent", name] + (["--keep-session"] if keep_session else [])
    try:
        _run(args, timeout=15)
    except Exception:
        pass
    # belt-and-suspenders: if the record is already gone but a session/tmux
    # leftover survived (e.g. a crashed test), kill it directly.
    if not keep_session and _tmux_has_session(name):
        subprocess.run(["tmux", "kill-session", "-t", name], capture_output=True)


def _cleanup_orphaned_test_agents():
    """Remove any leftover agent from a PRIOR run of THIS file, in the REAL app.

    Scoped to this file's own "test_cli_" sub-prefix, not the whole "test_"
    namespace: the live "crew" app is shared by several concurrent live-test
    authors in this session, each minting their own "test_<area>_*" agents, so
    a broad "test_*" sweep here would race and delete another module's in-flight
    fixtures (and vice versa) — observed live while writing this file. Never
    touches a REAL_AGENTS name (that's also true of the "test_cli_" prefix by
    construction, but checked anyway)."""
    try:
        for a in gs.list_agents():
            name = a.get("name") or ""
            if name.startswith("test_cli_") and name not in REAL_AGENTS:
                _remove_test_agent(name)
    except gs.GraphError:
        pass  # MorphDB unreachable — individual tests will fail loudly anyway


_prev_app = None


def setUpModule():
    # Defensive: pin CREW_APP to the real app for any DIRECT graphstore calls this
    # module makes (cleanup, assertions) regardless of what ran before us in the
    # same `unittest discover` process. Saved + restored in tearDownModule so
    # later modules' test methods don't inherit the REAL app (that exact leak
    # put ~74 bogus objects in production data on 2026-07-18 — throwaway-app
    # modules now also re-pin in their own setUpModule as a second layer).
    global _prev_app
    _prev_app = os.environ.get("CREW_APP")
    os.environ["CREW_APP"] = "crew"
    _cleanup_orphaned_test_agents()


def tearDownModule():
    _cleanup_orphaned_test_agents()
    if _prev_app is None:
        os.environ.pop("CREW_APP", None)
    else:
        os.environ["CREW_APP"] = _prev_app


class SpawnConnectDisconnectRemove(unittest.TestCase):
    """The core live write path: spawn-agent --no-launch, connect (with budget
    caps — the schema-drift regression check), disconnect, remove-agent."""

    def setUp(self):
        self.a = _agent_name("a")
        self.b = _agent_name("b")
        self.homes = []

    def tearDown(self):
        _remove_test_agent(self.a)
        _remove_test_agent(self.b)

    def _spawn(self, name):
        home = os.path.join(HOME_BASE, name)
        self.homes.append(home)
        rc, out, err = _run(["spawn-agent", name, "--home", home,
                             "--role", "a test agent", "--no-launch"])
        self.assertEqual(rc, 0, f"spawn-agent failed: stdout={out!r} stderr={err!r}")
        self.assertIn(f"spawned agent '{name}'", out)
        return home

    def test_spawn_agent_no_launch_creates_real_tmux_session(self):
        home = self._spawn(self.a)
        # spawn-agent --no-launch still creates a REAL tmux session (bare bash in
        # the 'claude' window) — only the launch command is skipped.
        self.assertTrue(_tmux_has_session(self.a),
                        "expected a real (bare-bash) tmux session for --no-launch")
        self.assertTrue(os.path.isdir(home))
        self.assertTrue(os.path.isfile(os.path.join(home, "identity.md")))
        # "claude is booting" line must NOT appear for --no-launch
        rc, out, err = _run(["spawn-agent", self.b, "--home",
                            os.path.join(HOME_BASE, self.b), "--no-launch"])
        self.homes.append(os.path.join(HOME_BASE, self.b))
        self.assertEqual(rc, 0)
        self.assertNotIn("claude is booting", out)

    def test_duplicate_name_rejected(self):
        self._spawn(self.a)
        rc, out, err = _run(["spawn-agent", self.a, "--home",
                            os.path.join(HOME_BASE, self.a + "_dup"), "--no-launch"])
        self.assertNotEqual(rc, 0)
        self.assertIn("already exists", err)

    def test_connect_with_budget_caps_then_disconnect(self):
        """Schema-drift regression: create an edge with --token-cap/--cost-cap
        against the REAL, persistent 'crew' app (not a freshly-created throwaway
        app, which is never stale by construction — see SKILL.md)."""
        self._spawn(self.a)
        self._spawn(self.b)

        rc, out, err = _run(["connect", self.a, self.b,
                            "--label", "test-link",
                            "--when", "when testing happens",
                            "--does", "acknowledge",
                            "--token-cap", "1000", "--cost-cap", "0.5"])
        self.assertEqual(rc, 0, f"connect failed: stdout={out!r} stderr={err!r}")
        self.assertIn(f"connected {self.a} -> {self.b}", out)
        self.assertIn("(test-link)", out)
        self.assertIn("when testing happens", out)

        rc, out, err = _run(["edges"])
        self.assertEqual(rc, 0)
        self.assertIn(f"{self.a} -> {self.b}", out)
        self.assertIn("[test-link]", out)
        # identity.edge_budget format: "1,000 tok/hr + $0.5/hr"
        self.assertIn("1,000 tok/hr", out)
        self.assertIn("$0.5/hr", out)

        rc, out, err = _run(["peers", self.a])
        self.assertEqual(rc, 0)
        self.assertIn(f"→ {self.b}", out)

        rc, out, err = _run(["disconnect", self.a, self.b])
        self.assertEqual(rc, 0)
        self.assertIn(f"disconnected {self.a} and {self.b} (1 edge(s))", out)

        rc, out, err = _run(["edges"])
        self.assertEqual(rc, 0)
        self.assertNotIn(f"{self.a} -> {self.b}", out)

        # disconnecting again is a no-op, not an error
        rc, out, err = _run(["disconnect", self.a, self.b])
        self.assertEqual(rc, 0)
        self.assertIn(f"(no edges between {self.a} and {self.b})", out)

    def test_undirected_connect(self):
        self._spawn(self.a)
        self._spawn(self.b)
        rc, out, err = _run(["connect", self.a, self.b, "--undirected"])
        self.assertEqual(rc, 0)
        self.assertIn(f"connected {self.a} <-> {self.b}", out)
        rc, out, err = _run(["edges"])
        self.assertIn(f"{self.a} <-> {self.b}", out)

    def test_remove_agent_kills_session_and_drops_record(self):
        self._spawn(self.a)
        self.assertTrue(_tmux_has_session(self.a))
        rc, out, err = _run(["remove-agent", self.a])
        self.assertEqual(rc, 0, f"remove-agent failed: stdout={out!r} stderr={err!r}")
        self.assertIn(f"removed agent '{self.a}'", out)
        self.assertIn(f"killed session '{self.a}'", out)
        self.assertFalse(_tmux_has_session(self.a),
                         "tmux session should be gone after remove-agent")
        rc, out, err = _run(["agents"])
        self.assertNotIn(self.a, out)

    def test_remove_agent_keep_session(self):
        self._spawn(self.a)
        rc, out, err = _run(["remove-agent", self.a, "--keep-session"])
        self.assertEqual(rc, 0)
        self.assertIn(f"removed agent '{self.a}'", out)
        self.assertNotIn("killed session", out)
        self.assertTrue(_tmux_has_session(self.a),
                        "session should survive --keep-session")
        # cleanup the orphaned session ourselves (agent record is already gone,
        # so the normal remove-agent-based cleanup in tearDown can't find it)
        subprocess.run(["tmux", "kill-session", "-t", self.a], capture_output=True)


class UpDownRestart(unittest.TestCase):
    """crew up/down/restart against ONE of our own test_ agents. NEVER --all
    (ground rule 3): to test 'up' safely without booting a real claude, the agent
    is spawned with an inert --launch-cmd so a revive only ever types a harmless
    command into a bare bash pane."""

    def setUp(self):
        self.name = _agent_name("lifecycle")
        home = os.path.join(HOME_BASE, self.name)
        rc, out, err = _run(["spawn-agent", self.name, "--home", home,
                             "--launch-cmd", "true", "--no-launch"])
        self.assertEqual(rc, 0, f"setup spawn failed: {out!r} {err!r}")

    def tearDown(self):
        _remove_test_agent(self.name)

    def test_down_then_up_then_restart(self):
        self.assertTrue(_tmux_has_session(self.name))

        rc, out, err = _run(["down", self.name])
        self.assertEqual(rc, 0, f"down failed: {out!r} {err!r}")
        self.assertIn(f"{self.name}  stopped", out)
        self.assertFalse(_tmux_has_session(self.name))

        # down again on an already-down agent
        rc, out, err = _run(["down", self.name])
        self.assertEqual(rc, 0)
        self.assertIn(f"{self.name}  already down", out)

        # up revives it — spawn.start_session ALWAYS types the stored launch_cmd
        # ("true", inert) into the pane; this is the only safe way to exercise
        # the revive path live (ground rule 3).
        rc, out, err = _run(["up", self.name])
        self.assertEqual(rc, 0, f"up failed: {out!r} {err!r}")
        self.assertIn(f"{self.name}  started", out)
        self.assertTrue(_tmux_has_session(self.name))

        # up again on an already-up agent is a skip, not an error
        rc, out, err = _run(["up", self.name])
        self.assertEqual(rc, 0)
        self.assertIn(f"{self.name}  already up — skipped", out)

        # restart = down + up
        rc, out, err = _run(["restart", self.name])
        self.assertEqual(rc, 0, f"restart failed: {out!r} {err!r}")
        self.assertIn(f"{self.name}  restarted", out)
        self.assertTrue(_tmux_has_session(self.name))

    def test_lifecycle_commands_require_name_or_all(self):
        rc, out, err = _run(["down"])
        self.assertNotEqual(rc, 0)
        self.assertIn("give an agent name or --all", err)


class MessageDeliveryLivePane(unittest.TestCase):
    """Exercises mail.deliver() end-to-end against a --no-launch agent's bare-bash
    pane — the safe way to live-test the whole delivery/terminal pipeline without
    a real claude (ground rule 1). Message bodies are kept inert shell text."""

    def setUp(self):
        self.sender = _agent_name("sender")
        self.target = _agent_name("target")
        self.outsider = _agent_name("outsider")
        for n in (self.sender, self.target, self.outsider):
            rc, out, err = _run(["spawn-agent", n, "--home",
                                os.path.join(HOME_BASE, n), "--no-launch"])
            self.assertEqual(rc, 0, f"setup spawn of {n} failed: {out!r} {err!r}")
        rc, out, err = _run(["connect", self.sender, self.target,
                            "--when", "when there is news"])
        self.assertEqual(rc, 0, f"setup connect failed: {out!r} {err!r}")
        # connect() queues a "connections changed" notice to both ends (see
        # _wait_no_queued) — drain it so our own message test below observes a
        # clean queue and can assert on IMMEDIATE delivery, not the (also
        # correct, but harder to assert deterministically) eventual-delivery
        # fallback path.
        drained, out = _wait_no_queued(self.target)
        self.assertTrue(drained,
                        f"connect's 'connections changed' notice never drained "
                        f"from {self.target}'s queue: {out!r}")

    def tearDown(self):
        for n in (self.sender, self.target, self.outsider):
            _remove_test_agent(n)

    def test_authorized_message_delivers_to_bare_bash_pane(self):
        # whoami() resolves via $CREW_AGENT since there's no owning $TMUX_PANE in
        # this test process; identity spoofing via CREW_AGENT is exactly what
        # whoami()'s $TMUX_PANE-first check exists to prevent for an IN-pane
        # caller, which this subprocess is not.
        rc, out, err = _run(["message", self.target, "hello"],
                            env_extra={"CREW_AGENT": self.sender},
                            timeout=20)
        self.assertEqual(rc, 0, f"message failed: stdout={out!r} stderr={err!r}")
        self.assertTrue(out.startswith("[crew] "))
        self.assertIn("delivered to", out)

        rc, out, err = _run(["mail", self.target, "-n", "5"])
        self.assertEqual(rc, 0)
        self.assertIn(f"{self.sender} → {self.target}", out)
        self.assertIn("delivered", out)

    def test_unauthorized_message_blocked(self):
        rc, out, err = _run(["message", self.target, "hello"],
                            env_extra={"CREW_AGENT": self.outsider},
                            timeout=20)
        self.assertEqual(rc, 1)
        self.assertIn("BLOCKED", err)
        self.assertIn(f"'{self.outsider}' has no relationship to '{self.target}'", err)

        rc, out, err = _run(["mail", self.target, "--status", "blocked", "-n", "5"])
        self.assertEqual(rc, 0)
        self.assertIn(f"{self.outsider} → {self.target}", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
