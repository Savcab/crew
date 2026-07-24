"""crew.config — one place for the knobs every other module reads.

Two servers are in play and they MUST NOT collide:
  * MorphDB  — the data backend (agents + edges live here). Default 127.0.0.1:8787.
  * crew     — this project's dashboard + API.            Default 127.0.0.1:8788.

Everything is overridable by env so a second instance / a test run can move off
the live ports without touching code.
"""
import contextlib
import fcntl
import json
import os
import pwd
import re
import stat
import sys
import tempfile
import threading
import time

# --- paths ------------------------------------------------------------------ #
# The repo root (two dirs up from this file: crew/config.py -> crew/ -> repo/)
# and its var/ scratch dir (dashboard pid/log, project registry). Defined here
# (not cli.py) so any module can reach them without importing the CLI.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VAR = os.path.join(ROOT, "var")

# Agent runtimes may be sandboxed to their own home plus /tmp, so shared
# cross-process locks cannot live under the repository's var/ directory. The
# real /tmp parent is sticky and the per-UID root below is private (0700),
# giving every Crew CLI process for this OS user one sandbox-writable rendezvous
# without exposing lock contents to other users.
RUNTIME_STATE_ROOT = os.path.join(
    os.path.realpath("/tmp"),
    f"crew-{getattr(os, 'getuid', lambda: 0)()}-runtime",
)


def ensure_private_directory(path):
    """Create/validate one owner-only real directory and return its path.

    This deliberately rejects a symlink at the final component. Callers use a
    trusted/sticky parent (the runtime root above) or an explicit test fixture.
    Existing owner directories are tightened to 0700 before use.
    """
    path = os.path.abspath(path)
    try:
        os.makedirs(path, mode=0o700, exist_ok=True)
        info = os.lstat(path)
        uid = getattr(os, "getuid", lambda: info.st_uid)()
        if stat.S_ISLNK(info.st_mode):
            raise PermissionError(f"private runtime directory is a symlink: {path}")
        if not stat.S_ISDIR(info.st_mode):
            raise NotADirectoryError(
                f"private runtime path is not a directory: {path}")
        if info.st_uid != uid:
            raise PermissionError(
                f"private runtime directory is owned by uid {info.st_uid}, "
                f"expected {uid}: {path}")
        if stat.S_IMODE(info.st_mode) != 0o700:
            os.chmod(path, 0o700)
            tightened = os.lstat(path)
            if (stat.S_ISLNK(tightened.st_mode)
                    or tightened.st_uid != uid
                    or stat.S_IMODE(tightened.st_mode) != 0o700):
                raise PermissionError(
                    f"could not secure private runtime directory: {path}")
    except OSError as error:
        raise OSError(f"unsafe or unwritable private runtime directory {path!r}: "
                      f"{error}") from error
    return path


