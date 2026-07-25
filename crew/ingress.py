"""Lean foreground Cloudflare Quick Tunnel lifecycle.

This module deliberately owns neither CLI parsing nor durable ingress state.
The caller must hold the app-scoped ingress lease before calling
``run_ingress`` and may publish from ``on_ready``.  Publication happens only
after a request has traversed the public tunnel to the gateway's temporary
secret-header readiness route and that route has been disabled.
"""

from collections import deque
import hashlib
import http.client
import json
import math
import os
import re
import secrets
import shutil
import signal
import socket
import ssl
import stat
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config


MAX_LOG_LINE_BYTES = 16 * 1024
MAX_RETAINED_LOG_LINES = 200
MAX_METRICS_RESPONSE_BYTES = 4096
DEFAULT_STARTUP_TIMEOUT = 45.0
DEFAULT_READINESS_TIMEOUT = 3.0
DEFAULT_READINESS_INTERVAL = 0.25
DEFAULT_SHUTDOWN_TIMEOUT = 5.0

_QUICK_TUNNEL_HOST = re.compile(
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.trycloudflare\.com"
)
_URL_IN_LOG = re.compile(r"https://[A-Za-z0-9.-]+(?=[\s|\"']|$)")


class IngressError(RuntimeError):
    """The public-ingress lifecycle could not be started or kept healthy."""


class IngressStopped(IngressError):
    """The operator requested shutdown before the tunnel became public."""


def find_cloudflared(explicit_path=None):
    """Resolve one executable to an absolute real path; never install it."""
    if explicit_path:
        candidate = os.fspath(explicit_path)
        if not os.path.isabs(candidate):
            candidate = shutil.which(candidate)
    else:
        candidate = shutil.which("cloudflared")
    if not candidate:
        raise IngressError(
            "cloudflared is required; install cloudflared and try again")
    candidate = os.path.realpath(os.path.abspath(candidate))
    if not os.path.isfile(candidate) or not os.access(candidate, os.X_OK):
        raise IngressError(
            f"cloudflared path is not an executable file: {candidate}")
    return candidate


def _runtime_identity(morphdb_origin, app):
    material = (
        str(morphdb_origin).rstrip("/") + "\0" + str(app)
    ).encode("utf-8")
    return hashlib.sha256(material).hexdigest()[:24]


def write_cloudflared_config(
        runtime_dir, *, morphdb_origin, app, gateway_origin,
        config_path=None):
    """Write one owner-only config that pins the per-run Unix origin."""
    runtime_dir = config.ensure_private_directory(runtime_dir)
    if (
            not isinstance(gateway_origin, str)
            or not gateway_origin.startswith("unix:/")
            or "\0" in gateway_origin):
        raise IngressError("cloudflared gateway origin must be a Unix socket")
    gateway_path = gateway_origin[len("unix:"):]
    if (
            not os.path.isabs(gateway_path)
            or os.path.abspath(gateway_path) != gateway_path
            or os.path.dirname(gateway_path) != os.path.abspath(runtime_dir)
            or len(os.fsencode(gateway_path)) >= 100):
        raise IngressError(
            "cloudflared gateway origin must use this private runtime")
    if config_path is None:
        path = os.path.join(
            runtime_dir,
            "cloudflared-"
            f"{_runtime_identity(morphdb_origin, app)}-"
            f"{secrets.token_hex(8)}.cf.json",
        )
    else:
        if not isinstance(config_path, (str, os.PathLike)):
            raise IngressError("cloudflared config path must be a path")
        path = os.path.abspath(os.fspath(config_path))
        if (
                not os.path.isabs(os.fspath(config_path))
                or os.path.dirname(path) != os.path.abspath(runtime_dir)
                or not os.path.basename(path).endswith(".cf.json")):
            raise IngressError(
                "cloudflared config path must be an app-owned .cf.json file")
    # Every config is scoped to one lease and must be immutable from the
    # perspective of a hard-orphaned child. Refuse even an owner-controlled
    # pre-existing file instead of truncating or reusing it.
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as error:
        raise IngressError(
            f"could not create cloudflared config: {error}") from error
    created_info = None
    completed = False
    try:
        info = os.fstat(fd)
        created_info = info
        uid = getattr(os, "getuid", lambda: info.st_uid)()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != uid
                or info.st_nlink != 1):
            raise IngressError(
                "cloudflared config is not an owner-controlled regular file")
        os.fchmod(fd, 0o600)
        os.ftruncate(fd, 0)
        document = {
            "ingress": [{
                "service": gateway_origin,
                "originRequest": {
                    "disableChunkedEncoding": True,
                },
            }],
        }
        encoded = (
            json.dumps(document, separators=(",", ":"), sort_keys=True)
            + "\n"
        ).encode("utf-8")
        remaining = memoryview(encoded)
        while remaining:
            written = os.write(fd, remaining)
            if written <= 0:
                raise IngressError("could not write cloudflared config")
            remaining = remaining[written:]
        os.fsync(fd)
        completed = True
    except IngressError:
        raise
    except OSError as error:
        raise IngressError(
            f"could not write cloudflared config: {error}") from error
    finally:
        os.close(fd)
        if not completed and created_info is not None:
            try:
                current = os.lstat(path)
                if (
                        current.st_dev == created_info.st_dev
                        and current.st_ino == created_info.st_ino):
                    os.unlink(path)
            except OSError:
                pass
    return path


