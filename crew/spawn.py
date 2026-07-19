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
import json
import os
import re
import subprocess
import time

from . import config, graphstore as gs, guard, identity


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
        default = ref.split("/")[-1]
    if not default:
        for cand in ("main", "master"):
            if _git(["rev-parse", "--verify", cand], cwd=repo_cwd) is not None:
                default = cand
                break
    default = default or (_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_cwd) or "HEAD")
    return os.path.realpath(os.path.join(base, f"{repo}-worktrees", name)), default


def _create_worktree(repo_cwd, wt, default):
    """Materialize the worktree computed by _worktree_paths (run only AFTER checks)."""
    if not os.path.exists(wt):
        _git(["worktree", "prune"], cwd=repo_cwd)
        ok, out, err = _run(["git", "worktree", "add", "--detach", wt, default], cwd=repo_cwd)
        if not ok:
            raise gs.GraphError(f"git worktree add failed: {err or out}")


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
        try:
            with open(cfg_path) as f:
                cfg = json.load(f)
        except (OSError, ValueError):
            cfg = {}
        if not isinstance(cfg, dict):
            return
        projects = cfg.setdefault("projects", {})
        if not isinstance(projects, dict):
            return
        entry = projects.setdefault(home, {})
        if not isinstance(entry, dict):
            return
        if entry.get("hasTrustDialogAccepted") is True:
            return  # already trusted — don't rewrite the (large, live) config
        entry["hasTrustDialogAccepted"] = True
        tmp = cfg_path + ".crew.tmp"
        with open(tmp, "w") as f:
            json.dump(cfg, f, indent=2)
        os.replace(tmp, cfg_path)   # atomic — never leave a half-written config
    except OSError:
        pass


def _plan_home(name, home=None, repo=None):
    """Decide the agent's home PATH + optional worktree name WITHOUT creating it.
    Precedence: explicit --home, then --repo (a worktree), else <cwd>/<name>.
    Returns (home_path, worktree_name, materialize_plan) where materialize_plan is
    passed to _materialize_home AFTER the safety/conflict checks pass."""
    if repo:
        repo_cwd = os.path.abspath(os.path.expanduser(repo))
        wt, default = _worktree_paths(repo_cwd, name)
        return wt, name, ("worktree", repo_cwd, default)
    if home:
        h = os.path.realpath(os.path.expanduser(home))
    else:
        h = os.path.realpath(
            os.path.join(config.crew_root(), config.current_project(), name))
    return h, None, ("mkdir",)


def _materialize_home(home_path, plan):
    """Create the home (or worktree) only after unsafe/conflict checks pass — so a
    refused spawn never leaves a stray dir inside another agent's tree."""
    if plan and plan[0] == "worktree":
        _create_worktree(plan[1], home_path, plan[2])
    else:
        os.makedirs(home_path, exist_ok=True)


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
        except gs.GraphError:
            nb = None
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
        except gs.GraphError:
            pr = None
        if pr:
            v = gs.edge_view(edge, agent_guid)
            e = dict(edge); e["_action"] = v["in_action"]; e["_reply"] = v["in_reply"]
            out.append((pr, e))
    return out


def rewrite_identity(agent, notify=False):
    """Re-render the agent's durable identity after its edges change, writing BOTH:
      * identity.md — the full human/agent-readable record in the home, and
      * CLAUDE.md   — the managed block Claude auto-loads at every session start,
                      so the agent's "who I may message" is always truthful.

    `agent` is a dict with at least name + home + _guid. When `notify` and the agent
    is connected to someone, we queue a small heads-up message (via the durable
    message log) so the flusher delivers it the moment the agent is idle — we never
    type a nudge into the pane blindly (that left unsubmitted text in the prompt and
    could land mid-dialog). The files are the source of truth regardless."""
    neighbors = _resolve_neighbors(agent["_guid"])
    incoming = _resolve_incoming(agent["_guid"])
    home = agent.get("home") or "."
    # WAVE 3: a foreman's identity carries a live "Graph powers" section (quota
    # numbers computed fresh via guard.quota_state) — only bother computing it
    # for a can_edit_graph agent, the common case doesn't pay for the lookup.
    quota = guard.quota_state() if agent.get("can_edit_graph") else None
    path = identity.write_identity(
        home, identity.render_identity_md(agent, neighbors, incoming, quota))
    identity.write_claude_md(
        home, identity.render_claude_md(agent, neighbors, incoming, quota))
    if notify and agent.get("name") and (neighbors or incoming):
        try:
            gs.create_message(
                "crew", agent["name"],
                f"your connections changed — re-read {config.IDENTITY_FILE} for who "
                "you may message now", status="queued")
        except gs.GraphError:
            pass
    return path


