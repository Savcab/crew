"""Process-level tests for Crew's public-ingress lease and rendezvous state."""

import json
import os
import stat
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from crew import ingress_state  # noqa: E402


ORIGIN = "http://127.0.0.1:18787"
APP = "crewtest-ingress-state"
PUBLIC_URL = "https://single-demo.trycloudflare.com"


_HOLDER = textwrap.dedent(
    """
    import json
    import os
    import sys

    from crew import ingress_state

    runtime_dir, origin, app, public_url, exit_mode = sys.argv[1:]
    ingress_state._RUNTIME_DIR = runtime_dir
    lease = ingress_state.acquire_lease(origin=origin, app=app)
    state = lease.publish(public_url)
    print(json.dumps({
        "state": state,
        "lock_path": lease.lock_path,
        "state_path": lease.state_path,
    }), flush=True)
    command = sys.stdin.readline().strip()
    if exit_mode == "crash" or command == "crash":
        os._exit(17)
    lease.close()
    """
)


_READER = textwrap.dedent(
    """
    import json
    import sys

    from crew import ingress_state

    runtime_dir, origin, app = sys.argv[1:]
    ingress_state._RUNTIME_DIR = runtime_dir
    print(json.dumps(
        ingress_state.read_active_state(origin=origin, app=app),
        sort_keys=True,
    ), flush=True)
    """
)


class IngressStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="crew-ingress-state-")
        self.addCleanup(self.temp.cleanup)
        self.runtime_dir = os.path.join(self.temp.name, "rendezvous")
        patcher = mock.patch.object(
            ingress_state, "_RUNTIME_DIR", self.runtime_dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.children = []
        self.addCleanup(self._stop_children)

    def _stop_children(self):
        for child in reversed(self.children):
            try:
                if child.poll() is None:
                    try:
                        child.stdin.write("close\n")
                        child.stdin.flush()
                        child.wait(timeout=5)
                    except (BrokenPipeError, OSError, subprocess.TimeoutExpired):
                        child.terminate()
                        try:
                            child.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            child.kill()
                            child.wait(timeout=5)
            finally:
                for stream in (child.stdin, child.stdout, child.stderr):
                    if stream is not None:
                        stream.close()

    def _holder(self, *, origin=ORIGIN, app=APP, exit_mode="close"):
        child = subprocess.Popen(
            [
                sys.executable, "-c", _HOLDER,
                self.runtime_dir, origin, app, PUBLIC_URL, exit_mode,
            ],
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.children.append(child)
        line = child.stdout.readline()
        if not line:
            stderr = child.stderr.read()
            self.fail(f"lease holder exited before readiness: {stderr}")
        return child, json.loads(line)

    def _read_from_process(self, *, origin=ORIGIN, app=APP):
        result = subprocess.run(
            [
                sys.executable, "-c", _READER,
                self.runtime_dir, origin, app,
            ],
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_same_backend_and_app_exclude_a_second_process(self):
        self._holder()

        with self.assertRaises(ingress_state.IngressAlreadyRunning):
            ingress_state.acquire_lease(origin=ORIGIN, app=APP)

    def test_different_app_and_backend_scopes_are_independent(self):
        self._holder()

        app_lease = ingress_state.acquire_lease(
            origin=ORIGIN, app=APP + "-other")
        self.addCleanup(app_lease.close)
        backend_lease = ingress_state.acquire_lease(
            origin="https://morph.example.test:9443", app=APP)
        self.addCleanup(backend_lease.close)

        self.assertNotEqual(app_lease.lock_path, backend_lease.lock_path)
        self.assertNotEqual(
            app_lease.lock_path,
            ingress_state._scope_paths(ORIGIN, APP).lock_path)

    def test_crashed_holder_state_is_ignored_and_removed(self):
        child, ready = self._holder(exit_mode="crash")
        self.assertEqual(
            self._read_from_process()["public_base_url"], PUBLIC_URL)

        child.stdin.write("crash\n")
        child.stdin.flush()
        self.assertEqual(child.wait(timeout=5), 17)
        self.assertTrue(os.path.exists(ready["state_path"]))

        self.assertIsNone(self._read_from_process())
        self.assertFalse(os.path.exists(ready["state_path"]))

    def test_concurrent_readers_never_mistake_stale_cleanup_for_owner(self):
        child, ready = self._holder(exit_mode="crash")
        child.stdin.write("crash\n")
        child.stdin.flush()
        self.assertEqual(child.wait(timeout=5), 17)
        self.assertTrue(os.path.exists(ready["state_path"]))

        barrier = threading.Barrier(9)
        results = []
        errors = []

        def read():
            try:
                barrier.wait()
                results.append(self._read_from_process())
            except BaseException as error:
                errors.append(error)

        readers = [threading.Thread(target=read) for _ in range(8)]
        for reader in readers:
            reader.start()
        barrier.wait()
        for reader in readers:
            reader.join(timeout=10)
            self.assertFalse(reader.is_alive())

        self.assertEqual(errors, [])
        self.assertEqual(results, [None] * 8)
        self.assertFalse(os.path.exists(ready["state_path"]))

    def test_new_owner_never_makes_predecessor_state_look_live(self):
        child, ready = self._holder(exit_mode="crash")
        child.stdin.write("crash\n")
        child.stdin.flush()
        self.assertEqual(child.wait(timeout=5), 17)
        self.assertTrue(os.path.exists(ready["state_path"]))

        entered_cleanup = threading.Event()
        release_cleanup = threading.Event()
        original_unlink = ingress_state._unlink_stale_state
        acquired = []
        errors = []

        def paused_unlink(directory_fd, state_name):
            if threading.current_thread().name == "new-ingress-owner":
                entered_cleanup.set()
                if not release_cleanup.wait(5):
                    raise RuntimeError("test did not release paused cleanup")
            return original_unlink(directory_fd, state_name)

        def acquire():
            try:
                lease = ingress_state.acquire_lease(
                    origin=ORIGIN, app=APP)
                acquired.append(lease)
            except BaseException as error:
                errors.append(error)

        with mock.patch.object(
                ingress_state, "_unlink_stale_state",
                side_effect=paused_unlink):
            starter = threading.Thread(
                target=acquire, name="new-ingress-owner")
            starter.start()
            self.assertTrue(entered_cleanup.wait(2))
            # The new owner holds only a compatible shared lock while the
            # predecessor's state exists, so a reader must clear/ignore it.
            self.assertIsNone(
                ingress_state.read_active_state(origin=ORIGIN, app=APP))
            release_cleanup.set()
            starter.join(timeout=5)

        self.assertFalse(starter.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(acquired), 1)
        acquired[0].close()

    def test_state_is_visible_only_for_lifetime_of_lease(self):
        child, ready = self._holder()

        active = self._read_from_process()
        self.assertEqual(active["origin"], ORIGIN)
        self.assertEqual(active["app"], APP)
        self.assertEqual(active["public_base_url"], PUBLIC_URL)
        self.assertEqual(active["pid"], child.pid)

        child.stdin.write("close\n")
        child.stdin.flush()
        self.assertEqual(child.wait(timeout=5), 0)
        self.assertFalse(os.path.exists(ready["state_path"]))
        self.assertIsNone(self._read_from_process())

    def test_runtime_files_are_private_regular_bounded_and_cloexec(self):
        lease = ingress_state.acquire_lease(origin=ORIGIN, app=APP)
        self.addCleanup(lease.close)
        lease.publish(PUBLIC_URL)

        self.assertEqual(stat.S_IMODE(os.stat(lease.state_dir).st_mode), 0o700)
        self.assertFalse(os.get_inheritable(lease._lock_fd))
        for path in (lease.lock_path, lease.state_path):
            info = os.lstat(path)
            self.assertTrue(stat.S_ISREG(info.st_mode))
            self.assertEqual(info.st_uid, os.getuid())
            self.assertEqual(stat.S_IMODE(info.st_mode), 0o600)
            self.assertLessEqual(len(os.path.basename(path)), 80)
            self.assertNotIn(APP, os.path.basename(path))
        self.assertEqual(os.path.dirname(lease.config_path), lease.state_dir)
        self.assertLessEqual(len(os.path.basename(lease.config_path)), 80)

    def test_each_lease_gets_a_distinct_per_run_config_path(self):
        first = ingress_state.acquire_lease(origin=ORIGIN, app=APP)
        first_path = first.config_path
        first.close()

        second = ingress_state.acquire_lease(origin=ORIGIN, app=APP)
        self.addCleanup(second.close)
        self.assertNotEqual(first_path, second.config_path)
        self.assertEqual(
            os.path.dirname(first_path), os.path.dirname(second.config_path))
        self.assertTrue(first_path.endswith(".cf.json"))
        self.assertTrue(second.config_path.endswith(".cf.json"))

    def test_scope_and_public_url_validation_are_strict(self):
        origin, app = ingress_state.canonical_scope(
            "HTTP://LOCALHOST:80/", APP)
        self.assertEqual(origin, "http://localhost")
        self.assertEqual(app, APP)
        hosted, _ = ingress_state.canonical_scope(
            "HTTPS://API.Example.Test:443/live/", APP)
        self.assertEqual(hosted, "https://api.example.test/live")
        self.assertEqual(
            ingress_state.validate_public_base_url(PUBLIC_URL + "/"),
            PUBLIC_URL,
        )

        invalid_origins = (
            "127.0.0.1:8787",
            "ftp://morph.example.test",
            "http://user:pass@morph.example.test",
            "http://morph.example.test/api//v1",
            "http://morph.example.test/api/../v1",
            "http://morph.example.test/api/%2e%2e/v1",
            "http://morph.example.test?tenant=x",
            "http://morph.example.test#frag",
        )
        for value in invalid_origins:
            with self.subTest(origin=value):
                with self.assertRaises(ValueError):
                    ingress_state.canonical_scope(value, APP)

        for value in ("", " app", "app ", "app\nother", "x" * 256):
            with self.subTest(app=value):
                with self.assertRaises(ValueError):
                    ingress_state.canonical_scope(ORIGIN, value)

        invalid_public_urls = (
            "http://single-demo.trycloudflare.com",
            "https://trycloudflare.com",
            "https://a.b.trycloudflare.com",
            "https://single-demo.trycloudflare.com:443",
            "https://single-demo.trycloudflare.com/path",
            "https://single-demo.trycloudflare.com?x=1",
            "https://-bad.trycloudflare.com",
            "https://bad-.trycloudflare.com",
        )
        for value in invalid_public_urls:
            with self.subTest(public_url=value):
                with self.assertRaises(ValueError):
                    ingress_state.validate_public_base_url(value)

    def test_reader_rejects_state_for_a_different_exact_scope(self):
        lease = ingress_state.acquire_lease(origin=ORIGIN, app=APP)
        self.addCleanup(lease.close)
        state = lease.publish(PUBLIC_URL)

        state["app"] = APP + "-forged"
        ingress_state._atomic_write_json(
            lease._directory_fd, lease._paths.state_name, state)

        self.assertIsNone(
            ingress_state.read_active_state(origin=ORIGIN, app=APP))


if __name__ == "__main__":
    unittest.main()