def runtime_state_dir(name):
    """Return a validated owner-only subdirectory of the shared runtime root."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", str(name or "")):
        raise OSError(f"invalid Crew runtime directory name: {name!r}")
    ensure_private_directory(RUNTIME_STATE_ROOT)
    return ensure_private_directory(os.path.join(RUNTIME_STATE_ROOT, name))


# Every Crew-owned tmux command uses an explicit named server. This prevents an
# inherited $TMUX from redirecting lifecycle or delivery operations into a
# user's unrelated server. On macOS the socket root sits under the Unix-socket
# tree allowed by Evergreen's SRT policy; other platforms use Crew's private
# runtime state. A fixed owner-only config may override that root; mutable
# process environment never selects the control-plane endpoint.
TMUX_ENDPOINT_CREW = "crew"
TMUX_ENDPOINT_LEGACY = "legacy"
CREW_TMUX_SOCKET_NAME = "crew"
TMUX_SOCKET_PATH_MAX = 100
_OWNER_HOME = pwd.getpwuid(getattr(os, "getuid", lambda: 0)()).pw_dir
TMUX_CONFIG_FILE = os.path.join(_OWNER_HOME, ".config", "crew", "tmux-root")
# Explicit in-process hook for isolated tests. Never populated from process
# environment: managed agents can freely replace their own env before invoking
# Crew, so an env-selected control-plane socket would be a routing vulnerability.
_TMUX_TMPDIR_TEST_OVERRIDE = None


class TmuxTarget(str):
    """String-compatible session/pane target carrying its server endpoint."""

    def __new__(cls, value, endpoint=TMUX_ENDPOINT_CREW):
        obj = super().__new__(cls, str(value))
        obj.endpoint = endpoint
        return obj


def tmux_target(value, endpoint=TMUX_ENDPOINT_CREW):
    if endpoint not in (TMUX_ENDPOINT_CREW, TMUX_ENDPOINT_LEGACY):
        raise OSError(f"invalid tmux endpoint: {endpoint!r}")
    if isinstance(value, TmuxTarget) and value.endpoint == endpoint:
        return value
    return TmuxTarget(value, endpoint)


def tmux_target_endpoint(*values, default=TMUX_ENDPOINT_CREW):
    endpoints = {
        value.endpoint for value in values if isinstance(value, TmuxTarget)
    }
    if len(endpoints) > 1:
        raise OSError("tmux command mixes targets from different servers")
    return next(iter(endpoints), default)


def tmux_target_key(value, default=TMUX_ENDPOINT_CREW):
    """Endpoint-aware identity for a session or pane target."""
    return (tmux_target_endpoint(value, default=default), str(value))


def same_tmux_target(left, right):
    return (left is not None and right is not None
            and tmux_target_key(left) == tmux_target_key(right))


def _configured_tmux_tmpdir():
    """Read the fixed operator-owned tmux-root config, if one exists."""
    if _TMUX_TMPDIR_TEST_OVERRIDE is not None:
        return str(_TMUX_TMPDIR_TEST_OVERRIDE)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(TMUX_CONFIG_FILE, flags)
    except FileNotFoundError:
        return ""
    except OSError as error:
        raise OSError(
            f"unsafe Crew tmux config {TMUX_CONFIG_FILE!r}: {error}") from error
    try:
        info = os.fstat(fd)
        uid = getattr(os, "getuid", lambda: info.st_uid)()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != uid
                or stat.S_IMODE(info.st_mode) & 0o077 or info.st_size > 4096):
            raise OSError(
                f"Crew tmux config must be an owner-only regular file: "
                f"{TMUX_CONFIG_FILE}")
        raw = os.read(fd, 4097)
    finally:
        os.close(fd)
    try:
        value = raw.decode("utf-8").strip()
    except UnicodeDecodeError as error:
        raise OSError("Crew tmux config is not valid UTF-8") from error
    if not value or "\n" in value or "\r" in value:
        raise OSError(
            "Crew tmux config must contain one absolute directory path")
    return value


def crew_tmux_tmpdir():
    """Return Crew's validated owner-only tmux socket root."""
    def secure(path):
        path = os.path.abspath(path)
        current = os.path.sep
        for component in path.split(os.path.sep)[1:]:
            current = os.path.join(current, component)
            try:
                info = os.lstat(current)
            except FileNotFoundError:
                break
            if stat.S_ISLNK(info.st_mode) and info.st_uid != 0:
                raise OSError(
                    f"Crew tmux directory traverses an untrusted symlink: "
                    f"{current}")
        result = ensure_private_directory(path)
        return result

    override = _configured_tmux_tmpdir()
    if override:
        path = override
        if not os.path.isabs(path):
            raise OSError("Crew tmux root must be an absolute path")
        return secure(path)
    if sys.platform == "darwin":
        uid = getattr(os, "getuid", lambda: 0)()
        path = os.path.join(
            os.path.realpath("/tmp"), "claude", f"crew-{uid}-tmux")
        return secure(path)
    return runtime_state_dir("tmux")


