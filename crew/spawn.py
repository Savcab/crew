"""crew.spawn — bring an agent to life: a home dir, a tmux session, a launched
coding-agent runtime, and an identity.md that makes it a durable, named agent.

The hard rules from the design:
  * ONE agent per directory, and NO agent nested inside another agent's home —
    enforced via graphstore.home_conflict BEFORE anything is created, so two
    agents' work can never overlap on disk.
  * Every agent gets an identity.md written into its home; a restarted claude
    re-reads it to resume the identity.

Side effects only — the durable data lives in MorphDB (crew.graphstore). Ports
the worktree/tmux mechanics from the old crew's spawn_worker, generalized: there
is no manager/worker role anymore, just agents.
"""
import contextlib
import fcntl
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
import threading
import time

from . import config, graphstore as gs, guard, identity, runtime as runtimes


# --------------------------------------------------------------------------- #
# small shell helpers (ported from the old crew CLI)
# --------------------------------------------------------------------------- #
def _run(cmd, cwd=None, timeout=120, env=None):
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                           timeout=timeout, env=env)
        return p.returncode == 0, p.stdout.strip(), p.stderr.strip()
    except (subprocess.SubprocessError, OSError) as e:
        return False, "", str(e)


def _git(args, cwd=None):
    ok, out, _ = _run(["git", *args], cwd=cwd)
    return out if ok else None


def _tmux(*args, timeout=10, endpoint=None):
    endpoint = endpoint or config.tmux_target_endpoint(*args)
    try:
        command = config.tmux_command(*args, endpoint=endpoint)
        environment = config.tmux_environment(endpoint=endpoint)
    except OSError as error:
        return False, str(error)
    ok, out, err = _run(command, timeout=timeout, env=environment)
    return ok, (out if ok else err)


# --------------------------------------------------------------------------- #
# home resolution
# --------------------------------------------------------------------------- #
def _worktree_paths(repo_cwd, name):
    """Compute the worktree PATH + default branch for a --repo agent WITHOUT creating
    anything — so the home-conflict / unsafe-home checks can run before we touch the
    repo. (The old `wt` layout: <repo>-worktrees/<name>.) Raises on a non-repo."""
    common = _git(["rev-parse", "--git-common-dir"], cwd=repo_cwd)
    if not common:
        raise gs.GraphError(f"--repo {repo_cwd!r} is not inside a git repo")
    if not os.path.isabs(common):
        common = os.path.abspath(os.path.join(repo_cwd, common))
    main_root = os.path.realpath(os.path.join(common, ".."))
    base, repo = os.path.dirname(main_root), os.path.basename(main_root)
    default = None
    ref = _git(["symbolic-ref", "refs/remotes/origin/HEAD"], cwd=repo_cwd)
    if ref:
        prefix = "refs/remotes/origin/"
        default = ref[len(prefix):] if ref.startswith(prefix) else None
    if not default:
        for cand in ("main", "master"):
            if _git(["rev-parse", "--verify", cand], cwd=repo_cwd) is not None:
                default = cand
                break
    default = default or (_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_cwd) or "HEAD")
    return os.path.realpath(os.path.join(base, f"{repo}-worktrees", name)), default


def _create_worktree(repo_cwd, wt, default, branch):
    """Materialize a worktree on a persistent named branch after safety checks."""
    if os.path.lexists(wt):
        if not os.path.isdir(wt):
            raise gs.GraphError(
                f"existing worktree path {wt!r} is not a directory")
        ok, out, err = _run(
            ["git", "worktree", "list", "--porcelain", "-z"],
            cwd=repo_cwd)
        if not ok:
            raise gs.GraphError(
                f"could not verify existing worktree {wt!r}: {err or out}")

        registered = []
        record = {}
        for field in out.split("\0"):
            if not field:
                if record:
                    registered.append(record)
                    record = {}
                continue
            key, _, value = field.partition(" ")
            record[key] = value
        if record:
            registered.append(record)

        match = None
        for candidate in registered:
            path = candidate.get("worktree")
            if not path:
                continue
            try:
                same = os.path.samefile(path, wt)
            except OSError:
                same = False
            if same:
                match = candidate
                break
        if match is None:
            raise gs.GraphError(
                f"existing path {wt!r} is not a registered worktree for "
                f"repository {repo_cwd!r}")

        expected_ref = f"refs/heads/{branch}"
        actual_ref = match.get("branch")
        if actual_ref != expected_ref:
            actual = (actual_ref.removeprefix("refs/heads/")
                      if actual_ref else "detached HEAD")
            raise gs.GraphError(
                f"existing worktree {wt!r} is on branch {actual!r}, not "
                f"the expected branch {branch!r}")
        return

    _git(["worktree", "prune"], cwd=repo_cwd)
    exists, _, _ = _run(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=repo_cwd)
    args = (["git", "worktree", "add", wt, branch] if exists else
            ["git", "worktree", "add", "-b", branch, wt, default])
    ok, out, err = _run(args, cwd=repo_cwd)
    if not ok:
        raise gs.GraphError(f"git worktree add failed: {err or out}")


def _is_registered_git_worktree(home):
    """Whether ``home`` is an exact top-level entry in Git's worktree registry."""
    top = _git(["rev-parse", "--show-toplevel"], cwd=home)
    if not top or os.path.realpath(top) != os.path.realpath(home):
        return False
    ok, raw, _ = _run(
        ["git", "worktree", "list", "--porcelain", "-z"], cwd=home)
    if not ok:
        return False
    for field in raw.split("\0"):
        if not field.startswith("worktree "):
            continue
        candidate = field.partition(" ")[2]
        if candidate and os.path.realpath(candidate) == os.path.realpath(home):
            return True
    return False


_CLAUDE_TRUST_MARKER = "_crewTrustPreviousState"
_CLAUDE_CONFIG_THREAD_LOCK = threading.RLock()


@contextlib.contextmanager
def _claude_config_lock():
    """Serialize Crew's Claude config read-modify-write across processes."""
    try:
        directory = config.runtime_state_dir("claude-config-locks")
    except OSError as error:
        raise gs.GraphError(f"could not secure Claude config lock: {error}") from error
    path = os.path.join(directory, "claude-config.lock")
    with _CLAUDE_CONFIG_THREAD_LOCK:
        flags = os.O_CREAT | os.O_RDWR
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(path, flags, 0o600)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise PermissionError(
                    f"unsafe Claude config lock file: {path}")
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
        except OSError as error:
            try:
                os.close(fd)
            except (NameError, OSError):
                pass
            raise gs.GraphError(
                f"could not secure Claude config lock: {error}") from error
        try:
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(fd)
            except OSError:
                pass


