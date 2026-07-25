"""Real-socket behavior tests for the public hook-only local gateway."""

import http.client
import json
import os
import socket
import stat
import tempfile
import threading
import time
import unittest
from unittest import mock

from crew import webhooks
from crew.server import hook_gateway


TOKEN = "A" * 43
READY_SECRET = "readiness-secret-kept-out-of-responses"


def _read_raw_response(sock, timeout=2.0):
    sock.settimeout(timeout)
    chunks = []
    while True:
        try:
            chunk = sock.recv(65536)
        except (ConnectionResetError, socket.timeout):
            break
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks)


def _raw_request(port, request, timeout=2.0):
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
        sock.sendall(request)
        return _read_raw_response(sock, timeout=timeout)


def _raw_unix_request(path, request, timeout=2.0):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect(path)
        sock.sendall(request)
        return _read_raw_response(sock, timeout=timeout)


def _status_from_raw(response):
    return int(response.split(b"\r\n", 1)[0].split()[1])


class HookGatewayTest(unittest.TestCase):
    def setUp(self):
        self.capability = mock.patch.object(
            hook_gateway.webhooks, "capability_exists", return_value=True)
        self.receive = mock.patch.object(
            hook_gateway.webhooks, "receive",
            return_value={"ok": True, "request_id": "receipt-1"})
        self.capability_mock = self.capability.start()
        self.receive_mock = self.receive.start()
        self.addCleanup(self.capability.stop)
        self.addCleanup(self.receive.stop)
        self.gateway = hook_gateway.start_hook_gateway(
            readiness_secret=READY_SECRET,
            body_timeout=0.25,
        )
        self.addCleanup(self.gateway.close)

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.gateway.port, timeout=2)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            raw = response.read()
            return response.status, dict(response.getheaders()), raw
        finally:
            connection.close()

    def test_starts_on_loopback_and_closes_idempotently(self):
        self.assertEqual(self.gateway.address[0], "127.0.0.1")
        self.assertEqual(
            self.gateway.base_url,
            f"http://127.0.0.1:{self.gateway.port}")
        self.assertTrue(self.gateway.is_alive())
        self.gateway.close()
        self.gateway.close()
        self.assertFalse(self.gateway.is_alive())

    def test_private_unix_origin_is_unique_owner_only_and_removed(self):
        with tempfile.TemporaryDirectory() as td:
            os.chmod(td, 0o700)
            path = os.path.join(td, "gateway.sock")
            gateway = hook_gateway.start_hook_gateway(
                readiness_secret=READY_SECRET,
                unix_socket_path=path,
            )
            try:
                self.assertEqual(gateway.address, path)
                self.assertEqual(gateway.socket_path, path)
                self.assertEqual(gateway.origin_url, f"unix:{path}")
                self.assertEqual(
                    stat.S_IMODE(os.lstat(path).st_mode), 0o600)
                name, value = gateway.readiness_header
                response = _raw_unix_request(
                    path,
                    (
                        f"GET {gateway.readiness_path} HTTP/1.1\r\n"
                        "Host: localhost\r\n"
                        f"{name}: {value}\r\n"
                        "Connection: close\r\n\r\n"
                    ).encode("ascii"),
                )
                self.assertEqual(_status_from_raw(response), 204)
            finally:
                gateway.close()
            gateway.close()
            self.assertFalse(os.path.lexists(path))

    def test_unix_origin_never_replaces_an_existing_path(self):
        with tempfile.TemporaryDirectory() as td:
            os.chmod(td, 0o700)
            path = os.path.join(td, "gateway.sock")
            with open(path, "wb") as existing:
                existing.write(b"keep")
            gateway = hook_gateway.HookGateway(
                readiness_secret=READY_SECRET,
                unix_socket_path=path,
            )
            with self.assertRaises(FileExistsError):
                gateway.start()
            with open(path, "rb") as existing:
                self.assertEqual(existing.read(), b"keep")

    def test_exact_hook_post_prechecks_then_receives_sanitized_request(self):
        status, headers, raw = self.request(
            "POST", f"/hooks/{TOKEN}", b'{"message":"hello"}',
            {
                "Content-Type": "application/json",
                "Idempotency-Key": "delivery-1",
            })

        self.assertEqual(status, 202)
        self.assertEqual(json.loads(raw), {
            "ok": True, "request_id": "receipt-1"})
        self.assertEqual(headers.get("Connection"), "close")
        self.assertNotIn("Server", headers)
        self.capability_mock.assert_called_once_with(TOKEN)
        self.receive_mock.assert_called_once()
        args, kwargs = self.receive_mock.call_args
        self.assertEqual(args[:2], (TOKEN, b'{"message":"hello"}'))
        self.assertEqual(kwargs["content_type"], "application/json")
        self.assertEqual(
            kwargs["headers"]["Idempotency-Key"], "delivery-1")

    def test_capability_is_checked_before_any_body_read(self):
        checked = threading.Event()

        def capability_exists(token):
            self.assertEqual(token, TOKEN)
            checked.set()
            return True

        self.capability_mock.side_effect = capability_exists
        self.capability_mock.return_value = mock.DEFAULT
        with socket.create_connection(
                ("127.0.0.1", self.gateway.port), timeout=2) as sock:
            sock.sendall(
                f"POST /hooks/{TOKEN} HTTP/1.1\r\n"
                "Host: localhost\r\n"
                "Content-Length: 4\r\n"
                "Connection: close\r\n\r\n".encode("ascii"))
            self.assertTrue(checked.wait(1))
            self.assertFalse(self.receive_mock.called)
            sock.sendall(b"test")
            response = _read_raw_response(sock)
        self.assertEqual(_status_from_raw(response), 202)
        self.receive_mock.assert_called_once()

    def test_unknown_capability_is_rejected_without_waiting_for_body(self):
        self.capability_mock.return_value = False
        request = (
            f"POST /hooks/{TOKEN} HTTP/1.1\r\n"
            "Host: localhost\r\n"
            "Content-Length: 999999\r\n"
            "Connection: close\r\n\r\n"
        ).encode("ascii")
        started = time.monotonic()
        response = _raw_request(self.gateway.port, request)
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(_status_from_raw(response), 404)
        self.assertEqual(json.loads(response.split(b"\r\n\r\n", 1)[1]), {
            "ok": False, "error": "not found"})
        self.assertNotIn(TOKEN.encode(), response)
        self.receive_mock.assert_not_called()

    def test_exact_raw_path_and_post_method_are_required(self):
        paths = [
            "/", "/api/graph/snapshot", "/hooks", "/hooks/",
            f"/hooks/{TOKEN}/", f"/hooks/{TOKEN}?x=1",
            f"/hooks/%41{'A' * 41}", f"/hooks/{'A' * 42}",
            f"/hooks/{'A' * 44}", f"//hooks/{TOKEN}",
        ]
        for path in paths:
            with self.subTest(path=path):
                response = _raw_request(
                    self.gateway.port,
                    (
                        f"POST {path} HTTP/1.1\r\n"
                        "Host: localhost\r\n"
                        "Content-Length: 0\r\n"
                        "Connection: close\r\n\r\n"
                    ).encode("ascii"))
                self.assertEqual(_status_from_raw(response), 404)
                self.assertEqual(
                    json.loads(response.split(b"\r\n\r\n", 1)[1]),
                    {"ok": False, "error": "not found"})

        absolute = _raw_request(
            self.gateway.port,
            (
                f"POST http://127.0.0.1:{self.gateway.port}/hooks/{TOKEN} "
                "HTTP/1.1\r\nHost: localhost\r\nContent-Length: 0\r\n"
                "Connection: close\r\n\r\n"
            ).encode("ascii"))
        self.assertEqual(_status_from_raw(absolute), 404)

        for method in ("GET", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE"):
            with self.subTest(method=method):
                status, _, raw = self.request(method, f"/hooks/{TOKEN}")
                self.assertEqual(status, 404)
                self.assertEqual(
                    json.loads(raw), {"ok": False, "error": "not found"})
        self.receive_mock.assert_not_called()

    def test_readiness_requires_secret_and_disappears_when_disabled(self):
        name, value = self.gateway.readiness_header
        self.assertNotEqual(name.lower(), "authorization")
        self.assertEqual(value, READY_SECRET)

        status, headers, raw = self.request(
            "GET", self.gateway.readiness_path, headers={name: value})
        self.assertEqual((status, raw), (204, b""))
        self.assertEqual(headers.get("Connection"), "close")
        self.assertNotIn(READY_SECRET.encode(), raw)

        for supplied in (None, "wrong"):
            with self.subTest(supplied=supplied):
                request_headers = {} if supplied is None else {name: supplied}
                status, _, raw = self.request(
                    "GET", self.gateway.readiness_path,
                    headers=request_headers)
                self.assertEqual(status, 404)
                self.assertEqual(
                    json.loads(raw), {"ok": False, "error": "not found"})

        self.gateway.disable_readiness()
        self.gateway.disable_readiness()
        status, _, raw = self.request(
            "GET", self.gateway.readiness_path, headers={name: value})
        self.assertEqual(status, 404)
        self.assertEqual(
            json.loads(raw), {"ok": False, "error": "not found"})

    def test_rejects_ambiguous_or_unsupported_body_framing(self):
        cases = {
            "missing": (
                f"POST /hooks/{TOKEN} HTTP/1.1\r\nHost: localhost\r\n"
                "Connection: close\r\n\r\n"),
            "multiple": (
                f"POST /hooks/{TOKEN} HTTP/1.1\r\nHost: localhost\r\n"
                "Content-Length: 1\r\nContent-Length: 1\r\n"
                "Connection: close\r\n\r\nx"),
            "comma": (
                f"POST /hooks/{TOKEN} HTTP/1.1\r\nHost: localhost\r\n"
                "Content-Length: 1, 1\r\nConnection: close\r\n\r\nx"),
            "signed": (
                f"POST /hooks/{TOKEN} HTTP/1.1\r\nHost: localhost\r\n"
                "Content-Length: +1\r\nConnection: close\r\n\r\nx"),
            "transfer-encoding": (
                f"POST /hooks/{TOKEN} HTTP/1.1\r\nHost: localhost\r\n"
                "Transfer-Encoding: chunked\r\nContent-Length: 1\r\n"
                "Connection: close\r\n\r\n0\r\n\r\n"),
        }
        for name, request in cases.items():
            with self.subTest(name=name):
                response = _raw_request(
                    self.gateway.port, request.encode("ascii"))
                self.assertEqual(_status_from_raw(response), 400)
                self.assertEqual(
                    json.loads(response.split(b"\r\n\r\n", 1)[1]),
                    {"ok": False, "error": "bad request"})

        oversize = _raw_request(
            self.gateway.port,
            (
                f"POST /hooks/{TOKEN} HTTP/1.1\r\nHost: localhost\r\n"
                f"Content-Length: {hook_gateway.DEFAULT_MAX_BODY + 1}\r\n"
                "Connection: close\r\n\r\n"
            ).encode("ascii"))
        self.assertEqual(_status_from_raw(oversize), 413)
        self.assertEqual(
            json.loads(oversize.split(b"\r\n\r\n", 1)[1]),
            {"ok": False, "error": "request too large"})
        self.receive_mock.assert_not_called()

    def test_expect_is_rejected_without_sending_100_continue(self):
        response = _raw_request(
            self.gateway.port,
            (
                f"POST /hooks/{TOKEN} HTTP/1.1\r\n"
                "Host: localhost\r\n"
                "Content-Length: 4\r\n"
                "Expect: 100-continue\r\n"
                "Connection: close\r\n\r\n"
            ).encode("ascii"))
        self.assertEqual(_status_from_raw(response), 417)
        self.assertNotIn(b"100 Continue", response)
        self.assertEqual(
            json.loads(response.split(b"\r\n\r\n", 1)[1]),
            {"ok": False, "error": "expectation failed"})
        self.receive_mock.assert_not_called()

        hidden = _raw_request(
            self.gateway.port,
            (
                "POST /api/graph/snapshot HTTP/1.1\r\n"
                "Host: localhost\r\n"
                "Content-Length: 4\r\n"
                "Expect: 100-continue\r\n"
                "Connection: close\r\n\r\n"
            ).encode("ascii"))
        self.assertEqual(_status_from_raw(hidden), 404)
        self.assertNotIn(b"100 Continue", hidden)
        self.assertEqual(
            json.loads(hidden.split(b"\r\n\r\n", 1)[1]),
            {"ok": False, "error": "not found"})

    def test_body_deadline_is_absolute_even_when_bytes_keep_arriving(self):
        self.gateway.close()
        self.gateway = hook_gateway.start_hook_gateway(
            readiness_secret=READY_SECRET,
            body_timeout=0.18,
        )
        self.addCleanup(self.gateway.close)
        with socket.create_connection(
                ("127.0.0.1", self.gateway.port), timeout=2) as sock:
            sock.sendall(
                (
                    f"POST /hooks/{TOKEN} HTTP/1.1\r\nHost: localhost\r\n"
                    "Content-Length: 10\r\nConnection: close\r\n\r\na"
                ).encode("ascii"))
            started = time.monotonic()
            time.sleep(0.08)
            sock.sendall(b"b")
            time.sleep(0.08)
            sock.sendall(b"c")
            response = _read_raw_response(sock, timeout=1)
        self.assertLess(time.monotonic() - started, 0.6)
        self.assertEqual(_status_from_raw(response), 408)
        self.assertEqual(
            json.loads(response.split(b"\r\n\r\n", 1)[1]),
            {"ok": False, "error": "request timeout"})
        self.receive_mock.assert_not_called()

    def test_header_deadline_is_absolute_even_when_bytes_keep_arriving(self):
        self.gateway.close()
        self.gateway = hook_gateway.start_hook_gateway(
            readiness_secret=READY_SECRET,
            header_timeout=0.24,
            max_connections=1,
        )
        self.addCleanup(self.gateway.close)
        with socket.create_connection(
                ("127.0.0.1", self.gateway.port), timeout=2) as sock:
            sock.sendall(
                (
                    f"POST /hooks/{TOKEN} HTTP/1.1\r\n"
                    "Host: localhost\r\n"
                    "X-Slow: a"
                ).encode("ascii"))
            started = time.monotonic()
            for byte in (b"b", b"c", b"d"):
                time.sleep(0.07)
                sock.sendall(byte)
            response = _read_raw_response(sock, timeout=1)
        self.assertLess(time.monotonic() - started, 0.7)
        self.assertEqual(response, b"")
        self.receive_mock.assert_not_called()

        # The timed-out handler must release its sole admission slot.
        recovered = _raw_request(
            self.gateway.port,
            b"GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n",
        )
        self.assertEqual(_status_from_raw(recovered), 404)

    def test_strips_proxy_and_hop_by_hop_headers_but_keeps_signatures(self):
        status, _, _ = self.request(
            "POST", f"/hooks/{TOKEN}", b"x",
            {
                "Connection": "X-Remove, keep-alive",
                "X-Remove": "secret intermediary value",
                "Keep-Alive": "timeout=50",
                "Forwarded": "for=203.0.113.1",
                "X-Forwarded-For": "203.0.113.2",
                "X-Forwarded-Proto": "https",
                "X-Real-IP": "203.0.113.3",
                "CF-Connecting-IP": "203.0.113.4",
                "Proxy-Foo": "bar",
                hook_gateway.READINESS_HEADER_NAME: READY_SECRET,
                "X-Hub-Signature-256": "sha256=abc",
                "Idempotency-Key": "delivery-2",
                "X-Custom": "kept",
            })
        self.assertEqual(status, 202)
        forwarded = {
            key.lower(): value
            for key, value in self.receive_mock.call_args.kwargs[
                "headers"].items()
        }
        for removed in (
                "connection", "x-remove", "keep-alive", "forwarded",
                "x-forwarded-for", "x-forwarded-proto", "x-real-ip",
                "cf-connecting-ip", "proxy-foo"):
            self.assertNotIn(removed, forwarded)
        self.assertNotIn(
            hook_gateway.READINESS_HEADER_NAME.lower(), forwarded)
        self.assertEqual(
            forwarded["x-hub-signature-256"], "sha256=abc")
        self.assertEqual(forwarded["idempotency-key"], "delivery-2")
        self.assertEqual(forwarded["x-custom"], "kept")

    def test_receive_revalidates_and_public_errors_hide_internal_details(self):
        self.receive_mock.side_effect = webhooks.WebhookError(
            "private parser detail", status=422)
        status, headers, raw = self.request(
            "POST", f"/hooks/{TOKEN}", b"not-json",
            {"Content-Type": "application/json"})
        self.assertEqual(status, 422)
        self.assertEqual(
            json.loads(raw), {"ok": False, "error": "invalid webhook payload"})
        self.assertNotIn(b"private parser detail", raw)
        self.assertNotIn("Server", headers)

        self.receive_mock.side_effect = RuntimeError(
            "morphdb host and private capability")
        status, _, raw = self.request(
            "POST", f"/hooks/{TOKEN}", b"x")
        self.assertEqual(status, 503)
        self.assertEqual(
            json.loads(raw),
            {"ok": False, "error": "temporarily unavailable"})
        self.assertNotIn(b"morphdb", raw.lower())

    def test_parser_failures_are_json_and_do_not_leak_server_versions(self):
        response = _raw_request(
            self.gateway.port,
            b"GET /" + b"x" * 70000 + b" HTTP/1.1\r\n\r\n")
        self.assertEqual(_status_from_raw(response), 414)
        headers, raw = response.split(b"\r\n\r\n", 1)
        self.assertNotIn(b"Server:", headers)
        self.assertNotIn(b"Python", response)
        self.assertNotIn(b"BaseHTTP", response)
        self.assertNotIn(b"Traceback", response)
        self.assertEqual(
            json.loads(raw), {"ok": False, "error": "bad request"})

    def test_thirty_third_connection_is_rejected_before_thread_creation(self):
        self.gateway.close()
        entered = 0
        condition = threading.Condition()
        release = threading.Event()

        def blocked_receive(*_args, **_kwargs):
            nonlocal entered
            with condition:
                entered += 1
                condition.notify_all()
            release.wait(3)
            return {"ok": True}

        self.receive_mock.side_effect = blocked_receive
        self.gateway = hook_gateway.start_hook_gateway(
            readiness_secret=READY_SECRET,
            max_connections=32,
            body_timeout=1,
        )
        self.addCleanup(self.gateway.close)
        clients = []
        try:
            request = (
                f"POST /hooks/{TOKEN} HTTP/1.1\r\nHost: localhost\r\n"
                "Content-Length: 0\r\nConnection: close\r\n\r\n"
            ).encode("ascii")
            for _ in range(32):
                client = socket.create_connection(
                    ("127.0.0.1", self.gateway.port), timeout=2)
                clients.append(client)
                client.sendall(request)
            with condition:
                reached = condition.wait_for(lambda: entered == 32, timeout=2)
            self.assertTrue(reached, f"only {entered} handlers entered")

            response = _raw_request(self.gateway.port, request)
            self.assertEqual(_status_from_raw(response), 503)
            self.assertEqual(
                json.loads(response.split(b"\r\n\r\n", 1)[1]),
                {"ok": False, "error": "temporarily unavailable"})
            self.assertEqual(entered, 32)
            self.assertEqual(self.receive_mock.call_count, 32)
        finally:
            release.set()
            for client in clients:
                client.close()


if __name__ == "__main__":
    unittest.main()