# --------------------------------------------------------------------------- #
# spawn
# --------------------------------------------------------------------------- #
def _open_session(session, home, name, project):
    """Create the detached tmux session running a `claude` window for `name` in
    `home`, with the agent-mail identity + project pinned in the env, and return
    its claude pane_id. Kills the session and raises if the pane can't be
    resolved. Shared by spawn_agent (first boot) and start_session (revive a
    down agent). `session` is the (possibly project-prefixed) tmux session name;
    `name` is always the agent's PLAIN name — that's the messaging identity."""
    ok, err = _tmux("new-session", "-d", "-s", session, "-n", "claude",
                    "-c", home, "-e", f"AGENT_MAIL_NAME={name}",
                    "-e", "CREW_AGENT=" + name,
                    "-e", "CREW_PROJECT=" + project)
    if not ok:
        raise gs.GraphError(f"tmux new-session failed: {err}")
    ok, pane = _tmux("list-panes", "-t", f"{session}:claude", "-F", "#{pane_id}")
    pane_id = pane.splitlines()[0].strip() if (ok and pane.strip()) else None
    if not pane_id:
        _tmux("kill-session", "-t", session)
        raise gs.GraphError(f"could not resolve the claude pane for '{name}'")
    return pane_id


def _launch_claude(session, home, cmd):
    """Pre-accept the folder-trust dialog, then type the launch command + Enter into
    the agent's claude pane so it boots unattended (it runs in its own dedicated
    home, which the user created for it)."""
    _pretrust_home(home)
    _tmux("send-keys", "-t", f"{session}:claude", "-l", cmd)
    _tmux("send-keys", "-t", f"{session}:claude", "Enter")


def _refuse_spawn_confinement(actor, name, arg, reason):
    """Refuse an agent actor's --home/--repo/--launch-cmd override, WITH an
    audit row (crew.guard's audit(), so `crew audit` shows why just like any
    other refusal — this check lives in spawn.py, not guard.py, because it's
    about the ARGS an agent actor passed, not about graph topology)."""
    guard.audit(actor, "spawn", {"name": name, "refused_arg": arg}, "refused", reason)
    raise gs.GraphError(reason)


