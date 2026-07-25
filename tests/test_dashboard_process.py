"""Dashboard lifecycle state is isolated by listening port and process identity."""
import io
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from crew import cli, graphstore as gs  # noqa: E402
from crew.server import app  # noqa: E402


class DashboardProcessPathTests(unittest.TestCase):
    def test_dashboard_identity_closes_http_error_response(self):
        error = urllib.error.HTTPError(
            "http://127.0.0.1:19001/api/health",
            500,
            "server error",
            {},
            io.BytesIO(b'{"error":"broken"}'),
        )
        self.addCleanup(error.close)

        with mock.patch("urllib.request.urlopen", side_effect=error):
            self.assertIsNone(cli._dashboard_identity())

        self.assertTrue(error.closed)

    def test_dashboard_alive_closes_http_error_response(self):
        error = urllib.error.HTTPError(
            "http://127.0.0.1:19001/api/graph/snapshot",
            500,
            "server error",
            {},
            io.BytesIO(b'{"error":"broken"}'),
        )
        self.addCleanup(error.close)

        with mock.patch.object(cli, "_dashboard_identity", return_value=None), \
             mock.patch("urllib.request.urlopen", side_effect=error):
            self.assertFalse(cli._dashboard_alive())

        self.assertTrue(error.closed)

    def test_pid_log_and_capability_paths_are_port_scoped(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(cli, "VAR", tmp):
            first = cli._dashboard_paths(19001)
            second = cli._dashboard_paths(19002)

        self.assertEqual(first.pid, os.path.join(tmp, "dashboard-19001.pid"))
        self.assertEqual(first.log, os.path.join(tmp, "dashboard-19001.log"))
        self.assertEqual(first.capability,
                         os.path.join(tmp, "dashboard-19001.cap"))
        self.assertNotEqual(first.pid, second.pid)
        self.assertNotEqual(first.log, second.log)
        self.assertNotEqual(first.capability, second.capability)

    def test_start_writes_identity_metadata_only_for_its_port(self):
        process = SimpleNamespace(pid=43123)
        live = {
            "ok": True, "service": "crew-dashboard", "pid": 43123,
            "port": 19003, "app": cli.config.current_app(),
            "instance_id": "test-instance",
        }
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(cli, "VAR", tmp), \
             mock.patch.object(cli.config, "DASHBOARD_PORT", 19003), \
             mock.patch.object(cli, "_port_open", side_effect=[False, True]), \
             mock.patch.object(cli, "_dashboard_identity", return_value=live), \
             mock.patch.object(cli.secrets, "token_urlsafe",
                               return_value="test-instance"), \
             mock.patch.object(cli.subprocess, "Popen", return_value=process) as popen:
            url, started = cli.start_dashboard()

            paths = cli._dashboard_paths(19003)
            with open(paths.pid) as fh:
                metadata = json.load(fh)
            child_env = popen.call_args.kwargs["env"]

        self.assertTrue(started)
        self.assertEqual(url.split("/#cap=", 1)[0], "http://127.0.0.1:19003")
        self.assertEqual(metadata["pid"], 43123)
        self.assertEqual(metadata["port"], 19003)
        self.assertEqual(metadata["app"], cli.config.current_app())
        self.assertEqual(metadata["instance_id"],
                         child_env["CREW_DASHBOARD_INSTANCE_ID"])
        self.assertTrue(child_env["CREW_DASHBOARD_INSTANCE_ID"])
        self.assertIn("CREW_DASHBOARD_CAPABILITY", child_env)
        self.assertFalse(os.path.exists(os.path.join(tmp, "dashboard.pid")))

    def test_start_allows_schema_upgrade_to_delay_listener_past_three_seconds(self):
        process = SimpleNamespace(pid=43126)
        calls = {"port": 0}
        live = {
            "ok": True, "service": "crew-dashboard", "pid": 43126,
            "port": 19021, "app": cli.config.current_app(),
            "instance_id": "delayed-schema-instance",
        }

        def port_open():
            calls["port"] += 1
            return calls["port"] >= 42

        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(cli, "VAR", tmp), \
             mock.patch.object(cli.config, "DASHBOARD_PORT", 19021), \
             mock.patch.object(cli, "_port_open", side_effect=port_open), \
             mock.patch.object(cli, "_dashboard_identity", return_value=live), \
             mock.patch.object(cli.secrets, "token_urlsafe",
                               return_value="delayed-schema-instance"), \
             mock.patch.object(cli.time, "sleep"), \
             mock.patch.object(cli.subprocess, "Popen", return_value=process):
            url, started = cli.start_dashboard()

        self.assertTrue(started)
        self.assertIn("127.0.0.1:19021", url)
        self.assertGreater(calls["port"], 31)

    def test_start_refuses_a_dashboard_for_another_app(self):
        live = {
            "ok": True, "service": "crew-dashboard", "pid": 99,
            "port": 19012, "app": "crew-somewhere-else",
            "instance_id": "foreign-instance",
        }
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(cli, "VAR", tmp), \
             mock.patch.object(cli.config, "DASHBOARD_PORT", 19012), \
             mock.patch.object(cli, "_port_open", return_value=True), \
             mock.patch.object(cli, "_dashboard_identity", return_value=live), \
             mock.patch.object(cli.subprocess, "Popen") as popen:
            with self.assertRaises(gs.GraphError) as ctx:
                cli.start_dashboard()
        self.assertIn("another", str(ctx.exception).lower())
        popen.assert_not_called()

    def test_start_refuses_a_foreign_listener_instead_of_reporting_success(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(cli, "VAR", tmp), \
             mock.patch.object(cli.config, "DASHBOARD_PORT", 19013), \
             mock.patch.object(cli, "_port_open", return_value=True), \
             mock.patch.object(cli, "_dashboard_identity", return_value=None), \
             mock.patch.object(cli.subprocess, "Popen") as popen:
            with self.assertRaises(gs.GraphError) as ctx:
                cli.start_dashboard()
        self.assertIn("listening", str(ctx.exception).lower())
        popen.assert_not_called()

    def test_start_refuses_an_unowned_same_app_dashboard(self):
        live = {
            "ok": True, "service": "crew-dashboard", "pid": 100,
            "port": 19019, "app": cli.config.current_app(),
            "instance_id": "direct-instance",
        }
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(cli, "VAR", tmp), \
             mock.patch.object(cli.config, "DASHBOARD_PORT", 19019), \
             mock.patch.object(cli, "_port_open", return_value=True), \
             mock.patch.object(cli, "_dashboard_identity", return_value=live), \
             mock.patch.object(cli.subprocess, "Popen") as popen:
            with self.assertRaises(gs.GraphError) as ctx:
                cli.start_dashboard()
        self.assertIn("ownership", str(ctx.exception).lower())
        popen.assert_not_called()

    def test_start_reuses_only_matching_owned_same_app_dashboard(self):
        live = {
            "ok": True, "service": "crew-dashboard", "pid": 101,
            "port": 19020, "app": cli.config.current_app(),
            "instance_id": "owned-instance",
        }
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(cli, "VAR", tmp), \
             mock.patch.object(cli.config, "DASHBOARD_PORT", 19020), \
             mock.patch.object(cli, "_port_open", return_value=True), \
             mock.patch.object(cli, "_dashboard_identity", return_value=live), \
             mock.patch.object(cli.subprocess, "Popen") as popen:
            paths = cli._dashboard_paths(19020)
            with open(paths.pid, "w") as fh:
                json.dump({k: live[k] for k in
                           ("pid", "port", "app", "instance_id")}, fh)
            with open(paths.capability, "w") as fh:
                fh.write("owned-capability")
            url, started = cli.start_dashboard()
        self.assertFalse(started)
        self.assertIn("#cap=owned-capability", url)
        popen.assert_not_called()

    def test_start_failure_cleans_metadata_and_terminates_its_child(self):
        process = mock.Mock(pid=43124)
        process.wait.return_value = 0
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(cli, "VAR", tmp), \
             mock.patch.object(cli.config, "DASHBOARD_PORT", 19014), \
             mock.patch.object(cli, "_port_open", return_value=False), \
             mock.patch.object(cli.time, "sleep"), \
             mock.patch.object(cli.subprocess, "Popen", return_value=process):
            with self.assertRaises(gs.GraphError) as ctx:
                cli.start_dashboard()
            paths = cli._dashboard_paths(19014)
            self.assertFalse(os.path.exists(paths.pid))
            self.assertFalse(os.path.exists(paths.capability))
        self.assertIn("did not start", str(ctx.exception).lower())
        process.terminate.assert_called_once_with()

    def test_metadata_write_failure_does_not_leave_an_orphan_listener(self):
        process = mock.Mock(pid=43125)
        process.wait.return_value = 0
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(cli, "VAR", tmp), \
             mock.patch.object(cli.config, "DASHBOARD_PORT", 19016), \
             mock.patch.object(cli, "_port_open", return_value=False), \
             mock.patch.object(cli.subprocess, "Popen", return_value=process), \
             mock.patch.object(cli.json, "dump",
                               side_effect=OSError("disk full")):
            with self.assertRaises(gs.GraphError) as ctx:
                cli.start_dashboard()
            paths = cli._dashboard_paths(19016)
            self.assertFalse(os.path.exists(paths.pid))
            self.assertFalse(os.path.exists(paths.capability))
            self.assertFalse(any(name.endswith(".tmp")
                                 for name in os.listdir(tmp)))
        self.assertIn("metadata", str(ctx.exception).lower())
        process.terminate.assert_called_once_with()

    def test_child_spawn_failure_removes_the_unused_capability(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(cli, "VAR", tmp), \
             mock.patch.object(cli.config, "DASHBOARD_PORT", 19017), \
             mock.patch.object(cli, "_port_open", return_value=False), \
             mock.patch.object(cli.subprocess, "Popen",
                               side_effect=OSError("python unavailable")):
            with self.assertRaises(gs.GraphError) as ctx:
                cli.start_dashboard()
            paths = cli._dashboard_paths(19017)
            self.assertFalse(os.path.exists(paths.pid))
            self.assertFalse(os.path.exists(paths.capability))
        self.assertIn("launch", str(ctx.exception).lower())

    def test_capability_write_failure_is_a_clean_cli_error(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(cli, "VAR", tmp), \
             mock.patch.object(cli.config, "DASHBOARD_PORT", 19018), \
             mock.patch.object(cli, "_port_open", return_value=False), \
             mock.patch.object(cli, "_write_dashboard_capability",
                               side_effect=OSError("read-only filesystem")), \
             mock.patch.object(cli.subprocess, "Popen") as popen:
            with self.assertRaises(gs.GraphError) as ctx:
                cli.start_dashboard()
        self.assertIn("capability", str(ctx.exception).lower())
        popen.assert_not_called()

    def test_concurrent_starts_spawn_only_one_dashboard_process(self):
        """The port check and capability/PID claim are one locked operation."""
        state_lock = threading.Lock()
        second_entered = threading.Event()
        state = {"listening": False, "initial_checks": 0, "pids": 44000}
        results = []

        def port_open(*_args, **_kwargs):
            with state_lock:
                if state["listening"]:
                    return True
                state["initial_checks"] += 1
                check = state["initial_checks"]
            if check == 1:
                # Without a lifecycle lock the second starter enters and wakes
                # us. With the fix it cannot enter yet, so this bounded wait
                # expires and the first starter alone claims the port.
                second_entered.wait(0.25)
            else:
                second_entered.set()
            return False

        def popen(*_args, **_kwargs):
            with state_lock:
                state["pids"] += 1
                pid = state["pids"]
                state["listening"] = True
            return SimpleNamespace(pid=pid)

        def run_start():
            try:
                results.append(("ok", cli.start_dashboard()))
            except Exception as error:
                results.append(("error", f"{type(error).__name__}: {error}"))

        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(cli, "VAR", tmp), \
             mock.patch.object(cli.config, "DASHBOARD_PORT", 19011), \
             mock.patch.object(cli, "_port_open", side_effect=port_open), \
             mock.patch.object(
                 cli, "_dashboard_identity",
                 side_effect=lambda: ({
                     "ok": True, "service": "crew-dashboard",
                     "pid": state["pids"], "port": 19011,
                     "app": cli.config.current_app(),
                     "instance_id": "concurrent-instance",
                 } if state["listening"] else None)), \
             mock.patch.object(cli.secrets, "token_urlsafe",
                               return_value="concurrent-instance"), \
             mock.patch.object(cli.subprocess, "Popen", side_effect=popen) as spawned:
            threads = [
                threading.Thread(target=run_start, name=f"dashboard-start-{i}")
                for i in range(2)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)
            self.assertTrue(all(not thread.is_alive() for thread in threads),
                            "dashboard start race hung")

        self.assertEqual(spawned.call_count, 1, results)
        self.assertEqual([status for status, _ in results], ["ok", "ok"])


class DashboardStopIdentityTests(unittest.TestCase):
    def _metadata(self, port, pid=43210, instance_id="instance-a"):
        return {
            "pid": pid,
            "port": port,
            "app": cli.config.current_app(),
            "instance_id": instance_id,
        }

    def test_stop_signals_only_matching_live_dashboard_and_preserves_other_port(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(cli, "VAR", tmp), \
             mock.patch.object(cli.config, "DASHBOARD_PORT", 19004):
            ours = cli._dashboard_paths(19004)
            other = cli._dashboard_paths(19005)
            os.makedirs(tmp, exist_ok=True)
            with open(ours.pid, "w") as fh:
                json.dump(self._metadata(19004), fh)
            with open(ours.capability, "w") as fh:
                fh.write("ours")
            with open(other.pid, "w") as fh:
                json.dump(self._metadata(19005, pid=99999,
                                         instance_id="other"), fh)
            with open(other.capability, "w") as fh:
                fh.write("other-cap")

            live = self._metadata(19004)
            with mock.patch.object(cli, "_dashboard_identity",
                                   side_effect=[live, None]), \
                 mock.patch.object(cli, "_port_open", return_value=False), \
                 mock.patch.object(cli.os, "kill") as kill:
                stopped = cli.stop_dashboard()

            self.assertTrue(stopped)
            kill.assert_called_once_with(43210, 15)
            self.assertFalse(os.path.exists(ours.pid))
            self.assertFalse(os.path.exists(ours.capability))
            self.assertTrue(os.path.exists(other.pid))
            self.assertTrue(os.path.exists(other.capability))

    def test_stop_keeps_ownership_files_if_process_does_not_terminate(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(cli, "VAR", tmp), \
             mock.patch.object(cli.config, "DASHBOARD_PORT", 19015):
            paths = cli._dashboard_paths(19015)
            metadata = self._metadata(19015, pid=43215,
                                      instance_id="stuck-instance")
            with open(paths.pid, "w") as fh:
                json.dump(metadata, fh)
            with open(paths.capability, "w") as fh:
                fh.write("stuck-cap")

            with mock.patch.object(cli, "_dashboard_identity",
                                   return_value=metadata), \
                 mock.patch.object(cli.time, "sleep"), \
                 mock.patch.object(cli.os, "kill") as kill:
                stopped = cli.stop_dashboard()

            self.assertFalse(stopped)
            kill.assert_called_once_with(43215, 15)
            self.assertTrue(os.path.exists(paths.pid))
            self.assertTrue(os.path.exists(paths.capability))

    def test_stop_never_signals_a_stale_or_reused_pid(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(cli, "VAR", tmp), \
             mock.patch.object(cli.config, "DASHBOARD_PORT", 19006):
            paths = cli._dashboard_paths(19006)
            os.makedirs(tmp, exist_ok=True)
            with open(paths.pid, "w") as fh:
                json.dump(self._metadata(19006, pid=43211,
                                         instance_id="stale"), fh)
            with open(paths.capability, "w") as fh:
                fh.write("stale-cap")

            live = self._metadata(19006, pid=55555, instance_id="replacement")
            with mock.patch.object(cli, "_dashboard_identity", return_value=live), \
                 mock.patch.object(cli.os, "kill") as kill:
                stopped = cli.stop_dashboard()

            self.assertFalse(stopped)
            kill.assert_not_called()
            # A different live process now owns the port.  Preserve its files:
            # deleting a capability while refusing to stop would lock the
            # operator out of the replacement dashboard.
            self.assertTrue(os.path.exists(paths.pid))
            self.assertTrue(os.path.exists(paths.capability))

    def test_stop_cleans_closed_port_stale_metadata_without_signalling(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(cli, "VAR", tmp), \
             mock.patch.object(cli.config, "DASHBOARD_PORT", 19008):
            paths = cli._dashboard_paths(19008)
            with open(paths.pid, "w") as fh:
                json.dump(self._metadata(19008, pid=43212,
                                         instance_id="dead"), fh)
            with open(paths.capability, "w") as fh:
                fh.write("dead-cap")

            with mock.patch.object(cli, "_dashboard_identity", return_value=None), \
                 mock.patch.object(cli, "_port_open", return_value=False), \
                 mock.patch.object(cli.os, "kill") as kill:
                stopped = cli.stop_dashboard()

            self.assertFalse(stopped)
            kill.assert_not_called()
            self.assertFalse(os.path.exists(paths.pid))
            self.assertFalse(os.path.exists(paths.capability))

    def test_legacy_global_pid_is_signalled_only_when_it_owns_this_listener(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(cli, "VAR", tmp), \
             mock.patch.object(cli, "LEGACY_PIDFILE",
                               os.path.join(tmp, "dashboard.pid")), \
             mock.patch.object(cli.config, "DASHBOARD_PORT", 19009):
            with open(cli.LEGACY_PIDFILE, "w") as fh:
                fh.write("43213")
            paths = cli._dashboard_paths(19009)
            with open(paths.capability, "w") as fh:
                fh.write("legacy-cap")

            with mock.patch.object(cli, "_dashboard_identity", return_value=None), \
                 mock.patch.object(cli, "_dashboard_alive", return_value=True), \
                 mock.patch.object(cli, "_listener_pids",
                                   side_effect=[{43213}, set()]), \
                 mock.patch.object(cli.os, "kill") as kill:
                stopped = cli.stop_dashboard()

            self.assertTrue(stopped)
            kill.assert_called_once_with(43213, 15)
            self.assertFalse(os.path.exists(cli.LEGACY_PIDFILE))
            self.assertFalse(os.path.exists(paths.capability))

    def test_legacy_global_pid_for_another_port_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(cli, "VAR", tmp), \
             mock.patch.object(cli, "LEGACY_PIDFILE",
                               os.path.join(tmp, "dashboard.pid")), \
             mock.patch.object(cli.config, "DASHBOARD_PORT", 19010):
            with open(cli.LEGACY_PIDFILE, "w") as fh:
                fh.write("43214")

            with mock.patch.object(cli, "_dashboard_identity", return_value=None), \
                 mock.patch.object(cli, "_dashboard_alive", return_value=True), \
                 mock.patch.object(cli, "_listener_pids", return_value={77777}), \
                 mock.patch.object(cli, "_port_open", return_value=True), \
                 mock.patch.object(cli.os, "kill") as kill:
                stopped = cli.stop_dashboard()

            self.assertFalse(stopped)
            kill.assert_not_called()
            self.assertTrue(os.path.exists(cli.LEGACY_PIDFILE))


class DashboardHealthEndpointTests(unittest.TestCase):
    def test_health_payload_identifies_the_exact_server_process(self):
        with mock.patch.object(app.os, "getpid", return_value=24680), \
             mock.patch.object(app.config, "current_app", return_value="crew-demo"), \
             mock.patch.object(app, "PORT", 19007), \
             mock.patch.object(app, "DASHBOARD_INSTANCE_ID", "health-instance"):
            self.assertEqual(
                app._dashboard_health(),
                {
                    "ok": True,
                    "service": "crew-dashboard",
                    "pid": 24680,
                    "port": 19007,
                    "app": "crew-demo",
                    "instance_id": "health-instance",
                },
            )


if __name__ == "__main__":
    unittest.main()
