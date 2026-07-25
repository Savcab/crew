"""Behavior tests for the lean foreground public-ingress runner."""

import io
import json
import math
import os
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import urllib.request
import unittest
from unittest import mock

from crew import ingress


class _Response:
    def __init__(self, *, status=204, body=b"", url=None):
        self.status = status
        self.body = body
        self.url = url

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, limit=-1):
        return self.body if limit < 0 else self.body[:limit]

    def geturl(self):
        return self.url


class _RedirectedResponse(_Response):
    def __init__(self):
        super().__init__(url="https://unrelated.example.invalid/ready")

    def geturl(self):
        return "https://unrelated.example.invalid/ready"


class _FakeGateway:
    readiness_path = "/.well-known/crew-ingress-ready"

    def __init__(self, events, readiness_secret, *, alive=True):
        self.events = events
        self.readiness_secret = readiness_secret
        self.readiness_header = ("X-Crew-Ingress-Probe", readiness_secret)
        self.port = 48123
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.socket_path = None
        self.origin_url = self.base_url
        self.alive = alive
        self.disabled = False
        self.closed = False

    def start(self):
        self.events.append("gateway.start")

    def disable_readiness(self):
        self.events.append("gateway.disable")
        self.disabled = True

    def is_alive(self):
        return self.alive

    def close(self):
        self.events.append("gateway.close")
        self.closed = True
        self.alive = False


class _FakeProcess:
    def __init__(self, output=b"", *, returncode=None, wait_times_out=False):
        self.stdout = _BlockingStream(output)
        self.returncode = returncode
        self.wait_times_out = wait_times_out
        self.terminated = False
        self.killed = False
        self.wait_timeouts = []

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        if not self.wait_times_out:
            self.returncode = -signal.SIGTERM
            self.stdout.close()

    def kill(self):
        self.killed = True
        self.returncode = -signal.SIGKILL
        self.stdout.close()

    def wait(self, timeout=None):
        self.wait_timeouts.append(timeout)
        if self.returncode is None or (
                self.wait_times_out and not self.killed):
            raise subprocess.TimeoutExpired("cloudflared", timeout)
        return self.returncode


class _BlockingStream:
    """Readable test pipe that stays open until its fake process exits."""

    def __init__(self, initial):
        self._buffer = io.BytesIO(initial)
        self._condition = threading.Condition()
        self.closed = False

    def readline(self, size=-1):
        with self._condition:
            value = self._buffer.readline(size)
            if value:
                return value
            while not self.closed:
                self._condition.wait()
            return b""

    def close(self):
        with self._condition:
            if self.closed:
                return
            self.closed = True
            self._buffer.close()
            self._condition.notify_all()


def _quick_tunnel_metrics(host="quiet-pond.trycloudflare.com"):
    return b'{"hostname":"' + host.encode("ascii") + b'"}'


def _quick_tunnel_log(host="quiet-pond.trycloudflare.com"):
    return (
        b'{"level":"info","message":"|  https://'
        + host.encode("ascii") + b'  |"}\n'
    )


class FindCloudflaredTests(unittest.TestCase):
    def test_resolves_an_absolute_executable_and_never_installs(self):
        with tempfile.TemporaryDirectory() as td:
            binary = os.path.join(td, "cloudflared")
            with open(binary, "wb") as fh:
                fh.write(b"test")
            os.chmod(binary, 0o700)

            self.assertEqual(
                os.path.realpath(binary),
                ingress.find_cloudflared(binary),
            )

        with mock.patch("crew.ingress.shutil.which", return_value=None):
            with self.assertRaisesRegex(
                    ingress.IngressError, "install cloudflared"):
                ingress.find_cloudflared()

    def test_rejects_a_non_executable_explicit_path(self):
        with tempfile.TemporaryDirectory() as td:
            binary = os.path.join(td, "cloudflared")
            with open(binary, "wb") as fh:
                fh.write(b"test")
            os.chmod(binary, 0o600)
            with self.assertRaisesRegex(ingress.IngressError, "executable"):
                ingress.find_cloudflared(binary)