def remove_cloudflared_config(path, runtime_dir):
    """Remove only the exact owner-controlled config created for this run."""
    if path is None:
        return False
    runtime_dir = os.path.abspath(runtime_dir)
    path = os.path.abspath(os.fspath(path))
    if (
            os.path.dirname(path) != runtime_dir
            or not os.path.basename(path).endswith(".cf.json")):
        raise IngressError("refusing to remove a non-ingress config path")
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return False
    uid = getattr(os, "getuid", lambda: info.st_uid)()
    if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != uid
            or info.st_nlink != 1):
        raise IngressError(
            "refusing to remove an unsafe cloudflared config")
    os.unlink(path)
    return True


def _safe_child_env(runtime_dir):
    """Return fixed, non-secret child state independent of caller env."""
    runtime_dir = os.path.realpath(runtime_dir)
    return {
        "HOME": runtime_dir,
        "LANG": "C",
        "LC_ALL": "C",
        "TMPDIR": runtime_dir,
        "XDG_CONFIG_HOME": runtime_dir,
    }


def _reserve_loopback_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def _validated_quick_tunnel_url(value):
    try:
        parsed = urllib.parse.urlsplit(str(value).strip())
        port = parsed.port
    except (TypeError, ValueError):
        return None
    hostname = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or not _QUICK_TUNNEL_HOST.fullmatch(hostname)
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.netloc.lower() != hostname
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        return None
    return f"https://{hostname}"


def _validated_quick_tunnel_hostname(value):
    if not isinstance(value, str):
        return None
    hostname = value.strip().lower()
    if not _QUICK_TUNNEL_HOST.fullmatch(hostname):
        return None
    return f"https://{hostname}"


def _urls_from_json_log(line):
    try:
        payload = json.loads(line)
    except (TypeError, ValueError):
        return ()
    if not isinstance(payload, dict):
        return ()
    candidates = []
    direct = payload.get("url")
    if isinstance(direct, str):
        candidates.append(direct)
    message = payload.get("message")
    if isinstance(message, str):
        candidates.extend(_URL_IN_LOG.findall(message))
    return tuple(
        url for candidate in candidates
        if (url := _validated_quick_tunnel_url(candidate)) is not None
    )


def _bounded_text(raw):
    raw = raw[:MAX_LOG_LINE_BYTES]
    return raw.decode("utf-8", "ignore").rstrip("\r\n")


class CloudflaredLogCollector(threading.Thread):
    """Drain a binary cloudflared stream without retaining unbounded output."""

    def __init__(self, stream, *, max_lines=MAX_RETAINED_LOG_LINES):
        super().__init__(name="crew-cloudflared-log-drain", daemon=True)
        self._stream = stream
        self._lock = threading.Lock()
        self.lines = deque(maxlen=max(1, int(max_lines)))
        self.urls = set()
        self.error = None

    def _record(self, raw):
        text = _bounded_text(raw)
        with self._lock:
            self.lines.append(text)
            self.urls.update(_urls_from_json_log(text))

    def snapshot_urls(self):
        with self._lock:
            return set(self.urls)

    def run(self):
        try:
            while True:
                raw = self._stream.readline(MAX_LOG_LINE_BYTES + 1)
                if not raw:
                    return
                self._record(raw)
                while raw and not raw.endswith(b"\n"):
                    raw = self._stream.readline(MAX_LOG_LINE_BYTES + 1)
        except (OSError, ValueError) as error:
            self.error = error


