"""Dedicated Crew tmux endpoint and legacy-session migration regressions."""
import concurrent.futures
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from crew import config, graphstore as gs, mail, spawn  # noqa: E402
from crew.server import ptyio, tmuxio  # noqa: E402


class CrewTmuxEndpointConfigTests(unittest.TestCase):
    def test_invocation_targets_named_server_and_ignores_inherited_tmux(self):
        with tempfile.TemporaryDirectory(prefix="crew-tmux-endpoint-") as root, \
             mock.patch.object(config, "_TMUX_TMPDIR_TEST_OVERRIDE", root), \
             mock.patch.dict(os.environ, {
                 "CREW_TMUX_TMPDIR": "/tmp/attacker-selected-root",
                 "CREW_TMUX_SOCKET": "/tmp/attacker-selected.sock",
                 "TMUX_TMPDIR": "/tmp/wrong-server-root",
                 "TMUX": "/tmp/wrong-server-root/default,999,0",
            }, clear=False):
            command = config.tmux_command("list-sessions")
            environment = config.tmux_environment()
            socket_path = config.crew_tmux_socket_path()

        self.assertEqual(
            command,
            ["tmux", "-S", socket_path, "list-sessions"])
        self.assertEqual(environment["TMUX_TMPDIR"], root)
        self.assertNotIn("TMUX", environment)

    def test_owner_only_fixed_config_selects_root_but_mutable_env_cannot(self):
        with tempfile.TemporaryDirectory(prefix="crew-tmux-fixed-config-") as root:
            selected = os.path.join(root, "selected")
            hostile = os.path.join(root, "hostile")
            config_file = os.path.join(root, "tmux-root")
            with open(config_file, "w", encoding="utf-8") as stream:
                stream.write(selected + "\n")
            os.chmod(config_file, 0o600)
            with mock.patch.object(config, "_TMUX_TMPDIR_TEST_OVERRIDE", None), \
                 mock.patch.object(config, "TMUX_CONFIG_FILE", config_file), \
                 mock.patch.dict(os.environ, {
                     "CREW_TMUX_TMPDIR": hostile,
                     "CREW_TMUX_SOCKET": os.path.join(hostile, "crew.sock"),
                 }, clear=False):
                self.assertEqual(config.crew_tmux_tmpdir(), selected)
                self.assertEqual(
                    config.crew_tmux_socket_path(),
                    os.path.join(selected, "crew.sock"))

            os.chmod(config_file, 0o644)
            with mock.patch.object(config, "_TMUX_TMPDIR_TEST_OVERRIDE", None), \
                 mock.patch.object(config, "TMUX_CONFIG_FILE", config_file):
                with self.assertRaisesRegex(OSError, "owner-only"):
                    config.crew_tmux_tmpdir()

    def test_legacy_invocation_explicitly_targets_system_default_server(self):
        with mock.patch.dict(os.environ, {
                 "TMUX_TMPDIR": "/tmp/wrong-server-root",
                 "TMUX": "/tmp/wrong-server-root/other,999,0",
             }, clear=False):
            command = config.tmux_command(
                "has-session", "-t", "=worker",
                endpoint=config.TMUX_ENDPOINT_LEGACY)
            environment = config.tmux_environment(
                endpoint=config.TMUX_ENDPOINT_LEGACY)

        self.assertEqual(
            command, ["tmux", "-L", "default", "has-session", "-t", "=worker"])
        self.assertNotIn("TMUX", environment)
        self.assertNotIn("TMUX_TMPDIR", environment)

    def test_override_must_be_an_owner_only_real_absolute_directory(self):
        with tempfile.TemporaryDirectory(prefix="crew-tmux-security-") as root:
            secure = os.path.join(root, "secure")
            os.mkdir(secure, 0o777)
            with mock.patch.object(
                    config, "_TMUX_TMPDIR_TEST_OVERRIDE", secure):
                self.assertEqual(config.crew_tmux_tmpdir(), secure)
            self.assertEqual(stat.S_IMODE(os.stat(secure).st_mode), 0o700)

            real = os.path.join(root, "real")
            os.mkdir(real)
            linked = os.path.join(root, "linked")
            os.symlink(real, linked)
            with mock.patch.object(
                    config, "_TMUX_TMPDIR_TEST_OVERRIDE", linked):
                with self.assertRaisesRegex(OSError, "symlink|unsafe"):
                    config.crew_tmux_tmpdir()

            nested_real = os.path.join(real, "nested")
            os.mkdir(nested_real)
            through_link = os.path.join(linked, "nested")
            with mock.patch.object(
                    config, "_TMUX_TMPDIR_TEST_OVERRIDE", through_link):
                with self.assertRaisesRegex(OSError, "symlink|canonical|unsafe"):
                    config.crew_tmux_tmpdir()

            with mock.patch.object(
                    config, "_TMUX_TMPDIR_TEST_OVERRIDE", "relative/path"):
                with self.assertRaisesRegex(OSError, "absolute"):
                    config.crew_tmux_tmpdir()

    def test_socket_path_is_short_and_rejects_non_socket_or_foreign_mode(self):
        with tempfile.TemporaryDirectory(prefix="crew-tmux-socket-") as root, \
             mock.patch.object(config, "_TMUX_TMPDIR_TEST_OVERRIDE", root):
            path = config.crew_tmux_socket_path()
            self.assertEqual(path, os.path.join(root, "crew.sock"))
            self.assertLessEqual(len(os.fsencode(path)), config.TMUX_SOCKET_PATH_MAX)

            with open(path, "w"):
                pass
            with self.assertRaisesRegex(OSError, "socket"):
                config.crew_tmux_socket_path()

        with tempfile.TemporaryDirectory(
                prefix="crew-tmux-socket-path-" + "x" * 70) as long_root, \
             mock.patch.object(
                 config, "_TMUX_TMPDIR_TEST_OVERRIDE", long_root):
            with self.assertRaisesRegex(OSError, "too long"):
                config.crew_tmux_socket_path()

    def test_portable_default_uses_srt_tree_on_macos_and_runtime_state_elsewhere(self):
        uid = getattr(os, "getuid", lambda: 0)()
        with mock.patch.object(config, "_TMUX_TMPDIR_TEST_OVERRIDE", None), \
             mock.patch.object(config, "TMUX_CONFIG_FILE", "/no/such/config"):
            with mock.patch.object(config.sys, "platform", "darwin"), \
                 mock.patch.object(
                     config, "ensure_private_directory",
                     side_effect=lambda path: path) as secure:
                path = config.crew_tmux_tmpdir()
            self.assertEqual(
                path,
                os.path.join(os.path.realpath("/tmp"), "claude",
                             f"crew-{uid}-tmux"))
            secure.assert_called_once_with(path)

            with mock.patch.object(config.sys, "platform", "linux"), \
                 mock.patch.object(
                     config, "runtime_state_dir",
                     return_value="/tmp/private-runtime/tmux") as runtime_dir:
                self.assertEqual(
                    config.crew_tmux_tmpdir(), "/tmp/private-runtime/tmux")
            runtime_dir.assert_called_once_with("tmux")

    def test_managed_session_context_carries_direct_tmux_routing(self):
        with tempfile.TemporaryDirectory(prefix="crew-tmux-context-") as root, \
             mock.patch.object(config, "_TMUX_TMPDIR_TEST_OVERRIDE", root):
            context = dict(spawn._session_context("worker", "default"))

        self.assertEqual(context["TMUX_TMPDIR"], root)
        self.assertEqual(context["CREW_TMUX_SOCKET"], os.path.join(root, "crew.sock"))
        self.assertEqual(
            context["CREW_TMUX_SOCKET_NAME"], config.CREW_TMUX_SOCKET_NAME)


class CrewTmuxCallSiteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="crew-tmux-calls-")
        self.override = mock.patch.object(
            config, "_TMUX_TMPDIR_TEST_OVERRIDE", self.tmp.name)
        self.override.start()
        self.addCleanup(self.override.stop)
        self.addCleanup(self.tmp.cleanup)

    @staticmethod
    def _completed():
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def _assert_crew_call(self, call):
        command = call.args[0]
        self.assertEqual(
            command[:3], [mock.ANY, "-S", config.crew_tmux_socket_path()])
        self.assertEqual(call.kwargs["env"]["TMUX_TMPDIR"], self.tmp.name)
        self.assertNotIn("TMUX", call.kwargs["env"])

    def test_spawn_tmux_calls_use_the_dedicated_endpoint(self):
        with mock.patch.object(
                spawn.subprocess, "run", return_value=self._completed()) as run:
            spawn._tmux("list-sessions")
        self._assert_crew_call(run.call_args)

    def test_dashboard_tmux_calls_use_the_dedicated_endpoint(self):
        with mock.patch.object(
                tmuxio.subprocess, "run", return_value=self._completed()) as run:
            tmuxio.tmux("list-sessions")
        self._assert_crew_call(run.call_args)

    def test_pty_tmux_calls_use_the_dedicated_endpoint(self):
        with mock.patch.object(
                ptyio.subprocess, "run", return_value=self._completed()) as run:
            ptyio._tmux("list-sessions")
        self._assert_crew_call(run.call_args)

    def test_mail_send_keys_uses_the_panes_endpoint(self):
        pane = config.tmux_target("%7", config.TMUX_ENDPOINT_CREW)
        with mock.patch.object(
                 mail.subprocess, "run", return_value=self._completed()) as run, \
             mock.patch.object(mail.tmuxio, "capture_frame", return_value="before"), \
             mock.patch.object(mail, "_pane_ready", return_value=False), \
             mock.patch.object(mail.time, "sleep"):
            outcome = mail._type_into_pane(pane, "hello")

        self.assertTrue(outcome)
        self.assertEqual(len(run.call_args_list), 2)
        for call in run.call_args_list:
            self._assert_crew_call(call)

    def test_call_sites_honor_a_legacy_target_without_inherited_redirection(self):
        pane = config.tmux_target("%9", config.TMUX_ENDPOINT_LEGACY)
        with mock.patch.object(
                tmuxio.subprocess, "run", return_value=self._completed()) as run:
            tmuxio.tmux("capture-pane", "-t", pane, "-p")
        self.assertEqual(
            run.call_args.args[0][:3],
            [tmuxio.TMUX, "-L", "default"])
        self.assertNotIn("TMUX", run.call_args.kwargs["env"])
        self.assertNotIn("TMUX_TMPDIR", run.call_args.kwargs["env"])


