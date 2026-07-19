#!/usr/bin/env python3
"""crew — the CLI for the agent graph.

    crew [--project P] init           set up MorphDB schema + start the dashboard
    crew project create <name>        create an isolated project (its own MorphDB app)
    crew project list                 list known projects, mark the current one
    crew spawn-agent <name> ...       create a long-running agent (tmux + claude)
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

Graph-editing operations (spawn-agent, connect, disconnect, up, down,
remove-agent) are GATED: a human running the CLI can do anything; an agent
running the CLI from its own pane can do almost nothing unless the user grants
it the foreman flag (`crew foreman <name>`, a later wave) — see crew.guard.
Every decision, allowed or refused, is recorded and viewable with `crew audit`.

A top-level `--project` (before the subcommand, e.g. `crew --project demo
spawn-agent foo`) scopes the whole command to that project's own MorphDB app
("crew" for the default project, "crew-<name>" otherwise) and — for a
spawn-agent using the default home layout — its own subtree under crew_root().
Omit it (or set $CREW_PROJECT) to stay on the default project.

Identity is automatic inside a spawned agent (its $CREW_AGENT is pinned), so an
agent never passes its own name to `message`.
"""
import argparse
import os
import socket
import subprocess
import sys
import time

from . import config, graphstore as gs, guard, identity, mail, schema, spawn
from .server import tmuxio

ROOT = config.ROOT
VAR = config.VAR
PIDFILE = os.path.join(VAR, "dashboard.pid")
LOGFILE = os.path.join(VAR, "dashboard.log")

# The resolved caller identity for THIS process: "human" for an operator's own
# shell, or an agent's name when the CLI is run from inside that agent's own
# tmux pane (mail.whoami() resolves it the same anti-spoofing way messaging
# does). Set ONCE in main() and read by every mutating command via _actor().
_ACTOR = "human"


def _actor():
    return _ACTOR


def _warn(msg):
    print(f"[crew] {msg}", file=sys.stderr)


# --------------------------------------------------------------------------- #
# dashboard process management
# --------------------------------------------------------------------------- #
def _port_open(host=config.DASHBOARD_HOST, port=config.DASHBOARD_PORT):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.3)
    try:
        return s.connect_ex((host, port)) == 0
    finally:
        s.close()


def _dash_url():
    return f"http://{config.DASHBOARD_HOST}:{config.DASHBOARD_PORT}"


def _dashboard_alive():
    """True only if the thing on our port is actually the CREW dashboard — a
    port-open check alone once reported 'running' while MorphDB's admin UI was
    squatting the port and the real dashboard was down."""
    import json as _json
    import urllib.request
    try:
        with urllib.request.urlopen(f"{_dash_url()}/api/graph/snapshot",
                                    timeout=1.5) as r:
            d = _json.load(r)
            return isinstance(d, dict) and "agents" in d
    except Exception:
        return False


def start_dashboard():
    """Start the dashboard server detached (idempotent). Returns (url, started)."""
    if _port_open():
        if not _dashboard_alive():
            print(f"warning: something else is listening on {_dash_url()} "
                  f"(not the crew dashboard) — free the port first: "
                  f"lsof -nP -iTCP:{config.DASHBOARD_PORT} -sTCP:LISTEN",
                  file=sys.stderr)
        return _dash_url(), False
    os.makedirs(VAR, exist_ok=True)
    logf = open(LOGFILE, "a")
    p = subprocess.Popen([sys.executable, "-m", "crew.server.app"],
                         cwd=ROOT, stdout=logf, stderr=logf,
                         stdin=subprocess.DEVNULL, start_new_session=True)
    with open(PIDFILE, "w") as f:
        f.write(str(p.pid))
    for _ in range(30):
        if _port_open():
            return _dash_url(), True
        time.sleep(0.1)
    return _dash_url(), True


def stop_dashboard():
    pid = None
    try:
        with open(PIDFILE) as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        pass
    if pid:
        try:
            os.kill(pid, 15)
        except OSError:
            pass
    try:
        os.remove(PIDFILE)
    except OSError:
        pass


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
    _ensure_morphdb()
    used = schema.ensure_schema()
    print(f"MorphDB ready: app '{used}' at {config.morphdb_base()}")
    if not a.no_dashboard:
        url, started = start_dashboard()
        print(f"dashboard {'started' if started else 'already running'} → {url}")
    print("Next: open the dashboard and click + Agent, or `crew spawn-agent <name> --role \"...\"`.")
    return 0