class CloudflaredConfigTests(unittest.TestCase):
    def test_config_is_app_scoped_owner_only_and_pins_unix_origin(self):
        with tempfile.TemporaryDirectory() as td:
            gateway_origin = f"unix:{os.path.join(td, 'gateway.sock')}"
            path = ingress.write_cloudflared_config(
                td,
                morphdb_origin="http://127.0.0.1:8787",
                app="crew-red",
                gateway_origin=gateway_origin,
            )
            with open(path, encoding="utf-8") as fh:
                self.assertEqual(json.load(fh), {
                    "ingress": [{
                        "service": gateway_origin,
                        "originRequest": {
                            "disableChunkedEncoding": True,
                        },
                    }],
                })
            self.assertEqual(0o600, stat.S_IMODE(os.stat(path).st_mode))
            self.assertNotIn("crew-red", os.path.basename(path))

    def test_writes_an_explicit_lease_config_path(self):
        with tempfile.TemporaryDirectory() as td:
            requested = os.path.join(td, "scope.cf.json")
            gateway_origin = f"unix:{os.path.join(td, 'gateway.sock')}"
            path = ingress.write_cloudflared_config(
                td,
                morphdb_origin="http://127.0.0.1:8787",
                app="crew-red",
                gateway_origin=gateway_origin,
                config_path=requested,
            )
            self.assertEqual(requested, path)
            with open(path, encoding="utf-8") as fh:
                self.assertEqual(
                    json.load(fh)["ingress"][0]["service"],
                    gateway_origin,
                )
            self.assertEqual(0o600, stat.S_IMODE(os.stat(path).st_mode))

    def test_explicit_config_cannot_escape_or_alias_another_state_file(self):
        with tempfile.TemporaryDirectory() as td:
            outside = os.path.join(os.path.dirname(td), "outside.cf.json")
            state_path = os.path.join(td, "scope.json")
            for path in (outside, state_path):
                with self.subTest(path=path):
                    with self.assertRaisesRegex(
                            ingress.IngressError, "config path"):
                        ingress.write_cloudflared_config(
                            td,
                            morphdb_origin="http://127.0.0.1:8787",
                            app="crew-red",
                            gateway_origin=(
                                f"unix:{os.path.join(td, 'gateway.sock')}"),
                            config_path=path,
                        )

    def test_explicit_config_never_follows_a_symlink(self):
        with tempfile.TemporaryDirectory() as td:
            target = os.path.join(os.path.dirname(td), "sentinel")
            with open(target, "wb") as fh:
                fh.write(b"keep")
            self.addCleanup(
                lambda: os.path.exists(target) and os.unlink(target))
            path = os.path.join(td, "scope.cf.json")
            os.symlink(target, path)
            with self.assertRaisesRegex(
                    ingress.IngressError, "could not create"):
                ingress.write_cloudflared_config(
                    td,
                    morphdb_origin="http://127.0.0.1:8787",
                    app="crew-red",
                    gateway_origin=(
                        f"unix:{os.path.join(td, 'gateway.sock')}"),
                    config_path=path,
                )
            with open(target, "rb") as fh:
                self.assertEqual(b"keep", fh.read())

    def test_explicit_config_never_rewrites_an_existing_file(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "scope.cf.json")
            with open(path, "wb") as fh:
                fh.write(b"keep")
            with self.assertRaisesRegex(
                    ingress.IngressError, "could not create"):
                ingress.write_cloudflared_config(
                    td,
                    morphdb_origin="http://127.0.0.1:8787",
                    app="crew-red",
                    gateway_origin=(
                        f"unix:{os.path.join(td, 'gateway.sock')}"),
                    config_path=path,
                )
            with open(path, "rb") as fh:
                self.assertEqual(b"keep", fh.read())

    def test_gateway_origin_cannot_escape_the_private_runtime(self):
        with tempfile.TemporaryDirectory() as td:
            outside = os.path.join(os.path.dirname(td), "gateway.sock")
            with self.assertRaisesRegex(
                    ingress.IngressError, "private runtime"):
                ingress.write_cloudflared_config(
                    td,
                    morphdb_origin="http://127.0.0.1:8787",
                    app="crew-red",
                    gateway_origin=f"unix:{outside}",
                )

    def test_failed_config_write_removes_its_exact_created_file(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "scope.cf.json")
            with mock.patch(
                    "crew.ingress.os.fsync",
                    side_effect=OSError("synthetic disk failure")):
                with self.assertRaisesRegex(
                        ingress.IngressError, "could not write"):
                    ingress.write_cloudflared_config(
                        td,
                        morphdb_origin="http://127.0.0.1:8787",
                        app="crew-red",
                        gateway_origin=(
                            f"unix:{os.path.join(td, 'gateway.sock')}"),
                        config_path=path,
                    )
            self.assertFalse(os.path.lexists(path))


