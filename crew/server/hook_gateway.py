"""Local-only HTTP gateway for public webhook capabilities.

The tunnel process points at this server, never at Crew's dashboard.  Its
request surface is deliberately tiny: an exact hook capability POST and a
temporary secret-gated readiness probe used while bringing the tunnel online.
"""

import hmac
import json
import math
import os
import re
import socket
import stat
import threading
import time

from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn, UnixStreamServer

from .. import config, webhooks


LOOPBACK_HOST = "127.0.0.1"
DEFAULT_MAX_BODY = 1 << 20
DEFAULT_HEADER_TIMEOUT = 5.0
DEFAULT_BODY_TIMEOUT = 10.0
DEFAULT_MAX_CONNECTIONS = 32
READINESS_PATH = "/.crew-hook-gateway-ready"
READINESS_HEADER_NAME = "X-Crew-Hook-Gateway-Readiness"

_HOOK_PATH = re.compile(r"\A/hooks/([A-Za-z0-9_-]{43})\Z")
_HOP_BY_HOP_HEADERS = frozenset((
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
))
_PROXY_HEADERS = frozenset((
    "cf-connecting-ip",
    "forwarded",
    "proxy",
    "x-forwarded",
    "x-real-ip",
))
_STATUS_REASONS = {
    202: "Accepted",
    204: "No Content",
    400: "Bad Request",
    404: "Not Found",
    408: "Request Timeout",
    409: "Conflict",
    413: "Content Too Large",
    414: "URI Too Long",
    417: "Expectation Failed",
    422: "Unprocessable Content",
    431: "Request Header Fields Too Large",
    500: "Internal Server Error",
    503: "Service Unavailable",
    505: "HTTP Version Not Supported",
}


class _BodyTimeout(Exception):
    pass


class _ShortBody(Exception):
    pass


class _GatewayState:
    """Readiness capability state shared by every request handler."""

    def __init__(self, readiness_secret):
        self._secret = readiness_secret
        self._enabled = True
        self._active = 0
        self._condition = threading.Condition()

    def begin_readiness(self, values):
        if len(values) != 1:
            return False
        supplied = values[0]
        if not isinstance(supplied, str):
            return False
        with self._condition:
            if (
                    not self._enabled
                    or not hmac.compare_digest(
                        supplied.encode("utf-8"),
                        self._secret.encode("utf-8"))):
                return False
            self._active += 1
            return True

    def end_readiness(self):
        with self._condition:
            self._active -= 1
            if not self._active:
                self._condition.notify_all()

    def disable_readiness(self):
        with self._condition:
            self._enabled = False
            while self._active:
                self._condition.wait()


class _LimitedThreadingServerMixin(ThreadingMixIn):
    """Admission bound shared by TCP and Unix-domain HTTP servers."""

    daemon_threads = True
    block_on_close = False
    request_queue_size = DEFAULT_MAX_CONNECTIONS + 8

    def _configure_gateway(
            self, *, state, max_body, header_timeout, body_timeout,
            max_connections):
        self.gateway_state = state
        self.max_body = max_body
        self.header_timeout = header_timeout
        self.body_timeout = body_timeout
        self._connection_slots = threading.BoundedSemaphore(max_connections)

    def process_request(self, request, client_address):
        if not self._connection_slots.acquire(blocking=False):
            try:
                body = _json_bytes({
                    "ok": False, "error": "temporarily unavailable"})
                request.sendall(
                    b"HTTP/1.1 503 Service Unavailable\r\n"
                    b"Content-Type: application/json\r\n"
                    b"Cache-Control: no-store\r\n"
                    b"Connection: close\r\n"
                    + f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
                    + body)
            except OSError:
                pass
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._connection_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._connection_slots.release()

    def handle_error(self, request, client_address):
        # socketserver's default prints a traceback to stderr.  The gateway has
        # no secret-safe request logger, so unexpected handler failures are
        # intentionally reduced to a closed connection.
        return