def spawn_agent(name, role="", agent_identity="", home=None, repo=None,
                launch=True, launch_cmd=None, actor="human", foreman=False):
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
    guard.check(actor, "spawn", name=name)
    if not config.valid_agent_name(name):
        raise gs.GraphError(
            f"invalid agent name {name!r}: letters, digits, '_', '-' only "
            "(no dots/slashes/spaces), max 64 chars")
    if gs.get_agent_by_name(name):
        raise gs.GraphError(f"an agent named '{name}' already exists")

    if actor != "human":
        if home:
            _refuse_spawn_confinement(actor, name, "home",
                "agents may not pass --home — a spawned agent's home is "
                "always under the project's standard tree "
                f"({config.crew_root()}/{config.current_project()}/<name>) — "
                "ask the user to spawn it with a custom home")
        if repo:
            _refuse_spawn_confinement(actor, name, "repo",
                "agents may not pass --repo — a spawned agent's home is "
                "always under the project's standard tree, never a git "
                "worktree of your choosing — ask the user")
        if launch_cmd:
            _refuse_spawn_confinement(actor, name, "launch_cmd",
                "agents may not override --launch-cmd — a spawned agent "
                "always launches with the project default — ask the user")
        if foreman:
            _refuse_spawn_confinement(actor, name, "foreman",
                "agents may not pass --foreman — granting graph-editing power "
                "requires a human — ask the user to run `crew foreman <name>`")
        home, repo, launch_cmd = None, None, None  # belt-and-suspenders

    if foreman:
        guard.check(actor, "foreman", name=name, revoke=False)

    # Plan the home PATH first, run the safety + overlap checks, and only THEN
    # create it — so a refused spawn (unsafe home, or one nested in another agent's
    # tree) never leaves a stray directory or worktree behind on disk.
    home_path, worktree, plan = _plan_home(name, home=home, repo=repo)

    bad = gs.unsafe_home_reason(home_path)
    if bad:
        raise gs.GraphError(bad)
    conflict = gs.home_conflict(home_path)
    if conflict:
        raise gs.GraphError(
            f"home {home_path!r} overlaps agent '{conflict['name']}' "
            f"(home {conflict.get('home')!r}). One agent per directory, and no "
            "agent inside another's tree — pick a separate directory.")

    _materialize_home(home_path, plan)
    cmd = launch_cmd or config.LAUNCH_CMD

    # tmux session — refuse a pre-existing same-named session (never adopt one the
    # user is already running; that's the whole "crew only manages its own" rule).
    # Session name is project-scoped (config.session_name) so two projects' agents
    # never collide on a tmux session; the messaging identity (CREW_AGENT /
    # AGENT_MAIL_NAME) stays the PLAIN name regardless of project.
    project = config.current_project()
    session = config.session_name(project, name)
    if _tmux("has-session", "-t", session)[0]:
        raise gs.GraphError(
            f"a tmux session named '{session}' already exists; kill it first "
            f"(tmux kill-session -t {session}) or pick another name")
    pane_id = _open_session(session, home_path, name, project)

    try:
        agent = gs.create_agent(
            name, role=role, identity=agent_identity, home=home_path,
            session=session, pane=pane_id, worktree=worktree or "",
            launch_cmd=cmd, status="idle", can_edit_graph=bool(foreman), actor=actor)
    except Exception:
        _tmux("kill-session", "-t", session)
        raise

    # identity.md + CLAUDE.md (no neighbors yet — the user connects agents after).
    # The identity is delivered NATIVELY: claude auto-loads the home's CLAUDE.md at
    # startup, so there's no fragile post-boot send-keys injection to race (the old
    # 5s timer could fire before claude launched and type into a bare shell).
    rewrite_identity(agent)

    if launch:
        _launch_claude(session, home_path, cmd)
    return agent


def start_session(name, actor="human"):
    """Bring an EXISTING agent back up: (re)create its tmux session and relaunch
    claude in its home, so a 'down' agent can be revived from the dashboard. The
    record is durable; only the live session died. Idempotent — if the session is
    already running it's left alone. Refreshes the stored pane_id. Returns the agent.

    Gated FIRST (guard.check, op "up") — before touching tmux — so a refused
    revive never leaves a stray session."""
    a = gs.get_agent_by_name(name)
    if not a:
        raise gs.GraphError(f"no such agent: {name}")
    guard.check(actor, "up", name=name)
    project = config.current_project()
    session = a.get("session") or config.session_name(project, name)
    home = a.get("home") or os.getcwd()
    cmd = a.get("launch_cmd") or config.LAUNCH_CMD
    if _tmux("has-session", "-t", session)[0]:
        return a   # already running — nothing to do
    os.makedirs(home, exist_ok=True)   # home should already exist; be safe
    pane_id = _open_session(session, home, name, project)
    rewrite_identity(a)                # keep identity.md / CLAUDE.md current on revive
    _launch_claude(session, home, cmd)
    try:
        result = gs.update_agent(a["_guid"], pane=pane_id, status="idle", actor=actor) or a
    except gs.GraphError:
        result = a
    guard.audit(actor, "up", {"name": name}, "applied")
    return result


def _untrust_home(home):
    """Remove the trust entry _pretrust_home added for `home` from ~/.claude.json —
    so removing an agent doesn't leave a stale projects[] entry pointing at a home
    crew no longer manages. Best-effort and atomic; never raises."""
    if not home:
        return
    try:
        cfg_path = os.path.expanduser("~/.claude.json")
        try:
            with open(cfg_path) as f:
                cfg = json.load(f)
        except (OSError, ValueError):
            return
        projects = cfg.get("projects")
        if not isinstance(projects, dict) or home not in projects:
            return
        del projects[home]
        tmp = cfg_path + ".crew.tmp"
        with open(tmp, "w") as f:
            json.dump(cfg, f, indent=2)
        os.replace(tmp, cfg_path)
    except OSError:
        pass


