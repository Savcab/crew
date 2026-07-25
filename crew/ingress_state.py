"""Kernel-owned lifetime lease and public state for Crew webhook ingress.

The lock, not a PID or the presence of a JSON file, is the source of truth.
Each canonical MorphDB-origin/app pair gets one stable lock inode and one
atomically replaced state file in Crew's private per-UID runtime directory.
"""

import fcntl
import hashlib
import ipaddress
import json
import math
import os
import re
import secrets
import stat
import time
import urllib.parse

from . import config


STATE_VERSION = 1
MAX_STATE_BYTES = 4096
MAX_ORIGIN_CHARS = 2048
MAX_APP_BYTES = 255
_QUICK_TUNNEL_SUFFIX = ".trycloudflare.com"
_DNS_LABEL = re.compile(
    r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_BASE_PATH_SEGMENT = re.compile(
    r"^[A-Za-z0-9._~!$&'()*+,;=:@-]+$")
_LEASE_ID = re.compile(r"^[0-9a-f]{32}$")

_DEFAULT_RUNTIME_DIR = os.path.join(
    config.RUNTIME_STATE_ROOT, "webhook-ingress")
# Explicit in-process hook for isolated process tests. Production never reads
# this path from mutable environment.
_RUNTIME_DIR = _DEFAULT_RUNTIME_DIR


class IngressStateError(OSError):
    """Ingress lease or rendezvous state could not be handled safely."""


class IngressAlreadyRunning(IngressStateError):
    """The exact MorphDB-origin/app scope already has a live lease owner."""


class _ScopePaths:
    __slots__ = (
        "state_dir", "lock_name", "state_name", "config_name",
        "lock_path", "state_path", "config_path",
    )

    def __init__(self, state_dir, digest):
        self.state_dir = state_dir
        self.lock_name = f"{digest}.lock"
        self.state_name = f"{digest}.json"
        self.config_name = f"{digest}.cf.json"
        self.lock_path = os.path.join(state_dir, self.lock_name)
        self.state_path = os.path.join(state_dir, self.state_name)
        self.config_path = os.path.join(state_dir, self.config_name)


def _contains_unsafe_url_character(value):
    return ("\\" in value or any(
        ord(character) <= 0x20 or ord(character) == 0x7f
        for character in value))


def _canonical_hostname(hostname):
    if not hostname or "%" in hostname:
        raise ValueError("MorphDB origin requires a valid hostname")
    if ":" in hostname:
        try:
            return f"[{ipaddress.IPv6Address(hostname).compressed}]"
        except ValueError as error:
            raise ValueError("MorphDB origin has an invalid IPv6 hostname") \
                from error
    try:
        ascii_hostname = hostname.encode("idna").decode("ascii").lower()
    except (UnicodeError, ValueError) as error:
        raise ValueError("MorphDB origin has an invalid hostname") from error
    if len(ascii_hostname) > 253 or ascii_hostname.endswith("."):
        raise ValueError("MorphDB origin has an invalid hostname")
    labels = ascii_hostname.split(".")
    if any(not _DNS_LABEL.fullmatch(label) for label in labels):
        raise ValueError("MorphDB origin has an invalid hostname")
    return ascii_hostname


def _canonical_origin(value):
    if not isinstance(value, str):
        raise ValueError("MorphDB origin must be a URL string")
    if not value or value != value.strip() or len(value) > MAX_ORIGIN_CHARS:
        raise ValueError("MorphDB origin must be a bounded absolute URL")
    if _contains_unsafe_url_character(value):
        raise ValueError("MorphDB origin contains unsafe URL characters")
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise ValueError("MorphDB origin is not a valid URL") from error
    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        raise ValueError("MorphDB origin must use http or https")
    if (not parsed.netloc or parsed.username is not None
            or parsed.password is not None):
        raise ValueError("MorphDB origin cannot contain credentials")
    if parsed.query or parsed.fragment:
        raise ValueError(
            "MorphDB base URL cannot contain a query or fragment")
    hostname = _canonical_hostname(parsed.hostname)
    default_port = 80 if scheme == "http" else 443
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("MorphDB origin port is outside 1..65535")
    authority = hostname if port in (None, default_port) \
        else f"{hostname}:{port}"
    path = parsed.path
    if path in ("", "/"):
        path = ""
    else:
        # MorphDB's hosted base includes an API Gateway stage (for example
        # ``/live``). Preserve such paths as part of the backend identity, but
        # reject alternate spellings that could alias the same backend.
        if "%" in path:
            raise ValueError(
                "MorphDB base URL path cannot contain percent escapes")
        if path.endswith("/"):
            path = path[:-1]
        segments = path[1:].split("/") if path.startswith("/") else []
        if (
                not segments
                or any(
                    segment in ("", ".", "..")
                    or not _BASE_PATH_SEGMENT.fullmatch(segment)
                    for segment in segments)):
            raise ValueError(
                "MorphDB base URL contains an unsafe path")
        path = "/" + "/".join(segments)
    return f"{scheme}://{authority}{path}"


def _validate_app(value):
    if not isinstance(value, str):
        raise ValueError("Crew app must be a string")
    if not value or value != value.strip():
        raise ValueError("Crew app must be non-empty without outer whitespace")
    if any(ord(character) < 0x20 or ord(character) == 0x7f
           for character in value):
        raise ValueError("Crew app contains control characters")
    try:
        encoded = value.encode("utf-8")
    except UnicodeError as error:
        raise ValueError("Crew app is not valid UTF-8") from error
    if len(encoded) > MAX_APP_BYTES:
        raise ValueError(
            f"Crew app must be at most {MAX_APP_BYTES} UTF-8 bytes")
    return value


def canonical_scope(origin=None, app=None):
    """Return the canonical MorphDB origin and exact Crew app key."""
    selected_origin = config.morphdb_base() if origin is None else origin
    selected_app = config.current_app() if app is None else app
    return _canonical_origin(selected_origin), _validate_app(selected_app)


def validate_public_base_url(value):
    """Validate and canonicalize one Cloudflare Quick Tunnel base URL."""
    if not isinstance(value, str):
        raise ValueError("public ingress base URL must be a string")
    if not value or value != value.strip() or len(value) > 512:
        raise ValueError("public ingress base URL must be a bounded URL")
    if _contains_unsafe_url_character(value):
        raise ValueError("public ingress base URL contains unsafe characters")
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise ValueError("public ingress base URL is invalid") from error
    if (parsed.scheme.lower() != "https" or not parsed.netloc
            or parsed.username is not None or parsed.password is not None
            or port is not None):
        raise ValueError(
            "public ingress must be an uncredentialed https Quick Tunnel URL")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ValueError(
            "public ingress base URL cannot contain a path, query, or fragment")
    hostname = (parsed.hostname or "").lower()
    if not hostname.endswith(_QUICK_TUNNEL_SUFFIX):
        raise ValueError("public ingress must use trycloudflare.com")
    label = hostname[:-len(_QUICK_TUNNEL_SUFFIX)]
    if "." in label or not _DNS_LABEL.fullmatch(label):
        raise ValueError(
            "public ingress must use one DNS label below trycloudflare.com")
    return f"https://{label}{_QUICK_TUNNEL_SUFFIX}"


def _scope_paths(origin, app):
    canonical_origin, exact_app = canonical_scope(origin, app)
    identity = (
        f"crew-public-webhook-ingress-v1\0{canonical_origin}\0{exact_app}"
    ).encode("utf-8")
    digest = hashlib.sha256(identity).hexdigest()
    return _ScopePaths(os.path.abspath(_RUNTIME_DIR), digest)


def _secure_runtime_directory():
    try:
        if os.path.abspath(_RUNTIME_DIR) == os.path.abspath(
                _DEFAULT_RUNTIME_DIR):
            directory = config.runtime_state_dir("webhook-ingress")
        else:
            directory = config.ensure_private_directory(_RUNTIME_DIR)
        flags = os.O_RDONLY
        flags |= getattr(os, "O_DIRECTORY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_CLOEXEC", 0)
        fd = os.open(directory, flags)
        os.set_inheritable(fd, False)
        info = os.fstat(fd)
        uid = getattr(os, "getuid", lambda: info.st_uid)()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != uid
                or stat.S_IMODE(info.st_mode) != 0o700):
            raise PermissionError(
                "ingress runtime directory must be owner-controlled and 0700")
        return directory, fd
    except OSError as error:
        raise IngressStateError(
            f"could not secure ingress runtime directory: {error}") from error