def cmd_project_create(a):
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
    config.register_project(a.name)
    print(f"created project '{a.name}' → MorphDB app '{app}'")
    print(f"  use it:  crew --project {a.name} spawn-agent <name> ...")
    return 0


def cmd_project_list(a):
    current = config.current_project()
    for name in config.list_known_projects():
        marker = "*" if name == current else " "
        print(f"{marker} {name}  → {config.project_app(name)}")
    return 0


def cmd_spawn_agent(a):
    schema.ensure_schema()
    agent = spawn.spawn_agent(
        a.name, role=a.role or "", agent_identity=a.identity or "",
        home=a.home, repo=a.repo, launch=not a.no_launch, launch_cmd=a.launch_cmd,
        actor=_actor(), foreman=a.foreman)
    print(f"spawned agent '{agent['name']}' → session '{agent['session']}' "
          f"(home {agent['home']})")
    print(f"  identity: {os.path.join(agent['home'], config.IDENTITY_FILE)}")
    if a.foreman:
        print(f"  '{agent['name']}' is now foreman (can_edit_graph=true)")
    if not a.no_launch:
        print("  claude is booting; it will read its identity.md shortly.")
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
    guard.check(actor, "foreman", name=a.name, revoke=a.revoke)
    gs.update_agent(ag["_guid"], can_edit_graph=not a.revoke, actor=actor)
    guard.audit(actor, "foreman", {"name": a.name, "revoke": a.revoke}, "applied")
    try:
        spawn.rewrite_identity(gs.get_object(ag["_guid"]), notify=True)
    except gs.GraphError:
        pass
    if a.revoke:
        print(f"revoked foreman from '{a.name}'")
    else:
        print(f"'{a.name}' is now foreman (can_edit_graph=true)")
    return 0


def cmd_bless(a):
    """crew bless <agent> | crew bless --edge <src> <dst> | crew bless --all —
    human-only (guard op "bless"). Flips `blessed` true on the target row(s);
    --all sweeps every currently-unblessed agent + edge in the current
    project. Prints what got blessed."""
    actor = _actor()
    if a.all:
        agents = [x for x in gs.list_agents() if not x.get("blessed")]
        edges = [e for e in gs.list_edges() if not e.get("blessed")]
        for ag in agents:
            gs.bless_agent(ag["_guid"], actor=actor)
        for e in edges:
            gs.bless_edge(e["_guid"], actor=actor)
        print(f"blessed {len(agents)} agent(s) and {len(edges)} edge(s)")
        return 0
    if a.edge:
        src = _resolve_or_die(a.edge[0])
        tgt = _resolve_or_die(a.edge[1])
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
        print("[crew] give an agent name, --edge <src> <dst>, or --all", file=sys.stderr)
        return 1
    ag = _resolve_or_die(a.agent)
    if ag.get("blessed"):
        print(f"'{a.agent}' already blessed")
        return 0
    gs.bless_agent(ag["_guid"], actor=actor)
    print(f"blessed agent '{a.agent}'")
    return 0


def _resolve_or_die(name):
    a = gs.get_agent_by_name(name)
    if not a:
        raise gs.GraphError(f"no such agent: {name}")
    return a


def cmd_connect(a):
    schema.ensure_schema()
    src = _resolve_or_die(a.source)
    tgt = _resolve_or_die(a.target)
    edge = gs.create_edge(src["_guid"], tgt["_guid"], label=a.label or "",
                          description=a.desc or "",
                          conditions=a.when or [], target_action=a.does or "",
                          reply_expected=a.reply,
                          back_conditions=a.when_back or [], back_action=a.does_back or "",
                          back_reply=a.reply_back,
                          max_turns=a.max_turns or 0,
                          token_cap=a.token_cap or 0, cost_cap=a.cost_cap or 0,
                          directed=not a.undirected, transform=a.transform or "",
                          actor=_actor())
    # refresh identity.md on both ends so their "who I may message" is current
    for ag in (src, tgt):
        try:
            spawn.rewrite_identity(gs.get_object(ag["_guid"]), notify=True)
        except gs.GraphError:
            pass
    arrow = "<->" if a.undirected else "->"
    print(f"connected {src['name']} {arrow} {tgt['name']}"
          + (f"  ({a.label})" if a.label else ""))
    for w in (a.when or []):
        print(f"  {src['name']} messages {tgt['name']} when: {w}")
    for w in (a.when_back or []):
        print(f"  {tgt['name']} messages {src['name']} when: {w}")
    return 0