def crew_tmux_socket_path():
    """Validated bounded absolute socket used by Crew's named tmux server."""
    path = os.path.join(crew_tmux_tmpdir(), "crew.sock")
    if len(os.fsencode(path)) > TMUX_SOCKET_PATH_MAX:
        raise OSError(
            f"Crew tmux socket path is too long ({len(os.fsencode(path))} "
            f"bytes, max {TMUX_SOCKET_PATH_MAX}): {path}")
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return path
    uid = getattr(os, "getuid", lambda: info.st_uid)()
    if not stat.S_ISSOCK(info.st_mode):
        raise OSError(f"Crew tmux endpoint is not a Unix socket: {path}")
    if info.st_uid != uid or stat.S_IMODE(info.st_mode) & 0o077:
        raise OSError(
            f"Crew tmux socket must be owner-only and owned by uid {uid}: {path}")
    return path


def tmux_environment(endpoint=TMUX_ENDPOINT_CREW, environ=None):
    """Process environment for one explicit Crew or pre-upgrade tmux server."""
    if endpoint not in (TMUX_ENDPOINT_CREW, TMUX_ENDPOINT_LEGACY):
        raise OSError(f"invalid tmux endpoint: {endpoint!r}")
    result = dict(os.environ if environ is None else environ)
    # Nested-shell state is never authority for a Crew control-plane command.
    result.pop("TMUX", None)
    result.pop("TMUX_PANE", None)
    if endpoint == TMUX_ENDPOINT_CREW:
        result["TMUX_TMPDIR"] = crew_tmux_tmpdir()
    else:
        # Pre-upgrade Crew used tmux's system default endpoint.
        result.pop("TMUX_TMPDIR", None)
    return result


def tmux_command(*args, endpoint=TMUX_ENDPOINT_CREW, executable="tmux"):
    """Argv targeting one explicit server, independent of inherited $TMUX."""
    if endpoint == TMUX_ENDPOINT_CREW:
        return [executable, "-S", crew_tmux_socket_path(), *args]
    elif endpoint == TMUX_ENDPOINT_LEGACY:
        socket_name = "default"
    else:
        raise OSError(f"invalid tmux endpoint: {endpoint!r}")
    return [executable, "-L", socket_name, *args]

# --- MorphDB (the data backend) ------------------------------------------- #
# MORPHDB_HOST may be a full URL or a bare host[:port] (http:// assumed) — same
# rule the morphdb skill's own client uses, so a hosted MorphDB just works.
MORPHDB_HOST = os.environ.get("MORPHDB_HOST", "127.0.0.1:8787").strip()
DEFAULT_APP = "crew"

# --- projects --------------------------------------------------------------- #
# A "project" is an isolated slice of crew: its own MorphDB app (tenant) and
# (by default) its own subtree under crew_root(). The unnamed default project
# maps onto the original single-tenant "crew" app so existing installs/tests
# are unaffected.
DEFAULT_PROJECT = "default"


class ProjectRegistryError(ValueError):
    """The durable project registry cannot be read safely."""


def current_project():
    """The project we operate within. Read LIVE from $CREW_PROJECT on every call
    (not frozen at import), same rule as current_app()."""
    project = (os.environ.get("CREW_PROJECT") or DEFAULT_PROJECT).strip() or DEFAULT_PROJECT
    return require_project_name(project)


def project_app(project):
    """The MorphDB app key a project's data lives in."""
    project = require_project_name(project)
    return DEFAULT_APP if project == DEFAULT_PROJECT else f"crew-{project}"


def morphdb_base():
    return MORPHDB_HOST if "://" in MORPHDB_HOST else "http://" + MORPHDB_HOST


def current_app():
    """The MorphDB app key (the tenant) we read/write. Read LIVE from the env on
    every call (not frozen at import) so a test can point the whole stack at a
    throwaway app by setting $CREW_APP before exercising graphstore.

    Precedence: an explicit $CREW_APP always wins (tests rely on this pinning);
    otherwise the app is derived from the current project."""
    project = current_project()  # validate even when CREW_APP overrides the tenant
    override = os.environ.get("CREW_APP", "").strip()
    if override:
        return override
    return project_app(project)


def session_name(project, name):
    """The tmux session name for an agent: the plain name in the default
    project (unchanged behavior for existing installs), else prefixed with the
    project so two projects' agents can never collide on a session name."""
    project = require_project_name(project)
    return name if project == DEFAULT_PROJECT else f"{project}__{name}"


