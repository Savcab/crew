#!/usr/bin/env python3
"""crew — the CLI for the agent graph.

    crew [--project P] init           set up MorphDB schema + start the dashboard
    crew project create <name>        create an isolated project (its own MorphDB app)
    crew project list                 list known projects, mark the current one
    crew spawn-agent <name> ...       create a long-running coding agent
    crew webhook create|list|show|update|rotate|remove ...
                                       configure public ingress nodes (GATED)
    crew ingress run|status            expose hooks through a foreground tunnel
    crew connect <A> <B> --when "…"   define a relationship (and authorize A→B msg)
    crew disconnect <A> <B>           remove the relationship(s)
    crew cap <A> <B> [--max-turns N] [--token-cap N] [--cost-cap X]
                                       lower/raise the A->B edge's rate/budget caps (GATED)
    crew foreman <name> [--revoke]    grant/revoke the foreman flag (human-only, singleton)
    crew bless <agent>|--edge <A> <B>|--all   mark agent-authored change(s) reviewed (human-only)
    crew note agent <name> "text"     set a freeform note on an agent
    crew note edge <A> <B> "text"     set a freeform note on the A->B edge
    crew message <target> <text…>     message a connected agent (GATED)
    crew agents | edges | whoami      inspect
    crew status                       agent table: session up/down, pane state, mail
    crew up|down|restart <A>|--all    revive / kill / bounce agent sessions
    crew mail [<agent>] [-n N]        message log, newest first
    crew remove-agent <name>          delete an agent
    crew grant <agent> <path> [--ro|--rw]   grant an agent access to a path outside its home (human-only)
    crew revoke-grant <agent> <name>  revoke a grant (human-only)
    crew grants [<agent>]             list file grants (read-only, no gate)
    crew pending                      list pending approval requests, newest first
    crew approve <guid>               approve a pending request (human-only)
    crew reject <guid> [--why "…"]    reject a pending request (human-only)
    crew audit [-n N] [--refused] [--actor NAME]   graph-edit decision log, newest first
    crew dashboard {start|stop|status|open|logs}

Graph-editing and lifecycle operations are governed: a human running the CLI
can do anything; an ordinary agent has only narrow self-service and incident
actions; and a bounded foreman may update only its own children within finite
quotas. Every decision, allowed, pending, or refused, is recorded and viewable
with `crew audit`.

A top-level `--project` (before the subcommand, e.g. `crew --project demo
spawn-agent foo`) scopes the whole command to that project's own MorphDB app
("crew" for the default project, "crew-<name>" otherwise) and — for a
spawn-agent using the default home layout — its own subtree under crew_root().
Omit it (or set $CREW_PROJECT) to stay on the default project.

Identity is automatic inside a spawned agent: Crew resolves ownership from the
live managed tmux pane and durable agent record, while environment variables
are only hints. An agent never passes its own name to `message`.
"""
import argparse
import contextlib
import fcntl
import json
import math
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
from collections import namedtuple

from . import (
    config, graphstore as gs, guard, harness, identity, mail,
    runtime as runtimes, schema, spawn, webhooks,
)
from .server import tmuxio

ROOT = config.ROOT
VAR = config.VAR
_DashboardPaths = namedtuple("DashboardPaths", "pid log capability")
LEGACY_PIDFILE = os.path.join(VAR, "dashboard.pid")
_DASHBOARD_THREAD_LOCKS = {}
_DASHBOARD_THREAD_LOCKS_GUARD = threading.Lock()
DASHBOARD_START_ATTEMPTS = 150  # 15s: schema merge/backfill may delay binding

# The resolved caller identity for THIS process: "human" for an operator's own
# shell, or an agent's name when the CLI is run from inside that agent's own
# tmux pane (mail.whoami() resolves it the same anti-spoofing way messaging
# does). Set ONCE in main() and read by every mutating command via _actor().
_ACTOR = "human"


def _finite_float_arg(value):
    """Non-negative finite float; zero keeps its unlimited-cap meaning."""
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise argparse.ArgumentTypeError("must be a number") from error
    if not math.isfinite(number):
        raise argparse.ArgumentTypeError("must be a finite number")
    if number < 0:
        raise argparse.ArgumentTypeError("must be zero or a positive number")
    return number


def _nonnegative_int_arg(value):
    """Non-negative integer; zero keeps its unlimited-cap meaning."""
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise argparse.ArgumentTypeError("must be an integer") from error
    if number < 0:
        raise argparse.ArgumentTypeError("must be zero or a positive integer")
    return number


def _actor():
    return _ACTOR


def _inherited_agent_identity_hint():
    """Return a nonempty managed-agent marker inherited by this process."""
    for key in ("CREW_AGENT", "AGENT_MAIL_NAME"):
        value = (os.environ.get(key) or "").strip()
        if value:
            return key, value
    return None


def _warn(msg):
    print(f"[crew] {msg}", file=sys.stderr)


def _operator_agents(rows=None):
    """Quarantine identity-invalid rows at actionable CLI boundaries.

    The raw store remains visible to invariant/repair code. CLI commands keep
    operating on healthy rows and name each skipped GUID on stderr so an
    operator can repair or remove it deliberately.
    """
    persisted = gs.list_agents() if rows is None else rows
    usable, malformed = gs.partition_operational_agents(persisted)
    for row in malformed:
        guid = row.get("_guid") if isinstance(row, dict) else None
        label = repr(guid if guid is not None else "<missing-guid>")
        if len(label) > 160:
            label = label[:157] + "..."
        _warn(
            f"skipped malformed agent row {label}: "
            f"{gs.agent_row_problem(row)}")
    return usable


# --------------------------------------------------------------------------- #
# dashboard process management
# --------------------------------------------------------------------------- #
def _dashboard_paths(port=None):
    """Filesystem state for one listening port.

    A port is the dashboard process' exclusive resource, so it is also the
    lifecycle-state key.  Keeping the PID, log, and capability together avoids
    one project's dashboard overwriting or stopping another dashboard running
    on a different port.
    """
    port = config.DASHBOARD_PORT if port is None else int(port)
    stem = os.path.join(VAR, f"dashboard-{port}")
    return _DashboardPaths(stem + ".pid", stem + ".log", stem + ".cap")


def _dashboard_thread_lock(path):
    with _DASHBOARD_THREAD_LOCKS_GUARD:
        return _DASHBOARD_THREAD_LOCKS.setdefault(path, threading.RLock())


@contextlib.contextmanager
def _dashboard_lifecycle_lock(port=None):
    """Serialize process/capability/PID ownership for one dashboard port."""
    port = config.DASHBOARD_PORT if port is None else int(port)
    path = os.path.join(VAR, f"dashboard-{port}.lock")
    os.makedirs(VAR, mode=0o700, exist_ok=True)
    with _dashboard_thread_lock(path):
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)


def _port_open(host=config.DASHBOARD_HOST, port=config.DASHBOARD_PORT):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.3)
    try:
        return s.connect_ex((host, port)) == 0
    finally:
        s.close()


def _dash_url():
    return f"http://{config.DASHBOARD_HOST}:{config.DASHBOARD_PORT}"


def _read_dashboard_capability():
    try:
        with open(_dashboard_paths().capability) as fh:
            return fh.read().strip()
    except OSError:
        return ""


def _operator_dash_url():
    cap = _read_dashboard_capability()
    return f"{_dash_url()}/#cap={cap}" if cap else _dash_url()


def _write_dashboard_capability():
    cap = secrets.token_urlsafe(32)
    os.makedirs(VAR, exist_ok=True)
    path = _dashboard_paths().capability
    tmp = path + f".{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(cap)
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    except Exception:
        try: os.remove(tmp)
        except OSError: pass
        raise
    return cap


def _dashboard_identity():
    """Return the live server's process identity, or ``None``.

    The identity is deliberately separate from the graph snapshot: lifecycle
    commands must still work when MorphDB is unavailable, and a bare open port
    must never be enough evidence to signal a PID.
    """
    import urllib.error
    import urllib.request
    try:
        with urllib.request.urlopen(f"{_dash_url()}/api/health", timeout=1.0) as r:
            data = json.load(r)
        if (isinstance(data, dict)
                and data.get("ok") is True
                and data.get("service") == "crew-dashboard"):
            return data
    except urllib.error.HTTPError as error:
        error.close()
    except Exception:
        pass
    return None


def _dashboard_alive():
    """True only if the thing on our port is actually the CREW dashboard — a
    port-open check alone once reported 'running' while MorphDB's admin UI was
    squatting the port and the real dashboard was down."""
    import urllib.error
    import urllib.request
    if _dashboard_identity() is not None:
        return True
    try:
        with urllib.request.urlopen(f"{_dash_url()}/api/graph/snapshot",
                                    timeout=1.5) as r:
            d = json.load(r)
            return isinstance(d, dict) and "agents" in d
    except urllib.error.HTTPError as error:
        error.close()
        return False
    except Exception:
        return False