def cmd_disconnect(a):
    src = _resolve_or_die(a.source)
    tgt = _resolve_or_die(a.target)
    edges = (gs.edges_from_to(src["_guid"], tgt["_guid"])
             + gs.edges_from_to(tgt["_guid"], src["_guid"]))
    if not edges:
        print(f"(no edges between {src['name']} and {tgt['name']})")
        return 0
    for e in edges:
        gs.delete_edge(e["_guid"], actor=_actor())
    for ag in (src, tgt):
        try:
            spawn.rewrite_identity(gs.get_object(ag["_guid"]), notify=True)
        except gs.GraphError:
            pass
    print(f"disconnected {src['name']} and {tgt['name']} ({len(edges)} edge(s))")
    return 0


def _fmt_num(v):
    return f"{v:g}" if isinstance(v, (int, float)) else str(v)


def cmd_cap(a):
    """Update the authorizing A->B edge's rate/budget caps via update_edge —
    guard.check enforces DOWNHILL-ONLY for an agent actor (extend/reuse of the
    wave-1 endpoint+lower-only rule), anything for a human. Prints old->new
    for each cap that actually changed."""
    src = _resolve_or_die(a.source)
    tgt = _resolve_or_die(a.target)
    edges = gs.edges_from_to(src["_guid"], tgt["_guid"])
    if not edges:
        print(f"[crew] no edge {a.source} -> {a.target}", file=sys.stderr)
        return 1
    edge = edges[0]
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
    out = gs.update_edge(edge["_guid"], fields, actor=_actor())
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
    src = _resolve_or_die(a.source)
    tgt = _resolve_or_die(a.target)
    edges = gs.edges_from_to(src["_guid"], tgt["_guid"])
    if not edges:
        print(f"[crew] no edge {a.source} -> {a.target}", file=sys.stderr)
        return 1
    gs.set_edge_note(edges[0]["_guid"], a.text, actor=_actor())
    print(f"note set on edge {a.source} -> {a.target}")
    return 0


def cmd_agents(a):
    agents = gs.list_agents()
    if not agents:
        print("(no agents)")
        return 0
    for ag in agents:
        role = f"  — {ag['role']}" if ag.get("role") else ""
        print(f"{ag['name']:<16} {ag.get('home','')}{role}")
    return 0


def cmd_edges(a):
    edges = gs.list_edges()
    if not edges:
        print("(no edges)")
        return 0
    names = {ag["_guid"]: ag["name"] for ag in gs.list_agents()}
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
    """Operator → agent: seed/steer one of YOUR agents directly (ungated — this is
    you talking to your own agent, not peer mail)."""
    text = " ".join(a.words).strip()
    ok, msg = mail.say_to_agent(a.agent, text)
    print("[crew] " + msg, file=(sys.stdout if ok else sys.stderr))
    return 0 if ok else 1


def cmd_peers(a):
    """Show who an agent may message (and when) and who may message it (and what
    they expect). Defaults to the calling agent inside a session."""
    name = a.name or mail.whoami()
    ag = gs.get_agent_by_name(name)
    if not ag:
        print(f"[crew] no such agent: {name}", file=sys.stderr)
        return 1
    names = {x["_guid"]: x["name"] for x in gs.list_agents()}
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
        agents = gs.list_agents()
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
    """One line per agent: tmux session up/down, live pane state, queued + failed
    message counts, role. Pane state is the same detection the dashboard uses
    (tmuxio.detect_status on the claude pane's visible frame)."""
    agents = gs.list_agents()
    if not agents:
        print("(no agents)")
        return 0
    ok, raw = tmuxio.tmux("list-sessions", "-F", "#{session_name}")
    sessions = set(raw.strip().splitlines()) if ok else set()
    pane_map = tmuxio._session_pane_map(force=True)
    counts = {}
    for st in ("queued", "failed"):
        for m in gs.list_messages(status=st, limit=2000):
            k = (m.get("target"), st)
            counts[k] = counts.get(k, 0) + 1
    rows = []
    for ag in agents:
        sess = ag.get("session") or ag["name"]
        up = sess in sessions
        if sess in pane_map:
            state = tmuxio.detect_status(tmuxio.capture_frame(pane_map[sess]))
        else:
            state = "no claude" if up else "-"
        rows.append((ag["name"], "up" if up else "down", state,
                     str(counts.get((ag["name"], "queued"), 0)),
                     str(counts.get((ag["name"], "failed"), 0)),
                     ag.get("role") or ""))
    w = max([5] + [len(r[0]) for r in rows])
    print(f"{'agent':<{w}}  {'session':<7}  {'state':<11}  {'queued':>6}  {'failed':>6}  role")
    for name, up, state, q, f, role in rows:
        print(f"{name:<{w}}  {up:<7}  {state:<11}  {q:>6}  {f:>6}  {role}")
    return 0