def crew_root():
    """Base directory new agents' homes are planned under by default. Read LIVE
    from $CREW_ROOT on every call, default ~/crew."""
    return os.path.expanduser(os.environ.get("CREW_ROOT", "~/crew"))


def _projects_file():
    return os.path.join(VAR, "projects.json")


_PROJECT_REGISTRY_THREAD_LOCK = threading.RLock()


@contextlib.contextmanager
def _project_registry_lock():
    """Serialize registry reads/writes through owner-only runtime state.

    Agent sandboxes can read the repository's durable ``projects.json`` but
    cannot create its historical sibling lock file.  Keep the data in ``VAR``
    while rendezvousing through Crew's private per-UID runtime directory.
    """
    with _PROJECT_REGISTRY_THREAD_LOCK:
        fd = None
        try:
            directory = runtime_state_dir("project-registry-locks")
            lock_name = "projects.json.lock"
            lock_path = os.path.join(directory, lock_name)
            directory_flags = os.O_RDONLY
            directory_flags |= getattr(os, "O_DIRECTORY", 0)
            directory_flags |= getattr(os, "O_NOFOLLOW", 0)
            directory_flags |= getattr(os, "O_CLOEXEC", 0)
            directory_fd = os.open(directory, directory_flags)
            try:
                file_flags = os.O_CREAT | os.O_RDWR
                file_flags |= getattr(os, "O_NOFOLLOW", 0)
                file_flags |= getattr(os, "O_CLOEXEC", 0)
                # APFS can transiently report ENOENT to one contender when
                # two processes race to create the same O_NOFOLLOW openat
                # target. Keep the secure flags and retry that creation race
                # in place; any other error (including a symlink/ELOOP) still
                # fails closed immediately.
                for attempt in range(3):
                    try:
                        fd = os.open(
                            lock_name, file_flags, 0o600,
                            dir_fd=directory_fd)
                        break
                    except FileNotFoundError:
                        if attempt == 2:
                            raise
            finally:
                os.close(directory_fd)
            info = os.fstat(fd)
            uid = getattr(os, "getuid", lambda: info.st_uid)()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != uid:
                raise PermissionError(
                    "project registry lock must be an owner-controlled "
                    f"regular file: {lock_path}")
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
        except OSError as error:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
                fd = None
            raise ProjectRegistryError(
                f"could not secure project registry lock: {error}") from error
        try:
            yield
        finally:
            if fd is not None:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except OSError:
                    pass
                try:
                    os.close(fd)
                except OSError:
                    pass


def _normalize_project_entry(item, path):
    """One registry entry → {'name','description'[,'created_at']}. Entries are
    plain names (legacy format) or dicts (apps-gallery metadata); anything
    else is corruption and fails closed."""
    if isinstance(item, str):
        name, description, created_at = item, "", None
    elif isinstance(item, dict):
        name = item.get("name")
        description = item.get("description") or ""
        created_at = item.get("created_at")
    else:
        raise ProjectRegistryError(
            f"project registry {path!r} is corrupt: invalid entry {item!r}")
    if not valid_project_name(name):
        raise ProjectRegistryError(
            f"project registry {path!r} is corrupt: invalid project "
            f"entry {name!r}")
    if not isinstance(description, str):
        raise ProjectRegistryError(
            f"project registry {path!r} is corrupt: non-string description "
            f"on {name!r}")
    title = item.get("title") if isinstance(item, dict) else ""
    if title is not None and not isinstance(title, str):
        raise ProjectRegistryError(
            f"project registry {path!r} is corrupt: non-string title "
            f"on {name!r}")
    entry = {"name": name, "description": description}
    if title:
        entry["title"] = title
    if created_at is not None:
        entry["created_at"] = created_at
    return entry


def _read_project_entries_unlocked():
    """Read validated entries (may include a meta-only entry for the default
    project); caller must hold the registry lock."""
    path = _projects_file()
    try:
        with open(path) as f:
            data = json.load(f)
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as error:
        raise ProjectRegistryError(
            f"project registry {path!r} is corrupt or unreadable: {error}") from error
    if not isinstance(data, list):
        raise ProjectRegistryError(
            f"project registry {path!r} is corrupt: expected a JSON list")
    entries, seen = [], set()
    for item in data:
        entry = _normalize_project_entry(item, path)
        if entry["name"] in seen:
            continue
        seen.add(entry["name"])
        entries.append(entry)
    return entries


