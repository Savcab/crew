"""crew.spawn — bring an agent to life: a home dir, a tmux session, a launched
`claude`, and an identity.md that makes the session a durable, named agent.

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
import os
import subprocess
import threading

from . import config, graphstore as gs, identity


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


def _tmux(*args, timeout=10):
    ok, out, err = _run(["tmux", *args], timeout=timeout)
    return ok, (out if ok else err)


# --------------------------------------------------------------------------- #
# home resolution
# --------------------------------------------------------------------------- #
def _worktree_home(repo_cwd, name):
    """Create a detached git worktree for `name` next to its repo (the old `wt`
    layout: <repo>-worktrees/<name>) and return its path. Raises on failure."""
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
        default = ref.split("/")[-1]
    if not default:
        for cand in ("main", "master"):
            if _git(["rev-parse", "--verify", cand], cwd=repo_cwd) is not None:
                default = cand
                break
    default = default or (_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_cwd) or "HEAD")
    wt = os.path.join(base, f"{repo}-worktrees", name)
    if not os.path.exists(wt):
        _git(["worktree", "prune"], cwd=repo_cwd)
        ok, out, err = _run(["git", "worktree", "add", "--detach", wt, default], cwd=repo_cwd)
        if not ok:
            raise gs.GraphError(f"git worktree add failed: {err or out}")
    return os.path.realpath(wt), name


def _resolve_home(name, home=None, repo=None):
    """Decide the agent's home dir + optional worktree name, create it if needed.
    Precedence: explicit --home, then --repo (a worktree), else <cwd>/<name>."""
    if repo:
        return _worktree_home(os.path.abspath(os.path.expanduser(repo)), name)
    if home:
        h = os.path.realpath(os.path.expanduser(home))
    else:
        h = os.path.realpath(os.path.join(os.getcwd(), name))
    os.makedirs(h, exist_ok=True)
    return h, None


# --------------------------------------------------------------------------- #
# identity.md (re)render
# --------------------------------------------------------------------------- #
def _resolve_neighbors(agent_guid):
    """(neighbor_agent_dict, edge) list for everyone this agent may message."""
    out = []
    for tgt_guid, edge in gs.messageable_targets(agent_guid):
        try:
            nb = gs.get_object(tgt_guid)
        except gs.GraphError:
            nb = None
        if nb:
            out.append((nb, edge))
    return out


def rewrite_identity(agent, notify=False):
    """Re-render and write identity.md for `agent` (a dict with at least name +
    home + _guid). Call after the agent's edges change so the file always lists
    its current connections. Optionally nudge the live session to re-read it."""
    neighbors = _resolve_neighbors(agent["_guid"])
    text = identity.render_identity_md(agent, neighbors)
    path = identity.write_identity(agent.get("home") or ".", text)
    if notify and agent.get("session"):
        _tmux("send-keys", "-t", f"{agent['session']}:claude", "-l", "--",
              f"[crew] your connections changed — re-read {path}")
        _tmux("send-keys", "-t", f"{agent['session']}:claude", "Enter")
    return path


# --------------------------------------------------------------------------- #
# spawn
# --------------------------------------------------------------------------- #
def spawn_agent(name, role="", agent_identity="", home=None, repo=None,
                launch=True, launch_cmd=None):
    """Create a new agent end-to-end. Returns the MorphDB agent dict.

    Steps (fail-fast, cleans up a half-made tmux session on a later error):
      1. validate name + reject a home that collides with an existing agent's;
      2. make the home dir (or a git worktree for --repo);
      3. start a detached tmux session named `name`, pinned to AGENT_MAIL_NAME so
         the agent's messaging identity is fixed;
      4. write the MorphDB record + identity.md;
      5. launch claude in the pane and (after it boots) inject the spawn context.
    """
    if not config.valid_agent_name(name):
        raise gs.GraphError(
            f"invalid agent name {name!r}: letters, digits, '_', '-' only "
            "(no dots/slashes/spaces), max 64 chars")
    if gs.get_agent_by_name(name):
        raise gs.GraphError(f"an agent named '{name}' already exists")

    home_path, worktree = _resolve_home(name, home=home, repo=repo)

    bad = gs.unsafe_home_reason(home_path)
    if bad:
        raise gs.GraphError(bad)
    conflict = gs.home_conflict(home_path)
    if conflict:
        raise gs.GraphError(
            f"home {home_path!r} overlaps agent '{conflict['name']}' "
            f"(home {conflict.get('home')!r}). One agent per directory, and no "
            "agent inside another's tree — pick a separate directory.")

    cmd = launch_cmd or config.LAUNCH_CMD

    # tmux session (idempotent-ish: refuse a pre-existing same-named session).
    have, _ = _tmux("has-session", "-t", name)
    if have:
        raise gs.GraphError(
            f"a tmux session named '{name}' already exists; kill it first "
            f"(tmux kill-session -t {name}) or pick another name")
    ok, err = _tmux("new-session", "-d", "-s", name, "-n", "claude",
                    "-c", home_path, "-e", f"AGENT_MAIL_NAME={name}",
                    "-e", "CREW_AGENT=" + name)
    if not ok:
        raise gs.GraphError(f"tmux new-session failed: {err}")

    ok, pane = _tmux("list-panes", "-t", f"{name}:claude", "-F", "#{pane_id}")
    pane_id = pane.splitlines()[0].strip() if (ok and pane.strip()) else None
    if not pane_id:
        _tmux("kill-session", "-t", name)
        raise gs.GraphError(f"could not resolve the claude pane for '{name}'")

    try:
        agent = gs.create_agent(
            name, role=role, identity=agent_identity, home=home_path,
            session=name, pane=pane_id, worktree=worktree or "",
            launch_cmd=cmd, status="idle")
    except Exception:
        _tmux("kill-session", "-t", name)
        raise

    # identity.md (no neighbors yet — the user connects agents afterward).
    rewrite_identity(agent)

    if launch:
        _tmux("send-keys", "-t", f"{name}:claude", "-l", cmd)
        _tmux("send-keys", "-t", f"{name}:claude", "Enter")
        # inject the pointer-to-identity context once claude has booted. Done off
        # a timer so neither the CLI nor the dashboard request blocks on the wait.
        ctx = identity.render_spawn_context(agent, [])
        threading.Timer(5.0, _inject_context, args=(name, ctx)).start()
    return agent


def _inject_context(session, text):
    _tmux("send-keys", "-t", f"{session}:claude", "-l", "--", text)
    _tmux("send-keys", "-t", f"{session}:claude", "Enter")


def remove_agent(name, kill_session=True):
    """Delete an agent: drop its edges + record from MorphDB and (optionally) kill
    its tmux session. The home dir + identity.md are left on disk."""
    a = gs.get_agent_by_name(name)
    if not a:
        raise gs.GraphError(f"no such agent: {name}")
    gs.delete_agent(a["_guid"])
    if kill_session:
        _tmux("kill-session", "-t", a.get("session") or name)
    return a