def remove_agent(name, kill_session=True, actor="human"):
    """Delete an agent: drop its edges + record from MorphDB and (optionally) kill
    its tmux session. The home dir + identity.md are left on disk. Human-only
    (guard.check inside gs.delete_agent, op "remove" — not even a foreman may
    remove an agent this wave)."""
    a = gs.get_agent_by_name(name)
    if not a:
        raise gs.GraphError(f"no such agent: {name}")
    gs.delete_agent(a["_guid"], actor=actor)
    if kill_session:
        _tmux("kill-session", "-t", a.get("session") or name)
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


def _grant_slug(path):
    """Filesystem-safe slug from a path's basename — the default refs/<name>."""
    base = os.path.basename(str(path).rstrip(os.sep)) or "root"
    slug = _GRANT_SLUG_RE.sub("_", base).strip("_.")
    return slug or "grant"


def write_grant_link(home, path, name):
    """Create <home>/refs/<name> -> path (making refs/ if it doesn't exist yet).
    Lives here next to the other home-materializing code (_materialize_home,
    _pretrust_home) — identity.py stays a pure renderer."""
    refs = os.path.join(home, "refs")
    os.makedirs(refs, exist_ok=True)
    link = os.path.join(refs, name)
    if os.path.lexists(link):
        os.remove(link)  # caller already deduped `name`; stay best-effort anyway
    os.symlink(path, link)
    return link


def remove_grant_link(home, name):
    """Best-effort remove <home>/refs/<name> — ENOENT (already gone) is fine,
    never raises."""
    if not home:
        return
    try:
        os.remove(os.path.join(home, "refs", name))
    except OSError:
        pass


def _dedupe_grant_name(home, existing_grants, base):
    """`base`, or `base-2`/`base-3`/... on collision — checked against both the
    agent's current grants list (the durable source of truth) and any stray
    <home>/refs/<name> already on disk, so a leftover symlink from a prior
    failed run can't silently get overwritten."""
    used = {g.get("name") for g in (existing_grants or []) if isinstance(g, dict)}
    refs_dir = os.path.join(home, "refs") if home else ""
    name, i = base, 2
    while name in used or (refs_dir and os.path.lexists(os.path.join(refs_dir, name))):
        name = f"{base}-{i}"
        i += 1
    return name


def _home_contains(home, real_path):
    """True iff `real_path` is `home` itself or nested inside it (both
    normalized the same case-folded/symlink-resolved way as graphstore's own
    home-nesting checks) — "that's already their ground" refusal."""
    if not home:
        return False
    h, p = gs.normalize_home(home), gs.normalize_home(real_path)
    return p == h or p.startswith(h + os.sep)


def grant_path(agent_name, path, mode="ro", actor="human", _pre_approved=False):
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
    real_path = os.path.realpath(os.path.expanduser(str(path)))
    if not _pre_approved:
        guard.check(actor, "grant", name=agent_name, path=real_path, mode=mode)
    home = target.get("home") or ""
    if _home_contains(home, real_path):
        raise gs.GraphError(
            f"{real_path!r} is already inside {agent_name!r}'s own home "
            f"({home!r}) — that's already their ground, nothing to grant")
    existing = list(target.get("grants") or [])
    name = _dedupe_grant_name(home, existing, _grant_slug(real_path))
    write_grant_link(home, real_path, name)
    entry = {"name": name, "path": real_path, "mode": mode, "type": "path",
             "created_at": int(time.time()), "granted_by": "human"}
    gs.update_agent(target["_guid"], grants=existing + [entry], actor=actor)
    try:
        rewrite_identity(gs.get_object(target["_guid"]))
    except gs.GraphError:
        pass
    guard.audit(actor, "grant", {"name": agent_name, "path": real_path, "mode": mode},
               "applied")
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
    existing = list(target.get("grants") or [])
    remaining = [g for g in existing if g.get("name") != name]
    if len(remaining) == len(existing):
        raise gs.GraphError(f"no grant named {name!r} on agent {agent_name!r}")
    remove_grant_link(target.get("home"), name)
    gs.update_agent(target["_guid"], grants=remaining, actor=actor)
    try:
        rewrite_identity(gs.get_object(target["_guid"]))
    except gs.GraphError:
        pass
    guard.audit(actor, "revoke_grant", {"name": agent_name, "grant_name": name}, "applied")
    return target