def _lifecycle_agents(a):
    """Resolve <name>|--all into agent dicts for the up/down/restart commands."""
    if a.all:
        return gs.list_agents()
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
    Gated by guard.check (op "down") — there's no MorphDB row for a session
    kill, so this is the one lifecycle op that gates+audits itself here rather
    than inside crew.graphstore."""
    try:
        guard.check(actor, "down", name=ag["name"])
    except gs.GraphError as e:
        return f"error: {e}"
    sess = ag.get("session") or ag["name"]
    if not tmuxio.tmux("has-session", "-t", sess)[0]:
        guard.audit(actor, "down", {"name": ag["name"]}, "applied")
        return "already down"
    ok, err = tmuxio.tmux("kill-session", "-t", sess)
    result = "stopped" if ok else f"error: {err.strip() or 'kill-session failed'}"
    guard.audit(actor, "down", {"name": ag["name"]},
               "applied" if ok else "refused", "" if ok else result)
    return result


def cmd_up(a):
    """Revive down agent(s) via spawn.start_session; skip ones already up."""
    agents = _lifecycle_agents(a)
    if not agents:
        print("(no agents)")
        return 0
    actor = _actor()
    rows = []
    for ag in agents:
        if tmuxio.tmux("has-session", "-t", ag.get("session") or ag["name"])[0]:
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
        was = _kill_session(ag, actor)
        if was.startswith("error"):
            rows.append((ag["name"], was))
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
    for m, pair in zip(msgs, pairs):
        ts = time.strftime("%Y-%m-%d %H:%M",
                           time.localtime(int(m.get("created_at") or 0)))
        body = " ".join((m.get("body") or "").split())
        if len(body) > 60:
            body = body[:59] + "…"
        print(f"{ts}  {pair:<{w}}  {m.get('status') or '?':<9}  {body}")
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
    """crew pending — list result=pending graph_edit rows, newest first:
    id-prefix, actor, op, a human-readable args summary, and age."""
    res = gs.list_objects("graph_edit", result="pending", sort="created_at",
                          order="desc", limit=500)
    rows = (res or {}).get("objects", [])
    if not rows:
        print("(no pending requests)")
        return 0
    now = time.time()
    for r in rows:
        age = _fmt_age(now - (r.get("created_at") or now))
        summary = guard._summarize(r.get("op"), r.get("args") or {})
        print(f"{r['_guid'][:18]}  {r.get('actor') or '?':<16}  "
              f"{r.get('op') or '?':<12}  {summary}  ({age} ago)")
    return 0


def _resolve_pending(prefix):
    """Resolve a guid-or-prefix to exactly one PENDING graph_edit row —
    unique-prefix match, or a GraphError listing the matches (ambiguous) /
    saying so (none)."""
    res = gs.list_objects("graph_edit", result="pending", sort="created_at",
                          order="desc", limit=1000)
    rows = (res or {}).get("objects", [])
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
    res = gs.list_objects("graph_edit", sort="created_at", order="desc",
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
        stop_dashboard(); print("dashboard stopped")
    elif action == "open":
        url, _ = start_dashboard()
        import webbrowser; webbrowser.open(url); print(f"opened {url}")
    elif action == "logs":
        try:
            with open(LOGFILE) as f:
                print(f.read()[-4000:])
        except OSError:
            print("(no log yet)")
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
    sp = proj_sub.add_parser("create", help="create a new project")
    sp.add_argument("name")
    sp.set_defaults(fn=cmd_project_create)
    sp = proj_sub.add_parser("list", help="list known projects")
    sp.set_defaults(fn=cmd_project_list)

    s = sub.add_parser("spawn-agent", help="create a long-running agent")
    s.add_argument("name")
    s.add_argument("--role", help="short role, e.g. 'leads agent'")
    s.add_argument("--identity", help="freeform identity/mission text")
    s.add_argument("--home", help="the agent's home directory (must not overlap another agent)")
    s.add_argument("--repo", help="instead of --home: make a git worktree off this repo")
    s.add_argument("--launch-cmd", dest="launch_cmd", help="override the claude launch command")
    s.add_argument("--no-launch", action="store_true", help="don't auto-start claude")
    s.add_argument("--foreman", action="store_true",
                   help="grant graph-editing power (can_edit_graph) at creation — "
                        "human-only, singleton")
    s.set_defaults(fn=cmd_spawn_agent)

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
    s.add_argument("--max-turns", dest="max_turns", type=int, default=0,
                   help="rate-limit messages per hour on this link (0 = unlimited)")
    s.add_argument("--token-cap", dest="token_cap", type=int, default=0,
                   help="refuse sends once target has spent this many tokens in the last hour (0 = uncapped)")
    s.add_argument("--cost-cap", dest="cost_cap", type=float, default=0.0,
                   help="refuse sends once target has spent this many $ in the last hour (0 = uncapped)")
    s.add_argument("--undirected", action="store_true", help="two-way: either may message the other")
    s.add_argument("--transform", metavar="FILE",
                   help="path to a script in var/transforms/ that runs once per "
                        "delivered message on this edge (human-only)")
    s.set_defaults(fn=cmd_connect)

    s = sub.add_parser("disconnect", help="remove the relationship(s) between two agents")
    s.add_argument("source"); s.add_argument("target")
    s.set_defaults(fn=cmd_disconnect)

    s = sub.add_parser("cap", help="update an edge's rate/budget caps (downhill-only for agents)")
    s.add_argument("source"); s.add_argument("target")
    s.add_argument("--max-turns", dest="max_turns", type=int, default=None,
                   help="new hourly rate limit")
    s.add_argument("--token-cap", dest="token_cap", type=int, default=None,
                   help="new hourly token budget")
    s.add_argument("--cost-cap", dest="cost_cap", type=float, default=None,
                   help="new hourly $ budget")
    s.set_defaults(fn=cmd_cap)

    s = sub.add_parser("foreman", help="grant/revoke the foreman (can_edit_graph) flag — human-only, singleton")
    s.add_argument("name")
    s.add_argument("--revoke", action="store_true", help="revoke the flag instead of granting it")
    s.set_defaults(fn=cmd_foreman)

    s = sub.add_parser("bless", help="bless an agent, an edge (--edge SRC DST), or --all — human-only")
    s.add_argument("agent", nargs="?", help="agent name to bless")
    s.add_argument("--edge", nargs=2, metavar=("SRC", "DST"), help="bless the edge(s) SRC -> DST")
    s.add_argument("--all", action="store_true", help="bless every unblessed agent + edge")
    s.set_defaults(fn=cmd_bless)

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
    s.add_argument("-n", "--no-prefix", action="store_true", help="deliver verbatim")
    s.add_argument("words", nargs=argparse.REMAINDER, help="message body")
    s.set_defaults(fn=cmd_message)

    s = sub.add_parser("kickoff", help="seed/steer one of YOUR agents directly (ungated)")
    s.add_argument("agent")
    s.add_argument("words", nargs=argparse.REMAINDER, help="the message / seed task")
    s.set_defaults(fn=cmd_kickoff)

    s = sub.add_parser("peers", help="show who an agent may message and who may message it")
    s.add_argument("name", nargs="?", help="agent (defaults to the current session's agent)")
    s.set_defaults(fn=cmd_peers)

    sub.add_parser("whoami", help="show your agent identity").set_defaults(fn=cmd_whoami)

    s = sub.add_parser("status", help="one-line-per-agent table: session, pane state, mail counts")
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("up", help="revive down agent(s): recreate the tmux session + relaunch claude")
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
                   choices=["queued", "delivered", "failed",
                            "blocked", "ratelimited", "budget", "filtered"],
                   help="filter by delivery status (blocked/ratelimited/budget/"
                        "filtered are refused/dropped sends, kept for audit)")
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
    args = build_parser().parse_args(argv)
    if getattr(args, "project", None):
        os.environ["CREW_PROJECT"] = args.project
    # Resolve the caller identity ONCE, the same anti-spoofing way crew.mail
    # resolves message senders: the live tmux pane's session wins over any
    # env var, so an agent's own shell can't claim to be another actor. Fails
    # OPEN to "human" on any MorphDB hiccup — a backend that isn't up yet
    # (e.g. before the first `crew init`) must never break the CLI itself.
    try:
        who = mail.whoami()
        if who and who != "unknown" and gs.get_agent_by_name(who):
            _ACTOR = who
    except gs.GraphError:
        pass
    try:
        return args.fn(args)
    except gs.GraphError as e:
        msg = str(e)
        if "Unknown app" in msg:
            msg += "  — run `crew init` first to set up the crew backend."
        print(f"[crew] error: {msg}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