def make_stop_signal_handler(stop_event):
    """Return a signal-safe-enough handler that only sets an Event."""
    def request_stop(_signum, _frame):
        stop_event.set()
    return request_stop


def _install_signal_handlers(stop_event):
    if threading.current_thread() is not threading.main_thread():
        return {}
    old = {}
    handler = make_stop_signal_handler(stop_event)
    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous = signal.getsignal(signum)
            signal.signal(signum, handler)
            old[signum] = previous
    except BaseException:
        _restore_signal_handlers(old)
        raise
    return old


def _restore_signal_handlers(old_handlers):
    errors = []
    for signum, handler in old_handlers.items():
        try:
            signal.signal(signum, handler)
        except Exception as error:
            errors.append(error)
    if errors:
        raise errors[0]


def _default_preflight(*, morphdb_origin, app):
    from . import ingress_state
    held_origin, exact_app = ingress_state.canonical_scope(
        morphdb_origin, app)
    configured_origin, _ = ingress_state.canonical_scope(
        config.morphdb_base(), app)
    if held_origin != configured_origin:
        raise IngressError(
            "held ingress lease does not match the configured MorphDB origin")
    from . import schema
    return schema.ensure_schema(app=exact_app)


def _default_gateway_factory(*, readiness_secret, unix_socket_path):
    from .server.hook_gateway import HookGateway
    return HookGateway(
        readiness_secret=readiness_secret,
        unix_socket_path=unix_socket_path,
    )


def _gateway_enable_readiness(gateway):
    enable = getattr(gateway, "enable_readiness", None)
    if callable(enable):
        enable()


def _gateway_start(gateway):
    start = getattr(gateway, "start", None)
    if not callable(start):
        raise IngressError("hook gateway does not provide start()")
    start()


def _gateway_origin_url(gateway):
    value = getattr(gateway, "origin_url", None)
    if value is None:
        value = getattr(gateway, "base_url", None)
    if isinstance(value, str) and value.startswith("unix:"):
        path = value[len("unix:"):]
        socket_path = getattr(gateway, "socket_path", None)
        if (
                not path
                or not os.path.isabs(path)
                or os.path.abspath(path) != path
                or socket_path != path
                or "\0" in path
                or len(os.fsencode(path)) >= 100):
            raise IngressError(
                "hook gateway exposed an invalid Unix origin URL")
        return value
    raise IngressError(
        "hook gateway must expose a private Unix socket origin")


def _new_gateway_socket_path(runtime_dir):
    runtime_dir = config.ensure_private_directory(runtime_dir)
    for _attempt in range(8):
        path = os.path.join(
            runtime_dir, f"hook-{secrets.token_hex(12)}.sock")
        if len(os.fsencode(path)) >= 100:
            raise IngressError(
                "private runtime path is too long for a Unix hook socket")
        if not os.path.lexists(path):
            return path
    raise IngressError("could not allocate a unique Unix hook socket")


def _gateway_is_alive(gateway):
    check = getattr(gateway, "is_alive", None)
    if callable(check):
        return bool(check())
    thread = getattr(gateway, "thread", None)
    if thread is not None and callable(getattr(thread, "is_alive", None)):
        return bool(thread.is_alive())
    return True


def _gateway_close(gateway):
    close = getattr(gateway, "close", None)
    if callable(close):
        close()
        return
    shutdown = getattr(gateway, "shutdown", None)
    if callable(shutdown):
        shutdown()
    server_close = getattr(gateway, "server_close", None)
    if callable(server_close):
        server_close()


def _gateway_readiness_request(gateway, public_url):
    path = getattr(gateway, "readiness_path", None)
    header = getattr(gateway, "readiness_header", None)
    if (
        not isinstance(path, str)
        or not path.startswith("/")
        or "?" in path
        or "#" in path
        or not isinstance(header, (tuple, list))
        or len(header) != 2
        or not all(isinstance(value, str) and value for value in header)
    ):
        raise IngressError(
            "hook gateway did not expose its readiness request contract")
    name, value = header
    return urllib.request.Request(
        public_url + path,
        headers={
            name: value,
            "Cache-Control": "no-store",
            "Connection": "close",
        },
        method="GET",
    )


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, _request, _file, _code, _message, _headers,
                         _new_url):
        return None


_LOOPBACK_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}),
    _NoRedirect(),
)


