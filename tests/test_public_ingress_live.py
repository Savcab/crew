"""Opt-in, real-Internet verification for public webhook ingress.

This test owns a private local MorphDB process, a unique project/app, the
foreground ingress runner, and every synthetic row it creates.  It intentionally
prints only redacted pass/fail facts; the temporary public hostname, webhook
capabilities, and full hook URLs never leave process memory.
"""

import http.client
import json
import os
import re
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.parse
import unittest
import uuid
from unittest import mock


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from crew import (  # noqa: E402
    config,
    graphstore as gs,
    ingress_state,
    mail,
    schema,
    webhooks,
)


_LIVE_ENABLED = os.environ.get("CREW_RUN_PUBLIC_INGRESS_LIVE") == "1"
_QUICK_TUNNEL_SUFFIX = ".trycloudflare.com"
_TRANSIENT_HTTP_STATUSES = frozenset((429, 500, 502, 503, 504))
_MAX_PUBLIC_RESPONSE = 256 * 1024


def _official_cloudflared():
    """Return a real executable with cloudflared's official version signature."""
    if not _LIVE_ENABLED:
        return None
    candidate = shutil.which("cloudflared")
    if not candidate:
        return None
    candidate = os.path.realpath(candidate)
    try:
        result = subprocess.run(
            [candidate, "--version"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=5,
            check=False,
            env={"LANG": "C", "LC_ALL": "C"},
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if (
            result.returncode != 0
            or re.search(
                r"(?:^|\s)cloudflared version [^\s]+",
                result.stdout or "",
            ) is None):
        return None
    return candidate


_CLOUDFLARED = _official_cloudflared()


class PublicRequestError(RuntimeError):
    """A public request failed without reflecting its capability-bearing path."""


def _reserve_loopback_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _wait_for_morphdb(process, port, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("private MorphDB exited during startup")
        connection = http.client.HTTPConnection(
            "127.0.0.1", port, timeout=0.25)
        try:
            connection.request("GET", "/health")
            response = connection.getresponse()
            response.read()
            if response.status == 200:
                return
        except (OSError, http.client.HTTPException):
            pass
        finally:
            connection.close()
        time.sleep(0.05)
    raise RuntimeError("private MorphDB did not become healthy")


def _stop_owned_process(process):
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _stop_ingress_cli(process, timeout=30.0):
    """Interrupt and reap the exact process group owned by this test."""
    def signal_group(signum):
        try:
            os.killpg(process.pid, signum)
        except ProcessLookupError:
            pass

    if process.poll() is None:
        signal_group(signal.SIGINT)
    try:
        output, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        signal_group(signal.SIGTERM)
        try:
            output, _ = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            signal_group(signal.SIGKILL)
            output, _ = process.communicate(timeout=5)
    return process.returncode, output or ""


def _run_crew_cli(environment, *arguments, timeout=20.0):
    return subprocess.run(
        [os.path.join(ROOT, "bin", "crew"), *arguments],
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        close_fds=True,
        env=environment,
        timeout=timeout,
        check=False,
    )


def _request_once(
        origin, method, path, *, payload=None, headers=None, timeout=10.0,
        connect_host=None):
    """Make one TLS-verified request without ever including its path in errors."""
    try:
        canonical_origin = ingress_state.validate_public_base_url(origin)
        parsed = urllib.parse.urlsplit(canonical_origin)
    except (TypeError, ValueError) as error:
        raise PublicRequestError(
            "ingress returned an invalid public origin") from error

    context = ssl.create_default_context()
    connection = http.client.HTTPSConnection(
        parsed.hostname,
        443,
        timeout=timeout,
        context=context,
    )
    if connect_host is not None:
        # HTTPSConnection keeps the random Quick Tunnel hostname as Host and
        # TLS SNI.  Only the TCP destination changes, so normal CA/hostname
        # certificate validation remains fully enabled.
        def connect_to_edge(
                address, connect_timeout=socket._GLOBAL_DEFAULT_TIMEOUT,
                source_address=None):
            return socket.create_connection(
                (connect_host, address[1]),
                connect_timeout,
                source_address,
            )

        connection._create_connection = connect_to_edge
    try:
        connection.request(
            method,
            path,
            body=payload,
            headers=dict(headers or {}),
        )
        response = connection.getresponse()
        raw = response.read(_MAX_PUBLIC_RESPONSE + 1)
        if len(raw) > _MAX_PUBLIC_RESPONSE:
            raise PublicRequestError("public response exceeded its limit")
        response_headers = {
            str(name).lower(): str(value)
            for name, value in response.getheaders()
        }
        return response.status, response_headers, raw
    finally:
        connection.close()


def _public_request(origin, method, path, **kwargs):
    """Reach a Quick Tunnel when corporate DNS blocks its random hostname."""
    try:
        return _request_once(origin, method, path, **kwargs)
    except socket.gaierror as direct_error:
        hostname = urllib.parse.urlsplit(origin).hostname or ""
        if not hostname.endswith(_QUICK_TUNNEL_SUFFIX):
            raise PublicRequestError(
                "public ingress hostname did not resolve") from direct_error

    try:
        rows = socket.getaddrinfo(
            "trycloudflare.com", 443, type=socket.SOCK_STREAM)
    except socket.gaierror as error:
        raise PublicRequestError(
            "Cloudflare edge hostname did not resolve") from error
    addresses = list(dict.fromkeys(row[4][0] for row in rows))
    last_error = None
    last_response = None
    for address in addresses:
        try:
            result = _request_once(
                origin, method, path, connect_host=address, **kwargs)
            last_response = result
            if result[0] not in _TRANSIENT_HTTP_STATUSES:
                return result
        except (
                OSError,
                TimeoutError,
                http.client.HTTPException,
                ssl.SSLError,
                PublicRequestError,
        ) as error:
            last_error = error
    if last_response is not None:
        return last_response
    raise PublicRequestError(
        "all Cloudflare edge connections failed") from last_error


def _json_document(raw):
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PublicRequestError(
            "public response was not valid JSON") from error
    if not isinstance(value, dict):
        raise PublicRequestError("public response was not a JSON object")
    return value


def _eventually_json(
        origin, method, path, expected_status, *, timeout=30.0, **kwargs):
    """Retry propagation and ambiguous transports without exposing the path."""
    deadline = time.monotonic() + timeout
    last_error = None
    last_response = None
    while True:
        try:
            status, headers, raw = _public_request(
                origin, method, path, **kwargs)
            last_response = (status, headers, raw)
            if (
                    status == expected_status
                    or status not in _TRANSIENT_HTTP_STATUSES):
                return status, headers, _json_document(raw)
        except (
                OSError,
                TimeoutError,
                http.client.HTTPException,
                ssl.SSLError,
                PublicRequestError,
        ) as error:
            last_error = error
        if time.monotonic() >= deadline:
            if last_response is not None:
                raise PublicRequestError(
                    "public response did not reach the expected status")
            raise PublicRequestError(
                "public request did not complete before timeout") from last_error
        time.sleep(0.5)


def _public_post(origin, path, payload, idempotency_key):
    return _eventually_json(
        origin,
        "POST",
        path,
        202,
        payload=payload,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Idempotency-Key": idempotency_key,
            "Connection": "close",
        },
    )


@unittest.skipUnless(
    _LIVE_ENABLED and _CLOUDFLARED,
    "requires CREW_RUN_PUBLIC_INGRESS_LIVE=1 and official cloudflared on PATH",
)
class PublicIngressLiveTests(unittest.TestCase):
    maxDiff = 4096

    def assert_generic_404(self, result):
        status, headers, document = result
        self.assertEqual(status, 404, document)
        self.assertEqual(
            document, {"ok": False, "error": "not found"})
        self.assertEqual(headers.get("cache-control"), "no-store")
        self.assertTrue(
            headers.get("content-type", "").startswith("application/json"))

    def _messages_for(self, hook, target):
        return [
            row for row in gs.list_messages(
                target=target["name"], limit=20)
            if row.get("sender_guid") == hook["_guid"]
        ]

    def _assert_delivery_rows(
            self, hook, targets, edges, expected_bodies):
        total = 0
        for target, edge in zip(targets, edges):
            rows = self._messages_for(hook, target)
            total += len(rows)
            self.assertEqual(
                [row.get("body") for row in rows],
                list(expected_bodies),
            )
            for row in rows:
                self.assertEqual(row.get("sender"), hook["name"])
                self.assertEqual(row.get("target"), target["name"])
                self.assertEqual(
                    row.get("sender_guid"), hook["_guid"])
                self.assertEqual(
                    row.get("target_guid"), target["_guid"])
                self.assertEqual(row.get("edge_guid"), edge["_guid"])
        self.assertEqual(total, len(targets) * len(expected_bodies))
        return total

    def test_real_public_ingress_lifecycle(self):
        morphdb_binary = shutil.which("morphdb")
        self.assertTrue(
            morphdb_binary,
            "the live ingress test requires the MorphDB executable")

        nonce = uuid.uuid4().hex[:12]
        project = f"ingresslive_{nonce}"
        app = config.project_app(project)
        hook_name = f"live_hook_{nonce}"
        target_names = (
            f"live_alpha_{nonce}",
            f"live_beta_{nonce}",
        )
        first_body = "Live initial: synthetic public ingress event"
        rotated_body = "Live rotated: synthetic public ingress event"
        first_payload = json.dumps({
            "stage": "initial",
            "message": "synthetic public ingress event",
        }, separators=(",", ":")).encode("utf-8")
        rotated_payload = json.dumps({
            "stage": "rotated",
            "message": "synthetic public ingress event",
        }, separators=(",", ":")).encode("utf-8")

        with tempfile.TemporaryDirectory(
                prefix="crew-public-ingress-live-") as tempdir:
            morphdb_port = _reserve_loopback_port()
            morphdb_host = f"127.0.0.1:{morphdb_port}"
            morphdb_db = os.path.join(tempdir, "morphdb.sqlite3")
            morphdb_environment = {
                "HOME": tempdir,
                "LANG": "C",
                "LC_ALL": "C",
                "MORPHDB_QUIET": "1",
                "TMPDIR": tempdir,
            }
            morphdb_process = subprocess.Popen(
                [
                    os.path.realpath(morphdb_binary),
                    "run",
                    "--host", "127.0.0.1",
                    "--port", str(morphdb_port),
                    "--db", morphdb_db,
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                env=morphdb_environment,
            )
            ingress_process = None
            ingress_output = ""
            app_initialized = False
            morphdb_origin = f"http://{morphdb_host}"
            scope_paths = ingress_state._scope_paths(morphdb_origin, app)

            environment = {
                "CREW_APP": app,
                "CREW_PROJECT": project,
                "NO_PROXY": "127.0.0.1,localhost",
                "no_proxy": "127.0.0.1,localhost",
            }
            with mock.patch.dict(os.environ, environment), \
                    mock.patch.object(
                        config, "MORPHDB_HOST", morphdb_host), \
                    mock.patch.object(
                        config, "WEBHOOK_PUBLIC_BASE_URL", ""), \
                    mock.patch.object(
                        gs, "_INVARIANT_LOCK_DIR",
                        os.path.join(tempdir, "graph-locks")), \
                    mock.patch.object(
                        mail, "_VAR",
                        os.path.join(tempdir, "mail-locks")):
                try:
                    _wait_for_morphdb(morphdb_process, morphdb_port)
                    schema.ensure_schema(app)
                    app_initialized = True

                    hook = gs.create_webhook(
                        hook_name,
                        description="Synthetic public ingress live check",
                        template=(
                            "Live {{ payload.stage }}: "
                            "{{ payload.message }}"),
                    )
                    targets = [
                        gs.create_agent(
                            name,
                            home=os.path.join(tempdir, "agents", name),
                            session=name,
                            status="stopped",
                        )
                        for name in target_names
                    ]
                    edges = [
                        gs.create_edge(
                            hook["_guid"],
                            target["_guid"],
                            label=f"synthetic live route {index}",
                            directed=True,
                        )
                        for index, target in enumerate(targets, start=1)
                    ]
                    self.assertTrue(
                        all(
                            target.get("status") == "stopped"
                            for target in targets),
                        "live recipients must remain stopped")

                    cli_environment = {
                        "CREW_APP": app,
                        "CREW_PROJECT": project,
                        "HOME": tempdir,
                        "LANG": "C",
                        "LC_ALL": "C",
                        "MORPHDB_HOST": morphdb_origin,
                        "NO_PROXY": "127.0.0.1,localhost",
                        "PATH": (
                            os.path.dirname(_CLOUDFLARED)
                            + os.pathsep
                            + os.environ.get("PATH", os.defpath)
                        ),
                        "PYTHONUNBUFFERED": "1",
                        "TMPDIR": tempdir,
                    }
                    ingress_process = subprocess.Popen(
                        [
                            os.path.join(ROOT, "bin", "crew"),
                            "ingress",
                            "run",
                        ],
                        cwd=ROOT,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        close_fds=True,
                        env=cli_environment,
                        start_new_session=True,
                    )

                    ready_deadline = time.monotonic() + 125
                    active_state = None
                    while active_state is None:
                        if ingress_process.poll() is not None:
                            _, ingress_output = _stop_ingress_cli(
                                ingress_process)
                            self.fail(
                                "ingress CLI exited before readiness: "
                                f"{ingress_output.strip()}")
                        if time.monotonic() >= ready_deadline:
                            self.fail(
                                "ingress did not become ready before timeout")
                        active_state = ingress_state.read_active_state(
                            origin=morphdb_origin,
                            app=app,
                        )
                        if active_state is None:
                            time.sleep(0.1)

                    origin = active_state["public_base_url"]
                    hostname = (
                        urllib.parse.urlsplit(origin).hostname or "")
                    self.assertTrue(
                        hostname.endswith(_QUICK_TUNNEL_SUFFIX),
                        "public origin was not a Quick Tunnel hostname",
                    )
                    print(
                        "public_origin_valid_trycloudflare=true",
                        flush=True,
                    )
                    self.assertEqual(
                        active_state.get("public_base_url"), origin)

                    old_path = webhooks.public_path(hook)
                    status_cli = _run_crew_cli(
                        cli_environment, "ingress", "status")
                    self.assertEqual(
                        status_cli.returncode, 0,
                        "crew ingress status failed")
                    self.assertIn(
                        f"online \u2192 {origin}", status_cli.stdout)
                    self.assertNotIn("/hooks/", status_cli.stdout)

                    show_cli = _run_crew_cli(
                        cli_environment, "webhook", "show", hook_name)
                    self.assertEqual(
                        show_cli.returncode, 0,
                        "crew webhook show failed")
                    self.assertTrue(
                        f"url: {origin}{old_path}" in show_cli.stdout,
                        "crew webhook show did not use the active ingress")
                    print(
                        "cli_surface=status_online "
                        "show_public_url_true capability_redacted",
                        flush=True,
                    )

                    first_key = "crew-live-initial-" + uuid.uuid4().hex
                    first_status, _, first_response = _public_post(
                        origin, old_path, first_payload, first_key)
                    self.assertEqual(first_status, 202, first_response)
                    self.assertTrue(first_response.get("ok"), first_response)
                    self.assertFalse(
                        first_response.get("duplicate"), first_response)
                    self.assertEqual(
                        first_response.get("accepted"), 2, first_response)
                    self.assertEqual(
                        first_response.get("rejected"), 0, first_response)
                    self.assertEqual(
                        len(first_response.get("deliveries") or []), 2)
                    self.assertEqual(
                        self._assert_delivery_rows(
                            hook, targets, edges, (first_body,)),
                        2,
                    )
                    print(
                        "initial_delivery=status_202 accepted_2 "
                        "rejected_0 durable_messages_2",
                        flush=True,
                    )

                    replay_status, _, replay_response = _public_post(
                        origin, old_path, first_payload, first_key)
                    self.assertEqual(
                        replay_status, 202, replay_response)
                    self.assertTrue(
                        replay_response.get("duplicate"), replay_response)
                    self.assertEqual(
                        replay_response.get("accepted"), 2, replay_response)
                    self.assertEqual(
                        replay_response.get("rejected"), 0, replay_response)
                    self.assertEqual(
                        replay_response.get("delivery_id"),
                        first_response.get("delivery_id"),
                    )
                    self.assertEqual(
                        replay_response.get("deliveries"),
                        first_response.get("deliveries"),
                    )
                    self.assertEqual(
                        self._assert_delivery_rows(
                            hook, targets, edges, (first_body,)),
                        2,
                    )
                    print(
                        "idempotent_replay=duplicate_true "
                        "same_receipt_true durable_messages_2",
                        flush=True,
                    )

                    rotated_hook = gs.update_webhook(
                        hook["_guid"], rotate=True)
                    new_path = webhooks.public_path(rotated_hook)
                    self.assertTrue(
                        old_path != new_path,
                        "rotation did not replace the capability")
                    old_result = _eventually_json(
                        origin,
                        "POST",
                        old_path,
                        404,
                        payload=rotated_payload,
                        headers={
                            "Accept": "application/json",
                            "Content-Type": "application/json",
                            "Idempotency-Key": (
                                "crew-live-old-" + uuid.uuid4().hex),
                            "Connection": "close",
                        },
                    )
                    self.assert_generic_404(old_result)
                    self.assertEqual(
                        self._assert_delivery_rows(
                            hook, targets, edges, (first_body,)),
                        2,
                    )

                    rotated_status, _, rotated_response = _public_post(
                        origin,
                        new_path,
                        rotated_payload,
                        "crew-live-rotated-" + uuid.uuid4().hex,
                    )
                    self.assertEqual(
                        rotated_status, 202, rotated_response)
                    self.assertTrue(
                        rotated_response.get("ok"), rotated_response)
                    self.assertFalse(
                        rotated_response.get("duplicate"),
                        rotated_response,
                    )
                    self.assertEqual(
                        rotated_response.get("accepted"),
                        2,
                        rotated_response,
                    )
                    self.assertEqual(
                        rotated_response.get("rejected"),
                        0,
                        rotated_response,
                    )
                    self.assertEqual(
                        self._assert_delivery_rows(
                            hook,
                            targets,
                            edges,
                            (first_body, rotated_body),
                        ),
                        4,
                    )
                    print(
                        "rotation=old_404 new_202 durable_messages_4",
                        flush=True,
                    )

                    for control_path in (
                            "/",
                            "/api/graph/snapshot",
                            "/static/index.html",
                            "/terminal"):
                        self.assert_generic_404(_eventually_json(
                            origin,
                            "GET",
                            control_path,
                            404,
                            headers={
                                "Accept": "application/json",
                                "Connection": "close",
                            },
                        ))
                    print(
                        "control_surface=four_generic_404s",
                        flush=True,
                    )

                    returncode, ingress_output = _stop_ingress_cli(
                        ingress_process)
                    self.assertEqual(
                        returncode, 0,
                        f"foreground ingress CLI failed: {ingress_output}")
                    self.assertIn(
                        "public webhook ingress online", ingress_output)
                    self.assertIn(
                        "public webhook ingress stopped", ingress_output)
                    self.assertNotIn("/hooks/", ingress_output)
                    self.assertNotIn(old_path, ingress_output)
                    self.assertNotIn(new_path, ingress_output)
                    self.assertFalse(os.path.exists(scope_paths.state_path))
                    self.assertIsNone(ingress_state.read_active_state(
                        origin=morphdb_origin,
                        app=app,
                    ))
                    offline_cli = _run_crew_cli(
                        cli_environment, "ingress", "status")
                    self.assertEqual(
                        offline_cli.returncode, 0,
                        "offline crew ingress status failed")
                    self.assertEqual(
                        offline_cli.stdout,
                        "public webhook ingress offline\n",
                    )

                    try:
                        offline_status, _, _ = _public_request(
                            origin,
                            "POST",
                            new_path,
                            payload=rotated_payload,
                            headers={
                                "Content-Type": "application/json",
                                "Idempotency-Key": (
                                    "crew-live-offline-"
                                    + uuid.uuid4().hex),
                                "Connection": "close",
                            },
                            timeout=5,
                        )
                    except (
                            OSError,
                            TimeoutError,
                            http.client.HTTPException,
                            ssl.SSLError,
                            PublicRequestError,
                    ):
                        offline_status = None
                    self.assertNotEqual(
                        offline_status,
                        202,
                        "stopped ingress still accepted a public hook")
                    self.assertEqual(
                        self._assert_delivery_rows(
                            hook,
                            targets,
                            edges,
                            (first_body, rotated_body),
                        ),
                        4,
                    )
                    print(
                        "shutdown=state_removed_true "
                        "endpoint_accepts_false",
                        flush=True,
                    )
                finally:
                    cleanup_errors = []
                    if (
                            ingress_process is not None
                            and ingress_process.poll() is None):
                        try:
                            _, ingress_output = _stop_ingress_cli(
                                ingress_process)
                        except Exception as error:
                            cleanup_errors.append(
                                "ingress CLI cleanup failed: "
                                f"{type(error).__name__}")
                    if (
                            ingress_process is None
                            or ingress_process.poll() is not None):
                        try:
                            ingress_state.read_active_state(
                                origin=morphdb_origin,
                                app=app,
                            )
                            for path in (
                                    scope_paths.state_path,
                                    scope_paths.config_path,
                                    scope_paths.lock_path):
                                try:
                                    os.unlink(path)
                                except FileNotFoundError:
                                    pass
                        except Exception as error:
                            cleanup_errors.append(
                                "ingress state cleanup failed: "
                                f"{type(error).__name__}")
                    if app_initialized:
                        try:
                            gs._req(
                                "DELETE", f"/app/{app}", app=None)
                        except Exception as error:
                            cleanup_errors.append(
                                "throwaway app cleanup failed: "
                                f"{type(error).__name__}")
                    try:
                        _stop_owned_process(morphdb_process)
                    except Exception as error:
                        cleanup_errors.append(
                            "private MorphDB cleanup failed: "
                            f"{type(error).__name__}")
                    if cleanup_errors and sys.exc_info()[0] is None:
                        self.fail("; ".join(cleanup_errors))


if __name__ == "__main__":
    unittest.main(verbosity=2)