def _read_claude_config(path):
    """Read one regular config without following a user-controlled symlink."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return {}, 0o600
    if not stat.S_ISREG(info.st_mode):
        return None, None
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        with os.fdopen(fd) as stream:
            fd = None
            value = json.load(stream)
    finally:
        if fd is not None:
            os.close(fd)
    if not isinstance(value, dict):
        return None, None
    return value, stat.S_IMODE(info.st_mode) or 0o600


def _write_claude_config(path, value, mode):
    """Publish a complete config atomically, refusing a symlink swap."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        prefix=".claude.json.crew-", suffix=".tmp", dir=directory,
        text=True)
    try:
        os.fchmod(fd, mode or 0o600)
        with os.fdopen(fd, "w") as stream:
            fd = None
            json.dump(value, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            current = os.lstat(path)
        except FileNotFoundError:
            current = None
        if current is not None and not stat.S_ISREG(current.st_mode):
            return False
        os.replace(tmp, path)
        return True
    finally:
        if fd is not None:
            os.close(fd)
        try:
            os.remove(tmp)
        except FileNotFoundError:
            pass


def _pretrust_home(home):
    """Pre-accept Claude's "do you trust this folder?" dialog for the agent's home.

    A crew agent runs unattended, but a fresh `claude` in a never-before-seen dir
    pops a blocking trust prompt (separate from --dangerously-skip-permissions) and
    just sits there until someone picks "Yes". The agent's home is a dedicated
    workspace the USER created for it, so we record the same acceptance the dialog
    would — `projects[<home>].hasTrustDialogAccepted = true` in ~/.claude.json —
    before launching. Best-effort and atomic: any failure just means the user
    confirms the dialog once in the agent's terminal, so we never raise."""
    try:
        cfg_path = os.path.expanduser("~/.claude.json")
        with _claude_config_lock():
            try:
                cfg, mode = _read_claude_config(cfg_path)
            except (OSError, ValueError):
                # A malformed/unreadable user config is not ours to replace.
                return
            if cfg is None:
                return
            projects = cfg.setdefault("projects", {})
            if not isinstance(projects, dict):
                return
            entry = projects.setdefault(home, {})
            if not isinstance(entry, dict):
                return
            if entry.get("hasTrustDialogAccepted") is True:
                return  # user/preexisting trust is never marked as Crew-owned
            if _CLAUDE_TRUST_MARKER not in entry:
                entry[_CLAUDE_TRUST_MARKER] = {
                    "present": "hasTrustDialogAccepted" in entry,
                    "value": entry.get("hasTrustDialogAccepted"),
                }
            else:
                marker = entry[_CLAUDE_TRUST_MARKER]
                if not (isinstance(marker, dict)
                        and isinstance(marker.get("present"), bool)):
                    return  # never overwrite an unknown user-owned key
            entry["hasTrustDialogAccepted"] = True
            _write_claude_config(cfg_path, cfg, mode)
    except (OSError, gs.GraphError):
        pass


def _plan_home(name, home=None, repo=None, project=None):
    """Decide the agent's home PATH + optional worktree name WITHOUT creating it.
    Precedence: explicit --home, then --repo (a worktree), else <cwd>/<name>.
    Returns (home_path, worktree_name, materialize_plan) where materialize_plan is
    passed to _materialize_home AFTER the safety/conflict checks pass."""
    project = (config.current_project() if project is None
               else config.require_project_name(project))
    if repo:
        repo_cwd = os.path.abspath(os.path.expanduser(repo))
        worktree_name = (name if project == config.DEFAULT_PROJECT
                         else f"{project}__{name}")
        wt, default = _worktree_paths(repo_cwd, worktree_name)
        branch = f"crew/{project}/{name}"
        return wt, worktree_name, ("worktree", repo_cwd, default, branch)
    if home:
        h = os.path.realpath(os.path.expanduser(home))
    else:
        h = os.path.realpath(
            os.path.join(config.crew_root(), project, name))
    return h, None, ("mkdir",)


def _materialize_home(home_path, plan):
    """Create the home (or worktree) only after unsafe/conflict checks pass — so a
    refused spawn never leaves a stray dir inside another agent's tree."""
    if plan and plan[0] == "worktree":
        _create_worktree(plan[1], home_path, plan[2], plan[3])
    else:
        os.makedirs(home_path, exist_ok=True)


def _rollback_materialized_home(home_path, plan, existed_before):
    """Best-effort rollback for a home Crew created before spawn failed.

    Existing paths are never touched.  A normal home is removed only when still
    empty; a new worktree uses Git's non-forced removal, which refuses if any
    process managed to make it dirty.  These conservative operations may leave a
    recoverable artifact, but can never erase user work.
    """
    if existed_before:
        return
    if plan and plan[0] == "worktree":
        _run(["git", "worktree", "remove", home_path], cwd=plan[1])
        return
    try:
        os.rmdir(home_path)
    except (OSError, gs.GraphError):
        pass


# --------------------------------------------------------------------------- #
# identity.md (re)render
# --------------------------------------------------------------------------- #
def _resolve_neighbors(agent_guid):
    """(neighbor_agent_dict, edge) list for everyone this agent may message. The edge
    is annotated with this agent's OUTGOING view (`_conditions`, `_reply`) so the
    renderer shows the right direction's trigger list on a two-way edge."""
    out = []
    for tgt_guid, edge in gs.messageable_targets(agent_guid):
        try:
            nb = gs.get_object(tgt_guid)
        except gs.GraphError as error:
            if str(error).lstrip().startswith("404:"):
                nb = None
            else:
                raise
        if nb:
            v = gs.edge_view(edge, agent_guid)
            e = dict(edge); e["_conditions"] = v["out_conditions"]; e["_reply"] = v["out_reply"]
            out.append((nb, e))
    return out


def _resolve_incoming(agent_guid):
    """(peer_agent_dict, edge) list for everyone who may message this agent — the
    receiver's half. The edge is annotated with this agent's INCOMING view
    (`_action`, `_reply`) so the renderer shows what THIS agent does on receipt."""
    out = []
    for src_guid, edge in gs.incoming_edges(agent_guid):
        try:
            pr = gs.get_object(src_guid)
        except gs.GraphError as error:
            if str(error).lstrip().startswith("404:"):
                pr = None
            else:
                raise
        if pr:
            v = gs.edge_view(edge, agent_guid)
            e = dict(edge); e["_action"] = v["in_action"]; e["_reply"] = v["in_reply"]
            out.append((pr, e))
    return out


def _validated_agent_home(agent):
    """Canonical, safe stored home for an agent row, or a fail-closed error."""
    if not isinstance(agent, dict):
        raise gs.GraphError(
            "agent row has no valid absolute home")
    raw_home = agent.get("home")
    if not isinstance(raw_home, str) or not raw_home.strip():
        raise gs.GraphError(
            f"agent {agent.get('name') or '?'} has no valid absolute home")
    expanded_home = os.path.expanduser(raw_home)
    if not os.path.isabs(expanded_home):
        raise gs.GraphError(
            f"agent {agent.get('name') or '?'} has no valid absolute home "
            f"({raw_home!r})")
    try:
        stored_info = os.lstat(expanded_home)
    except FileNotFoundError:
        stored_info = None
    except OSError as error:
        raise gs.GraphError(
            f"agent {agent.get('name') or '?'} has no safely accessible home "
            f"({raw_home!r}): {error}") from error
    if stored_info is not None and stat.S_ISLNK(stored_info.st_mode):
        raise gs.GraphError(
            f"agent {agent.get('name') or '?'} has no valid absolute home: "
            f"stored home {raw_home!r} is a symlink")
    home = os.path.realpath(expanded_home)
    unsafe = gs.unsafe_home_reason(home)
    if unsafe:
        raise gs.GraphError(
            f"agent {agent.get('name') or '?'} has no valid absolute home: {unsafe}")
    return home


def notify_connection_change(agent):
    """Queue one durable best-effort connection-change notice for an agent.

    This is deliberately separate from identity rendering so graph mutations
    can publish every file transactionally before any irreversible notice is
    emitted.  The immutable target GUID prevents a reused name from receiving
    a stale endpoint's notice.
    """
    if (not isinstance(agent, dict)
            or agent.get("kind") == gs.WEBHOOK_KIND):
        return
    name = agent.get("name")
    guid = agent.get("_guid")
    if not name or not guid:
        return
    try:
        gs.create_message(
            "crew", name,
            f"your connections changed — re-read {config.IDENTITY_FILE} for who "
            "you may message now",
            status="queued", target_guid=guid)
    except Exception:
        pass


def rewrite_identity(agent, notify=False, exclude_agent_guids=None,
                     quota_override=None):
    """Re-render the agent's durable identity after its edges change, writing:
      * identity.md — the full human/agent-readable record in the home, and
      * the configured runtime's native managed file (CLAUDE.md / AGENTS.md),
        when that runtime defines one.

    `agent` is a dict with at least name + home + _guid. Legacy callers may use
    `notify=True` to queue a small heads-up after this single render. Edge
    transactions instead pass `notify=False` and invoke `notify_connection_change`
    only after every endpoint identity has committed. We never type a nudge into
    the pane blindly (that left unsubmitted text in the prompt and could land
    mid-dialog). The files are the source of truth regardless."""
    # Webhook nodes share the relation endpoint table but have no workspace,
    # tmux session, or runtime-native identity file. Their connected agents
    # still receive a normal incoming-peer entry.
    if (agent or {}).get("kind") == gs.WEBHOOK_KIND:
        return None
    home = _validated_agent_home(agent)

    excluded = set(exclude_agent_guids or ())
    neighbors = [
        pair for pair in _resolve_neighbors(agent["_guid"])
        if pair[0].get("_guid") not in excluded
    ]
    incoming = [
        pair for pair in _resolve_incoming(agent["_guid"])
        if pair[0].get("_guid") not in excluded
    ]
    # WAVE 3: a foreman's identity carries a live "Graph powers" section (quota
    # numbers computed fresh via guard.quota_state) — only bother computing it
    # for a can_edit_graph agent, the common case doesn't pay for the lookup.
    quota = None
    if agent.get("can_edit_graph"):
        quota = (quota_override if quota_override is not None
                 else guard.quota_state())
    try:
        portable_text = identity.render_identity_md(
            agent, neighbors, incoming, quota)
        runtime_key = runtimes.resolve_agent_runtime(agent)
        native_block = identity.render_native_md(agent, neighbors, incoming, quota)
        path = identity.write_identity_bundle(
            home, portable_text, runtime_key, native_block)
    except OSError as error:
        raise gs.GraphError(str(error)) from error
    if notify and (neighbors or incoming):
        notify_connection_change(agent)
    return path


def _projected_quota_after_agent_removal(agent):
    """Return the live quota snapshot as it will read after ``agent`` is gone.

    Survivor identities must be published before the irreversible agent delete,
    so a raw ``quota_state`` call still includes the target.  Mirror the two
    durable-row counters here while preserving every configured ceiling.
    """
    quota = dict(guard.quota_state())
    quota["agents_used"] = max(0, int(quota.get("agents_used") or 0) - 1)
    created_by = agent.get("created_by")
    if created_by not in (None, "", "human"):
        try:
            created_at = float(agent.get("created_at") or 0)
        except (TypeError, ValueError, OverflowError):
            created_at = 0
        if created_at >= time.time() - 3600:
            quota["spawns_this_hour"] = max(
                0, int(quota.get("spawns_this_hour") or 0) - 1)
    return quota


# --------------------------------------------------------------------------- #
# spawn
# --------------------------------------------------------------------------- #
def _session_context(name, project, runtime_key=None):
    """Ordered environment owned by every managed tmux session.

    tmux keeps a server-global environment that can be older than the CLI process
    creating/reviving a worker.  Always materialize Crew's effective context
    explicitly so an in-pane CLI cannot drift to another tenant or backend.
    """
    tmux_tmpdir = config.crew_tmux_tmpdir()
    return (
        ("AGENT_MAIL_NAME", name),
        ("CREW_AGENT", name),
        ("CREW_PROJECT", project),
        ("CREW_APP", config.current_app()),
        ("MORPHDB_HOST", config.MORPHDB_HOST),
        ("CREW_ROOT", config.crew_root()),
        ("CREW_RUNTIME", runtime_key or config.DEFAULT_RUNTIME),
        ("CREW_TMUX_TMPDIR", tmux_tmpdir),
        ("CREW_TMUX_SOCKET", config.crew_tmux_socket_path()),
        ("CREW_TMUX_SOCKET_NAME", config.CREW_TMUX_SOCKET_NAME),
        ("TMUX_TMPDIR", tmux_tmpdir),
        ("PATH", runtimes.agent_path()),
    )


def _pin_existing_session_context(
        session, name, project, runtime_key=None):
    """Repair the context of an already-existing bare/legacy session."""
    endpoint = config.tmux_target_endpoint(session)
    target = config.tmux_target(f"={session}", endpoint)
    for key, value in _session_context(name, project, runtime_key):
        ok, err = _tmux("set-environment", "-t", target, key, value)
        if not ok:
            raise gs.GraphError(
                f"could not pin {key} in tmux session '{session}': {err}")


def _pin_pane_context(
        pane, name, project, session=None, runtime_key=None):
    """Export Crew context inside the interactive shell owning ``pane``.

    A user's shell startup files can overwrite variables after tmux has built
    the session environment.  Updating tmux alone therefore is not sufficient:
    type one shell-quoted export into the exact owned pane before any runtime
    command so both the runtime and later in-pane Crew CLI calls inherit the
    same project/app/backend identity.
    """
    ready_channel = (
        f"crew-context-{os.getpid()}-{threading.get_ident()}-"
        f"{time.monotonic_ns()}")
    keys = [
        key for key, _value in _session_context(name, project, runtime_key)
    ]
    session = session or config.session_name(project, name)
    # A leading '=' is tmux's exact-name selector, but unquoted `=name` is a
    # special command-path expansion in zsh. Quote it explicitly in the command
    # typed into the pane (shlex.quote intentionally leaves '=' unquoted).
    shell_session_target = "'=" + session + "'"
    tmux_shell = "command " + " ".join(
        shlex.quote(part) for part in config.tmux_command())
    # Import tmux's already-pinned session values *after* shell startup files
    # run.  Asking tmux to emit shell-quoted assignments keeps this input line
    # short even when PATH is huge; typing the full expanded PATH could exceed
    # the tty's 1024-byte canonical startup buffer.  Every lookup and eval must
    # succeed before the shell signals readiness.  The session values are
    # already pinned (new-session -e or checked set-environment calls), so this
    # validates every imported key without embedding those potentially huge
    # values in the command itself.
    command = (
        "_crew_context_ok=0; for _crew_context_key in "
        + " ".join(shlex.quote(key) for key in keys)
        + "; do _crew_context_assignment=\"$(" + tmux_shell + " "
        "show-environment -s -t "
        + shell_session_target
        + " \"$_crew_context_key\")\" && eval \"$_crew_context_assignment\" "
        "|| { _crew_context_ok=1; break; }; done; "
        "unset _crew_context_assignment _crew_context_key; "
        '[ "$_crew_context_ok" = 0 ] && [ "$(command -v crew)" = '
        + shlex.quote(os.path.join(config.ROOT, "bin", "crew"))
        + " ]; _crew_context_ok=$?; "
        + f'[ "$_crew_context_ok" = 0 ] && {tmux_shell} wait-for -S '
        + shlex.quote(ready_channel) + "; unset _crew_context_ok"
    )
    # tmux send-keys only queues bytes. Signal through the tmux server from the
    # shell after the imports execute, so an immediate caller cannot interleave
    # its own command with setup.
    ok, err = _tmux("send-keys", "-t", pane, "-l", command)
    if not ok:
        raise gs.GraphError(
            f"could not pin Crew context inside pane {pane!r}: {err}")
    ok, err = _tmux("send-keys", "-t", pane, "Enter")
    if not ok:
        raise gs.GraphError(
            f"could not submit Crew context inside pane {pane!r}: {err}")
    ok, err = _tmux("wait-for", ready_channel, timeout=15)
    if not ok:
        raise gs.GraphError(
            f"Crew context did not finish inside pane {pane!r}: {err}")


def _verify_existing_session_context(
        session, name, project, endpoint=None, runtime_key=None):
    """Refuse to adopt a tmux session not already owned by this exact agent."""
    expected = dict(_session_context(name, project, runtime_key))
    endpoint = endpoint or config.tmux_target_endpoint(session)
    target = config.tmux_target(f"={session}", endpoint)
    for key in ("CREW_PROJECT", "CREW_AGENT", "CREW_APP", "MORPHDB_HOST"):
        ok, raw = _tmux("show-environment", "-t", target, key)
        prefix = key + "="
        actual = (raw or "").rstrip("\n")
        if not ok or not actual.startswith(prefix) or actual[len(prefix):] != expected[key]:
            raise gs.GraphError(
                f"refusing to adopt tmux session '{session}': ownership "
                f"environment {key} is missing or does not match this agent")


def _session_locations(agent, name, project):
    """Inspect both isolated Crew and pre-upgrade default tmux endpoints.

    Foreign sessions on the user's default server are intentionally ignored.
    A foreign collision inside Crew's private server is retained in the result
    so creation can fail loudly without ever adopting or killing it.
    """
    session = validated_session_name(agent, name, project)
    from .server import tmuxio
    owned = []
    exists = {}
    for endpoint in (config.TMUX_ENDPOINT_CREW, config.TMUX_ENDPOINT_LEGACY):
        exact = config.tmux_target(f"={session}", endpoint)
        endpoint_exists = _tmux("has-session", "-t", exact)[0]
        exists[endpoint] = endpoint_exists
        if not endpoint_exists:
            continue
        target = tmuxio.inspect_agent_session(
            agent, endpoint, project=project)
        if target is not None:
            owned.append(target)
    if len(owned) > 1:
        raise gs.GraphError(
            f"agent {name!r} is claimed by both Crew and legacy tmux servers; "
            "refusing to guess which live conversation is authoritative")
    return {
        "session": session,
        "owned": owned[0] if owned else None,
        "dedicated_exists": exists.get(config.TMUX_ENDPOINT_CREW, False),
        "legacy_exists": exists.get(config.TMUX_ENDPOINT_LEGACY, False),
    }


def validated_session_name(agent, name, project):
    """Return the sole session name Crew may target for this durable row.

    The row is untrusted durable input.  Legacy empty sessions can be derived
    safely, but a non-empty value must equal the canonical project/name mapping;
    accepting an arbitrary stored string would let a corrupt row redirect up,
    down, or remove onto an unrelated tmux session.
    """
    if not isinstance(agent, dict) or agent.get("name") != name:
        raise gs.GraphError(
            f"agent {name!r} has an invalid stored tmux identity")
    expected = config.session_name(project, name)
    stored = agent.get("session")
    if stored in (None, ""):
        return expected
    if not isinstance(stored, str) or stored != expected:
        raise gs.GraphError(
            f"agent {name!r} has invalid stored tmux session {stored!r}; "
            f"expected {expected!r}")
    return expected


def _owned_session_state(agent, name, project):
    """Return ``(canonical_name, exists)`` after live ownership verification."""
    locations = _session_locations(agent, name, project)
    if locations["owned"] is not None:
        return locations["owned"], True
    if locations["dedicated_exists"]:
        raise gs.GraphError(
            f"refusing to adopt tmux session {locations['session']!r} in "
            "Crew's private server: ownership context does not match this agent")
    return config.tmux_target(
        locations["session"], config.TMUX_ENDPOINT_CREW), False


def _session_owner_still_matches(agent, name, project, session):
    """Re-resolve one destructive ownership receipt at the kill boundary."""
    current = _session_locations(agent, name, project).get("owned")
    return config.same_tmux_target(current, session)


def _open_session(session, home, name, project, runtime_key="claude"):
    """Create the detached tmux session for `name` in
    `home`, with the agent-mail identity + complete Crew execution context pinned
    in the env, and return its runtime pane_id.  tmux's server-global environment
    may predate this CLI invocation, so relying on inheritance can select the
    wrong MorphDB/app (and make an owned pane look like a human operator).

    Kills the session and raises if the pane can't be resolved. Shared by
    spawn_agent (first boot) and start_session (revive a down agent). `session`
    is the (possibly project-prefixed) tmux session name; `name` is always the
    agent's PLAIN name — that's the messaging identity."""
    window = runtimes.window_name(runtime_key)
    env_args = []
    for key, value in _session_context(name, project, runtime_key):
        env_args.extend(("-e", f"{key}={value}"))
    ok, err = _tmux("new-session", "-d", "-s", session, "-n", window,
                    "-c", home, *env_args)
    if not ok:
        raise gs.GraphError(f"tmux new-session failed: {err}")
    ok, pane = _tmux("list-panes", "-t", f"{session}:{window}", "-F", "#{pane_id}")
    pane_id = pane.splitlines()[0].strip() if (ok and pane.strip()) else None
    if not pane_id:
        _tmux("kill-session", "-t", session)
        raise gs.GraphError(f"could not resolve the runtime pane for '{name}'")
    try:
        _pin_pane_context(
            pane_id, name, project, session=session, runtime_key=runtime_key)
    except gs.GraphError:
        _tmux("kill-session", "-t", f"={session}")
        raise
    return config.tmux_target(pane_id, config.TMUX_ENDPOINT_CREW)


def _launch_runtime(session, home, cmd, runtime_key, pane=None):
    """Launch a built-in runtime only after executable/readiness verification.

    Custom commands intentionally retain their historical fire-and-return
    semantics: Crew cannot know whether an arbitrary command is interactive or
    deliberately one-shot.  Claude and Codex have known process identities, so
    accepting only ``tmux send-keys`` would turn command-not-found and immediate
    process exits into false successful launches.
    """
    _require_runtime_launch_executable(runtime_key, cmd)
    if runtime_key == "claude":
        _pretrust_home(home)
    target = pane or f"{session}:{runtimes.window_name(runtime_key)}"
    ok, err = _tmux("send-keys", "-t", target, "-l", cmd)
    if not ok:
        raise gs.GraphError(
            f"could not type runtime launch command into '{target}': {err}")
    ok, err = _tmux("send-keys", "-t", target, "Enter")
    if not ok:
        _cancel_failed_runtime_launch(target)
        raise gs.GraphError(
            f"could not submit runtime launch command in '{target}': {err}")
    if runtime_key in ("claude", "codex"):
        try:
            _wait_for_runtime_ready(target, runtime_key, cmd)
        except Exception:
            # A truncated/unclosed command leaves the shell at a continuation
            # prompt; a late-starting runtime can also outlive our failed
            # readiness verdict.  Cancel the exact pane before returning a
            # retriable error so the next `crew up` begins at a clean prompt.
            _cancel_failed_runtime_launch(target)
            raise


def _cancel_failed_runtime_launch(target):
    """Best-effort reset of only the pane that received a failed launch."""
    return _tmux("send-keys", "-t", target, "C-c")


_RUNTIME_READY_TIMEOUT_SECONDS = 3.0
_RUNTIME_READY_STABILITY_SECONDS = 0.4
_RUNTIME_READY_POLL_SECONDS = 0.05


def _require_runtime_launch_executable(runtime_key, cmd):
    """Fail before typing when a built-in runtime's configured program is absent."""
    if runtime_key not in ("claude", "codex"):
        return
    program = runtimes.command_program(cmd)
    expanded = os.path.expanduser(program) if program else ""
    if expanded and os.path.dirname(expanded):
        available = os.path.isfile(expanded) and os.access(expanded, os.X_OK)
    else:
        available = bool(program and shutil.which(
            program, path=runtimes.agent_path()))
    if not available:
        shown = program or "<empty>"
        label = runtimes.adapter(runtime_key).label
        raise gs.GraphError(
            f"{label} launch executable {shown!r} is not available in the "
            "managed agent PATH")


def _runtime_process_present(target, runtime_key, cmd):
    """Whether the exact pane tty currently owns the configured built-in runtime."""
    from .server import tmuxio
    ok, raw_tty = _tmux(
        "list-panes", "-t", target, "-F", "#{pane_tty}")
    tty = (raw_tty or "").strip().splitlines()[0] if ok and raw_tty.strip() else ""
    if tty:
        ok, raw_ps, _ = _run(
            ["ps", "-axo", "tty=,comm=,command="], timeout=5)
        if ok:
            rows = tmuxio._parse_process_inventory(raw_ps).get(
                tmuxio._tty_name(tty), [])
            if any(runtimes.process_matches(
                    runtime_key, row.get("comm"), row.get("command"), cmd)
                    for row in rows):
                return True
    return tmuxio.pane_runs_runtime(target, runtime_key, cmd)


def _wait_for_runtime_ready(target, runtime_key, cmd):
    """Require a known runtime process to survive a short bounded stability window."""
    deadline = time.monotonic() + _RUNTIME_READY_TIMEOUT_SECONDS
    alive_since = None
    while True:
        now = time.monotonic()
        if _runtime_process_present(target, runtime_key, cmd):
            if alive_since is None:
                alive_since = now
            if now - alive_since >= _RUNTIME_READY_STABILITY_SECONDS:
                return
        else:
            alive_since = None
        if now >= deadline:
            break
        time.sleep(min(
            _RUNTIME_READY_POLL_SECONDS,
            max(0.0, deadline - now)))
    label = runtimes.adapter(runtime_key).label
    raise gs.GraphError(
        f"{label} runtime did not stay running in pane {target!r} after launch; "
        "check the launch command and pane output")


def _launch_claude(session, home, cmd):
    """Compatibility wrapper for older callers."""
    return _launch_runtime(session, home, cmd, "claude")


def _refuse_spawn_confinement(actor, name, arg, reason, actor_guid=None):
    """Refuse an agent actor's --home/--repo/--launch-cmd override, WITH an
    audit row (crew.guard's audit(), so `crew audit` shows why just like any
    other refusal — this check lives in spawn.py, not guard.py, because it's
    about the ARGS an agent actor passed, not about graph topology)."""
    audit_identity = {"actor_guid": actor_guid} if actor_guid else {}
    guard.audit(
        actor, "spawn", {"name": name, "refused_arg": arg}, "refused",
        reason, **audit_identity)
    raise gs.GraphError(reason)


def spawn_agent(name, role="", agent_identity="", home=None, repo=None,
                launch=True, launch_cmd=None, runtime=None, actor="human",
                foreman=False):
    """Serialize a name from pre-row side effects through initial launch."""
    try:
        config.current_project()
    except ValueError as error:
        raise gs.GraphError(str(error)) from error
    with gs._invariant_lock("agent-lifecycle"):
        actor_guid = (
            "" if actor == "human" else gs._resolve_actor_guid(actor))
        if actor != "human":
            gs._require_actor_guid(actor, actor_guid)
        return _spawn_agent_locked(
            name, role=role, agent_identity=agent_identity, home=home,
            repo=repo, launch=launch, launch_cmd=launch_cmd,
            runtime=runtime, actor=actor, foreman=foreman,
            _actor_guid=actor_guid)


def _spawn_agent_locked(name, role="", agent_identity="", home=None, repo=None,
                        launch=True, launch_cmd=None, runtime=None,
                        actor="human", foreman=False, _actor_guid=None):
    """Create a new agent end-to-end. Returns the MorphDB agent dict.

    Steps (fail-fast, cleans up a half-made tmux session on a later error):
      1. validate name + reject a home that collides with an existing agent's;
      2. make the home dir (or a git worktree for --repo);
      3. start a detached tmux session named `name`, pinned to AGENT_MAIL_NAME so
         the agent's messaging identity is fixed;
      4. write the MorphDB record + identity.md;
      5. launch claude in the pane and (after it boots) inject the spawn context.

    `actor` is gated FIRST (guard.check, op "spawn") — before the tmux session
    or home dir get created — so a refused spawn never leaves stray side
    effects on disk/in tmux. gs.create_agent re-checks (harmless — same actor,
    same answer) and is what actually stamps created_by/blessed.

    WAVE 2: an agent actor (any actor != "human") may NOT pick its own home —
    --home/--repo/--launch-cmd are refused outright, and the home always lands
    under crew_root()/<current_project()>/<name> (_plan_home's own default
    branch, reached by forcing home=repo=None below) with the project's
    default launch command. A human spawning is completely unaffected.

    WAVE 3: `foreman=True` grants can_edit_graph AT CREATION — human-only
    (an agent actor passing --foreman is refused outright, same confinement
    pattern as --home/--repo/--launch-cmd) and still subject to the SINGLETON
    rule (crew.guard._check_foreman_singleton, checked here before any side
    effect so a refused --foreman spawn never leaves a stray tmux session)."""
    try:
        project = config.current_project()
    except ValueError as e:
        raise gs.GraphError(str(e)) from e
    # These selectors describe two incompatible ownership models.  Refuse the
    # ambiguous request before the permission gate, graph lookup, repo probe, or
    # any other external boundary can run; silently preferring --repo used to
    # make the caller's explicit --home assertion untrue.
    if home is not None and repo is not None:
        raise gs.GraphError("--home and --repo are mutually exclusive")
    guard.check(actor, "spawn", name=name)
    actor_guid = (
        "" if actor == "human"
        else (_actor_guid or gs._resolve_actor_guid(actor)))
    if actor != "human":
        gs._require_actor_guid(actor, actor_guid)
    if not config.valid_agent_name(name):
        raise gs.GraphError(
            f"invalid agent name {name!r}: letters, digits, '_', '-' only "
            "(no dots/slashes/spaces), max 64 chars")
    if gs.get_node_by_name(name):
        raise gs.GraphError(f"a graph node named '{name}' already exists")

    if actor != "human":
        if home:
            _refuse_spawn_confinement(actor, name, "home",
                "agents may not pass --home — a spawned agent's home is "
                "always under the project's standard tree "
                f"({config.crew_root()}/{config.current_project()}/<name>) — "
                "ask the user to spawn it with a custom home", actor_guid)
        if repo:
            _refuse_spawn_confinement(actor, name, "repo",
                "agents may not pass --repo — a spawned agent's home is "
                "always under the project's standard tree, never a git "
                "worktree of your choosing — ask the user", actor_guid)
        if launch_cmd:
            _refuse_spawn_confinement(actor, name, "launch_cmd",
                "agents may not override --launch-cmd — a spawned agent "
                "always launches with the project default — ask the user",
                actor_guid)
        if runtime:
            _refuse_spawn_confinement(actor, name, "runtime",
                "agents may not override --runtime — a spawned agent always "
                "uses the project default — ask the user", actor_guid)
        if foreman:
            _refuse_spawn_confinement(actor, name, "foreman",
                "agents may not pass --foreman — granting graph-editing power "
                "requires a human — ask the user to run `crew foreman <name>`",
                actor_guid)
        home, repo, launch_cmd, runtime = None, None, None, None  # belt-and-suspenders

    if foreman:
        guard.check(actor, "foreman", name=name, revoke=False)

    # Plan the home PATH first, run the safety + overlap checks, and only THEN
    # create it — so a refused spawn (unsafe home, or one nested in another agent's
    # tree) never leaves a stray directory or worktree behind on disk.
    home_path, worktree, plan = _plan_home(
        name, home=home, repo=repo, project=project)

    bad = gs.unsafe_home_reason(home_path)
    if bad:
        raise gs.GraphError(bad)
    try:
        runtime_key = runtimes.resolve_runtime(runtime, launch_cmd)
        cmd = runtimes.launch_command(
            runtime_key, home_path, launch_cmd,
            environment=dict(_session_context(name, project, runtime_key)))
    except ValueError as e:
        raise gs.GraphError(str(e)) from e
    # tmux session — refuse a pre-existing same-named session (never adopt one the
    # user is already running; that's the whole "crew only manages its own" rule).
    # Session name is project-scoped (config.session_name) so two projects' agents
    # never collide on a tmux session; the messaging identity (CREW_AGENT /
    # AGENT_MAIL_NAME) stays the PLAIN name regardless of project.
    session = config.session_name(project, name)
    # Cross-project home ownership is one backend-global invariant, not an
    # app-local one. Keep the final overlap/name/session checks and every claim
    # side effect under one interprocess lock. Otherwise simultaneous spawns in
    # distinct apps can both observe an empty graph and share a physical home.
    # gs.create_agent takes the inner app agent lock; no path takes those locks
    # in the opposite order.
    with gs._home_claim_lock():
        if gs.get_node_by_name(name):
            raise gs.GraphError(f"a graph node named '{name}' already exists")
        conflict = gs.home_conflict_across_apps(home_path)
        if conflict:
            raise gs.GraphError(
                f"home {home_path!r} overlaps agent '{conflict['name']}' "
                f"(home {conflict.get('home')!r}). One agent per directory, and no "
                "agent inside another's tree — pick a separate directory.")
        if _tmux("has-session", "-t", session)[0]:
            raise gs.GraphError(
                f"a tmux session named '{session}' already exists; kill it first "
                f"(tmux kill-session -t {session}) or pick another name")
        home_existed = os.path.lexists(home_path)
        pane_id = None
        create_attempted = False
        try:
            _materialize_home(home_path, plan)
            pane_id = _open_session(
                session, home_path, name, project, runtime_key)
            create_attempted = True
            agent = gs.create_agent(
                name, role=role, identity=agent_identity, home=home_path,
                session=session, pane=pane_id, worktree=worktree or "",
                runtime=runtime_key, launch_cmd=cmd,
                status="idle" if launch else "not_started",
                can_edit_graph=bool(foreman), actor=actor,
                _actor_guid=actor_guid)
        except Exception:
            # An HTTP write can commit and then lose its response.  Never tear
            # down a session/home unless the backend positively confirms that
            # no durable row exists; on an unavailable backend, preservation is
            # safer than turning a recoverable spawn into a row with no home.
            durable_or_unknown = False
            if create_attempted:
                try:
                    durable_or_unknown = bool(gs.get_agent_by_name(name))
                except Exception:
                    durable_or_unknown = True
            if not durable_or_unknown:
                if pane_id is not None:
                    _tmux("kill-session", "-t", f"={session}")
                _rollback_materialized_home(home_path, plan, home_existed)
            raise

    # Close the create-row→initial-publication gap with the same GUID lock used
    # by grants, foreman changes, and edge identity transactions. A mutation may
    # have completed in the tiny interval before this lock was acquired, so
    # refetch under the lock and publish that current durable row, never the
    # stale POST response.
    with gs._identity_transaction_locks((agent["_guid"],)):
        current = gs.get_agent_by_name(name)
        if (not current or current.get("_guid") != agent.get("_guid")):
            raise gs.GraphError(
                f"agent {name!r} changed before its initial identity publish; "
                "retry after inspecting the durable row")
        agent = current
        # identity.md + native runtime identity. The runtime auto-loads this at
        # startup, avoiding a delayed send-keys identity injection.
        try:
            rewrite_identity(agent)
        except gs.GraphError:
            # The row/session are intentionally kept recoverable (the user may
            # replace a conflicting symlink and run `crew up`), but the runtime
            # was never launched and must not remain advertised as idle.
            if launch:
                try:
                    gs.update_agent_runtime_state(
                        agent["_guid"], status="not_started")
                except gs.GraphError:
                    pass
            raise

    if launch:
        try:
            _launch_runtime(session, home_path, cmd, runtime_key, pane=pane_id)
        except gs.GraphError:
            # Keep the durable row/session recoverable via `crew up`, but never
            # claim an interactive runtime started when tmux rejected the launch.
            try:
                gs.update_agent_runtime_state(
                    agent["_guid"], status="not_started")
            except gs.GraphError:
                pass
            raise
    return agent


def _first_session_pane(session, runtime_key):
    """Best shell pane for starting a runtime in an existing bare session."""
    for window in (runtimes.window_name(runtime_key), "claude"):
        ok, pane = _tmux("list-panes", "-t", f"{session}:{window}",
                         "-F", "#{pane_id}")
        if ok and pane.strip():
            return config.tmux_target(
                pane.strip().splitlines()[0],
                config.tmux_target_endpoint(session))
    ok, pane = _tmux("list-panes", "-t", session, "-F", "#{pane_id}")
    return (config.tmux_target(
        pane.strip().splitlines()[0], config.tmux_target_endpoint(session))
        if ok and pane.strip() else None)


def start_session(name, actor="human"):
    """Serialize revive against removal, grants, and identity mutations."""
    try:
        config.current_project()
    except ValueError as error:
        raise gs.GraphError(str(error)) from error
    with gs._invariant_lock("agent-lifecycle"):
        initial = gs.get_agent_by_name(name)
        if not initial:
            raise gs.GraphError(f"no such agent: {name}")
        guid = initial.get("_guid")
        actor_guid = (
            "" if actor == "human" else gs._resolve_actor_guid(actor))
        with gs._identity_transaction_locks((guid, actor_guid)):
            if actor != "human":
                gs._require_actor_guid(actor, actor_guid)
            current = gs.get_agent_by_name(name)
            if (not current or current.get("_guid") != guid
                    or current.get("name") != name):
                raise gs.GraphError(
                    f"agent {name!r} changed while waiting to start; retry")
            return _start_session_locked(
                name, actor=actor, _expected_guid=guid,
                _actor_guid=actor_guid)


def _start_session_locked(
        name, actor="human", _expected_guid=None, _actor_guid=None):
    """Bring an EXISTING agent back up: (re)create its tmux session and relaunch
    claude in its home, so a 'down' agent can be revived from the dashboard. The
    record is durable; only the live session died. Idempotent — if the session is
    already running it's left alone. Refreshes the stored pane_id. Returns the agent.

    Gated FIRST (guard.check, op "up") — before touching tmux — so a refused
    revive never leaves a stray session."""
    try:
        project = config.current_project()
    except ValueError as e:
        raise gs.GraphError(str(e)) from e
    a = gs.get_agent_by_name(name)
    if not a:
        raise gs.GraphError(f"no such agent: {name}")
    if _expected_guid and a.get("_guid") != _expected_guid:
        raise gs.GraphError(
            f"agent {name!r} changed while waiting to start; retry")
    actor_guid = (
        "" if actor == "human"
        else (_actor_guid or gs._resolve_actor_guid(actor)))
    if actor != "human":
        gs._require_actor_guid(actor, actor_guid)
    guard.check(actor, "up", name=name)
    session = validated_session_name(a, name, project)
    home = _validated_agent_home(a)
    # A recorded Git worktree is not interchangeable with an ordinary home
    # directory.  If it vanished, ``makedirs`` would silently recreate the path
    # without Git metadata/registration and the revived agent would work in a
    # misleading, branchless directory.  Preserve the durable row and require
    # the operator to restore or deliberately replace the worktree instead.
    if a.get("worktree"):
        if not os.path.isdir(home):
            raise gs.GraphError(
                f"recorded git worktree for agent {name!r} is missing at "
                f"{home!r}; restore the worktree before starting this agent")
        if not _is_registered_git_worktree(home):
            raise gs.GraphError(
                f"recorded worktree path {home!r} is not a registered git "
                "worktree; restore the worktree before starting this agent")
    runtime_key = runtimes.resolve_agent_runtime(a)
    try:
        cmd = runtimes.revive_launch_command(
            runtime_key, home, a.get("launch_cmd"),
            environment=dict(_session_context(name, project, runtime_key)))
    except ValueError as e:
        raise gs.GraphError(str(e)) from e
    locations = _session_locations(a, name, project)
    live_session = locations["owned"]
    if (live_session is not None
            and live_session.endpoint == config.TMUX_ENDPOINT_LEGACY):
        raise gs.GraphError(
            f"agent {name!r} is still running in Crew's pre-upgrade tmux "
            f"server; `crew up` will not interrupt its active conversation. "
            f"Run `crew down {name}` when you are ready to stop it, then "
            f"`crew up {name}` to recreate it on Crew's dedicated tmux endpoint")
    if live_session is None and locations["dedicated_exists"]:
        raise gs.GraphError(
            f"refusing to adopt tmux session {session!r} in Crew's private "
            "server: ownership context does not match this agent")
    exists = live_session is not None
    session = (live_session if live_session is not None else
               config.tmux_target(session, config.TMUX_ENDPOINT_CREW))
    if exists:
        _pin_existing_session_context(
            session, name, project, runtime_key=runtime_key)
        from .server import tmuxio
        running_pane = tmuxio.exact_runtime_pane(a, session)
        if running_pane:
            result = a
            if str(running_pane) != str(a.get("pane") or ""):
                result = gs.update_agent_runtime_state(
                    a["_guid"], pane=str(running_pane), status="idle") or a
            guard.audit(
                actor, "up", {"name": name}, "applied",
                **({"actor_guid": actor_guid} if actor_guid else {}))
            return result
        pane_id = _first_session_pane(session, runtime_key)
        if not pane_id:
            raise gs.GraphError(f"could not resolve a pane in session '{session}'")
        _pin_pane_context(
            pane_id, name, project, session=session, runtime_key=runtime_key)
    os.makedirs(home, exist_ok=True)   # home should already exist; be safe
    if not exists:
        pane_id = _open_session(
            str(session), home, name, project, runtime_key)
    rewrite_identity(a)                # keep identity files current on revive
    try:
        _launch_runtime(session, home, cmd, runtime_key, pane=pane_id)
    except gs.GraphError:
        # A bare managed session is still recoverable, but the durable row must
        # not continue advertising a runtime that failed readiness.
        try:
            gs.update_agent_runtime_state(a["_guid"], status="not_started")
        except gs.GraphError:
            pass
        raise
    result = gs.update_agent_runtime_state(
        a["_guid"], pane=str(pane_id), status="idle") or a
    guard.audit(
        actor, "up", {"name": name}, "applied",
        **({"actor_guid": actor_guid} if actor_guid else {}))
    return result


def stop_session(name, actor="human", refuse_legacy=False):
    """Serialize stop against revive/removal and durable identity mutations."""
    try:
        config.current_project()
    except ValueError as error:
        raise gs.GraphError(str(error)) from error
    with gs._invariant_lock("agent-lifecycle"):
        initial = gs.get_agent_by_name(name)
        if not initial:
            raise gs.GraphError(f"no such agent: {name}")
        guid = initial.get("_guid")
        actor_guid = (
            "" if actor == "human" else gs._resolve_actor_guid(actor))
        with gs._identity_transaction_locks((guid, actor_guid)):
            if actor != "human":
                gs._require_actor_guid(actor, actor_guid)
            current = gs.get_agent_by_name(name)
            if (not current or current.get("_guid") != guid
                    or current.get("name") != name):
                raise gs.GraphError(
                    f"agent {name!r} changed while waiting to stop; retry")
            return _stop_session_locked(
                name, actor=actor, _expected_guid=guid,
                _actor_guid=actor_guid, _refuse_legacy=refuse_legacy)


def _stop_session_locked(
        name, actor="human", _expected_guid=None, _actor_guid=None,
        _refuse_legacy=False):
    """Stop one exact Crew-owned session while preserving its durable row.

    Both the stored name and the live ownership environment are treated as
    untrusted.  This is the only safe tmux-kill boundary for ``crew down`` and
    ``crew restart``; callers receive ``True`` when a session was killed and
    ``False`` when it was already absent.
    """
    try:
        project = config.current_project()
    except ValueError as e:
        raise gs.GraphError(str(e)) from e
    agent = gs.get_agent_by_name(name)
    if not agent:
        raise gs.GraphError(f"no such agent: {name}")
    if _expected_guid and agent.get("_guid") != _expected_guid:
        raise gs.GraphError(
            f"agent {name!r} changed while waiting to stop; retry")
    actor_guid = (
        "" if actor == "human"
        else (_actor_guid or gs._resolve_actor_guid(actor)))
    if actor != "human":
        gs._require_actor_guid(actor, actor_guid)
    guard.check(actor, "down", name=name)
    session, exists = _owned_session_state(agent, name, project)
    if (exists and _refuse_legacy
            and config.tmux_target_endpoint(session)
            == config.TMUX_ENDPOINT_LEGACY):
        reason = (
            f"agent {name!r} is running in Crew's pre-upgrade tmux server; "
            "restart refuses to kill that active conversation before the new "
            f"endpoint is proven. Run `crew down {name}` when ready, then "
            f"`crew up {name}`")
        guard.audit(
            actor, "down", {"name": name}, "refused", reason,
            **({"actor_guid": actor_guid} if actor_guid else {}))
        raise gs.GraphError(reason)
    if not exists:
        guard.audit(
            actor, "down", {"name": name}, "applied",
            **({"actor_guid": actor_guid} if actor_guid else {}))
        return False
    # Ownership is a live capability, not a durable-row promise. Re-read both
    # endpoints immediately before the destructive tmux command so a session
    # that died, moved servers, or was replaced after the first inspection is
    # never killed solely because it reused the same text name.
    if not _session_owner_still_matches(
            agent, name, project, session):
        reason = (
            f"agent {name!r}'s tmux ownership changed while stopping; retry")
        guard.audit(
            actor, "down", {"name": name}, "refused", reason,
            **({"actor_guid": actor_guid} if actor_guid else {}))
        raise gs.GraphError(reason)
    exact = config.tmux_target(
        f"={session}", config.tmux_target_endpoint(session))
    ok, err = _tmux("kill-session", "-t", exact)
    if not ok:
        reason = (err or "").strip() or "kill-session failed"
        guard.audit(
            actor, "down", {"name": name}, "refused", reason,
            **({"actor_guid": actor_guid} if actor_guid else {}))
        raise gs.GraphError(reason)
    guard.audit(
        actor, "down", {"name": name}, "applied",
        **({"actor_guid": actor_guid} if actor_guid else {}))
    return True


def _untrust_home(home):
    """Restore exactly the trust state Crew replaced for ``home``.

    A preexisting trusted entry carries no Crew marker and is therefore left
    untouched.  Entries Crew changed remember both key presence and value, so
    removal never deletes the user's unrelated per-project metadata.
    """
    if not home:
        return
    try:
        cfg_path = os.path.expanduser("~/.claude.json")
        with _claude_config_lock():
            try:
                cfg, mode = _read_claude_config(cfg_path)
            except (OSError, ValueError):
                return
            if cfg is None:
                return
            projects = cfg.get("projects")
            if not isinstance(projects, dict):
                return
            entry = projects.get(home)
            if not isinstance(entry, dict):
                return
            marker = entry.get(_CLAUDE_TRUST_MARKER)
            if not (isinstance(marker, dict)
                    and isinstance(marker.get("present"), bool)):
                return
            entry.pop(_CLAUDE_TRUST_MARKER, None)
            if marker["present"]:
                entry["hasTrustDialogAccepted"] = marker.get("value")
            else:
                entry.pop("hasTrustDialogAccepted", None)
            if not entry:
                projects.pop(home, None)
            _write_claude_config(cfg_path, cfg, mode)
    except OSError:
        pass


def _removal_identity_guids(agent_guid):
    """Optimistic complete GUID lock set for one agent removal."""
    return tuple(dict.fromkeys(
        endpoint for edge in gs.edges_touching(agent_guid)
        for endpoint in (
            agent_guid, edge.get("source"), edge.get("target"))
        if endpoint
    )) or (agent_guid,)


def remove_agent(name, kill_session=True, actor="human"):
    """Serialize session shutdown through durable graph/identity deletion."""
    try:
        config.current_project()
    except ValueError as error:
        raise gs.GraphError(str(error)) from error
    with gs._invariant_lock("agent-lifecycle"):
        initial = gs.get_agent_by_name(name)
        if not initial:
            raise gs.GraphError(f"no such agent: {name}")
        guid = initial.get("_guid")
        for _attempt in range(8):
            planned = _removal_identity_guids(guid)
            with gs._identity_transaction_locks(planned):
                current = gs.get_agent_by_name(name)
                if (not current or current.get("_guid") != guid
                        or current.get("name") != name):
                    raise gs.GraphError(
                        f"agent {name!r} changed while waiting to remove; retry")
                if set(_removal_identity_guids(guid)) != set(planned):
                    continue
                return _remove_agent_locked(
                    name, kill_session=kill_session, actor=actor,
                    _expected_guid=guid)
        raise gs.GraphError(
            "agent connections kept changing during removal; retry once graph "
            "edits settle")


def _remove_agent_locked(name, kill_session=True, actor="human",
                         _expected_guid=None):
    """Kill an owned session, then delete the agent + incident durable edges.

    The home dir + identity.md are left on disk.  Session shutdown deliberately
    precedes the MorphDB cascade: a failed kill keeps the complete graph
    retriable instead of orphaning a live process whose identity row is gone.
    Human-only (guard.check inside gs.delete_agent, op "remove" — not even a
    foreman may remove an agent this wave).
    """
    try:
        project = config.current_project()
    except ValueError as e:
        raise gs.GraphError(str(e)) from e
    a = gs.get_agent_by_name(name)
    if not a:
        raise gs.GraphError(f"no such agent: {name}")
    if _expected_guid and a.get("_guid") != _expected_guid:
        raise gs.GraphError(
            f"agent {name!r} changed while waiting to remove; retry")
    # delete_agent gates again at the durable mutation boundary.  Gate here as
    # well so a refused actor cannot use session validation as an ownership
    # oracle, then validate every tmux target before the row disappears.
    guard.check(actor, "remove", name=name)
    session = None
    session_exists = False
    if kill_session:
        session, session_exists = _owned_session_state(a, name, project)
    if kill_session and session_exists:
        if not _session_owner_still_matches(a, name, project, session):
            raise gs.GraphError(
                f"agent {name!r}'s tmux ownership changed while removing; "
                "agent row and edges were preserved; retry")
        exact = config.tmux_target(
            f"={session}", config.tmux_target_endpoint(session))
        ok, err = _tmux("kill-session", "-t", exact)
        if not ok:
            raise gs.GraphError(
                f"owned tmux session {session!r} could not be killed; agent "
                "row and edges were preserved: "
                f"{(err or '').strip() or 'kill-session failed'}")
    projected_quota = None

    def projected_identity(agent, notify=False):
        nonlocal projected_quota
        extra = {}
        if agent.get("can_edit_graph"):
            if projected_quota is None:
                projected_quota = _projected_quota_after_agent_removal(a)
            extra["quota_override"] = projected_quota
        return rewrite_identity(
            agent, notify=notify, exclude_agent_guids={a["_guid"]}, **extra)

    gs.delete_agent(
        a["_guid"], actor=actor,
        _identity_projector=projected_identity,
        _identity_rewriter=rewrite_identity,
        _identity_notifier=notify_connection_change)
    if runtimes.resolve_agent_runtime(a) == "claude":
        _untrust_home(a.get("home"))
    return a


# --------------------------------------------------------------------------- #
# GRANTS WAVE: a symlink + declared-intent exception to an agent's home
# boundary. Two honesty-labeled layers (see crew.identity.render_file_grants
# for the exact wording rendered into identity.md/CLAUDE.md): the symlink +
# `grants` list entry are REAL side effects (discoverability + declared
# intent) but NOTHING here is filesystem-enforced — no sandbox stops the
# agent's shell from reaching anywhere else it already could. `mode` is
# recorded intent only.
#
# The permission gate (human-only; a foreman's `grant` is queued to the wave-4
# pending machinery instead of refused) lives in crew.guard.check(op="grant");
# these are the MECHANICS, called both by `crew grant` directly (actor=
# "human") and by guard.approve_pending's replay (actor=<original foreman
# requester>, _pre_approved=True — same escape-hatch shape as
# graphstore.create_edge/update_edge).
# --------------------------------------------------------------------------- #
_GRANT_SLUG_RE = re.compile(r"[^A-Za-z0-9_.-]+")
_GRANT_NAME_MAX_BYTES = 255


def _grant_slug(path):
    """Filesystem-safe slug from a path's basename — the default refs/<name>."""
    base = os.path.basename(str(path).rstrip(os.sep)) or "root"
    slug = _GRANT_SLUG_RE.sub("_", base).strip("_.")
    # Leave room for a decimal de-duplication suffix while staying beneath the
    # common NAME_MAX boundary. The replacement above makes this ASCII-only.
    return (slug or "grant")[:220]


def _validate_grant_name(name):
    """Return a single safe refs/ component, never a path traversal."""
    if (not isinstance(name, str) or not name or name in (".", "..")
            or _GRANT_SLUG_RE.search(name)
            or len(os.fsencode(name)) > _GRANT_NAME_MAX_BYTES):
        raise gs.GraphError(
            f"invalid grant name {name!r}: expected one filesystem-safe refs/ name")
    return name


def _grant_fs_error(action, error):
    if isinstance(error, gs.GraphError):
        return error
    return gs.GraphError(f"could not safely {action}: {error}")


@contextlib.contextmanager
def _locked_grant_refs(home, create=False):
    """Yield an O_NOFOLLOW refs directory fd under a locked real home fd.

    The agent controls everything inside its home, so ordinary ``makedirs`` /
    ``join`` traversal is not an acceptable authority boundary: ``refs`` may
    have been replaced with a symlink between Crew commands. Directory-relative
    operations keep every mutation pinned to the inspected directory inode.
    """
    if not isinstance(home, str) or not home:
        raise gs.GraphError("agent has no valid home for file grants")
    expanded_home = os.path.abspath(os.path.expanduser(home))
    try:
        os.makedirs(expanded_home, mode=0o700, exist_ok=True)
        home_info = os.lstat(expanded_home)
    except OSError as error:
        raise _grant_fs_error(f"open grant home {expanded_home!r}", error) from error
    if stat.S_ISLNK(home_info.st_mode):
        raise gs.GraphError(
            f"refusing grant home {expanded_home!r}: home is a symlink")
    if not stat.S_ISDIR(home_info.st_mode):
        raise gs.GraphError(
            f"refusing grant home {expanded_home!r}: home is not a directory")

    home_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        home_flags |= os.O_NOFOLLOW
    try:
        home_fd = os.open(expanded_home, home_flags)
    except OSError as error:
        raise _grant_fs_error(f"open grant home {expanded_home!r}", error) from error
    refs_fd = None
    try:
        fcntl.flock(home_fd, fcntl.LOCK_EX)
        try:
            refs_info = os.stat("refs", dir_fd=home_fd, follow_symlinks=False)
        except FileNotFoundError:
            if not create:
                yield home_fd, None
                return
            try:
                os.mkdir("refs", mode=0o700, dir_fd=home_fd)
                refs_info = os.stat(
                    "refs", dir_fd=home_fd, follow_symlinks=False)
            except OSError as error:
                raise _grant_fs_error(
                    f"create refs/ in {expanded_home!r}", error) from error
        except OSError as error:
            raise _grant_fs_error(
                f"inspect refs/ in {expanded_home!r}", error) from error

        if stat.S_ISLNK(refs_info.st_mode):
            raise gs.GraphError(
                f"refusing refs/ in {expanded_home!r}: refs is a symlink")
        if not stat.S_ISDIR(refs_info.st_mode):
            raise gs.GraphError(
                f"refusing refs/ in {expanded_home!r}: refs is not a directory")
        refs_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        if hasattr(os, "O_NOFOLLOW"):
            refs_flags |= os.O_NOFOLLOW
        try:
            refs_fd = os.open("refs", refs_flags, dir_fd=home_fd)
        except OSError as error:
            raise _grant_fs_error(
                f"open refs/ in {expanded_home!r}", error) from error
        opened = os.fstat(refs_fd)
        if ((opened.st_dev, opened.st_ino)
                != (refs_info.st_dev, refs_info.st_ino)):
            raise gs.GraphError(
                f"refusing refs/ in {expanded_home!r}: directory changed during inspection")
        yield home_fd, refs_fd
    finally:
        if refs_fd is not None:
            os.close(refs_fd)
        try:
            fcntl.flock(home_fd, fcntl.LOCK_UN)
        finally:
            os.close(home_fd)


def _verify_refs_binding(home_fd, refs_fd):
    """Ensure the opened refs inode is still the home directory's refs entry."""
    try:
        current = os.stat("refs", dir_fd=home_fd, follow_symlinks=False)
    except OSError as error:
        raise _grant_fs_error("verify refs/ after mutation", error) from error
    opened = os.fstat(refs_fd)
    if (stat.S_ISLNK(current.st_mode)
            or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)):
        raise gs.GraphError("refusing grant mutation: refs/ changed during operation")