def _open_loopback_url(request, *, timeout):
    parsed = urllib.parse.urlsplit(request.full_url)
    if (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or parsed.port is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path != "/quicktunnel"
            or parsed.query
            or parsed.fragment):
        raise IngressError("metrics request is not strict loopback HTTP")
    return _LOOPBACK_OPENER.open(request, timeout=timeout)


class _HTTPSResponse:
    """Small response adapter that closes its owning HTTPS connection."""

    def __init__(self, connection, response, final_url):
        self._connection = connection
        self._response = response
        self._final_url = final_url
        self.status = response.status

    def read(self, limit=-1):
        return self._response.read(limit)

    def geturl(self):
        return self._final_url

    def getcode(self):
        return self.status

    def close(self):
        try:
            self._response.close()
        finally:
            self._connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()
        return False


def _https_request_once(request, timeout, *, connect_host=None):
    parsed = urllib.parse.urlsplit(request.full_url)
    if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.port not in (None, 443)
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment):
        raise IngressError("public readiness request has an invalid URL")
    connection = http.client.HTTPSConnection(
        parsed.hostname,
        443,
        timeout=timeout,
        context=ssl.create_default_context(),
    )
    if connect_host is not None:
        # Keep the Quick Tunnel hostname for HTTP Host, TLS SNI, and normal CA
        # hostname verification. Only the TCP destination changes.
        def connect_to_edge(
                address, connect_timeout=socket._GLOBAL_DEFAULT_TIMEOUT,
                source_address=None):
            return socket.create_connection(
                (connect_host, address[1]),
                connect_timeout,
                source_address,
            )

        connection._create_connection = connect_to_edge
    path = urllib.parse.urlunsplit((
        "", "", parsed.path or "/", parsed.query, ""))
    try:
        connection.request(
            request.get_method(),
            path,
            body=request.data,
            headers=dict(request.header_items()),
        )
        response = connection.getresponse()
        return _HTTPSResponse(connection, response, request.full_url)
    except BaseException:
        connection.close()
        raise


def _open_public_url(request, *, timeout):
    """Open a TLS-verified Quick Tunnel URL with a bounded DNS fallback."""
    deadline = time.monotonic() + timeout
    try:
        return _https_request_once(request, timeout)
    except socket.gaierror as direct_error:
        hostname = urllib.parse.urlsplit(request.full_url).hostname or ""
        if _validated_quick_tunnel_hostname(hostname) is None:
            raise IngressError(
                "public readiness hostname did not resolve") from direct_error

    try:
        rows = socket.getaddrinfo(
            "trycloudflare.com", 443, type=socket.SOCK_STREAM)
    except socket.gaierror as error:
        raise urllib.error.URLError(
            "Cloudflare edge hostname did not resolve") from error
    addresses = list(dict.fromkeys(row[4][0] for row in rows))
    last_error = None
    last_response = None
    for address in addresses:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            response = _https_request_once(
                request, remaining, connect_host=address)
            if response.status not in (429, 500, 502, 503, 504):
                if last_response is not None:
                    last_response.close()
                return response
            if last_response is not None:
                last_response.close()
            last_response = response
        except (
                OSError,
                TimeoutError,
                http.client.HTTPException,
                ssl.SSLError,
        ) as error:
            last_error = error
    if last_response is not None:
        return last_response
    raise urllib.error.URLError(
        "all Cloudflare edge connections failed") from last_error


def _check_health(process, gateway, stop_event):
    if stop_event.is_set():
        raise IngressStopped("public ingress stopped before it became ready")
    returncode = process.poll()
    if returncode is not None:
        raise IngressError(
            f"cloudflared exited unexpectedly with status {returncode}")
    if not _gateway_is_alive(gateway):
        raise IngressError("hook gateway stopped unexpectedly")


def _require_collector_healthy(collector, process):
    if getattr(collector, "error", None) is not None:
        raise IngressError("cloudflared log drain failed")
    is_alive = getattr(collector, "is_alive", None)
    if (
            callable(is_alive)
            and not is_alive()
            and process.poll() is None):
        raise IngressError("cloudflared log drain ended unexpectedly")