def start_dashboard():
    """Start the dashboard server detached (idempotent). Returns (url, started)."""
    with _dashboard_lifecycle_lock():
        return _start_dashboard_locked()


def _start_dashboard_locked():
    """Start while holding this port's lifecycle ownership lock."""
    if _port_open():
        live = _dashboard_identity()
        if live and live.get("app") != config.current_app():
            raise gs.GraphError(
                f"{_dash_url()} is already a Crew dashboard for another app "
                f"({live.get('app')!r}, not {config.current_app()!r}); choose "
                "another $CREW_PORT for this project")
        if live is None:
            raise gs.GraphError(
                f"something else is listening on {_dash_url()} (not the Crew "
                "dashboard) — free the port first: "
                f"lsof -nP -iTCP:{config.DASHBOARD_PORT} -sTCP:LISTEN")
        metadata = _read_dashboard_metadata()
        capability = _read_dashboard_capability()
        if not _same_dashboard_process(metadata, live) or not capability:
            raise gs.GraphError(
                f"the Crew dashboard on {_dash_url()} has no matching local "
                "ownership metadata and operator capability; stop that exact "
                "process or choose another $CREW_PORT")
        return _operator_dash_url(), False
    os.makedirs(VAR, exist_ok=True)
    paths = _dashboard_paths()
    try:
        capability = _write_dashboard_capability()
    except Exception as error:
        raise gs.GraphError(
            f"could not write dashboard operator capability: {error}") from error
    instance_id = secrets.token_urlsafe(32)
    child_env = dict(os.environ)
    child_env["CREW_DASHBOARD_CAPABILITY"] = capability
    child_env["CREW_DASHBOARD_INSTANCE_ID"] = instance_id
    child_env["CREW_PORT"] = str(config.DASHBOARD_PORT)
    child_env["CREW_APP"] = config.current_app()
    try:
        with open(paths.log, "a") as logf:
            p = subprocess.Popen([sys.executable, "-m", "crew.server.app"],
                                 cwd=ROOT, stdout=logf, stderr=logf,
                                 stdin=subprocess.DEVNULL, start_new_session=True,
                                 env=child_env)
    except Exception as error:
        _remove_dashboard_files(paths)
        raise gs.GraphError(
            f"could not launch dashboard process: {error}") from error
    metadata = {
        "pid": p.pid,
        "port": config.DASHBOARD_PORT,
        "app": config.current_app(),
        "instance_id": instance_id,
    }
    tmp = paths.pid + f".{os.getpid()}.tmp"
    try:
        with open(tmp, "x") as f:
            json.dump(metadata, f)
        os.replace(tmp, paths.pid)
    except Exception as error:
        # The child may already be listening. Never leave it running without
        # the instance metadata required for a later ownership-safe stop.
        _terminate_dashboard_child(p)
        try:
            os.remove(tmp)
        except OSError:
            pass
        _remove_dashboard_files(paths)
        raise gs.GraphError(
            f"could not write dashboard ownership metadata: {error}") from error
    for _ in range(DASHBOARD_START_ATTEMPTS):
        if _port_open():
            live = _dashboard_identity()
            if _same_dashboard_process(metadata, live):
                return _operator_dash_url(), True
            if live is not None:
                _terminate_dashboard_child(p)
                _remove_dashboard_files(paths)
                raise gs.GraphError(
                    f"dashboard startup lost {_dash_url()} to a different "
                    "process; inspect the port and dashboard log")
        poll = getattr(p, "poll", None)
        if callable(poll) and poll() is not None:
            break
        time.sleep(0.1)
    _terminate_dashboard_child(p)
    _remove_dashboard_files(paths)
    raise gs.GraphError(
        f"dashboard did not start on {_dash_url()}; inspect {paths.log}")


def _terminate_dashboard_child(process):
    """Best-effort cleanup for the exact child this start attempt created."""
    try:
        process.terminate()
    except (AttributeError, OSError):
        try:
            os.kill(int(process.pid), 15)
        except (AttributeError, OSError, TypeError, ValueError):
            return
    wait = getattr(process, "wait", None)
    if not callable(wait):
        return
    try:
        wait(timeout=2)
    except (OSError, subprocess.SubprocessError):
        try:
            process.kill()
            wait(timeout=2)
        except (AttributeError, OSError, subprocess.SubprocessError):
            pass


def _read_dashboard_metadata():
    try:
        with open(_dashboard_paths().pid) as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _same_dashboard_process(expected, live):
    if not isinstance(expected, dict) or not isinstance(live, dict):
        return False
    if not expected.get("instance_id"):
        return False
    return all(expected.get(key) == live.get(key)
               for key in ("pid", "port", "app", "instance_id"))


def _legacy_dashboard_pid():
    try:
        with open(LEGACY_PIDFILE) as fh:
            return int(fh.read().strip())
    except (OSError, ValueError):
        return None


def _listener_pids(port=None):
    """Best-effort listener ownership for safely stopping pre-metadata servers."""
    port = config.DASHBOARD_PORT if port is None else int(port)
    try:
        result = subprocess.run(
            ["lsof", "-nP", "-t", f"-iTCP:{port}", "-sTCP:LISTEN"],
            capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        return set()
    out = set()
    for line in result.stdout.splitlines():
        try:
            out.add(int(line.strip()))
        except ValueError:
            pass
    return out


def _remove_dashboard_files(paths):
    for path in (paths.pid, paths.capability):
        try:
            os.remove(path)
        except OSError:
            pass


def _wait_dashboard_stopped(expected, attempts=30):
    """Wait until the exact dashboard no longer owns its listening port."""
    for _ in range(attempts):
        live = _dashboard_identity()
        if not _same_dashboard_process(expected, live):
            # A failed health read is not enough while something still owns the
            # port; the server may simply be between accept/read operations.
            if live is not None or not _port_open():
                return True
        time.sleep(0.1)
    return False


def _wait_listener_pid_stopped(pid, attempts=30):
    """Legacy fallback: wait until the signalled PID leaves this port."""
    for _ in range(attempts):
        if pid not in _listener_pids():
            return True
        time.sleep(0.1)
    return False


def stop_dashboard():
    """Stop only the dashboard proven to own this port and metadata file.

    PID reuse is normal OS behavior.  A PID file by itself is therefore never
    authority to send a signal: the live server must echo the same random
    instance identity.  For a legacy integer PID file, both the Crew snapshot
    and the OS listener PID must match before migration-era shutdown is allowed.
    """
    with _dashboard_lifecycle_lock():
        return _stop_dashboard_locked()


def _stop_dashboard_locked():
    """Stop while holding this port's lifecycle ownership lock."""
    paths = _dashboard_paths()
    metadata = _read_dashboard_metadata()
    live = _dashboard_identity()
    if metadata is not None:
        if not _same_dashboard_process(metadata, live):
            if live is None and not _port_open():
                _remove_dashboard_files(paths)
            else:
                _warn(f"refusing to stop {_dash_url()}: PID metadata does not "
                      "match the live dashboard process")
            return False
        try:
            os.kill(int(metadata["pid"]), 15)
        except OSError as error:
            _warn(f"could not stop dashboard process {metadata.get('pid')}: {error}")
            return False
        if not _wait_dashboard_stopped(metadata):
            _warn(f"dashboard process {metadata.get('pid')} did not terminate; "
                  "ownership metadata was preserved")
            return False
        # Another starter may have replaced these files after our process left.
        # Remove them only if they still describe the exact process we stopped.
        if _read_dashboard_metadata() == metadata:
            _remove_dashboard_files(paths)
        return True

    # Compatibility for dashboards started before port-scoped JSON metadata.
    # The old PID file was global, so never remove or signal it unless the PID
    # is also the verified listener for *this* port.
    legacy_pid = _legacy_dashboard_pid()
    if (legacy_pid is not None and _dashboard_alive()
            and legacy_pid in _listener_pids()):
        try:
            os.kill(legacy_pid, 15)
        except OSError as error:
            _warn(f"could not stop legacy dashboard process {legacy_pid}: {error}")
            return False
        if not _wait_listener_pid_stopped(legacy_pid):
            _warn(f"legacy dashboard process {legacy_pid} did not terminate; "
                  "ownership metadata was preserved")
            return False
        try:
            os.remove(LEGACY_PIDFILE)
        except OSError:
            pass
        _remove_dashboard_files(paths)
        return True
    if live is not None or _port_open():
        _warn(f"refusing to stop {_dash_url()}: no matching dashboard process metadata")
    else:
        _remove_dashboard_files(paths)
    return False


def _morphdb_up():
    from urllib.parse import urlparse
    u = urlparse(config.morphdb_base())
    host, port = (u.hostname or "127.0.0.1"), (u.port or 8787)
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.4)
    try:
        return s.connect_ex((host, port)) == 0
    finally:
        s.close()