def _refs_entry_exists(refs_fd, name):
    try:
        os.stat(name, dir_fd=refs_fd, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False
    except OSError as error:
        raise _grant_fs_error(f"inspect refs/{name}", error) from error


def write_grant_link(home, path, name):
    """Create one exact refs symlink without following home-controlled links."""
    name = _validate_grant_name(name)
    with _locked_grant_refs(home, create=True) as (home_fd, refs_fd):
        if _refs_entry_exists(refs_fd, name):
            raise gs.GraphError(f"refs/{name} already exists")
        try:
            os.symlink(path, name, dir_fd=refs_fd)
            try:
                _verify_refs_binding(home_fd, refs_fd)
            except Exception:
                try:
                    os.unlink(name, dir_fd=refs_fd)
                except OSError:
                    pass
                raise
        except OSError as error:
            raise _grant_fs_error(f"create refs/{name}", error) from error
    return os.path.join(home, "refs", name)


def remove_grant_link(home, name, expected_path=None):
    """Remove one verified grant symlink; a genuinely absent link is a no-op.

    A regular file, path-traversal name, refs symlink, or link redirected away
    from the durable grant path is refused rather than deleting agent/user data.
    Returns the raw symlink target when a link was removed, else ``None``.
    """
    name = _validate_grant_name(name)
    with _locked_grant_refs(home, create=False) as (home_fd, refs_fd):
        if refs_fd is None:
            return None
        try:
            info = os.stat(name, dir_fd=refs_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        except OSError as error:
            raise _grant_fs_error(f"inspect refs/{name}", error) from error
        if not stat.S_ISLNK(info.st_mode):
            raise gs.GraphError(
                f"refusing to remove refs/{name}: destination is not a symlink")
        try:
            raw_target = os.readlink(name, dir_fd=refs_fd)
        except OSError as error:
            raise _grant_fs_error(f"read refs/{name}", error) from error
        if expected_path is not None:
            observed = os.path.realpath(
                raw_target if os.path.isabs(raw_target)
                else os.path.join(home, "refs", raw_target))
            expected = os.path.realpath(expected_path)
            if observed != expected:
                raise gs.GraphError(
                    f"refusing to remove refs/{name}: link target no longer "
                    "matches the durable grant")
        try:
            os.unlink(name, dir_fd=refs_fd)
            try:
                _verify_refs_binding(home_fd, refs_fd)
            except Exception:
                try:
                    os.symlink(raw_target, name, dir_fd=refs_fd)
                except OSError:
                    pass
                raise
        except OSError as error:
            raise _grant_fs_error(f"remove refs/{name}", error) from error
        return raw_target


def _dedupe_grant_name(home, existing_grants, base):
    """`base`, or `base-2`/`base-3`/... on collision — checked against both the
    agent's current grants list (the durable source of truth) and any stray
    <home>/refs/<name> already on disk, so a leftover symlink from a prior
    failed run can't silently get overwritten."""
    used = {g.get("name") for g in (existing_grants or []) if isinstance(g, dict)}
    with _locked_grant_refs(home, create=True) as (_, refs_fd):
        name, i = base, 2
        while name in used or _refs_entry_exists(refs_fd, name):
            name = f"{base}-{i}"
            i += 1
        return _validate_grant_name(name)


def _materialize_deduped_grant_link(home, path, existing_grants, base):
    """Choose and create a collision-free link under one refs directory fd."""
    used = {g.get("name") for g in (existing_grants or []) if isinstance(g, dict)}
    with _locked_grant_refs(home, create=True) as (home_fd, refs_fd):
        name, i = _validate_grant_name(base), 2
        while name in used or _refs_entry_exists(refs_fd, name):
            name = _validate_grant_name(f"{base}-{i}")
            i += 1
        try:
            os.symlink(path, name, dir_fd=refs_fd)
            try:
                _verify_refs_binding(home_fd, refs_fd)
            except Exception:
                try:
                    os.unlink(name, dir_fd=refs_fd)
                except OSError:
                    pass
                raise
        except OSError as error:
            raise _grant_fs_error(f"create refs/{name}", error) from error
        return name


def _restore_grants_snapshot(guid, grants):
    """Idempotently restore durable grants, tolerating an ambiguous write error."""
    try:
        return gs.update_agent_grants(guid, grants)
    except Exception:
        try:
            current = gs.get_object(guid)
            if list(current.get("grants") or []) == list(grants or []):
                return current
        except Exception:
            pass
        raise


def _transaction_error(operation, error, rollback_errors):
    reason = f"{operation} failed: {error}"
    if rollback_errors:
        reason += "; rollback incomplete: " + "; ".join(rollback_errors)
    return gs.GraphError(reason)


def _home_contains(home, real_path):
    """True iff `real_path` is `home` itself or nested inside it (both
    normalized the same case-folded/symlink-resolved way as graphstore's own
    home-nesting checks) — "that's already their ground" refusal."""
    if not home:
        return False
    h, p = gs.normalize_home(home), gs.normalize_home(real_path)
    return p == h or p.startswith(h + os.sep)


@contextlib.contextmanager
def _grant_identity_transaction(guid, actor_guid=None):
    """Canonical lock order for one grant mutation through identity publish."""
    with gs._identity_transaction_locks((guid, actor_guid)):
        with gs._invariant_lock("agent-grants"):
            yield


def grant_path(agent_name, path, mode="ro", actor="human", _pre_approved=False,
               _expected_guid=None, _actor_guid=None):
    """`crew grant <agent> <path> [--ro|--rw]` end to end: normalize the path,
    gate the actor (guard.check, op "grant" — human-only; a foreman's request
    is queued to PENDING instead of applied), refuse a path already inside the
    GRANTEE's own home (redundant — it's already their ground), then apply
    every side effect together: os.symlink(path, <home>/refs/<name>), append
    the entry to agent.grants (via update_agent), rewrite identity.md/
    CLAUDE.md, and an audit row (op="grant"). Returns the appended entry.

    `_pre_approved=True` (guard.approve_pending's escape hatch ONLY) skips the
    guard.check call — the caller already ran check("human", ...) itself and
    is replaying a stored request; the write still executes with `actor=
    <original requester>` so the audit trail reads as if the requester's
    action had gone straight through, exactly like a human clicking approve."""
    if mode not in ("ro", "rw"):
        raise gs.GraphError(f"invalid grant mode {mode!r}: use --ro or --rw")
    target = gs.get_agent_by_name(agent_name)
    if not target:
        raise gs.GraphError(f"no such agent: {agent_name}")
    if _expected_guid and target.get("_guid") != _expected_guid:
        raise gs.GraphError(
            "pending grant target is stale: that agent name now belongs to a "
            "replacement identity; reject and submit a new request")
    try:
        real_path = os.path.realpath(os.path.expanduser(str(path)))
        target_exists = os.path.exists(real_path)
    except (OSError, ValueError) as error:
        raise gs.GraphError(f"invalid grant path {path!r}: {error}") from error
    if not target_exists:
        raise gs.GraphError(f"grant target {real_path!r} does not exist")
    if not _pre_approved:
        guard.check(
            actor, "grant", name=agent_name, agent_guid=target["_guid"],
            path=real_path, mode=mode)
    args = {"name": agent_name, "path": real_path, "mode": mode}
    guid = target["_guid"]
    actor_guid = (
        "" if actor == "human"
        else (_actor_guid or gs._resolve_actor_guid(actor)))
    # The scope is app-qualified by graphstore and GUID-qualified here. It
    # spans the read, filesystem mutation, durable patch, identity rewrite,
    # audit, and any compensation—not just the individual MorphDB PATCH.
    with _grant_identity_transaction(guid, actor_guid):
        if actor != "human":
            locked_actor = gs._require_actor_guid(actor, actor_guid)
            if _pre_approved and not locked_actor.get("can_edit_graph"):
                raise gs.GraphError(
                    "pending grant requester is no longer a foreman; submit "
                    "a new request")
        locked_target = gs.get_object(guid)
        if (locked_target.get("name") != agent_name
                or (_expected_guid and locked_target.get("_guid") != _expected_guid)):
            raise gs.GraphError(
                f"no such agent identity: {agent_name} (agent was renamed or "
                "replaced during grant)")
        if not os.path.exists(real_path):
            raise gs.GraphError(f"grant target {real_path!r} does not exist")
        home = _validated_agent_home(locked_target)
        if _home_contains(home, real_path):
            raise gs.GraphError(
                f"{real_path!r} is already inside {agent_name!r}'s own home "
                f"({home!r}) — that's already their ground, nothing to grant")
        existing = list(locked_target.get("grants") or [])
        name = None
        desired = None
        graph_update_attempted = False
        identity_attempted = False
        try:
            name = _materialize_deduped_grant_link(
                home, real_path, existing, _grant_slug(real_path))
            entry = {
                "name": name, "path": real_path, "mode": mode, "type": "path",
                "created_at": int(time.time()), "granted_by": "human",
            }
            desired = existing + [entry]
            graph_update_attempted = True
            updated = gs.update_agent_grants(guid, desired)
            identity_attempted = True
            rewrite_identity(updated)
        except Exception as error:
            rollback_errors = []
            if graph_update_attempted:
                try:
                    _restore_grants_snapshot(guid, existing)
                except Exception as rollback_error:
                    rollback_errors.append(f"durable grants: {rollback_error}")
            if name is not None:
                try:
                    remove_grant_link(home, name, expected_path=real_path)
                except Exception as rollback_error:
                    rollback_errors.append(f"refs/{name}: {rollback_error}")
            if identity_attempted:
                old_target = dict(locked_target)
                old_target["grants"] = existing
                try:
                    rewrite_identity(old_target)
                except Exception as rollback_error:
                    # An unsafe native destination can make both the forward
                    # write and compensation raise; identity.md is atomic and
                    # has already been restored before that native preflight.
                    rollback_errors.append(f"identity: {rollback_error}")
            failure = _transaction_error("grant", error, rollback_errors)
            guard.audit(
                actor, "grant", args, "failed", str(failure),
                **({"actor_guid": actor_guid} if actor_guid else {}))
            raise failure from error
        guard.audit(
            actor, "grant", args, "applied",
            **({"actor_guid": actor_guid} if actor_guid else {}))
        return entry


def revoke_grant(agent_name, name, actor="human"):
    """`crew revoke-grant <agent> <name>`: remove the symlink (best-effort),
    the agent.grants entry, rewrite identity, and an audit row
    (op="revoke_grant"). Human-only — guard.check gates it BEFORE anything is
    touched (op "revoke_grant" is human-only forever, no foreman/pending
    exception, same tier as remove/bless/foreman)."""
    target = gs.get_agent_by_name(agent_name)
    if not target:
        raise gs.GraphError(f"no such agent: {agent_name}")
    guard.check(actor, "revoke_grant", name=agent_name, grant_name=name)
    name = _validate_grant_name(name)
    args = {"name": agent_name, "grant_name": name}
    guid = target["_guid"]
    with _grant_identity_transaction(guid):
        locked_target = gs.get_object(guid)
        if locked_target.get("name") != agent_name:
            raise gs.GraphError(
                f"no such agent: {agent_name} (agent was renamed during revoke)")
        home = _validated_agent_home(locked_target)
        existing = list(locked_target.get("grants") or [])
        matches = [g for g in existing if isinstance(g, dict) and g.get("name") == name]
        if not matches:
            raise gs.GraphError(f"no grant named {name!r} on agent {agent_name!r}")
        entry = matches[0]
        remaining = [
            grant for grant in existing
            if not (isinstance(grant, dict) and grant.get("name") == name)
        ]
        removed_target = None
        link_remove_attempted = False
        graph_update_attempted = False
        identity_attempted = False
        try:
            link_remove_attempted = True
            removed_target = remove_grant_link(
                home, name, expected_path=entry.get("path"))
            graph_update_attempted = True
            updated = gs.update_agent_grants(guid, remaining)
            identity_attempted = True
            rewrite_identity(updated)
        except Exception as error:
            rollback_errors = []
            if graph_update_attempted:
                try:
                    _restore_grants_snapshot(guid, existing)
                except Exception as rollback_error:
                    rollback_errors.append(f"durable grants: {rollback_error}")
            if link_remove_attempted and removed_target is not None:
                try:
                    write_grant_link(home, removed_target, name)
                except Exception as rollback_error:
                    rollback_errors.append(f"refs/{name}: {rollback_error}")
            if identity_attempted:
                old_target = dict(locked_target)
                old_target["grants"] = existing
                try:
                    rewrite_identity(old_target)
                except Exception as rollback_error:
                    rollback_errors.append(f"identity: {rollback_error}")
            failure = _transaction_error("revoke grant", error, rollback_errors)
            guard.audit(actor, "revoke_grant", args, "failed", str(failure))
            raise failure from error
        guard.audit(actor, "revoke_grant", args, "applied")
        return locked_target
