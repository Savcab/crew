"""Operator-capability boundary for the local dashboard control plane."""
import base64
import http.client
import http.cookiejar
import json
import os
import socket
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from crew.server import app  # noqa: E402


class DashboardCapabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.old_cap = getattr(app, "OPERATOR_CAPABILITY", None)
        app.OPERATOR_CAPABILITY = "test-operator-capability"
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        app.OPERATOR_CAPABILITY = cls.old_cap

    def setUp(self):
        # This class is a pure HTTP/control-boundary unit suite. A missing mock
        # must fail the test instead of PATCH-upserting a fixture GUID into the
        # developer's current MorphDB tenant.
        no_morphdb = mock.patch.object(
            app.gs, "_req",
            side_effect=AssertionError(
                "dashboard capability unit test attempted MorphDB I/O"))
        no_morphdb.start()
        self.addCleanup(no_morphdb.stop)

    def _post(self, path, body=None, opener=None, *, origin=None,
              content_type="application/json", csrf=True):
        headers = {"Content-Type": content_type}
        if csrf:
            headers["X-Crew-CSRF"] = "1"
        if origin is not None:
            headers["Origin"] = origin
        req = urllib.request.Request(
            self.base + path,
            data=json.dumps(body or {}).encode(), method="POST",
            headers=headers)
        client = opener or urllib.request.build_opener()
        try:
            with client.open(req, timeout=5) as response:
                return response.status, dict(response.headers), json.load(response)
        except urllib.error.HTTPError as error:
            try:
                return error.code, dict(error.headers), json.load(error)
            finally:
                error.close()

    def _post_raw(self, path, raw, opener=None):
        request = urllib.request.Request(
            self.base + path,
            data=raw,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Crew-CSRF": "1",
            },
        )
        client = opener or urllib.request.build_opener()
        try:
            with client.open(request, timeout=5) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            try:
                return error.code, json.load(error)
            finally:
                error.close()

    def _get(self, path, opener=None, *, origin=None):
        client = opener or urllib.request.build_opener()
        request = urllib.request.Request(self.base + path)
        if origin is not None:
            request.add_header("Origin", origin)
        try:
            with client.open(request, timeout=5) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            try:
                return error.code, json.load(error)
            finally:
                error.close()

    def _get_headers(self, path, opener=None):
        client = opener or urllib.request.build_opener()
        try:
            with client.open(self.base + path, timeout=5) as response:
                response.read()
                return response.status, dict(response.headers)
        except urllib.error.HTTPError as error:
            try:
                error.read()
                return error.code, dict(error.headers)
            finally:
                error.close()

    def _raw_post(self, path, raw=b"{}", *, framing_headers=()):
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_port, timeout=5)
        connection.putrequest("POST", path)
        connection.putheader("Content-Type", "application/json")
        connection.putheader("X-Crew-CSRF", "1")
        connection.putheader(
            "Cookie",
            f"{app.OPERATOR_COOKIE}=test-operator-capability")
        connection.putheader("Connection", "close")
        for name, value in framing_headers:
            connection.putheader(name, value)
        connection.endheaders()
        if raw:
            connection.send(raw)
        try:
            response = connection.getresponse()
            body = json.loads(response.read() or b"null")
            return response.status, body
        finally:
            connection.close()

    def _raw_method(self, method, path, raw=None):
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_port, timeout=5)
        body = raw
        if body is None and method not in ("GET", "HEAD"):
            body = b"{}"
        headers = {
            "Content-Type": "application/json",
            "X-Crew-CSRF": "1",
            "Cookie": f"{app.OPERATOR_COOKIE}=test-operator-capability",
            "Connection": "close",
        }
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            raw_body = response.read()
            try:
                parsed = json.loads(raw_body) if raw_body else None
            except (TypeError, ValueError):
                parsed = raw_body.decode(errors="replace")
            return response.status, dict(response.headers), parsed
        finally:
            connection.close()

    def _short_body_post(self, path, raw, declared_length):
        request = (
            f"POST {path} HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{self.server.server_port}\r\n"
            "Content-Type: application/json\r\n"
            "X-Crew-CSRF: 1\r\n"
            f"Cookie: {app.OPERATOR_COOKIE}=test-operator-capability\r\n"
            f"Content-Length: {declared_length}\r\n"
            "Connection: close\r\n\r\n"
        ).encode() + raw
        with socket.create_connection(
                ("127.0.0.1", self.server.server_port), timeout=5) as client:
            client.sendall(request)
            client.shutdown(socket.SHUT_WR)
            chunks = []
            while True:
                chunk = client.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
        response = b"".join(chunks)
        head, _, body = response.partition(b"\r\n\r\n")
        status = int(head.split(b"\r\n", 1)[0].split()[1])
        return status, json.loads(body)

    def _operator(self):
        jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        status, _, body = self._post(
            "/api/auth/bootstrap",
            {"capability": "test-operator-capability"}, opener=opener)
        self.assertEqual(status, 200)
        self.assertTrue(body.get("ok"), body)
        return opener

    def test_every_control_post_is_forbidden_without_operator_cookie(self):
        for path in (
            "/api/pty/input", "/api/pty/resize", "/api/agent/create",
            "/api/agent/start", "/api/agent/remove", "/api/edge/create",
            "/api/edge/update", "/api/edge/delete", "/api/agent/bless",
            "/api/edge/bless", "/api/agent/foreman",
            "/api/pending/approve", "/api/pending/reject", "/api/expand",
        ):
            with self.subTest(path=path):
                status, _, body = self._post(path)
                self.assertEqual(status, 403)
                self.assertFalse(body.get("ok"))
                self.assertIn("operator", body.get("error", "").lower())

    def test_bootstrap_rejects_a_wrong_capability(self):
        status, headers, _ = self._post("/api/auth/bootstrap", {"capability": "wrong"})
        self.assertEqual(status, 403)
        self.assertNotIn("Set-Cookie", headers)

    def test_bootstrap_rejects_non_string_capabilities_at_the_json_boundary(self):
        for invalid in (123, False, None, {}, []):
            with self.subTest(value=invalid):
                status, headers, body = self._post(
                    "/api/auth/bootstrap", {"capability": invalid})
                self.assertEqual(status, 400, body)
                self.assertIn("capability", body.get("error", ""))
                self.assertNotIn("Set-Cookie", headers)

    def test_bootstrap_sets_hardened_cookie_and_allows_control_post(self):
        jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        status, headers, body = self._post(
            "/api/auth/bootstrap",
            {"capability": "test-operator-capability"}, opener=opener)
        self.assertEqual(status, 200)
        self.assertTrue(body.get("ok"))
        cookie = headers.get("Set-Cookie", "")
        self.assertTrue(
            cookie.startswith(f"crew_operator_{app.PORT}="), cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        with mock.patch.object(app.ptyio, "write_input", return_value=True) as write:
            status, _, body = self._post(
                "/api/pty/input", {"id": "pty-1", "b64": "aGk="}, opener=opener)
        self.assertEqual(status, 200)
        self.assertTrue(body.get("ok"))
        write.assert_called_once_with("pty-1", b"hi")

    def test_operator_cookie_names_are_scoped_per_dashboard_port(self):
        """Cookies ignore ports; distinct dashboards must not overwrite auth."""
        first = app._operator_cookie_name(8788)
        second = app._operator_cookie_name(18788)
        self.assertNotEqual(first, second)
        self.assertEqual(first, "crew_operator_8788")
        self.assertEqual(second, "crew_operator_18788")

    def test_pty_input_rejects_missing_non_string_empty_and_malformed_base64(self):
        opener = self._operator()
        cases = (
            ({"id": "pty-1"}, "b64"),
            ({"id": "pty-1", "b64": None}, "string"),
            ({"id": "pty-1", "b64": 123}, "string"),
            ({"id": "pty-1", "b64": False}, "string"),
            ({"id": "pty-1", "b64": []}, "string"),
            ({"id": "pty-1", "b64": ""}, "non-empty"),
            ({"id": "pty-1", "b64": "!!!!"}, "base64"),
        )
        for body_in, message in cases:
            with self.subTest(body=body_in), \
                 mock.patch.object(app.ptyio, "write_input",
                                   return_value=True) as write:
                status, _, body = self._post(
                    "/api/pty/input", body_in, opener=opener)
                self.assertEqual(status, 400, body)
                self.assertIn(message, body.get("error", "").lower())
                write.assert_not_called()

    def test_pty_input_rejects_missing_non_string_and_blank_id_before_write(self):
        opener = self._operator()
        cases = (
            ({"b64": "aGk="}, "id"),
            ({"id": None, "b64": "aGk="}, "string"),
            ({"id": False, "b64": "aGk="}, "string"),
            ({"id": 17, "b64": "aGk="}, "string"),
            ({"id": 1.5, "b64": "aGk="}, "string"),
            ({"id": [], "b64": "aGk="}, "string"),
            ({"id": {}, "b64": "aGk="}, "string"),
            ({"id": "", "b64": "aGk="}, "non-empty"),
            ({"id": "   ", "b64": "aGk="}, "non-empty"),
        )
        for body_in, message in cases:
            with self.subTest(body=body_in), \
                 mock.patch.object(app.ptyio, "write_input",
                                   return_value=True) as write:
                status, _, body = self._post(
                    "/api/pty/input", body_in, opener=opener)
                self.assertEqual(status, 400, body)
                self.assertIn(message, body.get("error", "").lower())
                write.assert_not_called()

    def test_pty_input_accepts_exact_256_kib_decoded_boundary(self):
        opener = self._operator()
        decoded = b"x" * (256 * 1024)
        encoded = base64.b64encode(decoded).decode()
        with mock.patch.object(app.ptyio, "write_input", return_value=True) as write:
            status, _, body = self._post(
                "/api/pty/input", {"id": "pty-1", "b64": encoded},
                opener=opener)
        self.assertEqual(status, 200)
        self.assertTrue(body.get("ok"), body)
        write.assert_called_once_with("pty-1", decoded)

    def test_pty_input_rejects_one_byte_over_decoded_limit(self):
        opener = self._operator()
        decoded = b"x" * (256 * 1024 + 1)
        encoded = base64.b64encode(decoded).decode()
        with mock.patch.object(app.ptyio, "write_input", return_value=True) as write:
            status, _, body = self._post(
                "/api/pty/input", {"id": "pty-1", "b64": encoded},
                opener=opener)
        self.assertEqual(status, 400, body)
        self.assertIn("256 kib", body.get("error", "").lower())
        write.assert_not_called()

    def test_pty_resize_requires_string_id_and_bounded_integer_dimensions(self):
        opener = self._operator()
        cases = (
            ({"cols": 80, "rows": 24}, "id"),
            ({"id": 7, "cols": 80, "rows": 24}, "id"),
            ({"id": "   ", "cols": 80, "rows": 24}, "id"),
            ({"id": "pty-1", "rows": 24}, "cols"),
            ({"id": "pty-1", "cols": 80}, "rows"),
            ({"id": "pty-1", "cols": True, "rows": 24}, "cols"),
            ({"id": "pty-1", "cols": 80, "rows": False}, "rows"),
            ({"id": "pty-1", "cols": "80", "rows": 24}, "cols"),
            ({"id": "pty-1", "cols": 80, "rows": "24"}, "rows"),
            ({"id": "pty-1", "cols": 80.5, "rows": 24}, "cols"),
            ({"id": "pty-1", "cols": 80, "rows": 24.5}, "rows"),
            ({"id": "pty-1", "cols": None, "rows": 24}, "cols"),
            ({"id": "pty-1", "cols": 80, "rows": []}, "rows"),
            ({"id": "pty-1", "cols": 1, "rows": 24}, "cols"),
            ({"id": "pty-1", "cols": 501, "rows": 24}, "cols"),
            ({"id": "pty-1", "cols": 80, "rows": 1}, "rows"),
            ({"id": "pty-1", "cols": 80, "rows": 301}, "rows"),
        )
        for body_in, field in cases:
            with self.subTest(body=body_in), \
                 mock.patch.object(app.ptyio, "set_size",
                                   return_value=True) as resize:
                status, _, body = self._post(
                    "/api/pty/resize", body_in, opener=opener)
                self.assertEqual(status, 400, body)
                self.assertIn(field, body.get("error", "").lower())
                resize.assert_not_called()

    def test_pty_resize_accepts_exact_dimension_boundaries(self):
        opener = self._operator()
        for cols, rows in ((2, 2), (500, 300)):
            with self.subTest(cols=cols, rows=rows), \
                 mock.patch.object(app.ptyio, "set_size",
                                   return_value=True) as resize:
                status, _, body = self._post(
                    "/api/pty/resize",
                    {"id": "pty-1", "cols": cols, "rows": rows},
                    opener=opener)
                self.assertEqual(status, 200, body)
                self.assertTrue(body.get("ok"), body)
                resize.assert_called_once_with("pty-1", cols, rows)

    def test_pty_stream_is_forbidden_without_operator_cookie(self):
        try:
            urllib.request.urlopen(self.base + "/api/pty/stream?t=anything", timeout=5)
        except urllib.error.HTTPError as error:
            try:
                self.assertEqual(error.code, 403)
                self.assertIn("operator", json.load(error).get("error", "").lower())
            finally:
                error.close()
        else:
            self.fail("PTY stream was reachable without an operator capability")

    def test_snapshot_and_pending_require_operator_cookie_but_health_is_public(self):
        with mock.patch.object(app, "_graph_snapshot",
                               return_value={"ok": True}) as snapshot, \
             mock.patch.object(app, "_pending_snapshot",
                               return_value={"ok": True}) as pending:
            for path in ("/api/graph/snapshot", "/api/pending"):
                with self.subTest(path=path):
                    status, body = self._get(path)
                    self.assertEqual(status, 403)
                    self.assertIn("operator", body.get("error", "").lower())
            snapshot.assert_not_called()
            pending.assert_not_called()

        status, body = self._get("/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(body.get("ok"), body)
        self.assertEqual(body.get("service"), "crew-dashboard")

    def test_health_handler_exception_returns_json_500_and_recovers(self):
        with mock.patch.object(
                app, "_dashboard_health",
                side_effect=RuntimeError("workspace identity unavailable")):
            status, body = self._get("/api/health")
        self.assertEqual(status, 500, body)
        self.assertFalse(body.get("ok"), body)
        self.assertIn("workspace identity unavailable", body.get("error", ""))

        status, body = self._get("/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(body.get("ok"), body)

    def test_authenticated_snapshot_and_pending_remain_available(self):
        opener = self._operator()
        with mock.patch.object(app, "_graph_snapshot",
                               return_value={"ok": True, "agents": [], "edges": []}), \
             mock.patch.object(app, "_pending_snapshot",
                               return_value={"ok": True, "pending": []}):
            for path in ("/api/graph/snapshot", "/api/pending"):
                with self.subTest(path=path):
                    status, body = self._get(path, opener=opener)
                    self.assertEqual(status, 200)
                    self.assertTrue(body.get("ok"), body)

    def test_snapshot_backend_exception_returns_json_500_and_server_survives(self):
        opener = self._operator()
        with mock.patch.object(
                app, "_graph_snapshot",
                side_effect=RuntimeError("backend unavailable")):
            status, body = self._get("/api/graph/snapshot", opener=opener)
        self.assertEqual(status, 500, body)
        self.assertFalse(body.get("ok"), body)
        self.assertIn("backend unavailable", body.get("error", ""))

        status, body = self._get("/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(body.get("ok"), body)

    def test_nonserializable_snapshot_result_returns_json_500(self):
        opener = self._operator()
        with mock.patch.object(
                app, "_graph_snapshot",
                return_value={"ok": True, "bad": object()}):
            status, body = self._get("/api/graph/snapshot", opener=opener)
        self.assertEqual(status, 500, body)
        self.assertFalse(body.get("ok"), body)
        self.assertIn("serializable", body.get("error", "").lower())

        status, body = self._get("/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(body.get("ok"), body)

    def test_nonfinite_snapshot_number_returns_standards_compliant_json_500(self):
        opener = self._operator()
        with mock.patch.object(
                app, "_graph_snapshot",
                return_value={"ok": True, "bad": float("nan")}):
            status, body = self._get("/api/graph/snapshot", opener=opener)
        self.assertEqual(status, 500, body)
        self.assertFalse(body.get("ok"), body)
        self.assertIn("json", body.get("error", "").lower())

    def test_pending_backend_exception_returns_json_500_and_server_survives(self):
        opener = self._operator()
        with mock.patch.object(
                app, "_pending_snapshot",
                side_effect=RuntimeError("pending backend unavailable")):
            status, body = self._get("/api/pending", opener=opener)
        self.assertEqual(status, 500, body)
        self.assertFalse(body.get("ok"), body)
        self.assertIn("pending backend unavailable", body.get("error", ""))

        status, body = self._get("/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(body.get("ok"), body)

    def test_foreign_origin_cannot_open_a_stateful_pty_stream(self):
        """SameSite cookies still cross localhost ports; bind SSE to its origin."""
        opener = self._operator()
        with mock.patch.object(app.Handler, "_pty_stream") as stream:
            status, body = self._get(
                "/api/pty/stream?t=builder",
                opener=opener,
                origin="http://127.0.0.1:65530",
            )
        self.assertEqual(status, 403)
        self.assertIn("origin", body.get("error", "").lower())
        stream.assert_not_called()

    def test_pty_stream_setup_exception_returns_json_500(self):
        opener = self._operator()
        with mock.patch.object(
                app.Handler, "_pty_stream",
                side_effect=RuntimeError("PTY attach unavailable")):
            status, body = self._get(
                "/api/pty/stream?t=builder", opener=opener)
        self.assertEqual(status, 500, body)
        self.assertFalse(body.get("ok"), body)
        self.assertIn("PTY attach unavailable", body.get("error", ""))

        status, body = self._get("/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(body.get("ok"), body)

    def test_pty_stream_closes_allocated_attach_when_setup_fails(self):
        opener = self._operator()
        agents = [{"_guid": "agent-builder", "name": "builder",
                   "session": "builder",
                   "runtime": "claude"}]
        with mock.patch.object(app.gs, "list_agents", return_value=agents), \
             mock.patch.object(
                 app.tmuxio, "owned_agent_session", return_value="builder"), \
             mock.patch.object(app.ptyio, "open_attach",
                               return_value=("pty-allocated", 123)), \
             mock.patch.object(app.ptyio, "set_size",
                               side_effect=RuntimeError("resize unavailable")), \
             mock.patch.object(app.ptyio, "close") as close:
            status, body = self._get(
                "/api/pty/stream?t=builder:claude", opener=opener)
        self.assertEqual(status, 500, body)
        self.assertFalse(body.get("ok"), body)
        self.assertIn("resize unavailable", body.get("error", ""))
        close.assert_called_once_with("pty-allocated")

    def test_pty_stream_refuses_a_reused_unowned_same_named_session(self):
        opener = self._operator()
        agents = [{"_guid": "agent-builder", "name": "builder",
                   "session": "builder",
                   "runtime": "claude"}]
        with mock.patch.object(app.gs, "list_agents", return_value=agents), \
             mock.patch.object(
                 app.tmuxio, "owned_agent_session", return_value=None,
                 create=True), \
             mock.patch.object(app.ptyio, "open_attach") as attach:
            status, body = self._get(
                "/api/pty/stream?t=builder:claude", opener=opener)
        self.assertEqual(status, 403, body)
        self.assertIn("not a crew agent session", body.get("error", ""))
        attach.assert_not_called()

    def test_snapshot_marks_a_reused_unowned_same_named_session_down(self):
        agents = [{"name": "builder", "session": "builder",
                   "runtime": "claude", "status": "idle"}]
        with mock.patch.object(
                 app.tmuxio, "session_names", return_value={"builder"}), \
             mock.patch.object(
                 app.tmuxio, "_session_pane_map",
                 return_value={"builder": "%foreign"}), \
             mock.patch.object(
                 app.tmuxio, "owned_agent_session", return_value=None,
                 create=True):
            app._enrich_live_status(agents)

        self.assertFalse(agents[0]["session_alive"])
        self.assertFalse(agents[0]["runtime_alive"])
        self.assertEqual(agents[0]["live_status"], "down")

    def test_pty_stream_resize_false_closes_before_sse_headers(self):
        handler = app.Handler.__new__(app.Handler)
        handler.connection = mock.Mock()
        handler.wfile = mock.Mock()
        handler.send_response = mock.Mock()
        handler.send_header = mock.Mock()
        handler.end_headers = mock.Mock()
        handler._json = mock.Mock()
        agents = [{"_guid": "agent-builder", "name": "builder",
                   "session": "builder",
                   "runtime": "claude"}]
        with mock.patch.object(app.gs, "list_agents", return_value=agents), \
             mock.patch.object(
                 app.tmuxio, "owned_agent_session", return_value="builder",
                 create=True), \
             mock.patch.object(app.ptyio, "open_attach",
                               return_value=("pty-allocated", 123)), \
             mock.patch.object(app.ptyio, "set_size", return_value=False), \
             mock.patch.object(app.ptyio, "read_loop"), \
             mock.patch.object(app.ptyio, "close") as close:
            handler._pty_stream("builder:claude", "80", "24")

        handler._json.assert_called_once_with({
            "ok": False, "error": "could not size PTY attach"}, 500)
        handler.send_response.assert_not_called()
        close.assert_called_once_with("pty-allocated")

    def test_pty_stream_preserves_an_owned_legacy_sessions_endpoint(self):
        handler = app.Handler.__new__(app.Handler)
        handler.connection = mock.Mock()
        handler.wfile = mock.Mock()
        handler.send_response = mock.Mock()
        handler.send_header = mock.Mock()
        handler.end_headers = mock.Mock()
        handler._json = mock.Mock()
        agents = [{"_guid": "agent-builder", "name": "builder",
                   "session": "builder",
                   "runtime": "claude"}]
        legacy = app.config.tmux_target(
            "builder", app.config.TMUX_ENDPOINT_LEGACY)
        with mock.patch.object(app.gs, "list_agents", return_value=agents), \
             mock.patch.object(
                 app.tmuxio, "owned_agent_session", return_value=legacy), \
             mock.patch.object(app.ptyio, "open_attach",
                               return_value=("pty-allocated", 123)) as attach, \
             mock.patch.object(app.ptyio, "set_size", return_value=False), \
             mock.patch.object(app.ptyio, "close"):
            handler._pty_stream("builder:claude", "80", "24")

        attach.assert_called_once_with(legacy, "claude")
        attached_session = attach.call_args.args[0]
        self.assertIsInstance(attached_session, app.config.TmuxTarget)
        self.assertEqual(
            attached_session.endpoint, app.config.TMUX_ENDPOINT_LEGACY)

    def test_corrupt_stored_session_cannot_authorize_an_unrelated_tmux_session(self):
        corrupt = {"_guid": "agent-builder", "name": "builder",
                   "session": "manager", "runtime": "claude"}
        canonical = {"_guid": "agent-worker", "name": "worker",
                     "session": "worker", "runtime": "claude"}
        with mock.patch.object(app.config, "current_project", return_value="default"), \
             mock.patch.object(app.gs, "list_agents",
                               return_value=[corrupt, canonical]), \
             mock.patch.object(app.tmuxio, "session_names",
                               return_value={"worker"}), \
             mock.patch.object(
                 app.tmuxio, "owned_agent_session",
                 side_effect=lambda agent, **_kwargs: (
                     "worker" if agent.get("name") == "worker" else None)):
            self.assertEqual(app._agent_session(corrupt), "")
            self.assertEqual(app._crew_sessions(), {"worker"})

    def test_pty_stream_closes_allocated_attach_when_headers_fail(self):
        handler = app.Handler.__new__(app.Handler)
        handler.connection = mock.Mock()
        handler.send_response = mock.Mock()
        handler.send_header = mock.Mock()
        handler.end_headers = mock.Mock(
            side_effect=RuntimeError("client disconnected during headers"))
        agents = [{"_guid": "agent-builder", "name": "builder",
                   "session": "builder",
                   "runtime": "claude"}]
        with mock.patch.object(app.gs, "list_agents", return_value=agents), \
             mock.patch.object(
                 app.tmuxio, "owned_agent_session", return_value="builder"), \
             mock.patch.object(app.ptyio, "open_attach",
                               return_value=("pty-allocated", 123)), \
             mock.patch.object(app.ptyio, "set_size", return_value=True), \
             mock.patch.object(app.ptyio, "close") as close:
            with self.assertRaisesRegex(RuntimeError, "headers"):
                handler._pty_stream("builder:claude", "80", "24")
        close.assert_called_once_with("pty-allocated")

    def test_graph_snapshot_names_the_current_workspace_tenant(self):
        with mock.patch.object(app.config, "current_app",
                               return_value="crew-demo"), \
             mock.patch.object(app.gs, "list_agents", return_value=[]), \
             mock.patch.object(app.gs, "list_edges", return_value=[]), \
             mock.patch.object(app.tmuxio, "session_names", return_value=set()), \
             mock.patch.object(app.tmuxio, "_session_pane_map", return_value={}), \
             mock.patch.object(app, "_pending_rows", return_value=[]), \
             mock.patch.object(app, "_status_transitions"):
            snapshot = app._graph_snapshot()
        self.assertTrue(snapshot.get("ok"), snapshot)
        self.assertEqual(snapshot.get("workspace_key"), "crew-demo")

    def test_graph_snapshot_quarantines_malformed_agent_identities(self):
        sparse_valid = {"_guid": "agent-builder", "name": "builder"}
        persisted = [
            {"_guid": "agent-null-name", "name": None},
            {"_guid": "agent-invalid-name", "name": "not a valid name"},
            {"name": "missing_guid"},
            sparse_valid,
        ]

        def enrich(agents):
            self.assertEqual(agents, [sparse_valid])
            agents[0]["live_status"] = "idle"
            return agents

        with mock.patch.object(app.gs, "list_agents", return_value=persisted), \
             mock.patch.object(app.gs, "list_edges", return_value=[]), \
             mock.patch.object(app, "_enrich_live_status", side_effect=enrich), \
             mock.patch.object(app, "_pending_rows", return_value=[]), \
             mock.patch.object(app, "_status_transitions") as transitions:
            snapshot = app._graph_snapshot()

        self.assertTrue(snapshot.get("ok"), snapshot)
        self.assertEqual(snapshot["agents"], [{
            "_guid": "agent-builder",
            "name": "builder",
            "live_status": "idle",
        }])
        transitions.assert_called_once_with(snapshot["agents"])

    def test_pending_grant_summary_exposes_target_path_and_mode(self):
        row = {
            "op": "grant",
            "args": {
                "agent": "builder",
                "agent_guid": "agent-builder-guid",
                "path": "/srv/customer-data",
                "mode": "rw",
            },
        }
        summary = app._pending_summary(
            row,
            {"agent-builder-guid": {
                "_guid": "agent-builder-guid", "name": "builder"}},
        )
        self.assertIn("builder", summary)
        self.assertIn("/srv/customer-data", summary)
        self.assertIn("rw", summary)

    def test_pending_rows_include_unresolved_claim_and_failure_states(self):
        rows = {
            "pending": [{"_guid": "p", "result": "pending", "created_at": 1}],
            "applying": [{"_guid": "a", "result": "applying", "created_at": 3}],
            "approval_failed": [{
                "_guid": "f", "result": "approval_failed", "created_at": 2,
                "reason": "manual review needed",
            }],
        }

        def listed(_otype, **filters):
            return {"objects": rows.get(filters.get("result"), [])}

        with mock.patch.object(app.gs, "list_objects", side_effect=listed):
            result = app._pending_rows()

        self.assertEqual([row["_guid"] for row in result], ["a", "f", "p"])

    def test_every_response_has_hardened_server_security_and_cache_headers(self):
        for path, client, cache in (
            ("/", None, "no-store"),
            ("/api/health", None, "no-store"),
            ("/static/index.html", None, "no-cache"),
        ):
            with self.subTest(path=path):
                status, headers = self._get_headers(path, opener=client)
                self.assertEqual(status, 200)
                server = headers.get("Server", "")
                self.assertNotIn("BaseHTTP", server)
                self.assertNotIn("Python", server)
                self.assertEqual(server, "Crew")
                self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")
                self.assertEqual(headers.get("Referrer-Policy"), "no-referrer")
                csp = headers.get("Content-Security-Policy", "")
                for directive in (
                    "default-src 'self'", "connect-src 'self'",
                    "style-src 'self' 'unsafe-inline'", "object-src 'none'",
                    "base-uri 'none'", "frame-ancestors 'none'",
                ):
                    self.assertIn(directive, csp)
                self.assertEqual(headers.get("Cache-Control"), cache)

    def test_project_agent_plain_name_cannot_attach_to_another_tmux_session(self):
        opener = self._operator()
        agents = [{"name": "foo", "session": "demo__foo", "runtime": "claude"}]
        with mock.patch.object(app.gs, "list_agents", return_value=agents), \
             mock.patch.object(app.ptyio, "open_attach",
                               return_value=(None, None)) as attach:
            status, body = self._get("/api/pty/stream?t=foo", opener=opener)
        self.assertEqual(status, 403)
        self.assertIn("not a crew agent session", body.get("error", ""))
        attach.assert_not_called()

    def test_legacy_empty_session_resolves_only_current_project_session(self):
        opener = self._operator()
        agents = [{"name": "foo", "session": "", "runtime": "claude"}]
        with mock.patch.object(app.config, "current_project", return_value="demo"), \
             mock.patch.object(app.gs, "list_agents", return_value=agents), \
             mock.patch.object(app.ptyio, "open_attach",
                               return_value=(None, None)) as attach:
            status, body = self._get("/api/pty/stream?t=foo", opener=opener)
        self.assertEqual(status, 403)
        self.assertIn("not a crew agent session", body.get("error", ""))
        attach.assert_not_called()

    def test_foreign_origin_is_rejected_even_with_valid_operator_cookie(self):
        opener = self._operator()
        with mock.patch.object(app.ptyio, "write_input", return_value=True) as write:
            status, _, body = self._post(
                "/api/pty/input", {"id": "pty-1", "b64": "aGk="},
                opener=opener, origin="http://127.0.0.1:65500")
        self.assertEqual(status, 403)
        self.assertIn("origin", body.get("error", "").lower())
        write.assert_not_called()

    def test_authenticated_post_requires_csrf_header(self):
        opener = self._operator()
        with mock.patch.object(app.ptyio, "write_input", return_value=True) as write:
            status, _, body = self._post(
                "/api/pty/input", {"id": "pty-1", "b64": "aGk="},
                opener=opener, origin=self.base, csrf=False)
        self.assertEqual(status, 403)
        self.assertIn("csrf", body.get("error", "").lower())
        write.assert_not_called()

    def test_authenticated_post_requires_json_content_type(self):
        opener = self._operator()
        with mock.patch.object(app.ptyio, "write_input", return_value=True) as write:
            status, _, body = self._post(
                "/api/pty/input", {"id": "pty-1", "b64": "aGk="},
                opener=opener, origin=self.base, content_type="text/plain")
        self.assertEqual(status, 415)
        self.assertIn("json", body.get("error", "").lower())
        write.assert_not_called()

    def test_authenticated_posts_reject_malformed_nonobject_and_nonstandard_json(self):
        opener = self._operator()
        cases = (
            (b'{"name":', "valid JSON"),
            (b'[]', "JSON object"),
            (b'"builder"', "JSON object"),
            (b'{"cost_cap": NaN}', "valid JSON"),
            (b'{"cost_cap": Infinity}', "valid JSON"),
        )
        for raw, message in cases:
            with self.subTest(raw=raw):
                status, body = self._post_raw(
                    "/api/agent/create", raw, opener=opener)
                self.assertEqual(status, 400, body)
                self.assertFalse(body.get("ok"))
                self.assertIn(message.lower(), body.get("error", "").lower())

    def test_malformed_content_length_is_rejected_before_body_processing(self):
        cases = ("bogus", "-1", "+2", "2.5")
        for value in cases:
            with self.subTest(content_length=value), \
                 mock.patch.object(app.ptyio, "write_input") as write:
                status, body = self._raw_post(
                    "/api/pty/input", b'{"id":"pty-1","b64":"aGk="}',
                    framing_headers=(("Content-Length", value),))
                self.assertEqual(status, 400, body)
                self.assertFalse(body.get("ok"), body)
                self.assertIn("content-length", body.get("error", "").lower())
                write.assert_not_called()

    def test_extremely_large_content_length_returns_json_413(self):
        with mock.patch.object(app.ptyio, "write_input") as write:
            status, body = self._raw_post(
                "/api/pty/input", b"",
                framing_headers=(("Content-Length", "9" * 5000),))
        self.assertEqual(status, 413, body)
        self.assertFalse(body.get("ok"), body)
        self.assertIn("large", body.get("error", "").lower())
        write.assert_not_called()

    def test_unsupported_transfer_encoding_is_rejected_before_body_processing(self):
        with mock.patch.object(app.ptyio, "write_input") as write:
            status, body = self._raw_post(
                "/api/pty/input", b"2\r\n{}\r\n0\r\n\r\n",
                framing_headers=(("Transfer-Encoding", "chunked"),))
        self.assertEqual(status, 400, body)
        self.assertFalse(body.get("ok"), body)
        self.assertIn("transfer-encoding", body.get("error", "").lower())
        write.assert_not_called()

    def test_duplicate_content_length_is_rejected_before_body_processing(self):
        raw = b'{"id":"pty-1","b64":"aGk="}'
        with mock.patch.object(app.ptyio, "write_input",
                               return_value=True) as write:
            status, body = self._raw_post(
                "/api/pty/input", raw,
                framing_headers=(
                    ("Content-Length", str(len(raw))),
                    ("Content-Length", str(len(raw))),
                ))
        self.assertEqual(status, 400, body)
        self.assertFalse(body.get("ok"), body)
        self.assertIn("content-length", body.get("error", "").lower())
        write.assert_not_called()

    def test_truncated_body_is_rejected_before_body_processing(self):
        raw = b'{"id":"pty-1","b64":"aGk="}'
        with mock.patch.object(app.ptyio, "write_input",
                               return_value=True) as write:
            status, body = self._short_body_post(
                "/api/pty/input", raw, len(raw) + 20)
        self.assertEqual(status, 400, body)
        self.assertFalse(body.get("ok"), body)
        self.assertIn("content-length", body.get("error", "").lower())
        write.assert_not_called()

    def test_known_api_paths_reject_wrong_methods_with_json_405_and_allow(self):
        cases = (
            ("GET", "/api/agent/remove", "POST"),
            ("POST", "/api/health", "GET"),
            ("PUT", "/api/graph/snapshot", "GET"),
            ("DELETE", "/api/pty/input", "POST"),
        )
        for method, path, allowed in cases:
            with self.subTest(method=method, path=path):
                status, headers, body = self._raw_method(method, path)
                self.assertEqual(status, 405, body)
                self.assertEqual(headers.get("Allow"), allowed)
                self.assertIsInstance(body, dict)
                self.assertFalse(body.get("ok"), body)
                self.assertIn("method", body.get("error", "").lower())

    def test_same_origin_json_post_with_csrf_header_remains_available(self):
        opener = self._operator()
        with mock.patch.object(app.ptyio, "write_input", return_value=True) as write:
            status, _, body = self._post(
                "/api/pty/input", {"id": "pty-1", "b64": "aGk="},
                opener=opener, origin=self.base)
        self.assertEqual(status, 200)
        self.assertTrue(body.get("ok"), body)
        write.assert_called_once_with("pty-1", b"hi")

    def test_unexpected_control_handler_exceptions_return_json_500(self):
        opener = self._operator()
        cases = (
            ("/api/pty/input", {"id": "pty-1", "b64": "aGk="},
             app.ptyio, "write_input"),
            ("/api/pty/resize", {"id": "pty-1", "cols": 80, "rows": 24},
             app.ptyio, "set_size"),
            ("/api/agent/remove", {"name": "worker"},
             app.Handler, "_agent_remove"),
            ("/api/pending/reject", {"guid": "pending-guid"},
             app.Handler, "_pending_reject"),
        )
        for path, request_body, owner, method in cases:
            with self.subTest(path=path), \
                 mock.patch.object(
                     owner, method,
                     side_effect=RuntimeError(f"{method} unavailable")):
                status, _, body = self._post(
                    path, request_body, opener=opener)
                self.assertEqual(status, 500, body)
                self.assertFalse(body.get("ok"), body)
                self.assertIn(f"{method} unavailable", body.get("error", ""))

            status, body = self._get("/api/health")
            self.assertEqual(status, 200)
            self.assertTrue(body.get("ok"), body)

    def test_api_text_fields_reject_non_string_json_types_before_dispatch(self):
        opener = self._operator()
        def reached_dispatch(handler, _path, _data):
            handler._json({"ok": True})
        # Representative required/optional strings across every mutating API
        # family. Validation belongs at the HTTP boundary, before a handler can
        # coerce a JSON number into an agent name/path/label/reason.
        cases = (
            ("/api/agent/create", "name", {}),
            ("/api/agent/create", "home", {"name": "strict-agent"}),
            ("/api/agent/start", "name", {}),
            ("/api/agent/remove", "name", {}),
            ("/api/edge/create", "source", {"target": "tgt"}),
            ("/api/edge/create", "label", {"source": "src", "target": "tgt"}),
            ("/api/edge/update", "guid", {}),
            ("/api/edge/update", "target_action", {"guid": "edge-guid"}),
            ("/api/edge/delete", "guid", {}),
            ("/api/agent/bless", "name", {}),
            ("/api/edge/bless", "guid", {}),
            ("/api/agent/foreman", "name", {}),
            ("/api/pending/approve", "guid", {}),
            ("/api/pending/reject", "reason", {"guid": "pending-guid"}),
            ("/api/expand", "text", {"kind": "agent"}),
        )
        invalid_values = (123, False, None, {}, [])
        for path, field, base in cases:
            for invalid in invalid_values:
                with self.subTest(path=path, field=field, value=invalid), \
                     mock.patch.object(
                         app.Handler, "_dispatch_post", autospec=True,
                         side_effect=reached_dispatch) as dispatch:
                    status, _, body = self._post(
                        path, {**base, field: invalid}, opener=opener)
                self.assertEqual(status, 400, body)
                self.assertIn(field, body.get("error", ""))
                dispatch.assert_not_called()

    def test_edge_condition_arrays_reject_wrong_container_or_members(self):
        opener = self._operator()
        def reached_dispatch(handler, _path, _data):
            handler._json({"ok": True})
        cases = (
            ("/api/edge/create", "conditions",
             {"source": "src", "target": "tgt"}),
            ("/api/edge/create", "back_conditions",
             {"source": "src", "target": "tgt"}),
            ("/api/edge/update", "conditions", {"guid": "edge-guid"}),
            ("/api/edge/update", "back_conditions", {"guid": "edge-guid"}),
        )
        for path, field, base in cases:
            for invalid in ("not-an-array", None, {}, ["valid", 7]):
                with self.subTest(path=path, field=field, value=invalid), \
                     mock.patch.object(
                         app.Handler, "_dispatch_post", autospec=True,
                         side_effect=reached_dispatch) as dispatch:
                    status, _, body = self._post(
                        path, {**base, field: invalid}, opener=opener)
                self.assertEqual(status, 400, body)
                self.assertIn(field, body.get("error", ""))
                dispatch.assert_not_called()

    def test_every_api_boolean_rejects_string_number_and_null(self):
        opener = self._operator()
        cases = (
            ("/api/agent/create", "launch", {"name": "bool_agent"},
             app.spawn, "spawn_agent"),
            ("/api/agent/remove", "kill_session", {"name": "bool_agent"},
             app.spawn, "remove_agent"),
            ("/api/edge/create", "reply_expected",
             {"source": "src", "target": "tgt"}, app.gs, "create_edge"),
            ("/api/edge/create", "back_reply",
             {"source": "src", "target": "tgt"}, app.gs, "create_edge"),
            ("/api/edge/create", "directed",
             {"source": "src", "target": "tgt"}, app.gs, "create_edge"),
            ("/api/edge/update", "reply_expected", {"guid": "edge-bool"},
             app.gs, "update_edge"),
            ("/api/edge/update", "back_reply", {"guid": "edge-bool"},
             app.gs, "update_edge"),
            ("/api/edge/update", "directed", {"guid": "edge-bool"},
             app.gs, "update_edge"),
            ("/api/agent/foreman", "revoke", {"name": "bool_agent"},
             app.gs, "set_foreman"),
        )
        agents = [{"_guid": "src-guid", "name": "src"},
                  {"_guid": "tgt-guid", "name": "tgt"}]
        current_edge = {"_guid": "edge-bool", "source": "src-guid",
                        "target": "tgt-guid", "directed": True}
        for path, field, base, owner, method in cases:
            for invalid in ("false", 0, None):
                with self.subTest(path=path, field=field, value=invalid), \
                     mock.patch.object(app.gs, "get_agent_by_name",
                                       side_effect=agents) as get_agent, \
                     mock.patch.object(app.gs, "get_object",
                                       return_value=current_edge), \
                     mock.patch.object(owner, method) as mutation, \
                     mock.patch.object(app, "_rewrite_endpoint_identities"), \
                     mock.patch.object(app.guard, "check"), \
                     mock.patch.object(app.guard, "audit"), \
                     mock.patch.object(app.spawn, "rewrite_identity"):
                    if method == "spawn_agent":
                        mutation.return_value = {"name": "bool_agent"}
                    elif method in ("create_edge", "update_edge"):
                        mutation.return_value = current_edge
                    status, _, body = self._post(
                        path, {**base, field: invalid}, opener=opener)
                    self.assertEqual(status, 400, body)
                    self.assertIn(field, body.get("error", ""))
                    mutation.assert_not_called()

    def test_omitted_api_booleans_keep_existing_defaults(self):
        opener = self._operator()
        created_agent = {"_guid": "agent-guid", "name": "new-agent"}
        with mock.patch.object(app.spawn, "spawn_agent",
                               return_value=created_agent) as spawn_agent:
            status, _, body = self._post(
                "/api/agent/create", {"name": "new-agent"}, opener=opener)
        self.assertEqual(status, 200)
        self.assertTrue(body.get("ok"), body)
        self.assertTrue(spawn_agent.call_args.kwargs["launch"])

        with mock.patch.object(app.spawn, "remove_agent") as remove_agent:
            status, _, body = self._post(
                "/api/agent/remove", {"name": "old-agent"}, opener=opener)
        self.assertEqual(status, 200)
        self.assertTrue(remove_agent.call_args.kwargs["kill_session"])

        agents = [{"_guid": "src-guid", "name": "src"},
                  {"_guid": "tgt-guid", "name": "tgt"}]
        with mock.patch.object(app.gs, "get_agent_by_name", side_effect=agents), \
             mock.patch.object(app.gs, "create_edge", return_value={}) as create, \
             mock.patch.object(app, "_rewrite_endpoint_identities"):
            status, _, body = self._post(
                "/api/edge/create", {"source": "src", "target": "tgt"},
                opener=opener)
        self.assertEqual(status, 200)
        self.assertFalse(create.call_args.kwargs["reply_expected"])
        self.assertFalse(create.call_args.kwargs["back_reply"])
        self.assertTrue(create.call_args.kwargs["directed"])

        foreman = {"_guid": "foreman-guid", "name": "foreman"}
        with mock.patch.object(app.gs, "get_agent_by_name", return_value=foreman), \
             mock.patch.object(app.gs, "set_foreman") as set_foreman:
            status, _, body = self._post(
                "/api/agent/foreman", {"name": "foreman"}, opener=opener)
        self.assertEqual(status, 200)
        self.assertFalse(set_foreman.call_args.kwargs["revoke"])
        self.assertIs(
            set_foreman.call_args.kwargs["_identity_rewriter"],
            app.spawn.rewrite_identity)

    def test_create_rejects_non_finite_cost_cap_before_graphstore_write(self):
        opener = self._operator()
        agents = [
            {"_guid": "agent_src", "name": "src"},
            {"_guid": "agent_tgt", "name": "tgt"},
        ]
        with mock.patch.object(app.gs, "get_agent_by_name", side_effect=agents), \
             mock.patch.object(app.gs, "create_edge", return_value={}) as create, \
             mock.patch.object(app, "_rewrite_endpoint_identities"):
            status, _, body = self._post(
                "/api/edge/create",
                {"source": "src", "target": "tgt", "cost_cap": "nan"},
                opener=opener)
        self.assertEqual(status, 200)
        self.assertFalse(body.get("ok"), body)
        self.assertIn("finite", body.get("error", "").lower())
        create.assert_not_called()

    def test_create_rejects_boolean_caps_before_graphstore_write(self):
        opener = self._operator()
        agents = [
            {"_guid": "agent_src", "name": "src"},
            {"_guid": "agent_tgt", "name": "tgt"},
        ]
        for field in ("max_turns", "token_cap", "cost_cap"):
            for value in (True, False, None):
                with self.subTest(field=field, value=value), \
                     mock.patch.object(
                         app.gs, "get_agent_by_name", side_effect=agents), \
                     mock.patch.object(app.gs, "create_edge") as create:
                    status, _, body = self._post(
                        "/api/edge/create",
                        {"source": "src", "target": "tgt", field: value},
                        opener=opener,
                    )
                self.assertEqual(status, 200, body)
                self.assertFalse(body.get("ok"), body)
                expected = "boolean" if isinstance(value, bool) else "number"
                self.assertIn(expected, body.get("error", "").lower())
                create.assert_not_called()

    def test_update_rejects_non_finite_cost_cap_before_graphstore_write(self):
        opener = self._operator()
        current = {
            "_guid": "edge_finite_api", "source": "agent_src",
            "target": "agent_tgt", "directed": True, "cost_cap": 1.0,
        }
        with mock.patch.object(app.gs, "get_object", return_value=current), \
             mock.patch.object(app.gs, "update_edge", return_value=current) as update:
            status, _, body = self._post(
                "/api/edge/update",
                {"guid": current["_guid"], "cost_cap": "inf"}, opener=opener)
        self.assertEqual(status, 200)
        self.assertFalse(body.get("ok"), body)
        self.assertIn("finite", body.get("error", "").lower())
        update.assert_not_called()

    def test_create_and_update_reject_negative_caps_before_graphstore_write(self):
        opener = self._operator()
        agents = [
            {"_guid": "agent_src", "name": "src"},
            {"_guid": "agent_tgt", "name": "tgt"},
        ]
        with mock.patch.object(app.gs, "get_agent_by_name", side_effect=agents), \
             mock.patch.object(app.gs, "create_edge", return_value={}) as create:
            status, _, body = self._post(
                "/api/edge/create",
                {"source": "src", "target": "tgt", "max_turns": -1},
                opener=opener)
        self.assertEqual(status, 200)
        self.assertFalse(body.get("ok"), body)
        self.assertRegex(body.get("error", "").lower(), "zero|positive")
        create.assert_not_called()

        current = {
            "_guid": "edge_nonnegative_api", "source": "agent_src",
            "target": "agent_tgt", "directed": True, "token_cap": 1,
        }
        with mock.patch.object(app.gs, "get_object", return_value=current), \
             mock.patch.object(app.gs, "update_edge", return_value=current) as update:
            status, _, body = self._post(
                "/api/edge/update",
                {"guid": current["_guid"], "token_cap": -1}, opener=opener)
        self.assertEqual(status, 200)
        self.assertFalse(body.get("ok"), body)
        self.assertRegex(body.get("error", "").lower(), "zero|positive")
        update.assert_not_called()


if __name__ == "__main__":
    unittest.main()