def _read_project_names_unlocked():
    """Read validated non-default names; caller must hold the registry lock."""
    return [entry["name"] for entry in _read_project_entries_unlocked()
            if entry["name"] != DEFAULT_PROJECT]


def _write_project_entries_unlocked(entries):
    """Atomically replace the registry using a process-unique temporary file."""
    path = _projects_file()
    try:
        os.makedirs(VAR, mode=0o700, exist_ok=True)
    except OSError as error:
        raise ProjectRegistryError(
            f"project registry directory {VAR!r} is unavailable: {error}") \
            from error
    fd, tmp = tempfile.mkstemp(
        prefix=".projects-", suffix=".tmp", dir=VAR, text=True)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            fd = None
            json.dump(entries, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        os.chmod(path, 0o600)
        # Make the rename durable when the platform permits directory fsync.
        try:
            dir_fd = os.open(VAR, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
    finally:
        if fd is not None:
            os.close(fd)
        try:
            os.remove(tmp)
        except FileNotFoundError:
            pass


def list_known_projects():
    """Every registered project, failing closed on corrupt durable state."""
    with _project_registry_lock():
        return [DEFAULT_PROJECT] + _read_project_names_unlocked()


def register_project(name, description="", title=""):
    """Add `name` to the project registry (var/projects.json), idempotent.
    `description` is the apps-gallery blurb; `title` is the free-text display
    name (spaces welcome — `name` is the machine slug used for the app key,
    tmux session prefix, and paths)."""
    name = require_project_name(name)
    with _project_registry_lock():
        entries = _read_project_entries_unlocked()
        if name == DEFAULT_PROJECT or any(
                e["name"] == name for e in entries):
            return False
        entry = {
            "name": name,
            "description": str(description or ""),
            "created_at": time.time(),
        }
        if title:
            entry["title"] = str(title)
        entries.append(entry)
        _write_project_entries_unlocked(entries)
    return True


def unregister_project(name):
    """Remove one named project from the registry, idempotently."""
    name = require_project_name(name)
    if name == DEFAULT_PROJECT:
        return False
    with _project_registry_lock():
        entries = _read_project_entries_unlocked()
        if not any(e["name"] == name for e in entries):
            return False
        _write_project_entries_unlocked(
            [e for e in entries if e["name"] != name])
    return True


def project_descriptions():
    """{project name: description} for every known project, DEFAULT included
    (a registry entry for the default project is metadata-only)."""
    with _project_registry_lock():
        entries = _read_project_entries_unlocked()
    descriptions = {DEFAULT_PROJECT: ""}
    for entry in entries:
        descriptions[entry["name"]] = entry["description"]
    return descriptions


def project_titles():
    """{project name: display title} — only entries that have one."""
    with _project_registry_lock():
        entries = _read_project_entries_unlocked()
    return {e["name"]: e["title"] for e in entries if e.get("title")}


def set_project_description(name, description):
    """Update (or, for the default project, create) a registry entry's
    description. Returns False for an unregistered named project."""
    name = require_project_name(name)
    with _project_registry_lock():
        entries = _read_project_entries_unlocked()
        for entry in entries:
            if entry["name"] == name:
                entry["description"] = str(description or "")
                _write_project_entries_unlocked(entries)
                return True
        if name != DEFAULT_PROJECT:
            return False
        entries.append({"name": DEFAULT_PROJECT,
                        "description": str(description or "")})
        _write_project_entries_unlocked(entries)
    return True


# --- crew dashboard ------------------------------------------------------- #
DASHBOARD_HOST = "127.0.0.1"
try:
    DASHBOARD_PORT = int(os.environ.get("CREW_PORT", "8788"))
except ValueError:
    DASHBOARD_PORT = 8788

# The dashboard remains loopback-only even when webhook ingress is exposed.
# A TLS reverse proxy/tunnel can publish only /hooks/* and set this origin so
# the operator UI copies the externally reachable URL instead of localhost.
WEBHOOK_PUBLIC_BASE_URL = os.environ.get(
    "CREW_WEBHOOK_PUBLIC_BASE_URL", "").strip().rstrip("/")

# Which interactive coding-agent runtime a new agent uses when neither
# --runtime nor an inferable --launch-cmd is supplied.
DEFAULT_RUNTIME = (os.environ.get("CREW_RUNTIME", "claude").strip().lower()
                   or "claude")

# How a new Claude agent is launched into its tmux pane. CREW_LAUNCH_CMD is the
# legacy spelling and remains supported; the runtime-specific name wins.
#
# `--dangerously-skip-permissions` only skips the prompts; it does NOT sandbox, so
# the agent's `crew message` can still reach the tmux socket. (If a user turns on
# Claude's bash sandbox via settings — CLAUDE_CODE_SANDBOXED=1 — delivery breaks;
# crew.mail detects that and prints the fix.)
CLAUDE_LAUNCH_CMD = os.environ.get(
    "CREW_CLAUDE_LAUNCH_CMD",
    os.environ.get("CREW_LAUNCH_CMD", "claude --dangerously-skip-permissions"))

# Codex receives per-home trust and tool-shell environment policy in
# crew.runtime, where the concrete home is available. This base command intentionally runs
# unattended, matching Crew's historical Claude default.
CODEX_LAUNCH_CMD = os.environ.get(
    "CREW_CODEX_LAUNCH_CMD",
    "codex --dangerously-bypass-approvals-and-sandbox --disable hooks")

# Backward-compatible import used by older callers and external scripts.
LAUNCH_CMD = CLAUDE_LAUNCH_CMD

# --- operator notifications ------------------------------------------------ #
# Where crew.notify POSTs its events (agent died / needs input / handoff
# expired). Set $CREW_WEBHOOK_URL — e.g. https://ntfy.sh/<your-topic> for phone
# push — or hardcode a URL here. Empty → notifications are silently off.
WEBHOOK_URL = os.environ.get("CREW_WEBHOOK_URL", "").strip()

# The identity file written into every agent's home dir (see crew.identity).
IDENTITY_FILE = "identity.md"

# An agent name becomes a tmux session name, an agent-mail identity, and (often)
# a directory basename, so it must be a safe slug: no slashes, dots (tmux parses
# '.' as window.pane), spaces, or shell metacharacters. Max 64 chars.
# Must START alphanumeric (no leading '-'): the name lands in tmux/CLI argv, where a
# leading dash would be parsed as an option flag (e.g. an agent named '-d').
_AGENT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

# These strings are control-plane identities, not available agent handles.
# Keep the comparison case-insensitive so the UI cannot create a visually
# confusable operator/system identity that later gets normalized by a caller.
RESERVED_AGENT_NAMES = frozenset({"human", "unknown", "crew"})


def reserved_agent_name(name):
    return isinstance(name, str) and name.casefold() in RESERVED_AGENT_NAMES


def valid_agent_name(name):
    return (isinstance(name, str)
            and _AGENT_NAME_RE.match(name) is not None
            and not reserved_agent_name(name))


# Same character rules as an agent name (it becomes half of an app key /
# session prefix), capped shorter since it's a prefix, not the whole slug.
_PROJECT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")


def valid_project_name(name):
    return isinstance(name, str) and _PROJECT_NAME_RE.match(name) is not None


def require_project_name(name):
    """Return a valid project slug or raise before it reaches paths/app/session ids."""
    if not valid_project_name(name):
        raise ValueError(
            f"invalid project name {name!r}: letters, digits, '_', '-' only "
            "(no dots/slashes/spaces), max 32 chars, must start alphanumeric")
    return name


# --- containment limits (wave 2) -------------------------------------------- #
# Every knob here bounds what an AGENT actor (never a human) can do to the
# graph — see crew.guard. Frozen at import (same style as DASHBOARD_PORT/
# LAUNCH_CMD above), overridable via env for a second instance / a test run.
try:
    MAX_AGENTS = int(os.environ.get("CREW_MAX_AGENTS", "12"))
except ValueError:
    MAX_AGENTS = 12

# Agent-actor spawns/hr, ALL agent actors combined, per project/app — caps how
# fast a foreman (or a chain of foremen) can grow the crew unsupervised.
try:
    SPAWN_RATE = int(os.environ.get("CREW_SPAWN_RATE", "4"))
except ValueError:
    SPAWN_RATE = 4

# Public capability creation is available to the foreman, but remains bounded
# independently from runtime-agent spawns.  The limit is per owning foreman in
# one project/app; human-created hooks do not consume it.
try:
    MAX_WEBHOOKS_PER_FOREMAN = int(
        os.environ.get("CREW_MAX_WEBHOOKS_PER_FOREMAN", "12"))
except ValueError:
    MAX_WEBHOOKS_PER_FOREMAN = 12

# An agent-actor `connect` must set ALL THREE edge caps to a finite value no
# higher than these ceilings (crew.guard's FINITE-CAPS RULE) — an agent can
# never hand out an unlimited/uncapped edge, only a human can.
try:
    AGENT_EDGE_MAX_TURNS_CEILING = int(os.environ.get("AGENT_EDGE_MAX_TURNS_CEILING", "30"))
except ValueError:
    AGENT_EDGE_MAX_TURNS_CEILING = 30

try:
    AGENT_EDGE_TOKEN_CAP_CEILING = int(os.environ.get("AGENT_EDGE_TOKEN_CAP_CEILING", "500000"))
except ValueError:
    AGENT_EDGE_TOKEN_CAP_CEILING = 500000

try:
    AGENT_EDGE_COST_CAP_CEILING = float(os.environ.get("AGENT_EDGE_COST_CAP_CEILING", "5.0"))
except ValueError:
    AGENT_EDGE_COST_CAP_CEILING = 5.0


# --- transform edges (wave 5) ------------------------------------------------ #
# How long crew.mail.deliver() waits for an edge's transform script before
# treating it as a failure (message filtered — see crew.mail's docstring).
try:
    TRANSFORM_TIMEOUT = float(os.environ.get("CREW_TRANSFORM_TIMEOUT", "5.0"))
except ValueError:
    TRANSFORM_TIMEOUT = 5.0

# The ONLY directory a transform script may live in — crew.graphstore refuses
# to attach (create_edge/update_edge) a transform path outside it (realpath
# containment) or that doesn't exist as a file. Human-only to attach (see
# crew.guard), so this is a deliberate narrow surface, not a sandbox.
TRANSFORMS_DIR = os.path.join(VAR, "transforms")


# --- one-blob LLM expansion (UI wave B) ------------------------------------ #
# POST /api/expand turns a human's one-paragraph description into structured
# edge/agent fields by shelling out to a `claude -p --output-format json`-
# shaped command. The default argv; overridable as a single SHELL STRING via
# $CREW_EXPAND_CMD (parsed with shlex, not a list) so a test can point it at
# tests/fixtures/expand_stub.sh without touching code. Read live (like
# CREW_APP/CREW_PROJECT elsewhere in this file) so a dashboard restart with a
# different env picks it up — there's no other way to reconfigure it, since
# the dashboard is a long-running process.
EXPAND_CMD = ["claude", "-p", "--output-format", "json", "--max-turns", "1"]


def expand_cmd():
    """The argv crew/server/app.py's /api/expand shells out to. $CREW_EXPAND_CMD,
    if set, is a shell string (shlex-split) — e.g. the path to a test stub."""
    override = os.environ.get("CREW_EXPAND_CMD", "").strip()
    if override:
        import shlex
        return shlex.split(override)
    return EXPAND_CMD


# How long /api/expand waits for the expander command before giving up and
# returning the verbatim fallback. Overridable so a test can force the
# timeout path in seconds instead of the real 60s default.
try:
    EXPAND_TIMEOUT = float(os.environ.get("CREW_EXPAND_TIMEOUT", "60"))
except ValueError:
    EXPAND_TIMEOUT = 60.0