class PublicReadinessOpenerTests(unittest.TestCase):
    def test_dns_fallback_preserves_the_verified_quick_tunnel_hostname(self):
        request = urllib.request.Request(
            "https://quiet-pond.trycloudflare.com/ready",
            headers={"X-Test": "secret"},
        )
        calls = []

        def open_once(received, timeout, *, connect_host=None):
            calls.append((received.full_url, timeout, connect_host))
            if connect_host is None:
                raise socket.gaierror("blocked random hostname")
            return _Response(url=received.full_url)

        addresses = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "",
             ("203.0.113.10", 443)),
        ]
        with mock.patch(
                "crew.ingress._https_request_once",
                side_effect=open_once), \
             mock.patch(
                 "crew.ingress.socket.getaddrinfo",
                 return_value=addresses):
            response = ingress._open_public_url(request, timeout=2)

        self.assertEqual(response.status, 204)
        self.assertEqual(response.geturl(), request.full_url)
        self.assertEqual(calls[0][2], None)
        self.assertEqual(calls[1][2], "203.0.113.10")
        self.assertTrue(all(call[1] > 0 for call in calls))


class IngressRunnerTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.binary = os.path.join(self.tempdir.name, "cloudflared")
        with open(self.binary, "wb") as fh:
            fh.write(b"fake")
        os.chmod(self.binary, 0o700)

    def _run(self, *, output=None, metrics_body=None, process=None,
             gateway=None, opener=None, metrics_opener=None, preflight=None,
             on_ready=None, on_stopping=None, stop_event=None, **overrides):
        events = []
        gateway = gateway or _FakeGateway(events, "placeholder")

        def gateway_factory(*, readiness_secret, unix_socket_path):
            gateway.readiness_secret = readiness_secret
            gateway.readiness_header = (
                "X-Crew-Ingress-Probe", readiness_secret)
            gateway.socket_path = unix_socket_path
            gateway.origin_url = f"unix:{unix_socket_path}"
            events.append("gateway.factory")
            return gateway

        process = process or _FakeProcess(
            (
                b'{"level":"info","message":"|  '
                b'https://quiet-pond.trycloudflare.com  |"}\n'
            )
            if output is None else output)
        popen_calls = []
        config_snapshots = []

        def popen(argv, **kwargs):
            popen_calls.append((argv, kwargs))
            child_argv = argv[argv.index("--") + 1:]
            config_path = child_argv[child_argv.index("--config") + 1]
            with open(config_path, encoding="utf-8") as source:
                config_snapshots.append(
                    (config_path, json.load(source)))
            events.append("popen")
            return process

        if preflight is None:
            preflight = lambda: events.append("preflight")
        if opener is None:
            opener = lambda request, timeout: _Response(url=request.full_url)
        metrics_requests = []
        if metrics_opener is None:
            metrics_body = (
                _quick_tunnel_metrics()
                if metrics_body is None else metrics_body
            )

            def metrics_opener(request, timeout):
                metrics_requests.append((request, timeout))
                return _Response(
                    body=metrics_body,
                    url=request.full_url,
                    status=200,
                )
        if stop_event is None:
            stop_event = threading.Event()

        ready_values = []

        def default_ready(public_url):
            events.append("ready")
            ready_values.append((public_url, gateway.disabled))
            stop_event.set()

        result = ingress.run_ingress(
            stop_event=stop_event,
            cloudflared_path=self.binary,
            gateway_factory=gateway_factory,
            preflight=preflight,
            on_ready=on_ready or default_ready,
            on_stopping=on_stopping or (
                lambda: events.append("stopping")),
            popen_factory=popen,
            opener=opener,
            metrics_opener=metrics_opener,
            runtime_dir=self.tempdir.name,
            metrics_port_factory=overrides.pop(
                "metrics_port_factory", lambda: 49111),
            startup_timeout=overrides.pop("startup_timeout", 0.2),
            readiness_interval=overrides.pop("readiness_interval", 0.001),
            shutdown_timeout=overrides.pop("shutdown_timeout", 0.001),
            install_signal_handlers=False,
            **overrides,
        )
        return {
            "events": events,
            "gateway": gateway,
            "process": process,
            "popen_calls": popen_calls,
            "config_snapshots": config_snapshots,
            "metrics_requests": metrics_requests,
            "ready_values": ready_values,
            "result": result,
        }

    def test_hardened_launch_public_probe_then_publish_and_cleanup(self):
        inherited = {
            "LANG": "C.UTF-8",
            "TMPDIR": "/private/tmp/safe",
            "CREW_APP": "secret-app",
            "MORPHDB_TOKEN": "morph-secret",
            "TUNNEL_TOKEN": "tunnel-secret",
            "CLOUDFLARE_API_TOKEN": "cf-secret",
            "AGENT_MAIL_NAME": "private-agent",
            "AWS_SECRET_ACCESS_KEY": "aws-secret",
            "HTTPS_PROXY": "https://user:password@example.invalid",
        }
        requests = []

        def opener(request, timeout):
            requests.append((request, timeout))
            return _Response(url=request.full_url)

        with mock.patch.dict(os.environ, inherited, clear=True):
            outcome = self._run(opener=opener)

        self.assertEqual(
            "https://quiet-pond.trycloudflare.com", outcome["result"])
        self.assertEqual(
            [("https://quiet-pond.trycloudflare.com", True)],
            outcome["ready_values"],
        )
        events = outcome["events"]
        self.assertLess(events.index("preflight"), events.index("gateway.start"))
        self.assertLess(events.index("gateway.start"), events.index("popen"))
        self.assertLess(events.index("gateway.disable"), events.index("ready"))
        self.assertLess(events.index("ready"), events.index("gateway.close"))

        self.assertEqual(1, len(requests))
        request, timeout = requests[0]
        self.assertEqual(
            "https://quiet-pond.trycloudflare.com"
            "/.well-known/crew-ingress-ready",
            request.full_url,
        )
        self.assertEqual(
            outcome["gateway"].readiness_secret,
            request.get_header("X-crew-ingress-probe"),
        )
        self.assertGreater(timeout, 0)
        self.assertTrue(outcome["metrics_requests"])
        metrics_request, metrics_timeout = outcome["metrics_requests"][0]
        self.assertEqual(
            "http://127.0.0.1:49111/quicktunnel",
            metrics_request.full_url,
        )
        self.assertGreater(metrics_timeout, 0)
        self.assertLessEqual(metrics_timeout, 0.2)

        watchdog_argv, kwargs = outcome["popen_calls"][0]
        separator = watchdog_argv.index("--")
        argv = watchdog_argv[separator + 1:]
        self.assertEqual(os.path.realpath(sys.executable), watchdog_argv[0])
        self.assertTrue(watchdog_argv[1].endswith(
            "crew/ingress_watchdog.py"))
        self.assertEqual("--parent-fd", watchdog_argv[2])
        self.assertGreaterEqual(int(watchdog_argv[3]), 0)
        self.assertIn("--shutdown-timeout", watchdog_argv)
        self.assertEqual(os.path.realpath(self.binary), argv[0])
        self.assertEqual("tunnel", argv[1])
        self.assertEqual(1, argv.count(os.path.realpath(self.binary)))
        self.assertIn("tunnel", argv)
        self.assertIn("--no-autoupdate", argv)
        self.assertNotIn("--no-chunked-encoding", argv)
        self.assertIn("--loglevel", argv)
        self.assertIn("info", argv)
        self.assertIn("--output", argv)
        self.assertIn("json", argv)
        self.assertNotIn("--logformat", argv)
        self.assertIn("--metrics", argv)
        self.assertIn("127.0.0.1:49111", argv)
        self.assertNotIn("--url", argv)
        self.assertNotIn("--unix-socket", argv)
        self.assertIn("--hello-world", argv)
        config_path = argv[argv.index("--config") + 1]
        self.assertEqual(len(outcome["config_snapshots"]), 1)
        snapshot_path, snapshot = outcome["config_snapshots"][0]
        self.assertEqual(snapshot_path, config_path)
        self.assertEqual(snapshot, {
            "ingress": [{
                "service": f"unix:{outcome['gateway'].socket_path}",
                "originRequest": {
                    "disableChunkedEncoding": True,
                },
            }],
        })
        self.assertFalse(os.path.exists(config_path))

        self.assertIs(kwargs["stdin"], subprocess.DEVNULL)
        self.assertIs(kwargs["stdout"], subprocess.PIPE)
        self.assertIs(kwargs["stderr"], subprocess.STDOUT)
        self.assertIs(kwargs["shell"], False)
        self.assertIs(kwargs["close_fds"], True)
        self.assertEqual(
            kwargs["pass_fds"],
            (int(watchdog_argv[3]),),
        )
        self.assertEqual(
            {
                "HOME": os.path.realpath(self.tempdir.name),
                "LANG": "C",
                "LC_ALL": "C",
                "TMPDIR": os.path.realpath(self.tempdir.name),
                "XDG_CONFIG_HOME": os.path.realpath(self.tempdir.name),
            },
            kwargs["env"],
        )
        self.assertEqual(os.path.realpath(self.tempdir.name), kwargs["cwd"])
        self.assertTrue(outcome["process"].terminated)
        self.assertFalse(outcome["process"].killed)
        self.assertTrue(outcome["gateway"].closed)

    def test_config_identity_uses_the_exact_held_lease_scope(self):
        real_writer = ingress.write_cloudflared_config
        calls = []

        def writer(
                runtime_dir, *, morphdb_origin, app, gateway_origin,
                config_path=None):
            calls.append((morphdb_origin, app))
            return real_writer(
                runtime_dir,
                morphdb_origin=morphdb_origin,
                app=app,
                gateway_origin=gateway_origin,
                config_path=config_path,
            )

        with mock.patch(
                "crew.ingress.write_cloudflared_config",
                side_effect=writer):
            self._run(
                morphdb_origin="http://127.0.0.1:19999/",
                app="crew-held-lease",
            )
        self.assertEqual(
            [("http://127.0.0.1:19999", "crew-held-lease")],
            calls,
        )

    def test_default_preflight_accepts_only_the_canonical_configured_origin(self):
        with mock.patch.object(
                ingress.config, "MORPHDB_HOST",
                "HTTP://Example.COM:80/"), \
             mock.patch("crew.schema.ensure_schema") as ensure:
            ingress._default_preflight(
                morphdb_origin="http://example.com",
                app="crew-held-lease",
            )
        ensure.assert_called_once_with(app="crew-held-lease")

        with mock.patch.object(
                ingress.config, "MORPHDB_HOST",
                "https://Example.COM/live/"), \
             mock.patch("crew.schema.ensure_schema") as ensure:
            ingress._default_preflight(
                morphdb_origin="https://example.com/live",
                app="crew-held-lease",
            )
        ensure.assert_called_once_with(app="crew-held-lease")

        with mock.patch.object(
                ingress.config, "MORPHDB_HOST",
                "http://127.0.0.1:8787"):
            with self.assertRaisesRegex(
                    ingress.IngressError, "does not match"):
                ingress._default_preflight(
                    morphdb_origin="http://127.0.0.1:9999",
                    app="crew-held-lease",
                )

    def test_rejects_non_single_label_or_non_https_tunnel_urls(self):
        invalid = _quick_tunnel_metrics(
            "nested.label.trycloudflare.com")
        opened = []
        with self.assertRaisesRegex(ingress.IngressError, "invalid hostname"):
            self._run(
                metrics_body=invalid,
                opener=lambda *_args, **_kwargs: opened.append(True),
                startup_timeout=0.02,
            )
        self.assertEqual([], opened)

    def test_metrics_hostname_must_match_the_exact_child_log(self):
        opened = []
        with self.assertRaisesRegex(
                ingress.IngressError, "does not match"):
            self._run(
                output=(
                    b'{"level":"info","message":"|  '
                    b'https://retained-child.trycloudflare.com  |"}\n'
                ),
                metrics_body=_quick_tunnel_metrics(
                    "local-hijacker.trycloudflare.com"),
                opener=lambda *_args, **_kwargs: opened.append(True),
            )
        self.assertEqual([], opened)

    def test_published_child_url_drift_stops_the_runner(self):
        expected = "https://quiet-pond.trycloudflare.com"
        drifted = "https://different-child.trycloudflare.com"

        class DriftingCollector:
            def __init__(self, _stream):
                self.calls = 0

            def start(self):
                return None

            def snapshot_urls(self):
                self.calls += 1
                return {expected if self.calls <= 2 else drifted}

            def join(self, timeout=None):
                return None

        with mock.patch(
                "crew.ingress.CloudflaredLogCollector",
                DriftingCollector):
            with self.assertRaisesRegex(
                    ingress.IngressError, "drifted"):
                self._run(on_ready=lambda *_args: None)

    def test_log_drain_failure_stops_the_published_runner(self):
        expected = "https://quiet-pond.trycloudflare.com"

        class FailingCollector:
            def __init__(self, _stream):
                self.error = None
                self.calls = 0

            def start(self):
                return None

            def snapshot_urls(self):
                self.calls += 1
                if self.calls >= 3:
                    self.error = OSError("simulated pipe failure")
                return {expected}

            def join(self, timeout=None):
                return None

        with mock.patch(
                "crew.ingress.CloudflaredLogCollector",
                FailingCollector):
            with self.assertRaisesRegex(
                    ingress.IngressError, "log drain failed"):
                self._run(on_ready=lambda *_args: None)

    def test_uses_strict_json_log_url_only_as_metrics_fallback(self):
        metrics_calls = []

        def unavailable(request, timeout):
            metrics_calls.append((request, timeout))
            return _Response(
                status=404,
                body=b"not found",
                url=request.full_url,
            )

        outcome = self._run(
            output=(
                b'{"level":"info","message":"|  '
                b'https://quiet-pond.trycloudflare.com  |"}\n'
            ),
            metrics_opener=unavailable,
        )
        self.assertEqual(
            "https://quiet-pond.trycloudflare.com",
            outcome["result"],
        )
        self.assertTrue(metrics_calls)

    def test_rejects_url_shaped_or_structurally_invalid_hostnames(self):
        invalid_values = (
            "https://quiet-pond.trycloudflare.com",
            "trycloudflare.com",
            "quiet-pond.trycloudflare.com.evil.test",
            "quiet-pond.trycloudflare.com:443",
            "-quiet.trycloudflare.com",
        )
        for hostname in invalid_values:
            with self.subTest(hostname=hostname):
                with self.assertRaisesRegex(
                        ingress.IngressError, "invalid hostname"):
                    self._run(
                        metrics_body=_quick_tunnel_metrics(hostname),
                        startup_timeout=0.01,
                    )

    def test_readiness_retry_stops_if_cloudflared_exits(self):
        process = _FakeProcess(_quick_tunnel_log())
        calls = []

        def opener(*_args, **_kwargs):
            calls.append(True)
            process.returncode = 7
            raise OSError("not ready")

        with self.assertRaisesRegex(ingress.IngressError, "exited.*7"):
            self._run(process=process, opener=opener)
        self.assertTrue(calls)

    def test_readiness_does_not_accept_a_redirected_204(self):
        with self.assertRaisesRegex(ingress.IngressError, "readiness timed out"):
            self._run(
                opener=lambda *_args, **_kwargs: _RedirectedResponse(),
                readiness_timeout=0.01,
            )

    def test_invalid_metrics_port_never_launches_cloudflared(self):
        events = []
        gateway = _FakeGateway(events, "placeholder")
        with self.assertRaisesRegex(ingress.IngressError, "metrics port"):
            self._run(gateway=gateway, metrics_port_factory=lambda: 0)
        self.assertTrue(gateway.closed)

    def test_gateway_death_tears_down_cloudflared(self):
        events = []
        gateway = _FakeGateway(events, "placeholder")

        def opener(*_args, **_kwargs):
            gateway.alive = False
            raise OSError("gateway went away")

        process = _FakeProcess(_quick_tunnel_log())
        with self.assertRaisesRegex(ingress.IngressError, "gateway stopped"):
            self._run(process=process, gateway=gateway, opener=opener)
        self.assertTrue(process.terminated)
        self.assertTrue(gateway.closed)

    def test_child_death_after_publish_tears_down_gateway(self):
        events = []
        gateway = _FakeGateway(events, "placeholder")
        process = _FakeProcess(_quick_tunnel_log())

        def on_ready(_url):
            process.returncode = 11

        with self.assertRaisesRegex(ingress.IngressError, "exited.*11"):
            self._run(
                process=process,
                gateway=gateway,
                on_ready=on_ready,
            )
        self.assertTrue(gateway.closed)

    def test_stop_race_after_health_poll_never_publishes(self):
        stop = threading.Event()

        class StopDuringPoll(_FakeProcess):
            def __init__(self):
                super().__init__(
                    b'{"level":"info","message":"|  '
                    b'https://quiet-pond.trycloudflare.com  |"}\n')
                self.polls = 0

            def poll(self):
                self.polls += 1
                if self.polls == 3:
                    stop.set()
                return self.returncode

        published = []
        with self.assertRaises(ingress.IngressStopped):
            self._run(
                process=StopDuringPoll(),
                stop_event=stop,
                on_ready=lambda *_args: published.append(True),
            )
        self.assertEqual([], published)

    def test_cleanup_escalates_only_the_retained_child(self):
        process = _FakeProcess(
            _quick_tunnel_log(), wait_times_out=True)
        outcome = self._run(process=process)
        self.assertTrue(process.terminated)
        self.assertTrue(process.killed)
        self.assertEqual(3, len(process.wait_timeouts))

    def test_cleanup_failure_cannot_skip_state_gateway_or_stream_cleanup(self):
        class TerminateFails(_FakeProcess):
            def terminate(self):
                self.terminated = True
                raise PermissionError("simulated terminate refusal")

        events = []
        gateway = _FakeGateway(events, "placeholder")
        process = TerminateFails(
            b'{"level":"info","message":"|  '
            b'https://quiet-pond.trycloudflare.com  |"}\n')
        with self.assertRaisesRegex(
                ingress.IngressError, "cleanup failed"):
            self._run(
                process=process,
                gateway=gateway,
                on_stopping=lambda: events.append("state.clear"),
            )
        self.assertTrue(process.stdout.closed)
        self.assertTrue(gateway.closed)
        self.assertLess(
            events.index("state.clear"), events.index("gateway.close"))

    def test_cleanup_failure_never_masks_the_primary_runner_error(self):
        class TerminateFails(_FakeProcess):
            def terminate(self):
                raise PermissionError("simulated terminate refusal")

        events = []
        gateway = _FakeGateway(events, "placeholder")
        process = TerminateFails(_quick_tunnel_log())

        def gateway_dies(*_args, **_kwargs):
            gateway.alive = False
            raise OSError("public probe failed")

        with self.assertRaisesRegex(
                ingress.IngressError, "gateway stopped"):
            self._run(
                process=process,
                gateway=gateway,
                opener=gateway_dies,
            )
        self.assertTrue(process.stdout.closed)
        self.assertTrue(gateway.closed)

    def test_signal_handler_only_sets_the_stop_event(self):
        stop = threading.Event()
        handler = ingress.make_stop_signal_handler(stop)
        handler(signal.SIGTERM, object())
        self.assertTrue(stop.is_set())

    def test_signal_install_rolls_back_and_restore_attempts_every_handler(self):
        old_handlers = {
            signal.SIGINT: object(),
            signal.SIGTERM: object(),
        }
        with mock.patch(
                "crew.ingress.signal.getsignal",
                side_effect=[
                    old_handlers[signal.SIGINT],
                    old_handlers[signal.SIGTERM],
                ]), \
             mock.patch(
                 "crew.ingress.signal.signal",
                 side_effect=[None, PermissionError("install"), None],
             ) as set_signal:
            with self.assertRaisesRegex(PermissionError, "install"):
                ingress._install_signal_handlers(threading.Event())
        self.assertEqual(3, set_signal.call_count)
        self.assertEqual(
            old_handlers[signal.SIGINT],
            set_signal.call_args_list[-1].args[1],
        )

        with mock.patch(
                "crew.ingress.signal.signal",
                side_effect=[PermissionError("restore"), None],
             ) as set_signal:
            with self.assertRaisesRegex(PermissionError, "restore"):
                ingress._restore_signal_handlers(old_handlers)
        self.assertEqual(2, set_signal.call_count)

    def test_all_lifecycle_timeouts_must_be_finite_positive_numbers(self):
        names = (
            "startup_timeout",
            "readiness_timeout",
            "readiness_request_timeout",
            "readiness_interval",
            "shutdown_timeout",
        )
        for name in names:
            for value in (0, -1, True, math.nan, math.inf):
                with self.subTest(name=name, value=value):
                    with mock.patch(
                            "crew.ingress.find_cloudflared",
                            side_effect=AssertionError(
                                "validation reached side effects")):
                        with self.assertRaisesRegex(ValueError, name):
                            ingress.run_ingress(**{name: value})

    def test_log_collector_bounds_retained_lines_and_line_size(self):
        huge = b"x" * (ingress.MAX_LOG_LINE_BYTES * 2) + b"\n"
        stream = io.BytesIO(
            huge + b'{"level":"info","message":"still running"}\n')
        collector = ingress.CloudflaredLogCollector(stream, max_lines=2)
        collector.start()
        collector.join(timeout=1)
        self.assertFalse(collector.is_alive())
        self.assertLessEqual(len(collector.lines), 2)
        self.assertTrue(all(
            len(line.encode("utf-8")) <= ingress.MAX_LOG_LINE_BYTES
            for line in collector.lines
        ))
        self.assertIn("still running", collector.lines[-1])

    def test_alive_child_with_closed_log_pipe_is_unhealthy(self):
        process = _FakeProcess()
        process.stdout.close()
        collector = ingress.CloudflaredLogCollector(
            io.BytesIO(_quick_tunnel_log()))
        collector.start()
        collector.join(timeout=1)
        self.assertFalse(collector.is_alive())
        self.assertIsNone(process.poll())
        with self.assertRaisesRegex(
                ingress.IngressError, "ended unexpectedly"):
            ingress._require_collector_healthy(collector, process)


if __name__ == "__main__":
    unittest.main()
