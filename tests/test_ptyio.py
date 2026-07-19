"""PTY/tmux transport contracts, including isolated real-tmux checks.

The live checks create only ``crew_ptyaudit_*`` sessions and tear down those
exact names.  They never inspect, attach to, or mutate any other tmux session.
"""
from __future__ import annotations

import os
import select
import shutil
import signal
import subprocess
import tempfile
import time
import unittest
import uuid
from unittest import mock

from crew import config
from crew.server import ptyio, tmuxio


class PtyTransportUnitTests(unittest.TestCase):
    def setUp(self):
        self.saved_sessions = ptyio._SESS
        self.saved_counter = ptyio._N[0]
        self.saved_override = ptyio._OVERRIDE_DONE[0]
        ptyio._SESS = {}
        ptyio._N[0] = 0
        ptyio._OVERRIDE_DONE[0] = False

    def tearDown(self):
        ptyio._SESS = self.saved_sessions
        ptyio._N[0] = self.saved_counter
        ptyio._OVERRIDE_DONE[0] = self.saved_override

    @staticmethod
    def _setup_tmux(*args, **kwargs):
        command = args[0]
        if command == "list-sessions":
            return True, "crew_ptyaudit_full"
        if command == "list-windows":
            return True, "agent\t0\t@7"
        if command == "show-options":
            return True, ptyio._NOALT_OVERRIDE
        return True, ""

    def test_open_attach_requires_an_exact_base_session_name(self):
        """A stored prefix must never resolve to another tmux session."""
        with mock.patch.object(ptyio, "_tmux", side_effect=self._setup_tmux), \
             mock.patch.object(ptyio.pty, "fork", return_value=(123, 9)) as fork:
            opened = ptyio.open_attach("crew_ptyaudit", "agent")
        self.assertEqual(opened, (None, None))
        fork.assert_not_called()

    def test_open_attach_rejects_a_missing_window_instead_of_attaching_default(self):
        with mock.patch.object(ptyio, "_tmux", side_effect=self._setup_tmux), \
             mock.patch.object(ptyio.pty, "fork", return_value=(123, 9)) as fork:
            opened = ptyio.open_attach("crew_ptyaudit_full", "missing")
        self.assertEqual(opened, (None, None))
        fork.assert_not_called()

    def test_open_attach_cleans_partial_group_setup_before_forking(self):
        calls = []

        def fake_tmux(*args, **kwargs):
            calls.append(args)
            if args[0] == "list-sessions":
                return True, "crew_ptyaudit_full"
            if args[0] == "list-windows":
                return True, "agent\t0\t@7"
            if args[0] == "select-window":
                return False, "can't find window"
            if args[0] == "show-options":
                return True, ptyio._NOALT_OVERRIDE
            return True, ""

        with mock.patch.object(ptyio, "_tmux", side_effect=fake_tmux), \
             mock.patch.object(ptyio.pty, "fork", return_value=(123, 9)) as fork:
            opened = ptyio.open_attach("crew_ptyaudit_full", "agent")
        self.assertEqual(opened, (None, None))
        fork.assert_not_called()
        killed = [c for c in calls if c[:1] == ("kill-session",)]
        self.assertEqual(len(killed), 1, calls)

    def test_open_attach_spawns_without_forkpty_in_the_threaded_server(self):
        """Dashboard request handlers must not call forkpty while threaded.

        Python 3.14 warns that forking a multithreaded process may deadlock the
        child before exec.  A separately opened PTY plus subprocess' constrained
        spawn path gives tmux the same terminal without running Python after a
        raw fork in the request thread.
        """
        with mock.patch.object(ptyio, "_tmux", side_effect=self._setup_tmux), \
             mock.patch.object(ptyio.pty, "openpty", return_value=(70, 71)), \
             mock.patch.object(ptyio.pty, "fork", return_value=(123, 9)) as fork, \
             mock.patch.object(ptyio.os, "posix_spawnp", return_value=123) as spawn, \
             mock.patch.object(ptyio.os, "close") as close:
            view, fd = ptyio.open_attach("crew_ptyaudit_full", "agent")

        self.assertEqual(fd, 70)
        fork.assert_not_called()
        spawn.assert_called_once()
        actions = spawn.call_args.kwargs["file_actions"]
        self.assertIn((os.POSIX_SPAWN_DUP2, 71, 0), actions)
        self.assertIn((os.POSIX_SPAWN_DUP2, 71, 1), actions)
        self.assertIn((os.POSIX_SPAWN_DUP2, 71, 2), actions)
        self.assertIn((os.POSIX_SPAWN_CLOSE, 70), actions)
        self.assertTrue(spawn.call_args.kwargs["setsid"])
        close.assert_called_once_with(71)

    def test_native_scroll_setup_retries_after_a_tmux_failure(self):
        with mock.patch.object(
                ptyio, "_tmux",
                side_effect=[(False, "read failed"), (False, "write failed")]):
            self.assertFalse(ptyio._ensure_native_scroll())
        self.assertFalse(ptyio._OVERRIDE_DONE[0])

    def test_write_input_retries_partial_writes_on_a_borrowed_descriptor(self):
        ptyio._SESS["view"] = {
            "fd": 7, "pid": 123, "view": "view", "key": "s:@7"}
        chunks = []

        def partial_write(fd, data):
            self.assertEqual(fd, 91)
            chunks.append(bytes(data))
            return min(2, len(data))

        payload = "héllo".encode("utf-8")
        with mock.patch.object(ptyio.os, "dup", return_value=91) as dup, \
             mock.patch.object(ptyio.os, "write", side_effect=partial_write), \
             mock.patch.object(ptyio.os, "close") as close:
            self.assertTrue(ptyio.write_input("view", payload))
        dup.assert_called_once_with(7)
        close.assert_called_once_with(91)
        self.assertGreater(len(chunks), 1)
        # Each retry begins exactly after the bytes accepted by the prior write.
        accepted = b"".join(chunk[:2] for chunk in chunks[:-1]) + chunks[-1]
        self.assertEqual(accepted, payload)

    def test_resize_fails_closed_when_the_pty_ioctl_fails(self):
        ptyio._SESS["view"] = {
            "fd": 7, "pid": 123, "view": "view", "key": "s:@7"}
        with mock.patch.object(ptyio.os, "dup", return_value=91), \
             mock.patch.object(ptyio.fcntl, "ioctl", side_effect=OSError("closed")), \
             mock.patch.object(ptyio.os, "close"), \
             mock.patch.object(ptyio, "_tmux", return_value=(True, "")) as tmux:
            self.assertFalse(ptyio.set_size("view", 100, 40))
        tmux.assert_not_called()

    def test_close_reaps_the_tmux_attach_child(self):
        ptyio._SESS["view"] = {
            "fd": 7, "pid": 123, "view": "view", "key": "s:@7"}
        with mock.patch.object(ptyio.os, "close"), \
             mock.patch.object(ptyio.os, "waitpid",
                               side_effect=[(0, 0), (123, signal.SIGKILL)]) as waitpid, \
             mock.patch.object(ptyio.os, "kill") as kill, \
             mock.patch.object(ptyio, "_tmux", return_value=(True, "")):
            ptyio.close("view")
        kill.assert_called_once_with(123, signal.SIGKILL)
        self.assertEqual(waitpid.call_args_list, [
            mock.call(123, os.WNOHANG), mock.call(123, 0)])

    def test_reaper_never_kills_an_unmarked_lookalike_session(self):
        lookalike = "_ngview_999999_1"
        with mock.patch.object(
                ptyio, "_tmux",
                side_effect=[(True, lookalike + "\t"), (True, "")]) as tmux, \
             mock.patch.object(ptyio.os, "kill", side_effect=ProcessLookupError):
            ptyio.reap_stale()
        self.assertFalse(
            any(call.args and call.args[0] == "kill-session"
                for call in tmux.call_args_list),
            tmux.call_args_list)

    def test_reaper_kills_only_a_marked_view_with_a_dead_owner(self):
        view = "_ngview_999998_1"

        def fake_tmux(*args, **kwargs):
            if args[0] == "list-sessions":
                return ((True, view + "\t999998")
                        if kwargs.get("endpoint") == config.TMUX_ENDPOINT_CREW
                        else (False, "no legacy server"))
            return True, ""

        with mock.patch.object(
                ptyio, "_tmux", side_effect=fake_tmux) as tmux, \
             mock.patch.object(ptyio.os, "kill", side_effect=ProcessLookupError):
            ptyio.reap_stale()
        self.assertIn(
            mock.call(
                "kill-session", "-t", view,
                endpoint=config.TMUX_ENDPOINT_CREW),
            tmux.call_args_list)

    def test_reaper_never_globally_kills_legacy_marked_lookalikes(self):
        calls = []

        def fake_tmux(*args, **kwargs):
            endpoint = kwargs.get("endpoint", config.TMUX_ENDPOINT_CREW)
            calls.append((args, endpoint))
            if args[0] == "list-sessions":
                suffix = "1" if endpoint == config.TMUX_ENDPOINT_CREW else "2"
                return True, f"_ngview_999997_{suffix}\t999997"
            return True, ""

        with mock.patch.object(ptyio, "_tmux", side_effect=fake_tmux), \
             mock.patch.object(ptyio.os, "kill", side_effect=ProcessLookupError):
            ptyio.reap_stale()

        kills = {(args[-1], endpoint) for args, endpoint in calls
                 if args[0] == "kill-session"}
        self.assertEqual(kills, {
            ("_ngview_999997_1", config.TMUX_ENDPOINT_CREW),
        })
        self.assertFalse(any(
            endpoint == config.TMUX_ENDPOINT_LEGACY
            for _args, endpoint in calls), calls)