def _await_public_url(
        collector, process, gateway, stop_event, *, metrics_port,
        metrics_opener, timeout, interval, request_timeout):
    deadline = time.monotonic() + timeout
    metrics_candidate = None
    fallback_candidate = None
    fallback_at = None
    fallback_delay = min(1.0, max(0.01, timeout / 4))
    metrics_request = urllib.request.Request(
        f"http://127.0.0.1:{metrics_port}/quicktunnel",
        headers={"Accept": "application/json"},
        method="GET",
    )
    while True:
        _check_health(process, gateway, stop_event)
        _require_collector_healthy(collector, process)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise IngressError(
                "timed out waiting for cloudflared quick-tunnel URL")
        try:
            with metrics_opener(
                    metrics_request,
                    timeout=min(request_timeout, remaining)) as response:
                status = getattr(response, "status", None)
                if (
                    status == 200
                    and (
                        not callable(getattr(response, "geturl", None))
                        or response.geturl() == metrics_request.full_url
                    )
                ):
                    raw = response.read(MAX_METRICS_RESPONSE_BYTES + 1)
                    if len(raw) > MAX_METRICS_RESPONSE_BYTES:
                        raise IngressError(
                            "cloudflared /quicktunnel response is too large")
                    try:
                        payload = json.loads(raw)
                    except (TypeError, ValueError) as error:
                        raise IngressError(
                            "cloudflared /quicktunnel returned invalid JSON"
                        ) from error
                    hostname = (
                        payload.get("hostname")
                        if isinstance(payload, dict) else None
                    )
                    if hostname:
                        public_url = _validated_quick_tunnel_hostname(hostname)
                        if public_url is None:
                            raise IngressError(
                                "cloudflared /quicktunnel returned an invalid "
                                "hostname")
                        if (
                                metrics_candidate is not None
                                and metrics_candidate != public_url):
                            raise IngressError(
                                "cloudflared /quicktunnel hostname changed")
                        metrics_candidate = public_url
        except IngressError:
            raise
        except (
            OSError,
            TimeoutError,
            urllib.error.HTTPError,
            urllib.error.URLError,
        ):
            # The loopback metrics listener often appears a moment after the
            # process starts. Older version-pinned clients can fall back to the
            # same strict hostname embedded in their structured log message.
            pass

        urls = collector.snapshot_urls()
        if len(urls) > 1:
            raise IngressError(
                "cloudflared advertised multiple quick-tunnel URLs")
        if len(urls) == 1:
            found = next(iter(urls))
            if metrics_candidate is not None:
                if metrics_candidate != found:
                    raise IngressError(
                        "cloudflared /quicktunnel hostname does not match "
                        "the retained child log")
                return found
            if fallback_candidate != found:
                fallback_candidate = found
                fallback_at = time.monotonic()
            elif time.monotonic() - fallback_at >= fallback_delay:
                return fallback_candidate
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise IngressError(
                "timed out waiting for cloudflared quick-tunnel URL")
        stop_event.wait(min(interval, remaining))


def _probe_public_gateway(
        public_url, collector, gateway, process, stop_event, opener, *,
        deadline, interval, request_timeout):
    request = _gateway_readiness_request(gateway, public_url)
    last_error = None
    while True:
        _check_health(process, gateway, stop_event)
        _require_collector_healthy(collector, process)
        _require_advertised_url(collector, public_url)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            detail = f": {last_error}" if last_error else ""
            raise IngressError(
                f"public hook gateway readiness timed out{detail}")
        try:
            with opener(
                    request,
                    timeout=min(request_timeout, remaining)) as response:
                status = getattr(response, "status", None)
                if status is None and callable(getattr(response, "getcode", None)):
                    status = response.getcode()
                final_url = request.full_url
                if callable(getattr(response, "geturl", None)):
                    final_url = response.geturl()
                if status == 204 and final_url == request.full_url:
                    return
                last_error = (
                    f"HTTP {status}" if final_url == request.full_url
                    else "redirected response"
                )
        except (
            OSError,
            TimeoutError,
            urllib.error.HTTPError,
            urllib.error.URLError,
        ) as error:
            last_error = str(error)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            detail = f": {last_error}" if last_error else ""
            raise IngressError(
                f"public hook gateway readiness timed out{detail}")
        stop_event.wait(min(interval, remaining))


def _terminate_child(process, timeout):
    if process is None:
        return
    errors = []
    try:
        returncode = process.poll()
    except OSError as error:
        errors.append(error)
        returncode = None
    if returncode is not None:
        return
    try:
        process.terminate()
    except ProcessLookupError:
        return
    except OSError as error:
        errors.append(error)
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except ProcessLookupError:
            return
        except OSError as error:
            errors.append(error)
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as error:
            errors.append(error)
        except OSError as error:
            errors.append(error)
    except OSError as error:
        errors.append(error)
    if errors:
        raise errors[0]


