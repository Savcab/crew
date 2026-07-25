"""Process-level tests for the exact-child ingress watchdog."""

import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest

from crew import ingress, ingress_watchdog
from crew.server import hook_gateway


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WATCHDOG_PATH = os.path.join(REPO_ROOT, "crew", "ingress_watchdog.py")

_FAKE_CHILD = textwrap.dedent(
    """
    import json
    import os
    import signal
    import sys

    pid_path, argv_path = sys.argv[1:3]
    with open(pid_path, "w", encoding="utf-8") as output:
        output.write(str(os.getpid()))
        output.flush()
        os.fsync(output.fileno())
    with open(argv_path, "w", encoding="utf-8") as output:
        json.dump(sys.argv[3:], output)
        output.flush()
        os.fsync(output.fileno())
    signal.pause()
    """
)

_OWNER = textwrap.dedent(
    """
    import json
    import os
    import subprocess
    import sys
    import time

    from crew.ingress import write_cloudflared_config
    from crew.server.hook_gateway import HookGateway

    watchdog_path, child_path, pid_path, argv_path, origin = sys.argv[1:]
    gateway = HookGateway(
        readiness_secret="watchdog-integration-readiness",
        unix_socket_path=origin,
    ).start()
    config_path = origin + ".cf.json"
    write_cloudflared_config(
        os.path.dirname(origin),
        morphdb_origin="http://127.0.0.1:8787",
        app="watchdog-test",
        gateway_origin="unix:" + origin,
        config_path=config_path,
    )
    parent_read, parent_write = os.pipe()
    os.set_inheritable(parent_read, False)
    os.set_inheritable(parent_write, False)
    watchdog = subprocess.Popen(
        [
            sys.executable,
            watchdog_path,
            "--parent-fd", str(parent_read),
            "--shutdown-timeout", "1",
            "--",
            sys.executable, child_path, pid_path, argv_path,
            "--config", config_path,
            "--hello-world",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        pass_fds=(parent_read,),
    )
    os.close(parent_read)
    print(json.dumps({"watchdog_pid": watchdog.pid}), flush=True)
    while True:
        time.sleep(60)
    """
)


def _pid_exists(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def _wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.025)
    return predicate()


class IngressWatchdogTests(unittest.TestCase):
    def test_dead_parent_before_spawn_never_creates_child(self):
        parent_read, parent_write = os.pipe()
        os.close(parent_write)
        launched = []

        result = ingress_watchdog.supervise(
            parent_read,
            [sys.executable, "-c", "raise SystemExit(99)"],
            popen_factory=lambda *_args, **_kwargs: launched.append(True),
        )

        self.assertEqual(result, 0)
        self.assertEqual(launched, [])

    def test_sigkill_owner_terminates_exact_child_and_never_reuses_origin(self):
        with tempfile.TemporaryDirectory(
                prefix="cw-", dir=os.path.realpath("/tmp")) as td:
            child_script = os.path.join(td, "fake_cloudflared.py")
            child_pid_path = os.path.join(td, "child.pid")
            child_argv_path = os.path.join(td, "child.argv.json")
            old_socket = os.path.join(td, "run-old.sock")
            with open(child_script, "w", encoding="utf-8") as output:
                output.write(_FAKE_CHILD)

            owner = subprocess.Popen(
                [
                    sys.executable, "-c", _OWNER,
                    WATCHDOG_PATH,
                    child_script,
                    child_pid_path,
                    child_argv_path,
                    old_socket,
                ],
                cwd=REPO_ROOT,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            watchdog_pid = None
            child_pid = None
            try:
                owner_line = owner.stdout.readline()
                if not owner_line:
                    self.fail(
                        f"owner exited early: {owner.stderr.read()}")
                watchdog_pid = json.loads(owner_line)["watchdog_pid"]
                self.assertTrue(_wait_until(
                    lambda: os.path.exists(child_pid_path)))
                with open(child_pid_path, encoding="utf-8") as source:
                    child_pid = int(source.read())
                self.assertTrue(_pid_exists(watchdog_pid))
                self.assertTrue(_pid_exists(child_pid))
                self.assertTrue(stat.S_ISSOCK(
                    os.lstat(old_socket).st_mode))

                os.kill(owner.pid, signal.SIGKILL)
                self.assertEqual(owner.wait(timeout=5), -signal.SIGKILL)
                self.assertTrue(
                    _wait_until(lambda: not _pid_exists(child_pid)),
                    "watchdog left its exact cloudflared child alive",
                )
                self.assertTrue(
                    _wait_until(lambda: not _pid_exists(watchdog_pid)),
                    "watchdog did not exit after parent-pipe EOF",
                )

                with open(child_argv_path, encoding="utf-8") as source:
                    child_argv = json.load(source)
                old_config = old_socket + ".cf.json"
                self.assertEqual(
                    child_argv,
                    ["--config", old_config, "--hello-world"],
                )
                with open(old_config, encoding="utf-8") as source:
                    self.assertEqual(
                        json.load(source)["ingress"][0]["service"],
                        f"unix:{old_socket}",
                    )
                # SIGKILL leaves the old filesystem socket name behind, which
                # is the dangerous case for a reusable origin. Crew's
                # allocator and per-run config must both remain distinct.
                self.assertTrue(os.path.lexists(old_socket))
                self.assertTrue(os.path.exists(old_config))
                new_socket = ingress._new_gateway_socket_path(td)
                self.assertNotEqual(old_socket, new_socket)
                decoy = hook_gateway.start_hook_gateway(
                    readiness_secret="new-run-readiness",
                    unix_socket_path=new_socket,
                )
                try:
                    self.assertTrue(stat.S_ISSOCK(
                        os.lstat(new_socket).st_mode))
                    new_config = ingress.write_cloudflared_config(
                        td,
                        morphdb_origin="http://127.0.0.1:8787",
                        app="watchdog-test",
                        gateway_origin=f"unix:{new_socket}",
                    )
                    self.addCleanup(
                        lambda: os.path.exists(new_config)
                        and os.unlink(new_config))
                    self.assertNotEqual(old_config, new_config)
                    with open(new_config, encoding="utf-8") as source:
                        self.assertEqual(
                            json.load(source)["ingress"][0]["service"],
                            f"unix:{new_socket}",
                        )
                    self.assertEqual(
                        child_argv[1], old_config)
                    self.assertNotEqual(child_argv[1], new_config)
                finally:
                    decoy.close()
            finally:
                if owner.poll() is None:
                    os.kill(owner.pid, signal.SIGKILL)
                    owner.wait(timeout=5)
                for pid in (child_pid, watchdog_pid):
                    if pid is not None and _pid_exists(pid):
                        try:
                            os.kill(pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                if owner.stdout is not None:
                    owner.stdout.close()
                if owner.stderr is not None:
                    owner.stderr.close()


if __name__ == "__main__":
    unittest.main()
