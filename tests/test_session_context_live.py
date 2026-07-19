"""Live isolation test for the environment of a managed tmux session.

The fixture owns one throwaway MorphDB app, one uniquely named tmux session, and
one temporary home. It never starts a coding runtime and cleans only those exact
resources.
"""
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from unittest import mock


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CREW_BIN = os.path.join(ROOT, "bin", "crew")
sys.path.insert(0, ROOT)

from crew import config, graphstore as gs, schema, spawn  # noqa: E402


def _tmux_run(args, **kwargs):
    """Run one test tmux command against Crew's explicit private endpoint."""
    return subprocess.run(
        config.tmux_command(*args), env=config.tmux_environment(), **kwargs)


class InvalidProjectSelectorLive(unittest.TestCase):
    def test_invalid_env_selector_fails_before_backend_or_filesystem(self):
        with tempfile.TemporaryDirectory(prefix="crew-invalid-project-") as root:
            name = f"invalid_project_{os.getpid()}"
            env = dict(os.environ)
            env.update({
                "CREW_PROJECT": "../../escape",
                "CREW_ROOT": root,
                # An early selector error must win over this unreachable backend.
                "MORPHDB_HOST": "127.0.0.1:1",
            })
            env.pop("CREW_APP", None)
            result = subprocess.run(
                [sys.executable, CREW_BIN, "spawn-agent", name, "--no-launch"],
                cwd=ROOT, env=env, capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn("invalid project", result.stderr)
            self.assertNotIn("cannot reach MorphDB", result.stderr)
            self.assertEqual(os.listdir(root), [])


class RuntimeLaunchFailureLive(unittest.TestCase):
    def test_nonexistent_real_tmux_target_is_a_launch_error(self):
        missing = f"crew_missing_launch_{os.getpid()}_{int(time.time() * 1000)}"
        with self.assertRaisesRegex(gs.GraphError, "type runtime launch command"):
            spawn._launch_runtime(missing, "/tmp", "true", "custom")


class CrossProjectHomeOverlapLive(unittest.TestCase):
    def setUp(self):
        run_id = f"{int(time.time() * 1000) % 100000000}_{os.getpid()}"
        self.project_a = f"ha{run_id}"[:32]
        self.project_b = f"hb{run_id}"[:32]
        self.app_a = config.project_app(self.project_a)
        self.app_b = config.project_app(self.project_b)
        self.name_a = f"homea_{run_id}"[:64]
        self.name_b = f"homeb_{run_id}"[:64]
        self.session_a = config.session_name(self.project_a, self.name_a)
        self.session_b = config.session_name(self.project_b, self.name_b)
        self.tmp = tempfile.mkdtemp(prefix="crew-cross-project-home-")
        self.shared = os.path.join(self.tmp, "shared")
        self.addCleanup(self._cleanup_fixture)
        for app in (self.app_a, self.app_b):
            schema.ensure_schema(app)

    def _project_env(self, project):
        env = dict(os.environ)
        env.pop("CREW_APP", None)
        env.update({"CREW_PROJECT": project, "CREW_ROOT": self.tmp})
        return env

    def _cleanup_fixture(self):
        for project, name, session in (
                (self.project_a, self.name_a, self.session_a),
                (self.project_b, self.name_b, self.session_b)):
            try:
                with mock.patch.dict(os.environ, self._project_env(project), clear=True):
                    if gs.get_agent_by_name(name):
                        spawn.remove_agent(name)
            except Exception:
                pass
            _tmux_run(["kill-session", "-t", f"={session}"],
                      capture_output=True, text=True)
        for app in (self.app_a, self.app_b):
            try:
                gs._req("DELETE", f"/app/{app}", app=None)
            except gs.GraphError:
                pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_nested_home_in_second_project_is_refused_before_side_effects(self):
        known = [config.DEFAULT_PROJECT, self.project_a, self.project_b]
        with mock.patch.object(config, "list_known_projects", return_value=known), \
             mock.patch.dict(os.environ, self._project_env(self.project_a), clear=True):
            spawn.spawn_agent(self.name_a, home=self.shared, launch=False)

        nested = os.path.join(self.shared, "nested")
        with mock.patch.object(config, "list_known_projects", return_value=known), \
             mock.patch.dict(os.environ, self._project_env(self.project_b), clear=True):
            with self.assertRaisesRegex(gs.GraphError, "overlaps agent"):
                spawn.spawn_agent(self.name_b, home=nested, launch=False)

        self.assertFalse(os.path.exists(nested))
        session = _tmux_run(
            ["has-session", "-t", f"={self.session_b}"],
            capture_output=True, text=True, timeout=5)
        self.assertNotEqual(session.returncode, 0)


class IsolatedSessionContextLive(unittest.TestCase):
    def setUp(self):
        run_id = f"{int(time.time() * 1000) % 100000000}_{os.getpid()}"
        self.app = f"crewtest-sessionctx-{run_id}"
        self.project = f"ctx{run_id}"[:32]
        self.name = f"ctxagent_{run_id}"[:64]
        self.session = f"{self.project}__{self.name}"
        self.tmp = tempfile.mkdtemp(prefix="crew-session-context-")
        self.home = os.path.join(self.tmp, "agent-home")
        self.host = config.morphdb_base()
        self.context = {
            "CREW_APP": self.app,
            "CREW_PROJECT": self.project,
            "CREW_ROOT": self.tmp,
            "CREW_RUNTIME": "codex",
            "MORPHDB_HOST": self.host,
        }
        # addCleanup runs even when setUp itself fails; tearDown would not. Every
        # destructive cleanup remains scoped to this test's exact app/session/home.
        self.addCleanup(self._cleanup_fixture)
        init = self._run(["init", "--no-dashboard"])
        self.assertEqual(init.returncode, 0, init.stdout + init.stderr)

    def _cleanup_fixture(self):
        try:
            self._run(["remove-agent", self.name])
        except Exception:
            pass
        _tmux_run(["kill-session", "-t", f"={self.session}"],
                  capture_output=True, text=True)
        try:
            url = (self.host.rstrip("/") + "/app/" +
                   urllib.parse.quote(self.app, safe=""))
            urllib.request.urlopen(
                urllib.request.Request(url, method="DELETE"), timeout=10).read()
        except (OSError, urllib.error.URLError):
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, args, env=None):
        child_env = dict(os.environ)
        for key in ("CREW_APP", "CREW_PROJECT", "CREW_ROOT", "CREW_RUNTIME",
                    "MORPHDB_HOST", "CREW_AGENT", "AGENT_MAIL_NAME", "TMUX_PANE"):
            child_env.pop(key, None)
        child_env.update(self.context)
        if env:
            child_env.update(env)
        return subprocess.run(
            [sys.executable, CREW_BIN, *args], cwd=ROOT, env=child_env,
            capture_output=True, text=True, timeout=30)

    def _session_value(self, key):
        result = _tmux_run(
            ["show-environment", "-t", f"={self.session}", key],
            capture_output=True, text=True, timeout=5)
        self.assertEqual(
            result.returncode, 0,
            f"managed session is missing {key}: {result.stdout!r} {result.stderr!r}")
        prefix = key + "="
        self.assertTrue(result.stdout.startswith(prefix), result.stdout)
        return result.stdout.rstrip("\n")[len(prefix):]

    def _pane_run(self, session, command, marker, timeout=20):
        """Run one command in the managed pane and return its exit code/output."""
        target = _tmux_run(
            ["list-panes", "-t", f"={session}", "-F", "#{pane_id}"],
            check=True, capture_output=True, text=True, timeout=5,
        ).stdout.strip().splitlines()[0]
        full = f"{command}; echo {marker}=$?"
        typed = _tmux_run(
            ["send-keys", "-t", target, "-l", full],
            capture_output=True, text=True, timeout=5)
        self.assertEqual(typed.returncode, 0, typed.stderr)
        submitted = _tmux_run(
            ["send-keys", "-t", target, "Enter"],
            capture_output=True, text=True, timeout=5)
        self.assertEqual(submitted.returncode, 0, submitted.stderr)
        deadline = time.monotonic() + timeout
        pane_text = ""
        while time.monotonic() < deadline:
            captured = _tmux_run(
                ["capture-pane", "-t", target, "-p", "-S", "-200"],
                capture_output=True, text=True, timeout=5)
            self.assertEqual(captured.returncode, 0, captured.stderr)
            pane_text = captured.stdout
            for code in (0, 1):
                if f"{marker}={code}" in pane_text:
                    return code, pane_text
            time.sleep(0.2)
        self.fail(f"timed out waiting for {marker}: {pane_text}")

    def _create_raw_agent(self, body):
        request = urllib.request.Request(
            self.host.rstrip("/") + "/objects/agent",
            data=json.dumps(body).encode(), method="POST")
        request.add_header("Content-Type", "application/json")
        request.add_header("X-App-Key", self.app)
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.read()

    def test_sparse_legacy_revive_refuses_before_tmux_or_filesystem(self):
        self._create_raw_agent({
            "name": self.name,
            "session": self.session,
            "runtime": "custom",
            "launch_cmd": "true",
            "status": "not_started",
            "created_at": int(time.time()),
        })

        revived = self._run(["up", self.name])
        self.assertEqual(revived.returncode, 1, revived.stdout + revived.stderr)
        self.assertIn("valid absolute home", revived.stdout + revived.stderr)
        session = _tmux_run(
            ["has-session", "-t", f"={self.session}"],
            capture_output=True, text=True, timeout=5)
        self.assertNotEqual(session.returncode, 0)
        self.assertFalse(os.path.exists(self.home))
        self.assertEqual(os.listdir(self.tmp), [])

    def test_reserved_authority_name_is_refused_before_side_effects(self):
        reserved_home = os.path.join(self.tmp, "reserved-human-home")
        reserved_session = f"{self.project}__Human"
        self.addCleanup(
            _tmux_run, ["kill-session", "-t", f"={reserved_session}"],
            capture_output=True, text=True)

        result = self._run([
            "spawn-agent", "Human", "--home", reserved_home, "--no-launch",
        ])

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("invalid agent name", result.stderr)
        self.assertFalse(os.path.exists(reserved_home))
        exists = _tmux_run(
            ["has-session", "-t", f"={reserved_session}"],
            capture_output=True, text=True, timeout=5)
        self.assertNotEqual(exists.returncode, 0)

    def test_preexisting_session_collision_does_not_materialize_home(self):
        created = _tmux_run(
            ["new-session", "-d", "-s", self.session, "-c", self.tmp],
            capture_output=True, text=True, timeout=5)
        self.assertEqual(created.returncode, 0, created.stderr)

        spawned = self._run([
            "spawn-agent", self.name, "--home", self.home, "--no-launch",
        ])
        self.assertEqual(spawned.returncode, 1, spawned.stdout + spawned.stderr)
        self.assertIn("already exists", spawned.stderr)
        self.assertFalse(os.path.exists(self.home))

    def test_unowned_preexisting_session_is_not_adopted_or_typed_into(self):
        os.makedirs(self.home)
        marker = os.path.join(self.tmp, "must-not-run")
        self._create_raw_agent({
            "name": self.name,
            "home": self.home,
            "session": self.session,
            "runtime": "custom",
            "launch_cmd": f"touch {marker}",
            "status": "not_started",
            "created_at": int(time.time()),
        })
        created = _tmux_run([
            "new-session", "-d", "-s", self.session, "-c", self.home,
            "-e", "CREW_AGENT=someone_else",
            "-e", f"CREW_PROJECT={self.project}",
            "-e", f"CREW_APP={self.app}",
            "-e", f"MORPHDB_HOST={self.host}",
        ], capture_output=True, text=True, timeout=5)
        self.assertEqual(created.returncode, 0, created.stderr)

        revived = self._run(["up", self.name])
        self.assertEqual(revived.returncode, 1, revived.stdout + revived.stderr)
        self.assertIn("refusing to adopt", revived.stdout + revived.stderr)
        self.assertEqual(self._session_value("CREW_AGENT"), "someone_else")
        self.assertFalse(os.path.exists(marker))

    def test_isolated_session_keeps_context_and_agent_authority(self):
        spawned = self._run([
            "spawn-agent", self.name, "--home", self.home, "--no-launch",
        ])
        self.assertEqual(spawned.returncode, 0, spawned.stdout + spawned.stderr)

        expected = dict(self.context)
        expected.update({
            "CREW_AGENT": self.name,
            "AGENT_MAIL_NAME": self.name,
        })
        actual = {key: self._session_value(key) for key in expected}
        self.assertEqual(actual, expected)
        self.assertEqual(
            self._session_value("PATH").split(os.pathsep)[0],
            os.path.join(ROOT, "bin"))

        pane = _tmux_run(
            ["list-panes", "-t", f"={self.session}", "-F", "#{pane_id}"],
            check=True, capture_output=True, text=True, timeout=5,
        ).stdout.strip().splitlines()[0]
        pane_env = dict(actual)
        pane_env["PATH"] = self._session_value("PATH")
        pane_env["TMUX_PANE"] = pane

        who = self._run(["whoami"], env=pane_env)
        self.assertEqual(who.returncode, 0, who.stdout + who.stderr)
        self.assertIn(f"name: {self.name}", who.stdout)

        note = self._run(
            ["note", "agent", self.name, "session context verified"], env=pane_env)
        self.assertEqual(note.returncode, 0, note.stdout + note.stderr)
        audit = self._run(["audit", "--actor", self.name, "-n", "10"])
        self.assertEqual(audit.returncode, 0, audit.stdout + audit.stderr)
        self.assertIn(self.name, audit.stdout)
        self.assertIn("note", audit.stdout)
        self.assertIn("applied", audit.stdout)

    def test_unset_identity_hints_inside_managed_pane_cannot_gain_human_authority(self):
        spawned = self._run([
            "spawn-agent", self.name, "--home", self.home, "--no-launch",
        ])
        self.assertEqual(spawned.returncode, 0, spawned.stdout + spawned.stderr)

        command = (
            "env -u TMUX_PANE -u CREW_AGENT -u AGENT_MAIL_NAME "
            f"{shlex.quote(sys.executable)} {shlex.quote(CREW_BIN)} "
            f"foreman {shlex.quote(self.name)}")
        rc, pane_text = self._pane_run(
            self.session, command, "CREW_IDENTITY_UNSET_RC")

        self.assertEqual(rc, 1, pane_text)
        self.assertIn("requires a human", pane_text)
        agent = self._run(["agents"])
        self.assertEqual(agent.returncode, 0, agent.stdout + agent.stderr)
        with mock.patch.dict(os.environ, self.context, clear=False):
            self.assertFalse(gs.get_agent_by_name(self.name).get("can_edit_graph"))

    def test_forged_peer_tmux_pane_does_not_impersonate_peer(self):
        peer = f"{self.name}_peer"[:64]
        peer_home = os.path.join(self.tmp, "peer-home")
        peer_session = f"{self.project}__{peer}"

        def cleanup_peer():
            try:
                self._run(["remove-agent", peer])
            except Exception:
                pass
            _tmux_run(["kill-session", "-t", f"={peer_session}"],
                      capture_output=True, text=True)

        self.addCleanup(cleanup_peer)
        for name, home in ((self.name, self.home), (peer, peer_home)):
            spawned = self._run([
                "spawn-agent", name, "--home", home, "--no-launch",
            ])
            self.assertEqual(spawned.returncode, 0, spawned.stdout + spawned.stderr)
        peer_pane = _tmux_run(
            ["list-panes", "-t", f"={peer_session}", "-F", "#{pane_id}"],
            check=True, capture_output=True, text=True, timeout=5,
        ).stdout.strip().splitlines()[0]

        command = (
            f"TMUX_PANE={shlex.quote(peer_pane)} "
            f"CREW_AGENT={shlex.quote(peer)} AGENT_MAIL_NAME={shlex.quote(peer)} "
            f"{shlex.quote(sys.executable)} {shlex.quote(CREW_BIN)} whoami")
        rc, pane_text = self._pane_run(
            self.session, command, "CREW_IDENTITY_SPOOF_RC")

        self.assertEqual(rc, 0, pane_text)
        self.assertIn(f"name: {self.name}", pane_text)
        self.assertNotIn(f"name: {peer}\n", pane_text)

    def test_session_rename_and_removed_markers_cannot_gain_human_authority(self):
        spawned = self._run([
            "spawn-agent", self.name, "--home", self.home, "--no-launch",
        ])
        self.assertEqual(spawned.returncode, 0, spawned.stdout + spawned.stderr)

        renamed = f"{self.session}_renamed"
        self.addCleanup(
            _tmux_run, ["kill-session", "-t", f"={renamed}"],
            capture_output=True, text=True)
        rename = _tmux_run(
            ["rename-session", "-t", f"={self.session}", renamed],
            capture_output=True, text=True, timeout=5)
        self.assertEqual(rename.returncode, 0, rename.stderr)
        for key in ("CREW_AGENT", "CREW_APP"):
            removed = _tmux_run(
                ["set-environment", "-u", "-t", f"={renamed}", key],
                capture_output=True, text=True, timeout=5)
            self.assertEqual(removed.returncode, 0, removed.stderr)

        command = (
            "env -u TMUX_PANE -u CREW_AGENT -u AGENT_MAIL_NAME "
            f"CREW_APP={shlex.quote(self.app)} "
            f"{shlex.quote(sys.executable)} {shlex.quote(CREW_BIN)} "
            f"foreman {shlex.quote(self.name)}")
        rc, pane_text = self._pane_run(
            renamed, command, "CREW_IDENTITY_RENAMED_RC")

        self.assertEqual(rc, 1, pane_text)
        self.assertIn("refusing to assume human authority", pane_text)
        with mock.patch.dict(os.environ, self.context, clear=False):
            self.assertFalse(gs.get_agent_by_name(self.name).get("can_edit_graph"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