class _LimitedThreadingHTTPServer(
        _LimitedThreadingServerMixin, HTTPServer):
    """Loopback TCP variant retained for direct local testing."""

    allow_reuse_address = True

    def __init__(
            self, server_address, handler_class, *,
            state, max_body, header_timeout, body_timeout, max_connections):
        if server_address[0] != LOOPBACK_HOST:
            raise ValueError("hook gateway must bind to 127.0.0.1")
        self._configure_gateway(
            state=state,
            max_body=max_body,
            header_timeout=header_timeout,
            body_timeout=body_timeout,
            max_connections=max_connections,
        )
        super().__init__(server_address, handler_class)


class _LimitedThreadingUnixHTTPServer(
        _LimitedThreadingServerMixin, UnixStreamServer):
    """Private Unix-domain variant used by the public tunnel."""

    allow_reuse_address = False

    def __init__(
            self, socket_path, handler_class, *,
            state, max_body, header_timeout, body_timeout, max_connections):
        self._configure_gateway(
            state=state,
            max_body=max_body,
            header_timeout=header_timeout,
            body_timeout=body_timeout,
            max_connections=max_connections,
        )
        super().__init__(socket_path, handler_class)


def _json_bytes(value):
    return (
        json.dumps(
            value, ensure_ascii=False, allow_nan=False,
            separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _blocked_header(name, connection_tokens):
    normalized = str(name or "").strip().lower()
    return (
        not normalized
        or normalized == READINESS_HEADER_NAME.lower()
        or normalized in _HOP_BY_HOP_HEADERS
        or normalized in connection_tokens
        or normalized in _PROXY_HEADERS
        or normalized.startswith("x-forwarded-")
        or normalized.startswith("proxy-")
    )


def _sanitized_headers(headers):
    connection_tokens = set()
    for value in headers.get_all("Connection", []):
        connection_tokens.update(
            token.strip().lower()
            for token in value.split(",")
            if token.strip())

    grouped = {}
    names = {}
    for name, value in headers.items():
        normalized = str(name or "").strip().lower()
        if _blocked_header(normalized, connection_tokens):
            continue
        names.setdefault(normalized, str(name))
        grouped.setdefault(normalized, []).append(str(value))
    return {
        names[normalized]: ", ".join(values)
        for normalized, values in grouped.items()
    }


def _header_value(headers, wanted):
    wanted = wanted.lower()
    for name, value in headers.items():
        if name.lower() == wanted:
            return value
    return ""


class _HookGatewayHandler(BaseHTTPRequestHandler):
    """Strict, non-logging request handler for the hook gateway."""

    protocol_version = "HTTP/1.1"

    def handle(self):
        # BaseHTTPRequestHandler applies no deadline while reading the request
        # line and headers. An absolute timer is required because a peer can
        # otherwise retain an admitted slot forever by sending one byte before
        # each ordinary socket timeout.
        self._header_deadline_lock = threading.Lock()
        self._headers_pending = True
        self._header_timer = threading.Timer(
            self.server.header_timeout,
            self._expire_header_read,
        )
        self._header_timer.daemon = True
        self.connection.settimeout(self.server.header_timeout)
        self._header_timer.start()
        try:
            super().handle()
        finally:
            self._complete_header_read()

    def _expire_header_read(self):
        with self._header_deadline_lock:
            if not self._headers_pending:
                return
            self._headers_pending = False
        self.close_connection = True
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def _complete_header_read(self):
        with self._header_deadline_lock:
            if not self._headers_pending:
                return
            self._headers_pending = False
            timer = self._header_timer
        timer.cancel()

    def parse_request(self):
        # BaseHTTPRequestHandler intentionally collapses a leading ``//`` in
        # ``self.path``.  Keep the request-target as it appeared on the wire so
        # only the one exact raw hook path can be admitted.
        try:
            parts = (
                self.raw_requestline.decode("iso-8859-1")
                .rstrip("\r\n")
                .split())
            self.raw_request_target = parts[1] if len(parts) >= 2 else ""
        except Exception:
            self.raw_request_target = ""
        try:
            return super().parse_request()
        finally:
            self._complete_header_read()

    def log_message(self, format, *args):
        return

    def log_request(self, code="-", size="-"):
        return

    def send_response(self, code, message=None):
        """Send a response line without BaseHTTPRequestHandler's Server header."""
        reason = message or _STATUS_REASONS.get(code, "")
        self.send_response_only(code, reason)

    def send_error(self, code, message=None, explain=None):
        # Parser failures and unknown methods must never use the stdlib HTML
        # error page or reflect a request line, method, capability, or version.
        if code == 501:
            self._send_json(404, {"ok": False, "error": "not found"})
            return
        safe_status = code if code in (400, 414, 431, 505) else 400
        self._send_json(
            safe_status, {"ok": False, "error": "bad request"})

    def handle_expect_100(self):
        # BaseHTTPRequestHandler otherwise emits an interim 100 response before
        # the capability and framing checks run. Preserve the gateway's
        # route-hiding contract: only an exact hook-shaped POST gets the
        # framing response; every other public path remains a generic 404.
        if (
                self.command == "POST"
                and _HOOK_PATH.fullmatch(
                    getattr(self, "raw_request_target", "") or "")):
            self._send_json(
                417, {"ok": False, "error": "expectation failed"})
        else:
            self._not_found()
        return False

    def _send_json(self, status, value):
        try:
            body = _json_bytes(value)
        except Exception:
            status = 503
            body = _json_bytes({
                "ok": False, "error": "temporarily unavailable"})
        self._send_body(status, body, "application/json")

    def _send_body(self, status, body, content_type):
        self.close_connection = True
        try:
            self.send_response(status)
            if content_type:
                self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD" and body:
                self.wfile.write(body)
                self.wfile.flush()
        except OSError:
            pass

    def _not_found(self):
        self._send_json(404, {"ok": False, "error": "not found"})

    def do_GET(self):
        if self.raw_request_target != READINESS_PATH:
            self._not_found()
            return
        values = self.headers.get_all(READINESS_HEADER_NAME, [])
        if not self.server.gateway_state.begin_readiness(values):
            self._not_found()
            return
        try:
            self._send_body(204, b"", "")
        finally:
            self.server.gateway_state.end_readiness()

    def do_POST(self):
        match = _HOOK_PATH.fullmatch(self.raw_request_target or "")
        if match is None:
            self._not_found()
            return
        token = match.group(1)

        # Reject random capabilities before allowing the connection to occupy
        # a body-read deadline. receive() independently resolves the token at
        # its serialized admission point, so rotations still fail closed.
        try:
            exists = webhooks.capability_exists(token)
        except Exception:
            self._send_json(
                503, {"ok": False, "error": "temporarily unavailable"})
            return
        if not exists:
            self._not_found()
            return

        if self.headers.get_all("Expect", []):
            self._send_json(
                417, {"ok": False, "error": "expectation failed"})
            return
        if self.headers.get_all("Transfer-Encoding", []):
            self._send_json(400, {"ok": False, "error": "bad request"})
            return
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) != 1:
            self._send_json(400, {"ok": False, "error": "bad request"})
            return
        raw_length = lengths[0]
        if re.fullmatch(r"[0-9]+", raw_length or "") is None:
            self._send_json(400, {"ok": False, "error": "bad request"})
            return
        normalized_length = raw_length.lstrip("0") or "0"
        max_text = str(self.server.max_body)
        if (
                len(normalized_length) > len(max_text)
                or (
                    len(normalized_length) == len(max_text)
                    and normalized_length > max_text)):
            self._send_json(
                413, {"ok": False, "error": "request too large"})
            return
        length = int(normalized_length)

        try:
            raw = self._read_body(length)
        except _BodyTimeout:
            self._send_json(
                408, {"ok": False, "error": "request timeout"})
            return
        except (_ShortBody, OSError):
            self._send_json(400, {"ok": False, "error": "bad request"})
            return

        headers = _sanitized_headers(self.headers)
        try:
            result = webhooks.receive(
                token,
                raw,
                content_type=_header_value(headers, "content-type"),
                headers=headers,
            )
        except webhooks.WebhookError as error:
            status = int(getattr(error, "status", 422))
            if status == 404:
                self._not_found()
            elif status == 409:
                self._send_json(
                    409, {"ok": False, "error": "webhook conflict"})
            elif status == 413:
                self._send_json(
                    413, {"ok": False, "error": "request too large"})
            elif status >= 500:
                self._send_json(
                    503,
                    {"ok": False, "error": "temporarily unavailable"})
            else:
                self._send_json(
                    422,
                    {"ok": False, "error": "invalid webhook payload"})
            return
        except Exception:
            self._send_json(
                503, {"ok": False, "error": "temporarily unavailable"})
            return
        self._send_json(202, result)

    def _read_body(self, length):
        deadline = time.monotonic() + self.server.body_timeout
        body = bytearray()
        while len(body) < length:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _BodyTimeout
            self.connection.settimeout(remaining)
            try:
                reader = getattr(self.rfile, "read1", self.rfile.read)
                chunk = reader(min(65536, length - len(body)))
            except (TimeoutError, socket.timeout) as error:
                raise _BodyTimeout from error
            if not chunk:
                raise _ShortBody
            body.extend(chunk)
        return bytes(body)


class HookGateway:
    """Own one local hook gateway and its bounded serving thread."""

    readiness_path = READINESS_PATH

    def __init__(
            self, readiness_secret, *, port=0,
            unix_socket_path=None,
            max_body=DEFAULT_MAX_BODY,
            header_timeout=DEFAULT_HEADER_TIMEOUT,
            body_timeout=DEFAULT_BODY_TIMEOUT,
            max_connections=DEFAULT_MAX_CONNECTIONS):
        if (
                not isinstance(readiness_secret, str)
                or not readiness_secret
                or "\r" in readiness_secret
                or "\n" in readiness_secret):
            raise ValueError("readiness_secret must be a non-empty header value")
        if (
                not isinstance(port, int) or isinstance(port, bool)
                or not 0 <= port <= 65535):
            raise ValueError("port must be an integer from 0 through 65535")
        if unix_socket_path is not None:
            if not isinstance(unix_socket_path, (str, os.PathLike)):
                raise ValueError("unix_socket_path must be an absolute path")
            unix_socket_path = os.fspath(unix_socket_path)
            if (
                    not isinstance(unix_socket_path, str)
                    or not os.path.isabs(unix_socket_path)
                    or os.path.abspath(unix_socket_path) != unix_socket_path
                    or "\0" in unix_socket_path
                    or len(os.fsencode(unix_socket_path)) >= 100):
                raise ValueError(
                    "unix_socket_path must be a short absolute path")
            if port != 0:
                raise ValueError(
                    "port and unix_socket_path cannot both be configured")
        if (
                not isinstance(max_body, int) or isinstance(max_body, bool)
                or max_body < 0):
            raise ValueError("max_body must be a non-negative integer")
        if (
                not isinstance(header_timeout, (int, float))
                or isinstance(header_timeout, bool)
                or not math.isfinite(float(header_timeout))
                or header_timeout <= 0):
            raise ValueError("header_timeout must be positive")
        if (
                not isinstance(body_timeout, (int, float))
                or isinstance(body_timeout, bool)
                or not math.isfinite(float(body_timeout))
                or body_timeout <= 0):
            raise ValueError("body_timeout must be positive")
        if (
                not isinstance(max_connections, int)
                or isinstance(max_connections, bool)
                or max_connections <= 0):
            raise ValueError("max_connections must be a positive integer")

        self._readiness_secret = readiness_secret
        self._requested_port = port
        self._unix_socket_path = unix_socket_path
        self._max_body = max_body
        self._header_timeout = float(header_timeout)
        self._body_timeout = float(body_timeout)
        self._max_connections = max_connections
        self._state = _GatewayState(readiness_secret)
        self._server = None
        self._thread = None
        self._closed = False
        self._owns_socket_path = False
        self._lifecycle_lock = threading.Lock()

    @property
    def readiness_header(self):
        return READINESS_HEADER_NAME, self._readiness_secret

    @property
    def address(self):
        server = self._server
        if server is None:
            if self._unix_socket_path is not None:
                return self._unix_socket_path
            return LOOPBACK_HOST, self._requested_port
        return server.server_address

    @property
    def port(self):
        if self._unix_socket_path is not None:
            raise RuntimeError("Unix hook gateway does not have a TCP port")
        return int(self.address[1])

    @property
    def base_url(self):
        if self._unix_socket_path is not None:
            raise RuntimeError("Unix hook gateway does not have an HTTP URL")
        if self._server is None:
            raise RuntimeError("hook gateway has not been started")
        return f"http://{LOOPBACK_HOST}:{self.port}"

    @property
    def socket_path(self):
        return self._unix_socket_path

    @property
    def origin_url(self):
        if self._server is None:
            raise RuntimeError("hook gateway has not been started")
        if self._unix_socket_path is not None:
            return f"unix:{self._unix_socket_path}"
        return self.base_url

    def _remove_owned_socket(self):
        if not self._owns_socket_path or self._unix_socket_path is None:
            return
        try:
            info = os.lstat(self._unix_socket_path)
            if stat.S_ISSOCK(info.st_mode):
                os.unlink(self._unix_socket_path)
        except FileNotFoundError:
            pass
        finally:
            self._owns_socket_path = False

    def start(self):
        with self._lifecycle_lock:
            if self._closed:
                raise RuntimeError("closed hook gateway cannot be restarted")
            if self._server is not None:
                return self
            if self._unix_socket_path is None:
                server = _LimitedThreadingHTTPServer(
                    (LOOPBACK_HOST, self._requested_port),
                    _HookGatewayHandler,
                    state=self._state,
                    max_body=self._max_body,
                    header_timeout=self._header_timeout,
                    body_timeout=self._body_timeout,
                    max_connections=self._max_connections,
                )
            else:
                parent = os.path.dirname(self._unix_socket_path)
                config.ensure_private_directory(parent)
                if os.path.lexists(self._unix_socket_path):
                    raise FileExistsError(
                        "refusing to replace an existing gateway socket path")
                server = _LimitedThreadingUnixHTTPServer(
                    self._unix_socket_path,
                    _HookGatewayHandler,
                    state=self._state,
                    max_body=self._max_body,
                    header_timeout=self._header_timeout,
                    body_timeout=self._body_timeout,
                    max_connections=self._max_connections,
                )
                self._owns_socket_path = True
                try:
                    os.chmod(self._unix_socket_path, 0o600)
                except BaseException:
                    server.server_close()
                    self._remove_owned_socket()
                    raise
            thread = threading.Thread(
                target=server.serve_forever,
                kwargs={"poll_interval": 0.05},
                name="crew-hook-gateway",
                daemon=True,
            )
            self._server = server
            self._thread = thread
            try:
                thread.start()
            except BaseException:
                self._server = None
                self._thread = None
                server.server_close()
                self._remove_owned_socket()
                raise
        return self

    def disable_readiness(self):
        self._state.disable_readiness()

    def is_alive(self):
        thread = self._thread
        return bool(thread is not None and thread.is_alive())

    def close(self):
        with self._lifecycle_lock:
            if self._closed:
                return
            self._closed = True
            server = self._server
            thread = self._thread
        if server is None:
            return
        if thread is not threading.current_thread() and thread.is_alive():
            server.shutdown()
        server.server_close()
        self._remove_owned_socket()
        if thread is not threading.current_thread():
            thread.join(timeout=2.0)

    shutdown = close
    server_close = close

    def __enter__(self):
        return self.start()

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()


def start_hook_gateway(**kwargs):
    """Construct, start, and return one :class:`HookGateway`."""
    return HookGateway(**kwargs).start()


__all__ = [
    "DEFAULT_BODY_TIMEOUT",
    "DEFAULT_MAX_BODY",
    "DEFAULT_MAX_CONNECTIONS",
    "HookGateway",
    "LOOPBACK_HOST",
    "READINESS_HEADER_NAME",
    "READINESS_PATH",
    "start_hook_gateway",
]
