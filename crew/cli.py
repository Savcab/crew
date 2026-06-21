#!/usr/bin/env python3
"""crew — the CLI for the agent graph.

    crew init                         set up MorphDB schema + start the dashboard
    crew spawn-agent <name> ...       create a long-running agent (tmux + claude)
    crew connect <A> <B> --when "…"   define a relationship (and authorize A→B msg)
    crew disconnect <A> <B>           remove the relationship(s)
    crew message <target> <text…>     message a connected agent (GATED)
    crew agents | edges | whoami      inspect
    crew remove-agent <name>          delete an agent
    crew dashboard {start|stop|status|open|logs}

Identity is automatic inside a spawned agent (its $CREW_AGENT is pinned), so an
agent never passes its own name to `message`.
"""
import argparse
import os
import socket
import subprocess
import sys
import time

from . import config, graphstore as gs, identity, mail, schema, spawn

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VAR = os.path.join(ROOT, "var")
PIDFILE = os.path.join(VAR, "dashboard.pid")
LOGFILE = os.path.join(VAR, "dashboard.log")


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


def start_dashboard():
    """Start the dashboard server detached (idempotent). Returns (url, started)."""
    if _port_open():
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


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
def cmd_init(a):
    used = schema.ensure_schema()
    print(f"MorphDB ready: app '{used}' at {config.morphdb_base()}")
    if not a.no_dashboard:
        url, started = start_dashboard()
        print(f"dashboard {'started' if started else 'already running'} → {url}")
    print("Next: open the dashboard and click + Agent, or `crew spawn-agent <name> --role \"...\"`.")
    return 0


def cmd_spawn_agent(a):
    schema.ensure_schema()
    agent = spawn.spawn_agent(
        a.name, role=a.role or "", agent_identity=a.identity or "",
        home=a.home, repo=a.repo, launch=not a.no_launch, launch_cmd=a.launch_cmd)
    print(f"spawned agent '{agent['name']}' → session '{agent['session']}' "
          f"(home {agent['home']})")
    print(f"  identity: {os.path.join(agent['home'], config.IDENTITY_FILE)}")
    if not a.no_launch:
        print("  claude is booting; it will read its identity.md shortly.")
    print(f"  connect it:  crew connect {agent['name']} <other> --when \"<condition>\"")
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
                          description=a.desc or "", condition=a.when or "",
                          directed=not a.undirected)
    # refresh identity.md on both ends so their "who I may message" is current
    for ag in (src, tgt):
        try:
            spawn.rewrite_identity(gs.get_object(ag["_guid"]), notify=True)
        except gs.GraphError:
            pass
    arrow = "<->" if a.undirected else "->"
    print(f"connected {src['name']} {arrow} {tgt['name']}"
          + (f"  ({a.label})" if a.label else ""))
    if a.when:
        print(f"  {src['name']} messages {tgt['name']} when: {a.when}")
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
        gs.delete_edge(e["_guid"])
    for ag in (src, tgt):
        try:
            spawn.rewrite_identity(gs.get_object(ag["_guid"]), notify=True)
        except gs.GraphError:
            pass
    print(f"disconnected {src['name']} and {tgt['name']} ({len(edges)} edge(s))")
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
        print(f"{s} {arrow} {t}{label}{cond}")
    return 0


def cmd_message(a):
    sender = mail.whoami()
    body = " ".join(a.words).strip()
    ok, msg = mail.deliver(a.target, body, sender=sender, no_prefix=a.no_prefix)
    print("[crew] " + msg, file=(sys.stdout if ok else sys.stderr))
    return 0 if ok else 1


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
    ag = spawn.remove_agent(a.name, kill_session=not a.keep_session)
    print(f"removed agent '{ag['name']}'"
          + ("" if a.keep_session else f" (killed session '{ag.get('session')}')"))
    return 0


def cmd_dashboard(a):
    action = a.action
    if action == "status":
        print(f"dashboard {'running' if _port_open() else 'stopped'} → {_dash_url()}")
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
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="set up MorphDB schema + start the dashboard")
    s.add_argument("--no-dashboard", action="store_true")
    s.set_defaults(fn=cmd_init)

    s = sub.add_parser("spawn-agent", help="create a long-running agent")
    s.add_argument("name")
    s.add_argument("--role", help="short role, e.g. 'leads agent'")
    s.add_argument("--identity", help="freeform identity/mission text")
    s.add_argument("--home", help="the agent's home directory (must not overlap another agent)")
    s.add_argument("--repo", help="instead of --home: make a git worktree off this repo")
    s.add_argument("--launch-cmd", dest="launch_cmd", help="override the claude launch command")
    s.add_argument("--no-launch", action="store_true", help="don't auto-start claude")
    s.set_defaults(fn=cmd_spawn_agent)

    s = sub.add_parser("connect", help="define a relationship A -> B (authorizes A to message B)")
    s.add_argument("source"); s.add_argument("target")
    s.add_argument("--label", help="short name for the relationship")
    s.add_argument("--desc", help="what each side does / how they relate")
    s.add_argument("--when", help="the condition under which source should message target")
    s.add_argument("--undirected", action="store_true", help="either may message the other")
    s.set_defaults(fn=cmd_connect)

    s = sub.add_parser("disconnect", help="remove the relationship(s) between two agents")
    s.add_argument("source"); s.add_argument("target")
    s.set_defaults(fn=cmd_disconnect)

    sub.add_parser("agents", help="list agents").set_defaults(fn=cmd_agents)
    sub.add_parser("edges", help="list relationships").set_defaults(fn=cmd_edges)

    s = sub.add_parser("message", help="message a connected agent (gated)")
    s.add_argument("target")
    s.add_argument("-n", "--no-prefix", action="store_true", help="deliver verbatim")
    s.add_argument("words", nargs=argparse.REMAINDER, help="message body")
    s.set_defaults(fn=cmd_message)

    sub.add_parser("whoami", help="show your agent identity").set_defaults(fn=cmd_whoami)

    s = sub.add_parser("remove-agent", help="delete an agent")
    s.add_argument("name"); s.add_argument("--keep-session", action="store_true")
    s.set_defaults(fn=cmd_remove_agent)

    s = sub.add_parser("dashboard", help="manage the dashboard server")
    s.add_argument("action", choices=["start", "stop", "status", "open", "logs"])
    s.set_defaults(fn=cmd_dashboard)

    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
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