def _close_fd(fd):
    if fd is None:
        return
    try:
        os.close(fd)
    except OSError:
        pass


def _stop_watchdog(process, timeout):
    """Give parent-pipe EOF a chance, then stop the retained watchdog."""
    if process is None:
        return
    try:
        process.wait(timeout=timeout)
        return
    except subprocess.TimeoutExpired:
        _terminate_child(process, timeout)


def _require_advertised_url(collector, public_url):
    advertised = collector.snapshot_urls()
    if advertised != {public_url}:
        raise IngressError(
            "cloudflared child URL drifted from the published ingress URL")


def _cloudflared_argv(
        executable, config_path, metrics_port):
    return [
        executable,
        "tunnel",
        "--config", config_path,
        "--no-autoupdate",
        "--loglevel", "info",
        "--output", "json",
        "--metrics", f"127.0.0.1:{metrics_port}",
        # Quick Tunnel dispatch requires --url or --hello-world even when an
        # explicit config ingress rule supplies the real service. The private
        # config wins during ingress parsing, so the built-in server is never
        # the published origin.
        "--hello-world",
    ]


def _watchdog_argv(parent_fd, child_argv, shutdown_timeout):
    watchdog_path = os.path.realpath(os.path.join(
        os.path.dirname(__file__), "ingress_watchdog.py"))
    if not os.path.isfile(watchdog_path):
        raise IngressError("public ingress watchdog is missing")
    return [
        os.path.realpath(sys.executable),
        watchdog_path,
        "--parent-fd", str(parent_fd),
        "--shutdown-timeout", str(float(shutdown_timeout)),
        "--",
        *child_argv,
    ]