class TmuxPaneTargetTests(unittest.TestCase):
    def test_owned_agent_session_requires_exact_pinned_crew_environment(self):
        agent = {
            "name": "worker", "session": "demo__worker",
            "pane": "%worker",
            "runtime": "claude",
        }
        expected = {
            "CREW_PROJECT": "demo",
            "CREW_AGENT": "worker",
            "CREW_APP": "crew-demo",
            "MORPHDB_HOST": "127.0.0.1:18787",
        }

        def env_text(values):
            return "".join(f"{key}={value}\n" for key, value in values.items())

        crew_environments = [
            env_text({**expected, "CREW_AGENT": "intruder"}),
            env_text(expected),
        ]

        def fake_tmux(*args, **kwargs):
            endpoint = kwargs.get("endpoint") or config.tmux_target_endpoint(*args)
            if endpoint == config.TMUX_ENDPOINT_LEGACY:
                return False, "no server"
            if args[0] == "has-session":
                return True, ""
            if args[0] == "show-environment":
                return True, crew_environments.pop(0)
            if args[0] == "display-message":
                return True, "demo__worker"
            if args[0] == "list-panes":
                return True, "%worker\n%extra-shell"
            return True, ""

        with mock.patch.object(config, "current_project", return_value="demo"), \
             mock.patch.object(config, "current_app", return_value="crew-demo"), \
             mock.patch.object(config, "MORPHDB_HOST", "127.0.0.1:18787"), \
             mock.patch.object(tmuxio, "tmux", side_effect=fake_tmux) as tmux:
            self.assertIsNone(tmuxio.owned_agent_session(agent))
            self.assertEqual(tmuxio.owned_agent_session(agent), "demo__worker")

        self.assertEqual(tmux.call_args_list[0], mock.call(
            "has-session", "-t", "=demo__worker"))
        self.assertEqual(tmux.call_args_list[1], mock.call(
            "show-environment", "-t", "=demo__worker"))

    def test_runtime_pane_never_resolves_a_session_prefix(self):
        calls = []

        def fake_tmux(*args, **kwargs):
            calls.append(args)
            if args[:2] == ("has-session", "-t"):
                return (False, "can't find session") if args[2] == "=crew_short" \
                    else (True, "")
            if args[0] == "list-panes":
                return True, "crew_shorter\t%91\t/dev/ttys091"
            return True, ""

        processes = {
            "ttys091": [{"comm": "claude", "command": "claude"}],
        }
        with mock.patch.object(tmuxio, "tmux", side_effect=fake_tmux), \
             mock.patch.object(tmuxio, "process_inventory", return_value=processes):
            pane = tmuxio.runtime_pane("crew_short", "claude", fallback=True)
        self.assertIsNone(pane)
        self.assertFalse(any(call[0] == "list-panes" for call in calls), calls)

    def test_runtime_pane_uses_exact_tmux_target_syntax(self):
        calls = []

        def fake_tmux(*args, **kwargs):
            calls.append(args)
            if args[0] == "has-session":
                return True, ""
            if args[0] == "list-panes":
                return True, "crew_exact\t%8\t/dev/ttys008"
            return True, ""

        processes = {
            "ttys008": [{"comm": "codex", "command": "codex"}],
        }
        with mock.patch.object(tmuxio, "tmux", side_effect=fake_tmux), \
             mock.patch.object(tmuxio, "process_inventory", return_value=processes):
            self.assertEqual(
                tmuxio.runtime_pane("crew_exact", "codex", fallback=False), "%8")
        list_call = next(call for call in calls if call[0] == "list-panes")
        self.assertIn("=crew_exact", list_call)

    def test_stored_runtime_pane_uses_tmux_foreground_when_peer_ps_is_hidden(self):
        """Agent sandboxes may hide peer processes from ``ps -axo``.

        The exact ownership-bound pane remains safe to use when tmux itself
        proves that its foreground process is the configured runtime.
        """
        session = config.tmux_target("worker", config.TMUX_ENDPOINT_CREW)
        agent = {
            "name": "worker", "session": "worker", "pane": "%8",
            "runtime": "codex", "launch_cmd": "codex",
        }

        def fake_tmux(*args, **kwargs):
            self.assertEqual(args[0], "display-message")
            self.assertEqual(str(args[args.index("-t") + 1]), "%8")
            return True, "worker\t%8\tcodex\n"

        with mock.patch.object(tmuxio, "tmux", side_effect=fake_tmux), \
             mock.patch.object(tmuxio, "capture_frame") as capture:
            pane = tmuxio.stored_runtime_pane(agent, session)

        self.assertEqual(pane, "%8")
        self.assertIsInstance(pane, config.TmuxTarget)
        self.assertEqual(pane.endpoint, config.TMUX_ENDPOINT_CREW)
        capture.assert_not_called()

    def test_stored_runtime_pane_rejects_shell_or_wrong_exact_pane(self):
        session = config.tmux_target("worker", config.TMUX_ENDPOINT_CREW)
        agent = {
            "name": "worker", "session": "worker", "pane": "%8",
            "runtime": "codex", "launch_cmd": "codex",
        }
        responses = iter((
            (True, "worker\t%8\tzsh\n"),
            (True, "worker\t%9\tcodex\n"),
        ))
        with mock.patch.object(tmuxio, "tmux", side_effect=lambda *a, **k: next(responses)):
            self.assertIsNone(tmuxio.stored_runtime_pane(agent, session))
            self.assertIsNone(tmuxio.stored_runtime_pane(agent, session))

    def test_stored_runtime_pane_recognizes_claude_wrapper_only_with_claude_ui(self):
        session = config.tmux_target("worker", config.TMUX_ENDPOINT_CREW)
        agent = {
            "name": "worker", "session": "worker", "pane": "%8",
            "runtime": "claude", "launch_cmd": "claude --resume",
        }
        response = (True, "worker\t%8\tPython\n")
        claude_frame = (
            "result\n────────────────────────\n❯\u00a0\n"
            "⏵⏵ bypass permissions on (shift+tab to cycle)\n")
        with mock.patch.object(tmuxio, "tmux", return_value=response), \
             mock.patch.object(tmuxio, "capture_frame", return_value=claude_frame):
            self.assertEqual(tmuxio.stored_runtime_pane(agent, session), "%8")
        with mock.patch.object(tmuxio, "tmux", return_value=response), \
             mock.patch.object(tmuxio, "capture_frame", return_value=">>> "):
            self.assertIsNone(tmuxio.stored_runtime_pane(agent, session))

    def test_pane_ready_fails_closed_on_missing_target_or_capture_error(self):
        with mock.patch.object(tmuxio, "tmux", return_value=(False, "gone")), \
             mock.patch.object(tmuxio.time, "sleep") as sleep:
            self.assertFalse(tmuxio.pane_ready(None))
            self.assertFalse(tmuxio.pane_ready("%missing"))
        sleep.assert_not_called()

    def test_ordinary_prose_question_is_not_a_permission_prompt(self):
        prose = "I reviewed the design. Do you want to proceed with this plan?"
        self.assertEqual(tmuxio.detect_status(prose, "claude"), "idle")
        permission = "Do you want to proceed?\n❯ 1. Yes\n  2. No"
        self.assertEqual(
            tmuxio.detect_status(permission, "claude"), "needs_input")