class CrewTmuxIdentityTests(unittest.TestCase):
    def setUp(self):
        self.environment = mock.patch.dict(os.environ, {
            "CREW_APP": "crew", "CREW_PROJECT": "default",
        }, clear=False)
        self.environment.start()
        self.tmp = tempfile.TemporaryDirectory(prefix="crew-tmux-identity-")
        self.override = mock.patch.object(
            config, "_TMUX_TMPDIR_TEST_OVERRIDE", self.tmp.name)
        self.override.start()
        self.addCleanup(self.override.stop)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.environment.stop)

    @staticmethod
    def _ownership(agent="worker"):
        return ("CREW_PROJECT=default\n"
                f"CREW_AGENT={agent}\n"
                "CREW_APP=crew\n"
                "MORPHDB_HOST=127.0.0.1:18787\n")

    def test_owned_agent_session_finds_exact_legacy_owner_behind_foreign_dedicated_name(self):
        agent = {"name": "worker", "session": "worker", "pane": "%legacy"}

        def fake_tmux(*args, **kwargs):
            endpoint = kwargs.get("endpoint") or config.tmux_target_endpoint(*args)
            if args[0] == "has-session":
                return True, ""
            if endpoint == config.TMUX_ENDPOINT_CREW:
                return True, self._ownership("personal")
            if args[0] == "show-environment":
                return True, self._ownership()
            if args[0] == "display-message":
                return True, "worker"
            if args[0] == "list-panes":
                return True, "%legacy\n%extra-shell"
            return True, ""

        with mock.patch.object(config, "MORPHDB_HOST", "127.0.0.1:18787"), \
             mock.patch.object(tmuxio, "tmux", side_effect=fake_tmux):
            owned = tmuxio.owned_agent_session(agent)

        self.assertEqual(owned, "worker")
        self.assertIsInstance(owned, config.TmuxTarget)
        self.assertEqual(owned.endpoint, config.TMUX_ENDPOINT_LEGACY)

    def test_owned_agent_session_fails_closed_when_both_endpoints_claim_owner(self):
        agent = {"name": "worker", "session": "worker", "pane": "%same"}

        def fake_tmux(*args, **kwargs):
            if args[0] == "has-session":
                return True, ""
            if args[0] == "show-environment":
                return True, self._ownership()
            if args[0] == "display-message":
                return True, "worker"
            if args[0] == "list-panes":
                return True, "%same"
            return True, ""

        with mock.patch.object(config, "MORPHDB_HOST", "127.0.0.1:18787"), \
             mock.patch.object(tmuxio, "tmux", side_effect=fake_tmux):
            self.assertIsNone(tmuxio.owned_agent_session(agent))

    def test_owned_agent_session_requires_stored_pane_bound_to_same_session(self):
        def verify(agent, display_session="worker", panes="%stored"):
            def fake_tmux(*args, **kwargs):
                if args[0] == "has-session":
                    return True, ""
                if args[0] == "show-environment":
                    return True, self._ownership()
                if args[0] == "display-message":
                    return True, display_session
                if args[0] == "list-panes":
                    return True, panes
                return True, ""

            with mock.patch.object(
                     config, "MORPHDB_HOST", "127.0.0.1:18787"), \
                 mock.patch.object(tmuxio, "tmux", side_effect=fake_tmux):
                return tmuxio.inspect_agent_session(
                    agent, config.TMUX_ENDPOINT_CREW)

        self.assertIsNone(verify({"name": "worker", "session": "worker"}))
        self.assertIsNone(verify({
            "name": "worker", "session": "worker", "pane": "%stored",
        }, display_session="other"))
        self.assertIsNone(verify({
            "name": "worker", "session": "worker", "pane": "%stored",
        }, panes="%replacement"))
        self.assertEqual(verify({
            "name": "worker", "session": "worker", "pane": "%stored",
        }, panes="%stored\n%extra-shell"), "worker")

    def test_owned_agent_session_accepts_its_pane_while_dashboard_view_is_grouped(self):
        agent = {"name": "worker", "session": "worker", "pane": "%stored"}

        def fake_tmux(*args, **kwargs):
            if args[0] == "has-session":
                return True, ""
            if args[0] == "show-environment":
                return True, self._ownership()
            if args[0] == "display-message":
                # tmux reports the newest grouped view as #{session_name} for a
                # shared pane. #{session_group_list} still proves membership in
                # the exact durable base session.
                return True, "_ngview_123_1\tworker,_ngview_123_1"
            if args[0] == "list-panes":
                return True, "%stored"
            return True, ""

        with mock.patch.object(config, "MORPHDB_HOST", "127.0.0.1:18787"), \
             mock.patch.object(tmuxio, "tmux", side_effect=fake_tmux):
            self.assertEqual(
                tmuxio.inspect_agent_session(
                    agent, config.TMUX_ENDPOINT_CREW),
                "worker")

        def foreign_group(*args, **kwargs):
            if args[0] == "display-message":
                return True, "_ngview_123_1\tother,_ngview_123_1"
            return fake_tmux(*args, **kwargs)

        with mock.patch.object(config, "MORPHDB_HOST", "127.0.0.1:18787"), \
             mock.patch.object(tmuxio, "tmux", side_effect=foreign_group):
            self.assertIsNone(tmuxio.inspect_agent_session(
                agent, config.TMUX_ENDPOINT_CREW))

    def test_live_pane_identity_rejects_new_pane_but_allows_session_rename(self):
        agent = {
            "name": "worker", "session": "worker", "pane": "%stored",
        }

        def fake_tmux(*args, **kwargs):
            if args[0] == "has-session":
                return True, ""
            if args[0] == "show-environment":
                return True, self._ownership()
            if args[0] == "display-message":
                return True, "renamed-live-session"
            if args[0] == "list-panes":
                return True, "%stored\n%extra-shell"
            return True, ""

        stored_session = config.tmux_target(
            "worker", config.TMUX_ENDPOINT_CREW)
        renamed_session = config.tmux_target(
            "renamed-live-session", config.TMUX_ENDPOINT_CREW)
        with mock.patch.object(config, "MORPHDB_HOST", "127.0.0.1:18787"), \
             mock.patch.object(tmuxio, "tmux", side_effect=fake_tmux):
            self.assertFalse(tmuxio.agent_owns_live_target(
                agent, stored_session,
                config.tmux_target("%new", config.TMUX_ENDPOINT_CREW)))
            self.assertFalse(tmuxio.agent_owns_live_target(
                agent, stored_session,
                config.tmux_target("%stored", config.TMUX_ENDPOINT_LEGACY)))
            self.assertTrue(tmuxio.agent_owns_live_target(
                agent, renamed_session,
                config.tmux_target("%stored", config.TMUX_ENDPOINT_CREW)))

    def test_actual_tty_inventory_finds_owned_legacy_endpoint(self):
        legacy_pane = {
            "session": config.tmux_target(
                "worker", config.TMUX_ENDPOINT_LEGACY),
            "pane_id": config.tmux_target(
                "%legacy", config.TMUX_ENDPOINT_LEGACY),
            "tty": "ttys777",
        }

        def panes(session=None, endpoint=None):
            return ([legacy_pane]
                    if endpoint == config.TMUX_ENDPOINT_LEGACY else [])

        with mock.patch.object(mail, "_controlling_tty", return_value="ttys777"), \
             mock.patch.object(tmuxio, "_list_tmux_panes", side_effect=panes):
            actual, inventoried = mail._actual_tmux_context()

        self.assertTrue(inventoried)
        self.assertEqual(actual, legacy_pane)
        self.assertEqual(
            config.tmux_target_endpoint(actual["session"]),
            config.TMUX_ENDPOINT_LEGACY)

    def test_endpoint_aware_target_keys_do_not_collapse_same_text(self):
        crew = config.tmux_target("worker", config.TMUX_ENDPOINT_CREW)
        legacy = config.tmux_target("worker", config.TMUX_ENDPOINT_LEGACY)
        keyed = {config.tmux_target_key(crew), config.tmux_target_key(legacy)}
        self.assertEqual(len(keyed), 2)
        self.assertFalse(config.same_tmux_target(crew, legacy))

    def test_live_inventory_reports_owned_legacy_and_dedicated_rows_separately(self):
        agents = [
            {"_guid": "claude-guid", "name": "claude_worker",
             "session": "claude_worker", "pane": "%claude",
             "runtime": "claude"},
            {"_guid": "codex-guid", "name": "codex_worker",
             "session": "codex_worker", "pane": "%codex",
             "runtime": "codex"},
        ]
        sessions = {
            "claude-guid": config.tmux_target(
                "claude_worker", config.TMUX_ENDPOINT_LEGACY),
            "codex-guid": config.tmux_target(
                "codex_worker", config.TMUX_ENDPOINT_CREW),
        }

        def owned(agent):
            return sessions[agent["_guid"]]

        def panes(session=None, endpoint=None):
            if endpoint == config.TMUX_ENDPOINT_LEGACY:
                return [{
                    "session": config.tmux_target(
                        "claude_worker", config.TMUX_ENDPOINT_LEGACY),
                    "pane_id": config.tmux_target(
                        "%claude", config.TMUX_ENDPOINT_LEGACY),
                    "tty": "ttys101",
                }]
            return [{
                "session": config.tmux_target(
                    "codex_worker", config.TMUX_ENDPOINT_CREW),
                "pane_id": config.tmux_target(
                    "%codex", config.TMUX_ENDPOINT_CREW),
                "tty": "ttys102",
            }]

        processes = {
            "ttys101": [{"comm": "claude", "command": "claude"}],
            "ttys102": [{"comm": "codex", "command": "codex"}],
        }
        with mock.patch.object(
                 tmuxio, "owned_agent_session", side_effect=owned), \
             mock.patch.object(tmuxio, "_list_tmux_panes", side_effect=panes), \
             mock.patch.object(
                 tmuxio, "process_inventory", return_value=processes):
            inventory = tmuxio.live_agent_inventory(agents)

        claude = tmuxio.agent_snapshot_fields(
            agents[0], live=inventory["claude-guid"], capture=lambda _pane: "")
        codex = tmuxio.agent_snapshot_fields(
            agents[1], live=inventory["codex-guid"],
            capture=lambda _pane: "› ")
        self.assertTrue(claude["session_alive"])
        self.assertTrue(claude["runtime_alive"])
        self.assertEqual(claude["tmux_endpoint"], config.TMUX_ENDPOINT_LEGACY)
        self.assertTrue(claude["migration_required"])
        self.assertTrue(codex["session_alive"])
        self.assertTrue(codex["runtime_alive"])
        self.assertEqual(codex["tmux_endpoint"], config.TMUX_ENDPOINT_CREW)
        self.assertFalse(codex["migration_required"])

    def test_runtime_pane_retains_the_sessions_endpoint(self):
        session = config.tmux_target("worker", config.TMUX_ENDPOINT_LEGACY)
        calls = []

        def fake_tmux(*args, **kwargs):
            calls.append((args, kwargs))
            if args[0] == "has-session":
                return True, ""
            if args[0] == "list-panes":
                return True, "worker\t%8\t/dev/ttys008"
            return True, ""

        processes = {"ttys008": [{"comm": "codex", "command": "codex"}]}
        with mock.patch.object(tmuxio, "tmux", side_effect=fake_tmux), \
             mock.patch.object(tmuxio, "process_inventory", return_value=processes):
            pane = tmuxio.runtime_pane(
                session, "codex", fallback=False)

        self.assertEqual(pane, "%8")
        self.assertIsInstance(pane, config.TmuxTarget)
        self.assertEqual(pane.endpoint, config.TMUX_ENDPOINT_LEGACY)
        self.assertTrue(all(
            (kwargs.get("endpoint") or config.tmux_target_endpoint(*args))
            == config.TMUX_ENDPOINT_LEGACY
            for args, kwargs in calls), calls)

    def test_pane_context_uses_explicit_socket_even_if_shell_overwrites_tmux(self):
        calls = []

        def fake_tmux(*args, **kwargs):
            calls.append(args)
            return True, ""

        with mock.patch.object(spawn, "_tmux", side_effect=fake_tmux):
            spawn._pin_pane_context("%1", "worker", "default", session="worker")
        typed = next(args[-1] for args in calls
                     if args[:3] == ("send-keys", "-t", "%1") and "-l" in args)
        explicit = "command tmux -S " + config.crew_tmux_socket_path()
        self.assertIn(explicit, typed)
        self.assertNotIn("command tmux show-environment", typed)
        self.assertNotIn("command tmux wait-for", typed)

    def test_pty_attach_and_followup_operations_stay_on_the_base_endpoint(self):
        session = config.tmux_target("worker", config.TMUX_ENDPOINT_LEGACY)
        calls = []

        def fake_tmux(*args, **kwargs):
            calls.append((args, kwargs))
            if args[0] == "list-sessions":
                return True, "worker"
            if args[0] == "list-windows":
                return True, "agent\t0\t@7"
            if args[0] == "show-options":
                return True, ptyio._NOALT_OVERRIDE
            return True, ""

        saved_sessions = ptyio._SESS
        saved_counter = ptyio._N[0]
        saved_override = ptyio._OVERRIDE_DONE[0]
        ptyio._SESS = {}
        ptyio._N[0] = 0
        ptyio._OVERRIDE_DONE[0] = False
        try:
            with mock.patch.object(ptyio, "_tmux", side_effect=fake_tmux), \
                 mock.patch.object(ptyio.pty, "fork", return_value=(123, 9)):
                view, _fd = ptyio.open_attach(session, "agent")
                self.assertIsNotNone(view)
                self.assertEqual(
                    ptyio._SESS[view]["endpoint"],
                    config.TMUX_ENDPOINT_LEGACY)
                with mock.patch.object(ptyio.os, "dup", return_value=91), \
                     mock.patch.object(ptyio.fcntl, "ioctl"), \
                     mock.patch.object(ptyio.os, "close"):
                    self.assertTrue(ptyio.set_size(view, 90, 30))
            self.assertTrue(all(
                kwargs.get("endpoint") == config.TMUX_ENDPOINT_LEGACY
                for _args, kwargs in calls), calls)
        finally:
            ptyio._SESS = saved_sessions
            ptyio._N[0] = saved_counter
            ptyio._OVERRIDE_DONE[0] = saved_override

    def test_pty_child_exec_uses_explicit_endpoint_and_scrubbed_environment(self):
        command, environment = ptyio._attach_command(
            "_ngview_1_1", config.TMUX_ENDPOINT_LEGACY)
        self.assertEqual(
            command[:3], ["tmux", "-L", "default"])
        self.assertEqual(command[-3:], ["attach-session", "-t", "_ngview_1_1"])
        self.assertEqual(environment["TERM"], ptyio._DASH_TERM)
        self.assertNotIn("TMUX", environment)
        self.assertNotIn("TMUX_PANE", environment)

    def _legacy_only_tmux(self, calls):
        ownership = self._ownership()

        def fake_tmux(*args, **kwargs):
            endpoint = kwargs.get("endpoint") or config.tmux_target_endpoint(*args)
            calls.append((args, endpoint))
            if args[0] == "has-session":
                return endpoint == config.TMUX_ENDPOINT_LEGACY, ""
            if args[0] == "show-environment":
                if len(args) > 3:
                    key = args[-1]
                    line = next(
                        row for row in ownership.splitlines()
                        if row.startswith(key + "="))
                    return True, line
                return True, ownership
            if args[0] == "display-message":
                return True, "worker"
            if args[0] == "list-panes":
                return True, "%legacy"
            if args[0] == "kill-session":
                return True, ""
            return True, ""
        return fake_tmux

    def test_lifecycle_finds_the_exact_owned_legacy_endpoint(self):
        calls = []
        agent = {"name": "worker", "session": "worker", "pane": "%legacy"}
        with mock.patch.object(config, "MORPHDB_HOST", "127.0.0.1:18787"), \
             mock.patch.object(spawn, "_tmux", side_effect=self._legacy_only_tmux(calls)), \
             mock.patch.object(tmuxio, "tmux", side_effect=self._legacy_only_tmux(calls)):
            session, exists = spawn._owned_session_state(
                agent, "worker", "default")

        self.assertTrue(exists)
        self.assertEqual(session, "worker")
        self.assertIsInstance(session, config.TmuxTarget)
        self.assertEqual(session.endpoint, config.TMUX_ENDPOINT_LEGACY)

    def test_up_refuses_to_interrupt_a_live_legacy_conversation(self):
        calls = []
        agent = {
            "_guid": "agent-guid", "name": "worker", "session": "worker",
            "pane": "%legacy", "home": self.tmp.name, "runtime": "custom",
            "launch_cmd": "true", "status": "idle",
        }
        with mock.patch.object(config, "MORPHDB_HOST", "127.0.0.1:18787"), \
             mock.patch.object(spawn.gs, "get_agent_by_name", return_value=agent), \
             mock.patch.object(spawn.gs, "update_agent_runtime_state", return_value=agent), \
             mock.patch.object(spawn.guard, "check"), \
             mock.patch.object(spawn, "rewrite_identity"), \
             mock.patch.object(spawn, "_tmux", side_effect=self._legacy_only_tmux(calls)), \
             mock.patch.object(spawn, "_open_session") as open_session:
            with mock.patch.object(
                    tmuxio, "tmux", side_effect=self._legacy_only_tmux(calls)):
                with self.assertRaisesRegex(gs.GraphError, "crew down worker"):
                    spawn._start_session_locked("worker")

        open_session.assert_not_called()
        self.assertFalse(any(args[0] == "kill-session" for args, _ in calls), calls)

    def test_restart_refuses_live_legacy_before_any_kill(self):
        calls = []
        agent = {
            "_guid": "agent-guid", "name": "worker", "session": "worker",
            "pane": "%legacy", "home": self.tmp.name,
        }
        with mock.patch.object(config, "MORPHDB_HOST", "127.0.0.1:18787"), \
             mock.patch.object(spawn.gs, "get_agent_by_name", return_value=agent), \
             mock.patch.object(spawn.guard, "check"), \
             mock.patch.object(spawn.guard, "audit") as audit, \
             mock.patch.object(
                 spawn, "_tmux", side_effect=self._legacy_only_tmux(calls)), \
             mock.patch.object(
                 tmuxio, "tmux", side_effect=self._legacy_only_tmux(calls)):
            with self.assertRaisesRegex(gs.GraphError, "crew down worker"):
                spawn._stop_session_locked("worker", _refuse_legacy=True)

        self.assertFalse(any(args[0] == "kill-session" for args, _ in calls), calls)
        self.assertTrue(any(
            call.args[3] == "refused" for call in audit.call_args_list),
            audit.call_args_list)

    def test_stop_revalidates_exact_endpoint_owner_before_kill(self):
        calls = []
        agent = {
            "_guid": "agent-guid", "name": "worker", "session": "worker",
            "pane": "%legacy", "home": self.tmp.name,
        }
        initial = config.tmux_target(
            "worker", config.TMUX_ENDPOINT_LEGACY)
        with mock.patch.object(spawn.gs, "get_agent_by_name", return_value=agent), \
             mock.patch.object(spawn.guard, "check"), \
             mock.patch.object(spawn.guard, "audit"), \
             mock.patch.object(
                 spawn, "_session_locations",
                 side_effect=[
                     {"session": "worker", "owned": initial,
                      "dedicated_exists": False, "legacy_exists": True},
                     {"session": "worker", "owned": None,
                      "dedicated_exists": False, "legacy_exists": False},
                 ]), \
             mock.patch.object(spawn, "_tmux") as tmux:
            with self.assertRaisesRegex(gs.GraphError, "changed|retry"):
                spawn._stop_session_locked("worker")
        tmux.assert_not_called()

    def test_mail_revalidation_rejects_endpoint_flip_with_same_target_text(self):
        agent = {
            "name": "worker", "session": "worker", "pane": "%runtime",
            "runtime": "claude", "launch_cmd": "claude",
        }
        crew = config.tmux_target("worker", config.TMUX_ENDPOINT_CREW)
        legacy = config.tmux_target("worker", config.TMUX_ENDPOINT_LEGACY)
        pane = config.tmux_target("%runtime", config.TMUX_ENDPOINT_CREW)
        with mock.patch.object(
                 tmuxio, "owned_agent_session", side_effect=[crew, legacy]), \
             mock.patch.object(tmuxio, "runtime_pane", return_value=pane):
            resolved, _runtime = mail._pane_for_agent(agent)
        self.assertIsNone(resolved)

    def test_mail_uses_exact_stored_runtime_when_sandbox_hides_peer_ps(self):
        agent = {
            "name": "worker", "session": "worker", "pane": "%runtime",
            "runtime": "codex", "launch_cmd": "codex",
        }
        crew = config.tmux_target("worker", config.TMUX_ENDPOINT_CREW)
        pane = config.tmux_target("%runtime", config.TMUX_ENDPOINT_CREW)
        with mock.patch.object(
                 tmuxio, "owned_agent_session", side_effect=[crew, crew]), \
             mock.patch.object(tmuxio, "runtime_pane", return_value=None), \
             mock.patch.object(
                 tmuxio, "stored_runtime_pane", return_value=pane) as fallback:
            resolved, runtime_key = mail._pane_for_agent(agent)
        self.assertEqual(resolved, pane)
        self.assertEqual(runtime_key, "codex")
        fallback.assert_called_once_with(agent, crew)

    def test_down_kills_owned_legacy_but_never_same_named_dedicated_personal_session(self):
        calls = []
        agent = {
            "_guid": "agent-guid", "name": "worker", "session": "worker",
            "pane": "%legacy", "home": self.tmp.name,
        }
        base = self._legacy_only_tmux(calls)

        def fake_tmux(*args, **kwargs):
            endpoint = kwargs.get("endpoint") or config.tmux_target_endpoint(*args)
            # A same-named dedicated session exists but has foreign markers.
            if args[0] == "has-session" and endpoint == config.TMUX_ENDPOINT_CREW:
                calls.append((args, endpoint))
                return True, ""
            if args[0] == "show-environment" and endpoint == config.TMUX_ENDPOINT_CREW:
                calls.append((args, endpoint))
                return True, "CREW_PROJECT=personal"
            return base(*args, **kwargs)

        with mock.patch.object(config, "MORPHDB_HOST", "127.0.0.1:18787"), \
             mock.patch.object(spawn.gs, "get_agent_by_name", return_value=agent), \
             mock.patch.object(spawn.guard, "check"), \
             mock.patch.object(spawn.guard, "audit"), \
             mock.patch.object(spawn, "_tmux", side_effect=fake_tmux), \
             mock.patch.object(tmuxio, "tmux", side_effect=fake_tmux):
            self.assertTrue(spawn._stop_session_locked("worker"))

        kills = [(args, endpoint) for args, endpoint in calls
                 if args[0] == "kill-session"]
        self.assertEqual(len(kills), 1, calls)
        self.assertEqual(kills[0][1], config.TMUX_ENDPOINT_LEGACY)

    def test_mail_liveness_checks_preserve_the_owned_legacy_endpoint(self):
        agent = {"name": "worker", "session": "worker"}
        legacy = config.tmux_target("worker", config.TMUX_ENDPOINT_LEGACY)
        calls = []

        def fake_tmux(*args, **kwargs):
            calls.append((args, kwargs))
            return True, ""

        with mock.patch.object(
                 tmuxio, "owned_agent_session", return_value=legacy), \
             mock.patch.object(tmuxio, "tmux", side_effect=fake_tmux):
            live = mail._live_owned_session(agent)

        self.assertIs(live, legacy)
        target = calls[0][0][calls[0][0].index("-t") + 1]
        self.assertIsInstance(target, config.TmuxTarget)
        self.assertEqual(target.endpoint, config.TMUX_ENDPOINT_LEGACY)