def run_ingress(
    *,
    stop_event=None,
    cloudflared_path=None,
    morphdb_origin=None,
    app=None,
    gateway_factory=None,
    preflight=None,
    on_ready=None,
    on_stopping=None,
    popen_factory=subprocess.Popen,
    opener=_open_public_url,
    metrics_opener=_open_loopback_url,
    runtime_dir=None,
    config_path=None,
    metrics_port_factory=_reserve_loopback_port,
    startup_timeout=DEFAULT_STARTUP_TIMEOUT,
    readiness_timeout=DEFAULT_STARTUP_TIMEOUT,
    readiness_request_timeout=DEFAULT_READINESS_TIMEOUT,
    readiness_interval=DEFAULT_READINESS_INTERVAL,
    shutdown_timeout=DEFAULT_SHUTDOWN_TIMEOUT,
    install_signal_handlers=True,
):
    """Run one public hook ingress in the foreground.

    The caller owns the app-scoped lifetime lease and durable state.  ``on_ready``
    is invoked as ``on_ready(public_url)`` only after the external
    readiness probe succeeds and the temporary probe route is disabled.
    """
    for name, value in (
        ("startup_timeout", startup_timeout),
        ("readiness_timeout", readiness_timeout),
        ("readiness_request_timeout", readiness_request_timeout),
        ("readiness_interval", readiness_interval),
        ("shutdown_timeout", shutdown_timeout),
    ):
        if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or value <= 0):
            raise ValueError(f"{name} must be positive")

    stop_event = stop_event or threading.Event()
    gateway_factory = gateway_factory or _default_gateway_factory
    on_ready = on_ready or (lambda _url: None)
    on_stopping = on_stopping or (lambda: None)
    runtime_dir = runtime_dir or config.runtime_state_dir("ingress")
    morphdb_origin = (
        config.morphdb_base() if morphdb_origin is None else morphdb_origin)
    app = config.current_app() if app is None else app
    from . import ingress_state
    morphdb_origin, app = ingress_state.canonical_scope(
        morphdb_origin, app)
    if preflight is None:
        preflight = lambda: _default_preflight(
            morphdb_origin=morphdb_origin, app=app)

    executable = find_cloudflared(cloudflared_path)
    requested_config_path = config_path
    config_path = None

    process = None
    gateway = None
    collector = None
    watchdog_write_fd = None
    old_handlers = {}
    public_url = None
    try:
        if install_signal_handlers:
            old_handlers = _install_signal_handlers(stop_event)

        # The caller has acquired the app/backend lifetime lease before entry.
        # Do the potentially mutating schema reconciliation inside that lease.
        preflight()
        if stop_event.is_set():
            raise IngressStopped("public ingress stopped before startup")

        readiness_secret = secrets.token_urlsafe(32)
        gateway_socket_path = _new_gateway_socket_path(runtime_dir)
        try:
            gateway = gateway_factory(
                readiness_secret=readiness_secret,
                unix_socket_path=gateway_socket_path,
            )
        except TypeError as error:
            raise IngressError(
                "hook gateway factory does not accept a Unix socket origin"
            ) from error
        _gateway_enable_readiness(gateway)
        _gateway_start(gateway)
        gateway_origin = _gateway_origin_url(gateway)
        config_path = write_cloudflared_config(
            runtime_dir,
            morphdb_origin=morphdb_origin,
            app=app,
            gateway_origin=gateway_origin,
            config_path=requested_config_path,
        )

        try:
            metrics_port = metrics_port_factory()
            if isinstance(metrics_port, bool):
                raise ValueError
            metrics_port = int(metrics_port)
        except (TypeError, ValueError, OverflowError) as error:
            raise IngressError("cloudflared metrics port is invalid") from error
        if metrics_port < 1 or metrics_port > 65535:
            raise IngressError("cloudflared metrics port is invalid")
        child_argv = _cloudflared_argv(
            executable,
            config_path,
            metrics_port,
        )
        watchdog_read_fd = None
        try:
            watchdog_read_fd, watchdog_write_fd = os.pipe()
            os.set_inheritable(watchdog_read_fd, False)
            os.set_inheritable(watchdog_write_fd, False)
            argv = _watchdog_argv(
                watchdog_read_fd,
                child_argv,
                float(shutdown_timeout),
            )
            process = popen_factory(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                shell=False,
                close_fds=True,
                pass_fds=(watchdog_read_fd,),
                env=_safe_child_env(runtime_dir),
                cwd=os.path.realpath(runtime_dir),
                bufsize=0,
            )
        except (OSError, ValueError) as error:
            raise IngressError(
                f"could not start cloudflared: {error}") from error
        finally:
            _close_fd(watchdog_read_fd)
        if process.stdout is None:
            raise IngressError("cloudflared log pipe was not created")
        collector = CloudflaredLogCollector(process.stdout)
        collector.start()

        public_url = _await_public_url(
            collector,
            process,
            gateway,
            stop_event,
            metrics_port=metrics_port,
            metrics_opener=metrics_opener,
            timeout=float(startup_timeout),
            interval=float(readiness_interval),
            request_timeout=float(readiness_request_timeout),
        )
        _probe_public_gateway(
            public_url,
            collector,
            gateway,
            process,
            stop_event,
            opener,
            deadline=time.monotonic() + float(readiness_timeout),
            interval=float(readiness_interval),
            request_timeout=float(readiness_request_timeout),
        )
        gateway.disable_readiness()
        _check_health(process, gateway, stop_event)
        if stop_event.is_set():
            raise IngressStopped(
                "public ingress stopped before publication")
        on_ready(public_url)
        if stop_event.is_set():
            return public_url

        while not stop_event.wait(float(readiness_interval)):
            _check_health(process, gateway, threading.Event())
            _require_collector_healthy(collector, process)
            _require_advertised_url(collector, public_url)
        return public_url
    finally:
        had_primary_error = sys.exc_info()[0] is not None
        cleanup_errors = []

        def cleanup(step):
            try:
                step()
            except Exception as error:
                cleanup_errors.append(error)

        cleanup(on_stopping)
        if watchdog_write_fd is not None:
            cleanup(lambda: _close_fd(watchdog_write_fd))
            watchdog_write_fd = None
        if process is not None:
            cleanup(lambda: _stop_watchdog(
                process, float(shutdown_timeout)))
            if process.stdout is not None:
                cleanup(process.stdout.close)
        if collector is not None:
            cleanup(lambda: collector.join(
                timeout=float(shutdown_timeout)))
        if gateway is not None:
            cleanup(lambda: _gateway_close(gateway))
        if config_path is not None:
            cleanup(lambda: remove_cloudflared_config(
                config_path, runtime_dir))
        if old_handlers:
            cleanup(lambda: _restore_signal_handlers(old_handlers))
        if cleanup_errors and not had_primary_error:
            raise IngressError(
                f"public ingress cleanup failed: {cleanup_errors[0]}"
            ) from cleanup_errors[0]