def _open_lock(directory_fd, name):
    flags = os.O_CREAT | os.O_RDWR
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(name, flags, 0o600, dir_fd=directory_fd)
        os.set_inheritable(fd, False)
        info = os.fstat(fd)
        uid = getattr(os, "getuid", lambda: info.st_uid)()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != uid
                or info.st_nlink != 1):
            raise PermissionError(
                "ingress lock must be an owner-controlled regular file")
        os.fchmod(fd, 0o600)
        return fd
    except OSError as error:
        if "fd" in locals():
            os.close(fd)
        raise IngressStateError(
            f"could not safely open ingress lock: {error}") from error


def _state_target_info(directory_fd, name):
    try:
        info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    uid = getattr(os, "getuid", lambda: info.st_uid)()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != uid
            or info.st_nlink != 1):
        raise IngressStateError(
            "ingress state must be an owner-controlled regular file")
    return info


def _atomic_write_json(directory_fd, name, value):
    raw = (json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
    ) + "\n").encode("utf-8")
    if len(raw) > MAX_STATE_BYTES:
        raise IngressStateError("ingress state exceeds its size limit")
    temporary = (
        f".{name}.{os.getpid()}.{secrets.token_hex(8)}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    fd = None
    try:
        fd = os.open(temporary, flags, 0o600, dir_fd=directory_fd)
        os.set_inheritable(fd, False)
        os.fchmod(fd, 0o600)
        view = memoryview(raw)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short write while publishing ingress state")
            view = view[written:]
        os.fsync(fd)
        os.close(fd)
        fd = None
        _state_target_info(directory_fd, name)
        os.replace(
            temporary, name,
            src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        try:
            os.fsync(directory_fd)
        except OSError:
            pass
    except OSError as error:
        raise IngressStateError(
            f"could not atomically publish ingress state: {error}") from error
    finally:
        if fd is not None:
            os.close(fd)
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError:
            pass


def _read_json(directory_fd, name):
    flags = os.O_RDONLY
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(name, flags, dir_fd=directory_fd)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise IngressStateError(
            f"could not safely open ingress state: {error}") from error
    try:
        os.set_inheritable(fd, False)
        info = os.fstat(fd)
        uid = getattr(os, "getuid", lambda: info.st_uid)()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != uid
                or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_size > MAX_STATE_BYTES):
            raise IngressStateError(
                "ingress state must be an owner-controlled bounded 0600 file")
        chunks = []
        remaining = MAX_STATE_BYTES + 1
        while remaining:
            chunk = os.read(fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > MAX_STATE_BYTES:
            raise IngressStateError("ingress state exceeds its size limit")
    finally:
        os.close(fd)
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise IngressStateError("ingress state is not valid JSON") from error


def _unlink_stale_state(directory_fd, name):
    try:
        info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    # Unlinking a non-directory name through the already-open private directory
    # is safe even if an owner process replaced it with a symlink: the link
    # itself is removed and never followed.
    if stat.S_ISDIR(info.st_mode):
        raise IngressStateError(
            "refusing directory at ingress state file location")
    try:
        os.unlink(name, dir_fd=directory_fd)
        try:
            os.fsync(directory_fd)
        except OSError:
            pass
        return True
    except FileNotFoundError:
        # Compatible shared readers may race to remove the same stale file.
        return False
    except OSError as error:
        raise IngressStateError(
            f"could not clear stale ingress state: {error}") from error


def _validated_state(value, origin, app):
    if (not isinstance(value, dict)
            or type(value.get("version")) is not int
            or value.get("version") != STATE_VERSION):
        return None
    if value.get("origin") != origin or value.get("app") != app:
        return None
    try:
        public_url = validate_public_base_url(value.get("public_base_url"))
    except ValueError:
        return None
    if public_url != value.get("public_base_url"):
        return None
    pid = value.get("pid")
    started_at = value.get("started_at")
    published_at = value.get("published_at")
    lease_id = value.get("lease_id")
    if (type(pid) is not int or pid <= 0
            or type(started_at) not in (int, float)
            or type(published_at) not in (int, float)
            or not math.isfinite(started_at) or started_at <= 0
            or not math.isfinite(published_at) or published_at <= 0
            or not isinstance(lease_id, str)
            or not _LEASE_ID.fullmatch(lease_id)):
        return None
    return dict(value)


class IngressLease:
    """One non-inheritable lifetime lock for a canonical ingress scope."""

    __slots__ = (
        "origin", "app", "state_dir", "lock_path", "state_path",
        "config_path", "_paths", "_directory_fd", "_lock_fd", "_lease_id",
        "_started_at", "_closed",
    )

    def __init__(
            self, origin, app, paths, directory_fd, lock_fd):
        self.origin = origin
        self.app = app
        self.state_dir = paths.state_dir
        self.lock_path = paths.lock_path
        self.state_path = paths.state_path
        self._paths = paths
        self._directory_fd = directory_fd
        self._lock_fd = lock_fd
        self._lease_id = secrets.token_hex(16)
        # Config embeds the per-run Unix origin. It must never be rewritten by
        # a later lease while a hard-orphaned tunnel might still retain it.
        self.config_path = os.path.join(
            paths.state_dir,
            f"{paths.lock_name[:16]}-{self._lease_id}.cf.json",
        )
        self._started_at = time.time()
        self._closed = False

    def _require_open(self):
        if self._closed:
            raise IngressStateError("ingress lease is already closed")

    def publish(self, public_base_url):
        """Atomically publish validated state while retaining the lifetime lock."""
        self._require_open()
        public_url = validate_public_base_url(public_base_url)
        state = {
            "version": STATE_VERSION,
            "origin": self.origin,
            "app": self.app,
            "public_base_url": public_url,
            # Informational only. Liveness is always established with flock;
            # no reader or cleanup path signals this PID.
            "pid": os.getpid(),
            "started_at": self._started_at,
            "published_at": time.time(),
            "lease_id": self._lease_id,
        }
        _atomic_write_json(
            self._directory_fd, self._paths.state_name, state)
        return dict(state)

    def clear(self):
        """Remove only state published by this exact lease."""
        self._require_open()
        value = _read_json(self._directory_fd, self._paths.state_name)
        if value is None:
            return False
        validated = _validated_state(value, self.origin, self.app)
        if (validated is None
                or validated.get("lease_id") != self._lease_id):
            return False
        try:
            os.unlink(self._paths.state_name, dir_fd=self._directory_fd)
            try:
                os.fsync(self._directory_fd)
            except OSError:
                pass
            return True
        except FileNotFoundError:
            return False
        except OSError as error:
            raise IngressStateError(
                f"could not clear ingress state: {error}") from error

    def close(self):
        """Clear exact state, then release the kernel-owned lifetime lease."""
        if self._closed:
            return
        error = None
        try:
            self.clear()
        except OSError as caught:
            error = caught
        try:
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
        except OSError as caught:
            if error is None:
                error = IngressStateError(
                    f"could not release ingress lease: {caught}")
        finally:
            os.close(self._lock_fd)
            os.close(self._directory_fd)
            self._closed = True
        if error is not None:
            raise error

    def __enter__(self):
        self._require_open()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
        return False


def acquire_lease(origin=None, app=None):
    """Acquire the exact scope's non-blocking lifetime lease.

    A shared lock first proves there is no live exclusive owner while stale
    state is removed. Only then is it upgraded to the exclusive lifetime lock.
    This ordering prevents a reader from attributing a predecessor's stale
    JSON to a new owner that has not published yet.
    """
    canonical_origin, exact_app = canonical_scope(origin, app)
    paths = _scope_paths(canonical_origin, exact_app)
    state_dir, directory_fd = _secure_runtime_directory()
    # The default/test directory can be normalized by the security helper.
    paths = _ScopePaths(state_dir, os.path.basename(paths.lock_name)[:-5])
    lock_fd = None
    locked = False
    try:
        lock_fd = _open_lock(directory_fd, paths.lock_name)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            locked = True
        except BlockingIOError as error:
            raise IngressAlreadyRunning(
                f"public ingress is already running for "
                f"{canonical_origin} app {exact_app!r}") from error
        _unlink_stale_state(directory_fd, paths.state_name)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise IngressAlreadyRunning(
                f"public ingress is already starting or running for "
                f"{canonical_origin} app {exact_app!r}") from error
        return IngressLease(
            canonical_origin, exact_app, paths, directory_fd, lock_fd)
    except Exception:
        if lock_fd is not None:
            if locked:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                except OSError:
                    pass
            os.close(lock_fd)
        os.close(directory_fd)
        raise


def read_active_state(origin=None, app=None):
    """Return validated live ingress state, or ``None``.

    A JSON file is never liveness evidence. Readers take a non-blocking shared
    lock. Success proves that no exclusive lifetime owner exists, so they clear
    stale/crashed state and return ``None``. Contention can only be the
    foreground owner's exclusive lock (other readers are compatible), so only
    then may the atomically published, exact-scope state be returned.
    """
    canonical_origin, exact_app = canonical_scope(origin, app)
    paths = _scope_paths(canonical_origin, exact_app)
    state_dir, directory_fd = _secure_runtime_directory()
    paths = _ScopePaths(state_dir, os.path.basename(paths.lock_name)[:-5])
    lock_fd = None
    shared_locked = False
    try:
        lock_fd = _open_lock(directory_fd, paths.lock_name)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            shared_locked = True
        except BlockingIOError:
            try:
                value = _read_json(directory_fd, paths.state_name)
            except IngressStateError:
                return None
            return _validated_state(
                value, canonical_origin, exact_app)
        _unlink_stale_state(directory_fd, paths.state_name)
        return None
    finally:
        if lock_fd is not None:
            if shared_locked:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                except OSError:
                    pass
            os.close(lock_fd)
        os.close(directory_fd)