@unittest.skipUnless(shutil.which("tmux"), "tmux is required for endpoint checks")
class CrewTmuxLiveIsolationTests(unittest.TestCase):
    def setUp(self):
        self.environment = mock.patch.dict(os.environ, {
            "CREW_APP": "crew", "CREW_PROJECT": "default",
        }, clear=False)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def _run(self, command, environment=None, check=True):
        return subprocess.run(
            command, env=environment, check=check, capture_output=True,
            text=True, timeout=10)

    def test_inherited_personal_server_and_same_named_session_are_untouched(self):
        with tempfile.TemporaryDirectory(prefix="crew-endpoint-live-") as crew_root, \
             tempfile.TemporaryDirectory(prefix="crew-personal-live-") as personal_root:
            personal_socket = os.path.join(personal_root, "personal.sock")
            personal = ["tmux", "-S", personal_socket]
            name = "same_name_endpoint_test"
            self._run([*personal, "new-session", "-d", "-s", name, "sleep", "30"])
            try:
                inherited = f"{personal_socket},999,0"
                with mock.patch.object(
                         config, "_TMUX_TMPDIR_TEST_OVERRIDE", crew_root), \
                     mock.patch.dict(os.environ, {
                    "CREW_TMUX_TMPDIR": personal_root,
                    "TMUX": inherited,
                    "TMUX_PANE": "%999",
                    "TMUX_TMPDIR": personal_root,
                }, clear=False):
                    ok, error = spawn._tmux(
                        "new-session", "-d", "-s", name, "sleep", "30")
                    self.assertTrue(ok, error)
                    self.assertTrue(spawn._tmux("has-session", "-t", f"={name}")[0])
                    self.assertTrue(spawn._tmux("kill-session", "-t", f"={name}")[0])
                # Crew creation and teardown never reached the inherited server,
                # even though the personal server used the exact same name.
                self._run([*personal, "has-session", "-t", f"={name}"])
            finally:
                self._run(
                    [*personal, "kill-server"], check=False)

    def test_concurrent_first_sessions_share_one_new_private_server(self):
        with tempfile.TemporaryDirectory(
                prefix="crew-endpoint-concurrent-first-") as crew_root, \
             mock.patch.object(
                 config, "_TMUX_TMPDIR_TEST_OVERRIDE", crew_root):
            def create(name):
                return subprocess.run(
                    config.tmux_command(
                        "new-session", "-d", "-s", name, "sleep", "30"),
                    env=config.tmux_environment(), capture_output=True,
                    text=True, timeout=10)

            names = ("first_server_a", "first_server_b")
            try:
                with concurrent.futures.ThreadPoolExecutor(
                        max_workers=2) as executor:
                    results = list(executor.map(create, names))
                self.assertTrue(all(result.returncode == 0 for result in results), [
                    (result.returncode, result.stdout, result.stderr)
                    for result in results
                ])
                for name in names:
                    self._run(
                        config.tmux_command(
                            "has-session", "-t", f"={name}"),
                        config.tmux_environment())
            finally:
                self._run(
                    config.tmux_command("kill-server"),
                    config.tmux_environment(), check=False)

    def test_grouped_dashboard_view_preserves_exact_base_ownership(self):
        with tempfile.TemporaryDirectory(
                prefix="crew-endpoint-grouped-view-") as crew_root, \
             mock.patch.object(
                 config, "_TMUX_TMPDIR_TEST_OVERRIDE", crew_root), \
             mock.patch.object(config, "MORPHDB_HOST", "127.0.0.1:18787"):
            name = "grouped_owner_test"
            view = "_ngview_grouped_owner_test"
            environment = config.tmux_environment()
            env_args = []
            for key, value in spawn._session_context(
                    name, "default", "custom"):
                env_args.extend(("-e", f"{key}={value}"))
            try:
                self._run(config.tmux_command(
                    "new-session", "-d", "-s", name, "-n", "agent",
                    *env_args, "sleep 30"), environment)
                pane = self._run(config.tmux_command(
                    "list-panes", "-t", f"={name}", "-F", "#{pane_id}"),
                    environment).stdout.strip()
                agent = {
                    "name": name, "session": name, "pane": pane,
                    "runtime": "custom", "launch_cmd": "sleep 30",
                }
                self.assertIsNotNone(tmuxio.owned_agent_session(agent))
                self._run(config.tmux_command(
                    "new-session", "-d", "-t", f"={name}", "-s", view),
                    environment)
                self.assertIsNotNone(
                    tmuxio.owned_agent_session(agent),
                    "creating the dashboard's grouped view invalidated its base")
            finally:
                self._run(
                    config.tmux_command("kill-server"), environment,
                    check=False)

    def test_last_session_exit_allows_immediate_server_recreation(self):
        with tempfile.TemporaryDirectory(
                prefix="crew-endpoint-last-exit-") as crew_root, \
             mock.patch.object(
                 config, "_TMUX_TMPDIR_TEST_OVERRIDE", crew_root):
            environment = config.tmux_environment()
            try:
                self._run(config.tmux_command(
                    "new-session", "-d", "-s", "last_one", "sleep", "30"),
                    environment)
                self._run(config.tmux_command(
                    "kill-session", "-t", "=last_one"), environment)
                recreated = self._run(config.tmux_command(
                    "new-session", "-d", "-s", "recreated", "sleep", "30"),
                    environment, check=False)
                self.assertEqual(
                    recreated.returncode, 0,
                    recreated.stdout + recreated.stderr)
                self._run(config.tmux_command(
                    "has-session", "-t", "=recreated"), environment)
            finally:
                self._run(
                    config.tmux_command("kill-server"), environment,
                    check=False)

    def test_stale_socket_after_server_crash_is_recoverable(self):
        with tempfile.TemporaryDirectory(
                prefix="crew-endpoint-stale-socket-") as crew_root, \
             mock.patch.object(
                 config, "_TMUX_TMPDIR_TEST_OVERRIDE", crew_root):
            environment = config.tmux_environment()
            self._run(config.tmux_command(
                "new-session", "-d", "-s", "before_crash", "sleep", "30"),
                environment)
            pid_text = self._run(config.tmux_command(
                "display-message", "-p", "#{pid}"), environment).stdout.strip()
            self.assertTrue(pid_text.isdigit(), pid_text)
            server_pid = int(pid_text)
            os.kill(server_pid, signal.SIGKILL)
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                try:
                    os.kill(server_pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.02)
            try:
                ok, error = spawn._tmux(
                    "new-session", "-d", "-s", "after_crash", "sleep", "30")
                self.assertTrue(ok, error)
                self.assertTrue(spawn._tmux(
                    "has-session", "-t", "=after_crash")[0])
            finally:
                self._run(
                    config.tmux_command("kill-server"), environment,
                    check=False)

    def test_down_then_up_migrates_an_isolated_legacy_session(self):
        with tempfile.TemporaryDirectory(prefix="crew-endpoint-upgrade-") as crew_root, \
             tempfile.TemporaryDirectory(prefix="crew-legacy-upgrade-") as legacy_root, \
             tempfile.TemporaryDirectory(prefix="crew-agent-upgrade-") as home:
            original_environment = config.tmux_environment

            def isolated_environment(endpoint=config.TMUX_ENDPOINT_CREW, environ=None):
                result = original_environment(endpoint=endpoint, environ=environ)
                if endpoint == config.TMUX_ENDPOINT_LEGACY:
                    result["TMUX_TMPDIR"] = legacy_root
                return result

            agent = {
                "_guid": "live-agent-guid", "name": "worker",
                "session": "worker", "home": home, "runtime": "custom",
                "launch_cmd": "true", "status": "idle",
            }
            with mock.patch.object(
                     config, "_TMUX_TMPDIR_TEST_OVERRIDE", crew_root), \
                 mock.patch.object(
                     config, "tmux_environment", side_effect=isolated_environment):
                legacy_env = isolated_environment(config.TMUX_ENDPOINT_LEGACY)
                env_args = []
                for key, value in spawn._session_context("worker", "default"):
                    env_args.extend(("-e", f"{key}={value}"))
                self._run(
                    config.tmux_command(
                        "new-session", "-d", "-s", "worker", "-n", "agent",
                        "-c", home, *env_args,
                        endpoint=config.TMUX_ENDPOINT_LEGACY),
                    legacy_env)
                pane = self._run(
                    config.tmux_command(
                        "list-panes", "-t", "=worker", "-F", "#{pane_id}",
                        endpoint=config.TMUX_ENDPOINT_LEGACY),
                    legacy_env).stdout.strip()
                agent["pane"] = pane
                try:
                    with mock.patch.object(
                             spawn.gs, "get_agent_by_name", return_value=agent), \
                         mock.patch.object(spawn.guard, "check"), \
                         mock.patch.object(spawn.guard, "audit"):
                        self.assertTrue(spawn._stop_session_locked("worker"))

                    self.assertNotEqual(
                        self._run(
                            config.tmux_command(
                                "has-session", "-t", "=worker",
                                endpoint=config.TMUX_ENDPOINT_LEGACY),
                            legacy_env, check=False).returncode,
                        0)

                    updated = {**agent, "status": "idle"}
                    with mock.patch.object(
                             spawn.gs, "get_agent_by_name", return_value=agent), \
                         mock.patch.object(
                             spawn.gs, "update_agent_runtime_state",
                             return_value=updated), \
                         mock.patch.object(spawn.guard, "check"), \
                         mock.patch.object(spawn.guard, "audit"), \
                         mock.patch.object(spawn, "rewrite_identity"):
                        spawn._start_session_locked("worker")

                    self._run(
                        config.tmux_command("has-session", "-t", "=worker"),
                        isolated_environment())
                finally:
                    self._run(
                        config.tmux_command("kill-server"),
                        isolated_environment(), check=False)
                    self._run(
                        config.tmux_command(
                            "kill-server", endpoint=config.TMUX_ENDPOINT_LEGACY),
                        legacy_env, check=False)


if __name__ == "__main__":
    unittest.main()