def _ensure_morphdb():
    """crew's data lives in MorphDB — make sure it's reachable, starting it if not.
    Best-effort: if `morphdb` isn't installed we just warn and let the schema call
    surface the clear 'cannot reach MorphDB' error."""
    if _morphdb_up():
        return
    _warn("MorphDB not reachable — starting it…")
    try:
        subprocess.run(["morphdb", "start"], capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        _warn("could not run `morphdb start` (pip install morphdb)")
        return
    for _ in range(25):
        if _morphdb_up():
            return
        time.sleep(0.2)
    _warn("MorphDB still not reachable; check `morphdb status`")


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
def cmd_init(a):
    guard.check(_actor(), "init")
    _ensure_morphdb()
    used = schema.ensure_schema()
    print(f"MorphDB ready: app '{used}' at {config.morphdb_base()}")
    if not a.no_dashboard:
        url, started = start_dashboard()
        print(f"dashboard {'started' if started else 'already running'} → {url}")
    print("Next: open the dashboard and click + Agent, or `crew spawn-agent <name> --role \"...\"`.")
    return 0


def cmd_project_create(a):
    guard.check(_actor(), "project_create", name=a.name)
    if not config.valid_project_name(a.name):
        print(f"[crew] error: invalid project name {a.name!r}: letters, digits, "
              "'_', '-' only (no dots/slashes/spaces), max 32 chars, "
              "must start alphanumeric", file=sys.stderr)
        return 1
    if a.name == config.DEFAULT_PROJECT:
        print(f"'{config.DEFAULT_PROJECT}' is the built-in default project — nothing to create.")
        return 0
    _ensure_morphdb()
    app = schema.ensure_schema(app=config.project_app(a.name))
    config.register_project(a.name, description=a.description or "",
                            title=a.title or "")
    print(f"created project '{a.name}' → MorphDB app '{app}'")
    if a.no_foreman:
        print(f"  use it:  crew --project {a.name} spawn-agent <name> ...")
        return 0
    # Same chat-to-build foreman the dashboard gallery seeds — a new graph is
    # ready to configure itself the moment it exists. The graph outlives a
    # failed seed; report it and leave the operator a working fallback.
    ok, detail = spawn.seed_foreman(a.name, launch=not a.no_launch)
    if ok:
        state = "not started (--no-launch)" if a.no_launch else "booting"
        print(f"  seeded foreman 'foreman' ({state}) — open its terminal and "
              "describe the system you want to build")
    else:
        print(f"[crew] warning: foreman seed failed: {detail}", file=sys.stderr)
        print(f"  seed it yourself:  crew --project {a.name} spawn-agent "
              "foreman --foreman")
    return 0


def cmd_project_list(a):
    current = config.current_project()
    descriptions = config.project_descriptions()
    for name in config.list_known_projects():
        marker = "*" if name == current else " "
        blurb = descriptions.get(name, "")
        print(f"{marker} {name}  → {config.project_app(name)}"
              + (f"  — {blurb}" if blurb else ""))
    return 0


def cmd_spawn_agent(a):
    schema.ensure_schema()
    agent = spawn.spawn_agent(
        a.name, role=a.role or "", agent_identity=a.identity or "",
        home=a.home, repo=a.repo, launch=not a.no_launch, launch_cmd=a.launch_cmd,
        runtime=getattr(a, "runtime", None), actor=_actor(), foreman=a.foreman)
    print(f"spawned agent '{agent['name']}' → session '{agent['session']}' "
          f"(home {agent['home']})")
    print(f"  identity: {os.path.join(agent['home'], config.IDENTITY_FILE)}")
    runtime_key = runtimes.resolve_agent_runtime(agent)
    print(f"  runtime: {runtime_key} ({runtimes.adapter(runtime_key).label})")
    if a.foreman:
        print(f"  '{agent['name']}' is now foreman (can_edit_graph=true)")
    if not a.no_launch:
        print("  runtime is booting; it will read its native identity shortly.")
    print(f"  connect it:  crew connect {agent['name']} <other> --when \"<condition>\"")
    return 0


def cmd_foreman(a):
    """crew foreman <name> [--revoke] — human-only (guard op "foreman" already
    gated). Enforces the SINGLETON rule (guard.check refuses a grant while
    another agent already holds the flag) before touching MorphDB, then sets
    can_edit_graph via update_agent and rewrites the agent's identity files so
    the "Graph powers" section (or its absence, on revoke) is current on disk
    immediately."""
    ag = _resolve_or_die(a.name)
    actor = _actor()
    gs.set_foreman(
        ag["_guid"], revoke=a.revoke, actor=actor,
        _identity_rewriter=spawn.rewrite_identity)
    if a.revoke:
        print(f"revoked foreman from '{a.name}'")
    else:
        print(f"'{a.name}' is now foreman (can_edit_graph=true)")
    return 0


def cmd_bless(a):
    """crew bless <node> | crew bless --edge <src> <dst> | crew bless --all —
    human-only (guard op "bless"). Flips `blessed` true on the target row(s);
    --all sweeps every currently-unblessed graph node + edge in the current
    project. Prints what got blessed."""
    actor = _actor()
    if a.all:
        # Runtime-agent and webhook invariants are deliberately separate.
        # Keep the normal operational-agent quarantine, then add only webhook
        # rows with the immutable GUID/name pair blessing needs.
        nodes = list(_operator_agents())
        for hook in gs.list_webhooks():
            problem = gs.agent_row_problem(hook)
            if problem:
                _warn(
                    f"skipped malformed webhook row "
                    f"{hook.get('_guid')!r}: {problem}")
                continue
            nodes.append(hook)
        agents = [
            x for x in nodes
            if not x.get("blessed")
        ]
        edges = [e for e in gs.list_edges() if not e.get("blessed")]
        for ag in agents:
            gs.bless_agent(ag["_guid"], actor=actor)
        for e in edges:
            gs.bless_edge(e["_guid"], actor=actor)
        print(f"blessed {len(agents)} node(s) and {len(edges)} edge(s)")
        return 0
    if a.edge:
        src = _resolve_node_or_die(a.edge[0])
        tgt = _resolve_node_or_die(a.edge[1])
        edges = [e for e in gs.edges_from_to(src["_guid"], tgt["_guid"])
                 if not e.get("blessed")]
        if not edges:
            print(f"(no unblessed edge {a.edge[0]} -> {a.edge[1]})")
            return 0
        for e in edges:
            gs.bless_edge(e["_guid"], actor=actor)
        print(f"blessed {len(edges)} edge(s) {a.edge[0]} -> {a.edge[1]}")
        return 0
    if not a.agent:
        print("[crew] give a graph node name, --edge <src> <dst>, or --all",
              file=sys.stderr)
        return 1
    ag = _resolve_node_or_die(a.agent)
    if ag.get("blessed"):
        print(f"'{a.agent}' already blessed")
        return 0
    gs.bless_agent(ag["_guid"], actor=actor)
    print(f"blessed node '{a.agent}'")
    return 0


def _resolve_or_die(name):
    a = gs.get_agent_by_name(name)
    if not a:
        raise gs.GraphError(f"no such agent: {name}")
    return a


def _resolve_node_or_die(name):
    node = gs.get_node_by_name(name)
    if not node:
        raise gs.GraphError(f"no such graph node: {name}")
    return node


def _resolve_webhook_or_die(name):
    hook = gs.get_webhook_by_name(name)
    if not hook:
        raise gs.GraphError(f"no such webhook: {name}")
    return hook


def cmd_webhook_create(a):
    schema.ensure_schema()
    template = a.template or ""
    webhooks.validate_template(template)
    hook = gs.create_webhook(
        a.name, description=a.description or "", template=template,
        actor=_actor())
    print(f"created webhook '{hook['name']}'")
    print(f"  POST {webhooks.public_url(hook)}")
    return 0


def cmd_webhook_list(a):
    hooks = gs.list_webhooks()
    if not hooks:
        print("(no webhooks)")
        return 0
    current_agents = {}
    for hook in hooks:
        description = f"  — {hook.get('role')}" if hook.get("role") else ""
        owner_guid = hook.get("created_by_guid") or ""
        if owner_guid and owner_guid not in current_agents:
            current_agents[owner_guid] = gs.get_agent_by_guid(owner_guid)
        owner = current_agents.get(owner_guid)
        if not owner_guid:
            ownership = "human-managed"
        elif owner and owner.get("can_edit_graph"):
            ownership = f"owner:{owner['name']}"
        else:
            ownership = "human-managed (owner unavailable)"
        print(
            f"{hook.get('name') or '?':<16} "
            f"[{hook.get('status') or 'listening'}] {ownership}{description}")
    return 0


def cmd_webhook_show(a):
    resolved = _resolve_webhook_or_die(a.name)
    hook = gs.read_webhook(resolved["_guid"], actor=_actor())
    print(f"name: {hook['name']}")
    print(f"description: {hook.get('role') or ''}")
    print(f"template: {hook.get('webhook_template') or ''}")
    print(f"url: {webhooks.public_url(hook)}")
    print(f"last status: {hook.get('webhook_last_status') or 'never called'}")
    return 0


def cmd_webhook_update(a):
    if a.description is None and a.template is None:
        print(
            "[crew] nothing to change — pass --description or --template",
            file=sys.stderr)
        return 1
    if a.template is not None:
        webhooks.validate_template(a.template)
    hook = _resolve_webhook_or_die(a.name)
    updated = gs.update_webhook(
        hook["_guid"], description=a.description, template=a.template,
        actor=_actor())
    print(f"updated webhook '{updated['name']}'")
    return 0


def cmd_webhook_rotate(a):
    hook = _resolve_webhook_or_die(a.name)
    rotated = gs.update_webhook(
        hook["_guid"], rotate=True, actor=_actor())
    print(f"rotated webhook '{rotated['name']}'")
    print(f"  POST {webhooks.public_url(rotated)}")
    return 0


def cmd_webhook_remove(a):
    hook = _resolve_webhook_or_die(a.name)
    guid = hook["_guid"]

    def projected_identity(agent, notify=False):
        return spawn.rewrite_identity(
            agent, notify=notify, exclude_agent_guids={guid})

    gs.delete_webhook(
        guid, actor=_actor(), _identity_projector=projected_identity,
        _identity_rewriter=spawn.rewrite_identity,
        _identity_notifier=spawn.notify_connection_change)
    print(f"removed webhook '{hook['name']}'")
    return 0


def cmd_connect(a):
    schema.ensure_schema()
    src = _resolve_node_or_die(a.source)
    tgt = _resolve_node_or_die(a.target)
    edge = gs.create_edge(src["_guid"], tgt["_guid"], label=a.label or "",
                          description=a.desc or "",
                          conditions=a.when or [], target_action=a.does or "",
                          reply_expected=a.reply,
                          back_conditions=a.when_back or [], back_action=a.does_back or "",
                          back_reply=a.reply_back,
                          max_turns=a.max_turns or 0,
                          token_cap=a.token_cap or 0, cost_cap=a.cost_cap or 0,
                          directed=not a.undirected, transform=a.transform or "",
                          actor=_actor(),
                          _identity_rewriter=spawn.rewrite_identity,
                          _identity_notifier=spawn.notify_connection_change)
    arrow = "<->" if a.undirected else "->"
    print(f"connected {src['name']} {arrow} {tgt['name']}"
          + (f"  ({a.label})" if a.label else ""))
    for w in (a.when or []):
        print(f"  {src['name']} messages {tgt['name']} when: {w}")
    for w in (a.when_back or []):
        print(f"  {tgt['name']} messages {src['name']} when: {w}")
    return 0


def cmd_disconnect(a):
    src = _resolve_node_or_die(a.source)
    tgt = _resolve_node_or_die(a.target)
    edges = gs.disconnect_between(
        src["_guid"], tgt["_guid"], actor=_actor(),
        _identity_rewriter=spawn.rewrite_identity,
        _identity_notifier=spawn.notify_connection_change)
    if not edges:
        print(f"(no edges between {src['name']} and {tgt['name']})")
        return 0
    print(f"disconnected {src['name']} and {tgt['name']} ({len(edges)} edge(s))")
    return 0


def _fmt_num(v):
    return f"{v:g}" if isinstance(v, (int, float)) else str(v)


def cmd_cap(a):
    """Update the authorizing A->B edge's rate/budget caps via update_edge —
    guard.check enforces DOWNHILL-ONLY for an agent actor (extend/reuse of the
    wave-1 endpoint+lower-only rule), anything for a human. Prints old->new
    for each cap that actually changed."""
    src = _resolve_node_or_die(a.source)
    tgt = _resolve_node_or_die(a.target)
    edge = gs.authorizing_edge(src["name"], tgt["name"])
    if not edge:
        print(f"[crew] no edge {a.source} -> {a.target}", file=sys.stderr)
        return 1
    fields = {}
    for name, val in (("max_turns", a.max_turns), ("token_cap", a.token_cap),
                      ("cost_cap", a.cost_cap)):
        if val is not None:
            fields[name] = val
    if not fields:
        print("[crew] nothing to change — pass --max-turns/--token-cap/--cost-cap",
              file=sys.stderr)
        return 1
    old = {k: edge.get(k) for k in fields}
    out = gs.update_edge(
        edge["_guid"], fields, actor=_actor(),
        _identity_rewriter=spawn.rewrite_identity,
        _identity_notifier=spawn.notify_connection_change)
    changed = False
    for k in fields:
        ov, nv = old.get(k), out.get(k)
        if ov != nv:
            changed = True
            print(f"  {k}: {_fmt_num(ov)} -> {_fmt_num(nv)}")
    if not changed:
        print(f"(no change to {a.source} -> {a.target})")
    return 0


def cmd_note_agent(a):
    ag = _resolve_or_die(a.name)
    gs.set_agent_note(ag["_guid"], a.text, actor=_actor())
    print(f"note set on agent '{ag['name']}'")
    return 0


def cmd_note_edge(a):
    src = _resolve_node_or_die(a.source)
    tgt = _resolve_node_or_die(a.target)
    edge = gs.authorizing_edge(src["name"], tgt["name"])
    if not edge:
        print(f"[crew] no edge {a.source} -> {a.target}", file=sys.stderr)
        return 1
    gs.set_edge_note(edge["_guid"], a.text, actor=_actor())
    print(f"note set on edge {a.source} -> {a.target}")
    return 0


def cmd_agents(a):
    agents = _operator_agents()
    if not agents:
        print("(no agents)")
        return 0
    for ag in agents:
        role = f"  — {ag['role']}" if ag.get("role") else ""
        runtime_key = runtimes.resolve_agent_runtime(ag)
        print(f"{ag['name']:<16} [{runtime_key:<6}] {ag.get('home','')}{role}")
    return 0


def cmd_edges(a):
    edges = gs.list_edges()
    if not edges:
        print("(no edges)")
        return 0
    names = {
        node["_guid"]: node["name"]
        for node in _operator_agents(gs.list_nodes())
    }
    for e in edges:
        arrow = "<->" if not e.get("directed", True) else "->"
        s = names.get(e.get("source"), "?"); t = names.get(e.get("target"), "?")
        label = f"  [{e['label']}]" if e.get("label") else ""
        cond = f"  when: {e['condition']}" if e.get("condition") else ""
        b = identity.edge_budget(e)
        budget = f"  budget: {b}" if b else ""
        transform = f"  transform: {os.path.basename(e['transform'])}" if e.get("transform") else ""
        print(f"{s} {arrow} {t}{label}{cond}{budget}{transform}")
    return 0


def cmd_message(a):
    sender = mail.whoami()
    body = " ".join(a.words).strip()
    ok, msg = mail.deliver(a.target, body, sender=sender, no_prefix=a.no_prefix)
    print("[crew] " + msg, file=(sys.stdout if ok else sys.stderr))
    return 0 if ok else 1


def cmd_kickoff(a):
    """Operator → agent: seed/steer one of YOUR agents directly.

    The central mail path verifies that the already resolved caller is the human
    operator.  A managed agent must use the graph-gated ``crew message`` path.
    """
    text = " ".join(a.words).strip()
    ok, msg = mail.say_to_agent(a.agent, text, actor=_actor())
    print("[crew] " + msg, file=(sys.stdout if ok else sys.stderr))
    return 0 if ok else 1


def cmd_activity(a):
    """Set your own activity line, or read everyone's.

    With text: sets the CALLER's activity (an agent may only set its own —
    guard-enforced; a human may target any agent via --agent). Without text:
    prints every agent's current activity, so an agent can see what a peer is
    up to WITHOUT sending it mail, and the operator gets the same at-a-glance
    view the graph cards show.
    """
    text = " ".join(a.text or "").strip()
    if text or a.clear:
        actor = _actor()
        name = a.agent or (mail.whoami() if actor != "human" else None)
        if not name:
            print("[crew] which agent? set with --agent NAME (operator shells "
                  "have no implicit agent identity)", file=sys.stderr)
            return 1
        ag = _resolve_or_die(name)
        gs.set_agent_activity(ag["_guid"], "" if a.clear else text, actor=actor)
        print(f"activity {'cleared' if a.clear else 'set'} for '{ag['name']}'")
        return 0
    rows = _operator_agents()
    if a.agent:
        rows = [r for r in rows if r["name"] == a.agent]
        if not rows:
            print(f"[crew] no such agent: {a.agent}", file=sys.stderr)
            return 1
    now = time.time()
    for r in sorted(rows, key=lambda x: x["name"]):
        line = (r.get("activity") or "").strip()
        at = r.get("activity_at") or 0
        age = ""
        if line and at:
            m = int(max(0, now - at) // 60)
            age = f"  ({m}m ago)" if m else "  (just now)"
        print(f"  {r['name']}: {line or '—'}{age}")
    return 0


def cmd_harness(a):
    """Show what each agent's coding harness says it is working toward.

    `crew activity` is what an agent CHOOSES to tell you; this is what its
    harness already knows — the open goals it is pursuing — read from the
    harness's own durable state without the agent's cooperation. A runtime
    Crew has no reader for says so instead of reporting an empty goal it
    cannot vouch for.
    """
    rows = _operator_agents()
    if a.name:
        rows = [r for r in rows if r["name"] == a.name]
        if not rows:
            print(f"[crew] no such agent: {a.name}", file=sys.stderr)
            return 1
    rows.sort(key=lambda r: r["name"])
    states = harness.probe_many(rows)
    if a.json:
        print(json.dumps(
            [dict(agent=row["name"], **state.as_dict())
             for row, state in zip(rows, states)], indent=2))
        return 0
    for row, state in zip(rows, states):
        name = row["name"]
        if not state.supported:
            print(f"  {name}: {state.reason}")
            continue
        goal = state.goal or "—"
        if state.goal_count > 1:
            goal += f"  (+{state.goal_count - 1} more open)"
        print(f"  {name}")
        print(f"      goal: {goal}")
        if state.subagents:
            print(f"      subagents: {state.subagents} live")
        if state.reason:
            print(f"      note: {state.reason}")
    return 0


def cmd_peers(a):
    """Show who an agent may message (and when) and who may message it (and what
    they expect). Defaults to the calling agent inside a session."""
    name = a.name or mail.whoami()
    ag = gs.get_agent_by_name(name)
    if not ag:
        print(f"[crew] no such agent: {name}", file=sys.stderr)
        return 1
    names = {x["_guid"]: x["name"] for x in _operator_agents()}
    out = gs.messageable_targets(ag["_guid"])
    inc = gs.incoming_edges(ag["_guid"])
    print(f"{name} may message:")
    if out:
        for g, e in out:
            cond = f"  when: {e['condition']}" if e.get("condition") else ""
            cap = f"  (max {e['max_turns']}/hr)" if e.get("max_turns") else ""
            print(f"  → {names.get(g, '?')}{cond}{cap}")
    else:
        print("  (no one)")
    print(f"may message {name}:")
    if inc:
        for g, e in inc:
            act = f"  → {e['target_action']}" if e.get("target_action") else ""
            print(f"  ← {names.get(g, '?')}{act}")
    else:
        print("  (no one)")
    return 0


def cmd_whoami(a):
    name = mail.whoami()
    ag = gs.get_agent_by_name(name)
    print(f"name: {name}")
    if ag:
        print(f"role: {ag.get('role') or '(none)'}")
        print(f"runtime: {runtimes.resolve_agent_runtime(ag)}")
        print(f"home: {ag.get('home') or '(none)'}")
        targets = [gs.get_object(g).get("name") for g, _ in gs.messageable_targets(ag["_guid"])]
        print(f"may message: {', '.join(t for t in targets if t) or '(no connections)'}")
    else:
        print("role: (not a registered agent)")
    return 0


def cmd_remove_agent(a):
    ag = spawn.remove_agent(a.name, kill_session=not a.keep_session, actor=_actor())
    print(f"removed agent '{ag['name']}'"
          + ("" if a.keep_session else f" (killed session '{ag.get('session')}')"))
    return 0


def cmd_grant(a):
    """crew grant <agent> <path> [--ro|--rw] — human-only (a foreman's attempt
    is queued for approval instead of refused, see crew.guard/crew.pending).
    All the mechanics (symlink, agent.grants entry, identity rewrite, audit)
    happen together in crew.spawn.grant_path."""
    mode = "rw" if a.rw else "ro"
    entry = spawn.grant_path(a.agent, a.path, mode=mode, actor=_actor())
    print(f"granted '{a.agent}' {mode} access to {entry['path']}")
    print(f"  refs/{entry['name']} -> {entry['path']}")
    return 0


def cmd_revoke_grant(a):
    """crew revoke-grant <agent> <name> — human-only, no foreman exception."""
    spawn.revoke_grant(a.agent, a.name, actor=_actor())
    print(f"revoked grant '{a.name}' from '{a.agent}'")
    return 0


def cmd_grants(a):
    """crew grants [<agent>] — read-only listing (name, path, mode, age), no
    gate: any actor may run this, even a plain (non-foreman) agent."""
    if a.agent:
        agents = [_resolve_or_die(a.agent)]
    else:
        agents = _operator_agents()
    rows = [(ag["name"], g) for ag in agents for g in (ag.get("grants") or [])]
    if not rows:
        print("(no grants)")
        return 0
    now = time.time()
    for name, g in rows:
        age = _fmt_age(now - (g.get("created_at") or now))
        print(f"{name:<16} refs/{g.get('name', '?'):<14} {g.get('mode', 'ro'):<3} "
              f"{g.get('path', '?')}  ({age} ago)")
    return 0


def cmd_status(a):
    """One line per agent: session/runtime state and delivery-health counts."""
    agents = _operator_agents()
    if not agents:
        print("(no agents)")
        return 0
    inventory = tmuxio.live_agent_inventory(agents)
    counts = {}
    attention_states = ("submitting", "delivery_uncertain")
    for st in ("queued", "failed", *attention_states):
        for m in gs.list_messages(status=st, limit=2000):
            k = (m.get("target"), st)
            counts[k] = counts.get(k, 0) + 1
    rows = []
    migrations = []
    for ag in agents:
        exact_live = inventory.get(tmuxio.agent_inventory_key(ag), {})
        live = tmuxio.agent_snapshot_fields(
            ag, live=exact_live)
        if live["migration_required"]:
            migrations.append(ag["name"])
        rows.append((ag["name"], live["runtime"],
                     "up" if live["session_alive"] else "down",
                     live["live_status"],
                     str(counts.get((ag["name"], "queued"), 0)),
                     str(sum(counts.get((ag["name"], st), 0)
                             for st in attention_states)),
                     str(counts.get((ag["name"], "failed"), 0)),
                     ag.get("role") or ""))
    w = max([5] + [len(r[0]) for r in rows])
    print(f"{'agent':<{w}}  {'runtime':<7}  {'session':<7}  {'state':<11}  "
          f"{'queued':>6}  {'attention':>9}  {'failed':>6}  role")
    for name, runtime_key, up, state, q, attention, f, role in rows:
        print(f"{name:<{w}}  {runtime_key:<7}  {up:<7}  {state:<11}  "
              f"{q:>6}  {attention:>9}  {f:>6}  {role}")
    if migrations:
        names = ", ".join(migrations)
        print(f"\npre-upgrade tmux: {names}. To migrate without an implicit "
              "conversation restart, run `crew down <agent>` when ready, then "
              "`crew up <agent>`.")
    return 0


def _lifecycle_agents(a):
    """Resolve <name>|--all into agent dicts for the up/down/restart commands."""
    if a.all:
        return _operator_agents()
    if not a.name:
        raise gs.GraphError("give an agent name or --all")
    return [_resolve_or_die(a.name)]


def _print_results(rows):
    w = max(len(n) for n, _ in rows)
    for n, r in rows:
        print(f"{n:<{w}}  {r}")


def _kill_session(ag, actor):
    """Kill an agent's tmux session but keep its record + home — exactly how
    remove-agent kills sessions, minus the delete. Returns a result string.
    The spawn boundary owns permission, durable-row validation, exact live
    session ownership, and audit behavior."""
    try:
        stopped = spawn.stop_session(ag["name"], actor=actor)
        return "stopped" if stopped else "already down"
    except gs.GraphError as e:
        return f"error: {e}"


def cmd_up(a):
    """Revive down agent(s) via spawn.start_session; skip ones already up."""
    agents = _lifecycle_agents(a)
    if not agents:
        print("(no agents)")
        return 0
    actor = _actor()
    inventory = tmuxio.live_agent_inventory(agents)
    rows = []
    for ag in agents:
        try:
            session = spawn.validated_session_name(
                ag, ag["name"], config.current_project())
        except gs.GraphError as e:
            rows.append((ag.get("name") or "?", f"error: {e}"))
            continue
        runtime_key = runtimes.resolve_agent_runtime(ag)
        exact_live = inventory.get(tmuxio.agent_inventory_key(ag), {})
        live_session = exact_live.get("session")
        live_pane = exact_live.get("pane")
        dedicated = (live_session is not None
                     and config.tmux_target_endpoint(live_session)
                     == config.TMUX_ENDPOINT_CREW)
        # Custom commands may intentionally be one-shot (the long-standing
        # inert `true` lifecycle fixture is one). A still-live session counts
        # as up after first launch; `restart` remains the explicit rerun. The
        # important no-launch case is status=not_started, which does launch in
        # that existing bare session. Claude/Codex use process liveness.
        custom_session_up = (runtime_key == "custom" and dedicated
                             and ag.get("status") != "not_started")
        if (dedicated and live_pane is not None) or custom_session_up:
            rows.append((ag["name"], "already up — skipped"))
            continue
        try:
            spawn.start_session(ag["name"], actor=actor)
            rows.append((ag["name"], "started"))
        except gs.GraphError as e:
            rows.append((ag["name"], f"error: {e}"))
    _print_results(rows)
    return 1 if any(r.startswith("error") for _, r in rows) else 0


def cmd_down(a):
    """Kill agent session(s); the MorphDB record and home dir stay."""
    agents = _lifecycle_agents(a)
    if not agents:
        print("(no agents)")
        return 0
    actor = _actor()
    rows = [(ag["name"], _kill_session(ag, actor)) for ag in agents]
    _print_results(rows)
    return 1 if any(r.startswith("error") for _, r in rows) else 0


def cmd_restart(a):
    """down + up."""
    agents = _lifecycle_agents(a)
    if not agents:
        print("(no agents)")
        return 0
    actor = _actor()
    rows = []
    for ag in agents:
        try:
            stopped = spawn.stop_session(
                ag["name"], actor=actor, refuse_legacy=True)
            was = "stopped" if stopped else "already down"
        except gs.GraphError as error:
            rows.append((ag["name"], f"error: {error}"))
            continue
        try:
            spawn.start_session(ag["name"], actor=actor)
            rows.append((ag["name"],
                         "restarted" if was == "stopped" else "started (was down)"))
        except gs.GraphError as e:
            rows.append((ag["name"], f"error: {e}"))
    _print_results(rows)
    return 1 if any(r.startswith("error") for _, r in rows) else 0


def cmd_mail(a):
    """The message log, newest first: time, sender→target, status, body preview.
    With <agent>, only messages that agent sent or received (no existence check —
    a removed agent's history is still viewable)."""
    def fetch(**flt):
        res = gs.list_objects("message", sort="created_at", order="desc",
                              limit=a.n, status=a.status, **flt)
        return (res or {}).get("objects", [])
    if a.agent:
        seen = {}
        for m in fetch(sender=a.agent) + fetch(target=a.agent):
            seen[m["_guid"]] = m
        msgs = sorted(seen.values(),
                      key=lambda m: m.get("created_at") or 0, reverse=True)[:a.n]
    else:
        msgs = fetch()
    if not msgs:
        print("(no messages)")
        return 0
    pairs = [f"{m.get('sender') or '?'} → {m.get('target') or '?'}" for m in msgs]
    w = max(len(p) for p in pairs)
    status_w = max([9] + [len(m.get("status") or "?") for m in msgs])
    for m, pair in zip(msgs, pairs):
        ts = time.strftime("%Y-%m-%d %H:%M",
                           time.localtime(int(m.get("created_at") or 0)))
        body = " ".join((m.get("body") or "").split())
        if len(body) > 60:
            body = body[:59] + "…"
        detail = " ".join((m.get("status_detail") or "").split())
        if len(detail) > 120:
            detail = detail[:119] + "…"
        suffix = f" — {detail}" if detail else ""
        print(f"{ts}  {pair:<{w}}  {m.get('status') or '?':<{status_w}}  "
              f"{body}{suffix}")
    return 0


def _fmt_age(secs):
    secs = max(0, int(secs))
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h"
    return f"{secs // 86400}d"


def cmd_pending(a):
    """List approval requests that still need operator attention."""
    rows = []
    for result in guard.PENDING_ATTENTION_RESULTS:
        response = gs.list_objects(
            "graph_edit", result=result, sort="created_order",
            order="desc", limit=500)
        rows.extend((response or {}).get("objects", []))
    rows = sorted(
        {row.get("_guid"): row for row in rows if row.get("_guid")}.values(),
        key=lambda row: (
            row.get("created_order") or 0, row.get("created_at") or 0),
        reverse=True)[:500]
    if not rows:
        print("(no unresolved approval requests)")
        return 0
    now = time.time()
    for r in rows:
        age = _fmt_age(now - (r.get("created_at") or now))
        summary = guard._summarize(r.get("op"), r.get("args") or {})
        state = r.get("result") or "?"
        detail = ""
        if state == "applying":
            detail = (" — reconciliation/manual review required; the mutation "
                      "may have started, so do not replay it blindly")
        elif state == "approval_failed":
            reason = r.get("reason") or "failure detail unavailable"
            detail = (f" — {reason}; manual review required before any "
                      "recovery or retry")
        print(f"{r['_guid'][:18]}  {state:<16}  "
              f"{r.get('actor') or '?':<16}  {r.get('op') or '?':<12}  "
              f"{summary}  ({age} ago){detail}")
    return 0


def _resolve_pending(prefix):
    """Resolve a guid-or-prefix to exactly one PENDING graph_edit row —
    unique-prefix match, or a GraphError listing the matches (ambiguous) /
    saying so (none)."""
    rows = gs._list_all_exact("graph_edit", sort="created_order", order="desc",
                              result="pending")
    matches = [r for r in rows if (r.get("_guid") or "").startswith(prefix)]
    if not matches:
        raise gs.GraphError(f"no pending request matching '{prefix}'")
    if len(matches) > 1:
        ids = ", ".join(m["_guid"][:12] for m in matches)
        raise gs.GraphError(
            f"ambiguous prefix '{prefix}' matches {len(matches)} pending "
            f"requests: {ids} — give more characters")
    return matches[0]


def cmd_approve(a):
    row = _resolve_pending(a.guid)
    guard.approve_pending(row["_guid"], actor=_actor())
    print(f"approved {row['_guid'][:18]}  ({guard._summarize(row.get('op'), row.get('args') or {})})")
    return 0


def cmd_reject(a):
    row = _resolve_pending(a.guid)
    guard.reject_pending(row["_guid"], reason=a.why or "", actor=_actor())
    print(f"rejected {row['_guid'][:18]}  ({guard._summarize(row.get('op'), row.get('args') or {})})")
    return 0


def cmd_audit(a):
    """The graph_edit decision log, newest first: time, actor, op, result,
    reason. `--refused` narrows to refusals (the interesting ones when
    diagnosing "why can't my agent do X"); `--actor` narrows to one actor."""
    res = gs.list_objects("graph_edit", sort="created_order", order="desc",
                          limit=a.n, actor=a.actor)
    rows = (res or {}).get("objects", [])
    if a.refused:
        rows = [r for r in rows if r.get("result") == "refused"][:a.n]
    if not rows:
        print("(no audit rows)")
        return 0
    w = max(len(r.get("actor") or "?") for r in rows)
    for r in rows:
        ts = time.strftime("%Y-%m-%d %H:%M",
                           time.localtime(int(r.get("created_at") or 0)))
        reason = f"  — {r['reason']}" if r.get("reason") else ""
        print(f"{ts}  {r.get('actor') or '?':<{w}}  {r.get('op') or '?':<12}  "
              f"{r.get('result') or '?':<9}{reason}")
    return 0


def cmd_dashboard(a):
    action = a.action
    if action in ("start", "stop", "open"):
        guard.check(_actor(), "dashboard_control", action=action)
    if action == "status":
        if not _port_open():
            state = "stopped"
        elif _dashboard_alive():
            state = "running"
        else:
            state = ("port busy with something else (not crew) — "
                     f"lsof -nP -iTCP:{config.DASHBOARD_PORT} -sTCP:LISTEN")
        print(f"dashboard {state} → {_dash_url()}")
    elif action == "start":
        url, started = start_dashboard()
        print(f"dashboard {'started' if started else 'already running'} → {url}")
    elif action == "stop":
        stopped = stop_dashboard()
        print("dashboard stopped" if stopped else "dashboard not running (or not owned by this instance)")
    elif action == "open":
        url, _ = start_dashboard()
        import webbrowser; webbrowser.open(url); print(f"opened {url}")
    elif action == "logs":
        try:
            with open(_dashboard_paths().log) as f:
                print(f.read()[-4000:])
        except OSError:
            print("(no log yet)")
    return 0


def cmd_ingress(a):
    """Manage one foreground, hook-only public ingress for this project."""
    from . import ingress, ingress_state

    if a.action == "status":
        try:
            state = ingress_state.read_active_state()
        except (OSError, ValueError) as error:
            raise gs.GraphError(
                f"could not read public ingress state: {error}") from error
        if state is None:
            print("public webhook ingress offline")
        else:
            print(
                "public webhook ingress online → "
                f"{state['public_base_url']}")
        return 0

    guard.check(_actor(), "ingress_control", action=a.action)
    try:
        lease = ingress_state.acquire_lease()
        with lease:
            stop_event = threading.Event()

            def publish(public_url):
                if stop_event.is_set():
                    raise ingress.IngressStopped(
                        "public ingress stopped before publication")
                lease.publish(public_url)
                if stop_event.is_set():
                    lease.clear()
                    raise ingress.IngressStopped(
                        "public ingress stopped during publication")
                print(f"public webhook ingress online → {public_url}")
                print("copy a secret hook URL with: crew webhook show <name>")

            try:
                ingress.run_ingress(
                    stop_event=stop_event,
                    runtime_dir=lease.state_dir,
                    config_path=lease.config_path,
                    morphdb_origin=lease.origin,
                    app=lease.app,
                    on_ready=publish,
                    on_stopping=lease.clear,
                )
            except ingress.IngressStopped:
                pass
    except (
        ingress.IngressError,
        ingress_state.IngressStateError,
        ValueError,
    ) as error:
        raise gs.GraphError(str(error)) from error
    print("public webhook ingress stopped")
    return 0


# --------------------------------------------------------------------------- #
# parser
# --------------------------------------------------------------------------- #
def build_parser():
    p = argparse.ArgumentParser(prog="crew", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--project",
                   help="operate within this project (its own MorphDB app); "
                        "default project if omitted (or $CREW_PROJECT)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="set up MorphDB schema + start the dashboard")
    s.add_argument("--no-dashboard", action="store_true")
    s.set_defaults(fn=cmd_init)

    s = sub.add_parser("project", help="manage projects (isolated app-per-project)")
    proj_sub = s.add_subparsers(dest="project_cmd", required=True)
    sp = proj_sub.add_parser(
        "create", help="create a new project, seeded with a chat-to-build "
                       "foreman (like the dashboard gallery)")
    sp.add_argument("name")
    sp.add_argument("--description", default="",
                    help="what this graph is for (shown in the dashboard gallery)")
    sp.add_argument("--title", default="",
                    help="free-text display name for the gallery (the machine "
                         "slug stays `name`)")
    sp.add_argument("--no-foreman", action="store_true",
                    help="create an empty graph without the seeded foreman")
    sp.add_argument("--no-launch", action="store_true",
                    help="seed the foreman but don't start its runtime")
    sp.set_defaults(fn=cmd_project_create)
    sp = proj_sub.add_parser("list", help="list known projects")
    sp.set_defaults(fn=cmd_project_list)

    s = sub.add_parser("spawn-agent", help="create a long-running agent")
    s.add_argument("name")
    s.add_argument("--role", help="short role, e.g. 'leads agent'")
    s.add_argument("--identity", help="freeform identity/mission text")
    home_source = s.add_mutually_exclusive_group()
    home_source.add_argument(
        "--home", help="the agent's home directory (must not overlap another agent)")
    home_source.add_argument(
        "--repo", help="persistent named worktree branch from this repository "
                       "(instead of --home)")
    s.add_argument("--runtime", choices=runtimes.RUNTIME_KEYS,
                   help="coding-agent runtime (default: infer command, else $CREW_RUNTIME/claude)")
    s.add_argument("--launch-cmd", dest="launch_cmd", help="override the runtime launch command")
    s.add_argument("--no-launch", action="store_true", help="create its session but don't start the runtime")
    s.add_argument("--foreman", action="store_true",
                   help="grant graph-editing power (can_edit_graph) at creation — "
                        "human-only, singleton")
    s.set_defaults(fn=cmd_spawn_agent)

    s = sub.add_parser(
        "webhook", help="create and configure public webhook ingress nodes")
    webhook_sub = s.add_subparsers(dest="webhook_cmd", required=True)
    sw = webhook_sub.add_parser(
        "create", help="create a source-only webhook node")
    sw.add_argument("name")
    sw.add_argument("--description", default="", help="what this hook receives")
    sw.add_argument(
        "--template", default="",
        help="payload-to-message template using {{ payload.* }} placeholders")
    sw.set_defaults(fn=cmd_webhook_create)
    sw = webhook_sub.add_parser("list", help="list webhook nodes (URLs omitted)")
    sw.set_defaults(fn=cmd_webhook_list)
    sw = webhook_sub.add_parser(
        "show", help="show configuration and the secret POST URL (GATED)")
    sw.add_argument("name")
    sw.set_defaults(fn=cmd_webhook_show)
    sw = webhook_sub.add_parser(
        "update", help="change a webhook description or message template")
    sw.add_argument("name")
    sw.add_argument("--description", help="new description; pass '' to clear")
    sw.add_argument("--template", help="new message template; pass '' to clear")
    sw.set_defaults(fn=cmd_webhook_update)
    sw = webhook_sub.add_parser(
        "rotate", help="replace a webhook's secret POST URL immediately")
    sw.add_argument("name")
    sw.set_defaults(fn=cmd_webhook_rotate)
    sw = webhook_sub.add_parser(
        "remove", help="delete a webhook node and all of its routes")
    sw.add_argument("name")
    sw.set_defaults(fn=cmd_webhook_remove)

    s = sub.add_parser(
        "ingress", help="manage foreground public webhook ingress")
    ingress_sub = s.add_subparsers(dest="ingress_cmd", required=True)
    si = ingress_sub.add_parser(
        "run", help="expose hooks through a foreground Cloudflare tunnel")
    si.set_defaults(fn=cmd_ingress, action="run")
    si = ingress_sub.add_parser(
        "status", help="show whether this project's ingress is online")
    si.set_defaults(fn=cmd_ingress, action="status")

    s = sub.add_parser("connect", help="define a relationship A -> B (authorizes A to message B)")
    s.add_argument("source"); s.add_argument("target")
    s.add_argument("--label", help="short name for the relationship")
    s.add_argument("--desc", help="what each side does / how they relate")
    s.add_argument("--when", action="append",
                   help="a condition under which source messages target (repeatable for multiple)")
    s.add_argument("--does", help="what TARGET should do when source messages it")
    s.add_argument("--reply", action="store_true", help="target should reply to source")
    s.add_argument("--when-back", dest="when_back", action="append",
                   help="(two-way) a condition under which target messages source (repeatable)")
    s.add_argument("--does-back", dest="does_back", help="(two-way) what SOURCE does on receipt")
    s.add_argument("--reply-back", dest="reply_back", action="store_true",
                   help="(two-way) source should reply to target")
    s.add_argument("--max-turns", dest="max_turns", type=_nonnegative_int_arg, default=0,
                   help="rate-limit messages per hour on this link (0 = unlimited)")
    s.add_argument("--token-cap", dest="token_cap", type=_nonnegative_int_arg, default=0,
                   help="refuse sends once target has spent this many tokens in the last hour (0 = uncapped)")
    s.add_argument("--cost-cap", dest="cost_cap", type=_finite_float_arg, default=0.0,
                   help="refuse sends once target has spent this many $ in the last hour (0 = uncapped)")
    s.add_argument("--undirected", action="store_true", help="two-way: either may message the other")
    s.add_argument("--transform", metavar="FILE",
                   help="runs once before queueing or delivery for each message: "
                        "a script in var/transforms/ (human-only)")
    s.set_defaults(fn=cmd_connect)

    s = sub.add_parser("disconnect", help="remove the relationship(s) between two agents")
    s.add_argument("source"); s.add_argument("target")
    s.set_defaults(fn=cmd_disconnect)

    s = sub.add_parser("cap", help="update an edge's rate/budget caps (downhill-only for agents)")
    s.add_argument("source"); s.add_argument("target")
    s.add_argument("--max-turns", dest="max_turns", type=_nonnegative_int_arg, default=None,
                   help="new hourly rate limit")
    s.add_argument("--token-cap", dest="token_cap", type=_nonnegative_int_arg, default=None,
                   help="new hourly token budget")
    s.add_argument("--cost-cap", dest="cost_cap", type=_finite_float_arg, default=None,
                   help="new hourly $ budget")
    s.set_defaults(fn=cmd_cap)

    s = sub.add_parser("foreman", help="grant/revoke the foreman (can_edit_graph) flag — human-only, singleton")
    s.add_argument("name")
    s.add_argument("--revoke", action="store_true", help="revoke the flag instead of granting it")
    s.set_defaults(fn=cmd_foreman)

    s = sub.add_parser("bless", help="bless a graph node, an edge (--edge SRC DST), or --all — human-only")
    s.add_argument("agent", nargs="?", help="graph node name to bless")
    s.add_argument("--edge", nargs=2, metavar=("SRC", "DST"), help="bless the edge(s) SRC -> DST")
    s.add_argument(
        "--all", action="store_true",
        help="bless every unblessed graph node + edge")
    s.set_defaults(fn=cmd_bless)

    s = sub.add_parser(
        "activity",
        help="set your own status line ('working on website…') or, with no "
             "text, read every agent's current activity")
    s.add_argument("text", nargs="*", help="the status text; omit to read")
    s.add_argument("--agent", help="target/filter agent (setting is human-only "
                                   "for other agents; agents set their own)")
    s.add_argument("--clear", action="store_true", help="clear the status line")
    s.set_defaults(fn=cmd_activity)

    s = sub.add_parser(
        "harness",
        help="show the open goals each agent's coding harness reports, "
             "read from the harness's own state")
    s.add_argument("name", nargs="?", help="one agent; omit for all")
    s.add_argument("--json", action="store_true", help="machine-readable output")
    s.set_defaults(fn=cmd_harness)

    s = sub.add_parser("note", help="set a freeform note on an agent or edge")
    note_sub = s.add_subparsers(dest="note_cmd", required=True)
    sn = note_sub.add_parser("agent", help="note on an agent")
    sn.add_argument("name"); sn.add_argument("text")
    sn.set_defaults(fn=cmd_note_agent)
    sn = note_sub.add_parser("edge", help="note on an edge (source -> target)")
    sn.add_argument("source"); sn.add_argument("target"); sn.add_argument("text")
    sn.set_defaults(fn=cmd_note_edge)

    sub.add_parser("agents", help="list agents").set_defaults(fn=cmd_agents)
    sub.add_parser("edges", help="list relationships").set_defaults(fn=cmd_edges)

    s = sub.add_parser("message", help="message a connected agent (gated)")
    s.add_argument("target")
    s.add_argument(
        "-n", "--no-prefix", action="store_true",
        help="delivery safety and queueing still apply; omit the standard "
             "[crew msg from …] prefix")
    s.add_argument("words", nargs=argparse.REMAINDER, help="message body")
    s.set_defaults(fn=cmd_message)

    s = sub.add_parser(
        "kickoff", help="seed/steer one of YOUR agents directly (human-only)")
    s.add_argument("agent")
    s.add_argument("words", nargs=argparse.REMAINDER, help="the message / seed task")
    s.set_defaults(fn=cmd_kickoff)

    s = sub.add_parser("peers", help="show who an agent may message and who may message it")
    s.add_argument("name", nargs="?", help="agent (defaults to the current session's agent)")
    s.set_defaults(fn=cmd_peers)

    sub.add_parser("whoami", help="show your agent identity").set_defaults(fn=cmd_whoami)

    s = sub.add_parser("status", help="one-line-per-agent table: session, pane state, mail counts")
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("up", help="revive down agent(s): recreate the session or start its runtime")
    s.add_argument("name", nargs="?", help="agent to revive")
    s.add_argument("--all", action="store_true", help="every agent")
    s.set_defaults(fn=cmd_up)

    s = sub.add_parser("down", help="kill agent session(s); the record + home are kept")
    s.add_argument("name", nargs="?", help="agent to take down")
    s.add_argument("--all", action="store_true", help="every agent")
    s.set_defaults(fn=cmd_down)

    s = sub.add_parser("restart", help="down + up: bounce agent session(s)")
    s.add_argument("name", nargs="?", help="agent to restart")
    s.add_argument("--all", action="store_true", help="every agent")
    s.set_defaults(fn=cmd_restart)

    s = sub.add_parser("mail", help="show the message log, newest first")
    s.add_argument("agent", nargs="?", help="only messages sent by or to this agent")
    s.add_argument("--status",
                   choices=["queued", "submitting", "delivered",
                            "runtime_queued", "delivery_uncertain", "failed",
                            "blocked", "ratelimited", "budget",
                            "budget_unavailable", "filtered"],
                   help="filter by durable delivery status; submitting and "
                        "delivery_uncertain need operator attention, while "
                        "runtime_queued means the runtime accepted the input; "
                        "blocked/ratelimited/budget/budget_unavailable/filtered "
                        "are refused sends kept for audit")
    s.add_argument("-n", type=int, default=20, metavar="N",
                   help="max messages to show (default 20)")
    s.set_defaults(fn=cmd_mail)

    s = sub.add_parser("remove-agent", help="delete an agent")
    s.add_argument("name"); s.add_argument("--keep-session", action="store_true")
    s.set_defaults(fn=cmd_remove_agent)

    s = sub.add_parser("grant", help="grant an agent access to a path outside its home (human-only)")
    s.add_argument("agent")
    s.add_argument("path")
    mode_group = s.add_mutually_exclusive_group()
    mode_group.add_argument("--ro", action="store_true", help="read-only (default)")
    mode_group.add_argument("--rw", action="store_true", help="read-write")
    s.set_defaults(fn=cmd_grant)

    s = sub.add_parser("revoke-grant", help="revoke a previously granted path (human-only)")
    s.add_argument("agent")
    s.add_argument("name", help="the grant's name (refs/<name>) — see `crew grants <agent>`")
    s.set_defaults(fn=cmd_revoke_grant)

    s = sub.add_parser("grants", help="list an agent's (or every agent's) file grants — read-only, no gate")
    s.add_argument("agent", nargs="?", help="only this agent's grants")
    s.set_defaults(fn=cmd_grants)

    sub.add_parser("pending", help="list pending approval requests, newest first").set_defaults(fn=cmd_pending)

    s = sub.add_parser("approve", help="approve a pending request (human-only)")
    s.add_argument("guid", help="the pending request's guid, or a unique prefix")
    s.set_defaults(fn=cmd_approve)

    s = sub.add_parser("reject", help="reject a pending request (human-only)")
    s.add_argument("guid", help="the pending request's guid, or a unique prefix")
    s.add_argument("--why", help="reason shown to the requester")
    s.set_defaults(fn=cmd_reject)

    s = sub.add_parser("audit", help="show the graph-edit decision log (guard), newest first")
    s.add_argument("--refused", action="store_true", help="only refusals")
    s.add_argument("--actor", help="only this actor (\"human\" or an agent name)")
    s.add_argument("-n", type=int, default=20, metavar="N",
                   help="max rows to show (default 20)")
    s.set_defaults(fn=cmd_audit)

    s = sub.add_parser("dashboard", help="manage the dashboard server")
    s.add_argument("action", choices=["start", "stop", "status", "open", "logs"])
    s.set_defaults(fn=cmd_dashboard)

    return p


def main(argv=None):
    global _ACTOR
    _ACTOR = "human"
    args = build_parser().parse_args(argv)
    if getattr(args, "project", None):
        os.environ["CREW_PROJECT"] = args.project
    # Validate the selector before identity resolution or any command handler can
    # derive an app key, tmux name, or filesystem path from it.
    try:
        config.current_project()
    except ValueError as e:
        print(f"[crew] error: {e}", file=sys.stderr)
        return 1
    # Resolve the caller identity ONCE, the same anti-spoofing way crew.mail
    # resolves message senders: the live tmux pane's session wins over any
    # env var, so an agent's own shell can't claim to be another actor. Caller
    # resolution fails CLOSED: a crew pane selecting a different project/app
    # must never silently gain human authority.
    try:
        who = mail.whoami()
        registered = (
            gs.get_agent_by_name(who)
            if who and who != "unknown" else None)
        if registered:
            _ACTOR = who
        else:
            inherited = _inherited_agent_identity_hint()
            if inherited:
                key, value = inherited
                raise gs.GraphError(
                    f"inherited {key}={value!r} does not resolve to the "
                    "current registered agent; refusing to assume human "
                    "authority")
    except gs.GraphError as e:
        print(f"[crew] error: could not resolve caller identity: {e}", file=sys.stderr)
        return 1
    try:
        return args.fn(args)
    except (gs.GraphError, config.ProjectRegistryError) as e:
        msg = str(e)
        if "Unknown app" in msg:
            msg += "  — run `crew init` first to set up the crew backend."
        print(f"[crew] error: {msg}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