@unittest.skipUnless(shutil.which("tmux"), "tmux is required for live PTY checks")
class PtyTransportLiveTests(unittest.TestCase):
    def setUp(self):
        self.tmux_root = tempfile.TemporaryDirectory(
            prefix="crew-pty-live-endpoint-")
        self.tmux_env = mock.patch.object(
            config, "_TMUX_TMPDIR_TEST_OVERRIDE", self.tmux_root.name)
        self.tmux_env.start()
        suffix = uuid.uuid4().hex[:10]
        self.session = "crew_ptyaudit_" + suffix
        self.views = []
        subprocess.run(
            config.tmux_command(
                "new-session", "-d", "-s", self.session,
                "-n", "agent", "cat"),
            env=config.tmux_environment(), check=True, capture_output=True)

    def tearDown(self):
        for view in self.views:
            ptyio.close(view)
        subprocess.run(
            config.tmux_command("kill-session", "-t", self.session),
            env=config.tmux_environment(), check=False, capture_output=True)
        self.tmux_env.stop()
        self.tmux_root.cleanup()

    def _open(self, window="agent"):
        view, fd = ptyio.open_attach(self.session, window)
        if view:
            self.views.append(view)
        return view, fd

    def test_missing_window_fails_without_a_grouped_view_leak(self):
        view, fd = self._open("missing")
        self.assertIsNone(view)
        self.assertIsNone(fd)
        rows = subprocess.run(
            config.tmux_command(
                "list-sessions", "-F", "#{session_name}"),
            env=config.tmux_environment(), check=True,
            capture_output=True, text=True).stdout.splitlines()
        self.assertFalse(any(name.startswith(f"_ngview_{os.getpid()}_")
                             for name in rows), rows)

    def test_window_name_and_index_share_one_newest_view(self):
        first, _ = self._open("agent")
        self.assertIsNotNone(first)
        second, _ = self._open("0")
        self.assertIsNotNone(second)
        self.assertNotEqual(first, second)
        self.assertIsNone(ptyio.get_fd(first), "older alias view was not evicted")
        self.assertIsNotNone(ptyio.get_fd(second))

    def test_utf8_input_round_trips_and_resize_reaches_the_real_window(self):
        view, fd = self._open()
        self.assertIsNotNone(view)
        self.assertTrue(ptyio.set_size(view, 93, 31))
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            ready, _, _ = select.select([fd], [], [], 0.05)
            if not ready:
                break
            os.read(fd, 65536)

        payload = "pty-héllo-世界".encode("utf-8")
        self.assertTrue(ptyio.write_input(view, payload + b"\n"))
        data = b""
        deadline = time.monotonic() + 3
        while payload not in data and time.monotonic() < deadline:
            ready, _, _ = select.select([fd], [], [], 0.2)
            if ready:
                data += os.read(fd, 65536)
        self.assertIn(payload, data)
        size = subprocess.run(
            config.tmux_command(
                "display-message", "-p", "-t", f"{self.session}:agent",
                "#{window_width}x#{window_height}"),
            env=config.tmux_environment(), check=True,
            capture_output=True, text=True).stdout.strip()
        self.assertEqual(size, "93x31")

    def test_runtime_pane_finds_the_exact_custom_process(self):
        pane = tmuxio.runtime_pane(
            self.session, "custom", launch_cmd="cat", fallback=False)
        self.assertIsNotNone(pane)
        self.assertTrue(pane.startswith("%"), pane)


if __name__ == "__main__":
    unittest.main(verbosity=2)
