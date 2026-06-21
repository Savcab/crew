"""crew.graphstore — the agent graph, stored in MorphDB.

This REPLACES the old SQLite `crewdb.py`. The crew is no longer a fixed
manager/worker/task shape; it is a general directed graph:

    agent  — a node. ONE long-running identity bound to ONE home directory and
             (while live) one `claude` tmux session. Durable: the agent survives
             any single session; a restarted claude re-reads its identity.md.
    edge   — a relationship the USER defines between two agents, in natural
             language: what the source does, what the target does, and the
             `condition` under which the source should message the target. The
             edge is ALSO the authorization: an agent may message another ONLY if
             an edge connects them (see can_message — the delivery gate).

Why MorphDB and why edge-as-OBJECT (not a bare MorphDB relation): a relation
carries no per-link data, but our edges carry description/condition/direction.
So `edge` is a first-class type with two relations (`source`, `target` → agent)
plus its own fields. The messaging gate is then a single index-backed relation
filter — `GET /objects/edge?source=<A>&target=<B>` — exactly the query MorphDB
made filterable in its 2026-06-19 relation-filtering work.

All object I/O is plain HTTP against MorphDB's stable `/objects/*` endpoints
(stdlib urllib, zero deps). Schema setup lives in crew.schema.
"""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config


class GraphError(Exception):
    """Any MorphDB call that failed (HTTP error, bad input, server down)."""


# --------------------------------------------------------------------------- #
# Low-level HTTP to MorphDB
# --------------------------------------------------------------------------- #
def _req(method, path, body=None, app=None):
    """One request to MorphDB. Returns parsed JSON (or None on 204). Raises
    GraphError with the server's error message on a non-2xx, or a clear
    'is it running?' on a connection failure. `app` defaults to the live app key
    (config.current_app); pass app=None explicitly for the app-registration call
    that must NOT carry a tenant header."""
    url = config.morphdb_base().rstrip("/") + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    key = app if app is not None else config.current_app()
    if key:
        req.add_header("X-App-Key", key)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            msg = json.loads(raw)["error"]["message"]
        except Exception:
            msg = raw.decode(errors="replace") or e.reason
        raise GraphError(f"{e.code}: {msg}") from e
    except urllib.error.URLError as e:
        raise GraphError(
            f"cannot reach MorphDB at {config.morphdb_base()} ({e.reason}). "
            "Is it running? `morphdb status` / `morphdb start`."
        ) from e


def _qs(params):
    """Build a querystring, dropping None values (so optional filters omit cleanly)."""
    clean = {k: v for k, v in params.items() if v is not None}
    return ("?" + urllib.parse.urlencode(clean)) if clean else ""


# Generic object helpers (the frontend uses these same endpoints over fetch). ##
def create_object(otype, body):
    return _req("POST", f"/objects/{otype}", body)


def get_object(guid, include=None):
    return _req("GET", f"/object/{guid}{_qs({'include': include})}")


def list_objects(otype, include=None, sort=None, order=None, limit=None,
                 offset=None, **filters):
    """List/query objects. Field filters AND relation filters both ride in as
    plain kwargs (e.g. name='x' for a field, source=guid for a relation) — MorphDB
    resolves which is which. Returns the raw {objects,total,limit,offset} dict."""
    params = dict(filters)
    params.update({"include": include, "sort": sort, "order": order,
                   "limit": limit, "offset": offset})
    return _req("GET", f"/objects/{otype}{_qs(params)}")


def patch_object(otype, guid, body):
    return _req("PATCH", f"/objects/{otype}/{guid}", body)


def delete_object(otype, guid):
    return _req("DELETE", f"/objects/{otype}/{guid}")


# --------------------------------------------------------------------------- #
# Agents
# --------------------------------------------------------------------------- #
_AGENT_FIELDS = ("name", "role", "identity", "home", "session", "pane",
                 "worktree", "status", "launch_cmd")


def create_agent(name, role="", identity="", home=None, session=None,
                 pane=None, worktree=None, launch_cmd=None, status="idle"):
    """Insert an agent node. Caller is responsible for the spawn side-effects
    (tmux session, identity.md) — this is pure data. Returns the created object."""
    if not config.valid_agent_name(name):
        raise GraphError(
            f"invalid agent name {name!r}: letters, digits, '_', '-' only "
            "(no dots/slashes/spaces), max 64 chars")
    # Enforce the unique-name invariant HERE (the schema calls name the "unique
    # identity slug" and the by-name gate assumes it). Callers pre-check too, but
    # keeping it at the data layer closes the check-then-act window and keeps the
    # contract honest. Names are the messaging identity, so a duplicate would make
    # can_message ambiguous.
    if get_agent_by_name(name):
        raise GraphError(f"an agent named '{name}' already exists")
    body = {
        "name": name, "role": role or "", "identity": identity or "",
        "home": home or "", "session": session or name, "pane": pane or "",
        "worktree": worktree or "", "status": status or "idle",
        "launch_cmd": launch_cmd or "", "created_at": int(time.time()),
    }
    return create_object("agent", body)


def get_agent_by_name(name):
    """The agent with this exact name, or None. Name is indexed + unique-by-convention."""
    res = list_objects("agent", name=name, limit=1)
    objs = res.get("objects") if res else None
    return objs[0] if objs else None


def list_agents():
    res = list_objects("agent", sort="created_at", order="asc", limit=1000)
    return (res or {}).get("objects", [])


def update_agent(guid, **fields):
    body = {k: v for k, v in fields.items() if k in _AGENT_FIELDS or k == "status"}
    return patch_object("agent", guid, body)


def delete_agent(guid):
    """Delete an agent + its edges (MorphDB cascades the edge OBJECTS only if they
    point via relations — here edges are objects, so we drop them explicitly)."""
    for e in edges_touching(guid):
        delete_object("edge", e["_guid"])
    return delete_object("agent", guid)


# --------------------------------------------------------------------------- #
# Edges (relationships)
# --------------------------------------------------------------------------- #
def create_edge(source_guid, target_guid, label="", description="",
                condition="", directed=True, target_action="",
                reply_expected=False, max_turns=0):
    """Connect two agents. `directed=True` means only source→target may message;
    `directed=False` means either may message the other.

    The edge captures BOTH sides of the relationship:
      * `condition`      — when the SOURCE should message the target (the trigger);
      * `target_action`  — what the TARGET should do on receipt (its obligation);
      * `reply_expected` — whether the target should reply to the source;
      * `max_turns`      — cap on exchanges along this edge (0 = unlimited), so two
                           agents can't ping-pong forever (the gate enforces it)."""
    if source_guid == target_guid:
        raise GraphError("an agent cannot have an edge to itself")
    body = {
        "source": source_guid, "target": target_guid,
        "label": label or "", "description": description or "",
        "condition": condition or "", "target_action": target_action or "",
        "reply_expected": bool(reply_expected), "max_turns": int(max_turns or 0),
        "directed": bool(directed), "created_at": int(time.time()),
    }
    return create_object("edge", body)


def list_edges(include=None):
    res = list_objects("edge", include=include, sort="created_at", order="asc",
                       limit=2000)
    return (res or {}).get("objects", [])


def edges_from_to(source_guid, target_guid):
    """Every edge with this exact source AND target (index-backed relation filter)."""
    res = list_objects("edge", source=source_guid, target=target_guid, limit=50)
    return (res or {}).get("objects", [])


def edges_touching(agent_guid):
    """All edges with this agent on either end (for cascade-delete / neighbor scans)."""
    out = {}
    for key in ("source", "target"):
        res = list_objects("edge", limit=2000, **{key: agent_guid})
        for e in (res or {}).get("objects", []):
            out[e["_guid"]] = e
    return list(out.values())


def delete_edge(guid):
    return delete_object("edge", guid)


# --------------------------------------------------------------------------- #
# The delivery gate — "you can only message agents you're connected to"
# --------------------------------------------------------------------------- #
def can_message(sender_name, target_name):
    """Is sender→target authorized? True iff a directed edge source=sender,
    target=target exists, OR an UNDIRECTED edge connects them in either
    orientation. This is the hard wall enforced at delivery time (crew.mail)."""
    s = get_agent_by_name(sender_name)
    t = get_agent_by_name(target_name)
    if not s or not t:
        return False
    sg, tg = s["_guid"], t["_guid"]
    # any edge sender→target authorizes (directed or undirected — both let the
    # source message the target).
    if edges_from_to(sg, tg):
        return True
    # an undirected edge stored as target→sender also authorizes sender→target.
    return any(not e.get("directed", True) for e in edges_from_to(tg, sg))


def messageable_targets(agent_guid):
    """The agents this agent may message, each with the edge that authorizes it.
    Returns a list of (target_agent_guid, edge). Used to render identity.md.

      * every edge with source=agent  → may message its target
      * every UNDIRECTED edge with target=agent → may message its source
    """
    out = []
    seen = set()
    for e in (list_objects("edge", source=agent_guid, limit=2000) or {}).get("objects", []):
        g = e.get("target")
        if g and g not in seen:
            seen.add(g)
            out.append((g, e))
    for e in (list_objects("edge", target=agent_guid, limit=2000) or {}).get("objects", []):
        if e.get("directed", True):
            continue
        g = e.get("source")
        if g and g not in seen:
            seen.add(g)
            out.append((g, e))
    return out


def incoming_edges(agent_guid):
    """The agents that may message THIS agent, each with the authorizing edge.
    Returns (source_agent_guid, edge). Used to render the receiver's half of the
    contract in identity.md ("when X messages you, do <target_action>"):

      * every edge with target=agent  → its source may message it
      * every UNDIRECTED edge with source=agent → its target may message it back
    """
    out = []
    seen = set()
    for e in (list_objects("edge", target=agent_guid, limit=2000) or {}).get("objects", []):
        g = e.get("source")
        if g and g not in seen:
            seen.add(g)
            out.append((g, e))
    for e in (list_objects("edge", source=agent_guid, limit=2000) or {}).get("objects", []):
        if e.get("directed", True):
            continue
        g = e.get("target")
        if g and g not in seen:
            seen.add(g)
            out.append((g, e))
    return out


# --------------------------------------------------------------------------- #
# Message log (durable, observable delivery — queued/delivered/failed)
# --------------------------------------------------------------------------- #
def create_message(sender, target, body, status="queued"):
    return create_object("message", {
        "sender": sender, "target": target, "body": body,
        "status": status, "created_at": int(time.time()), "delivered_at": 0,
    })


def mark_message(guid, status, delivered=False):
    body = {"status": status}
    if delivered:
        body["delivered_at"] = int(time.time())
    return patch_object("message", guid, body)


def list_messages(status=None, target=None, limit=200):
    res = list_objects("message", status=status, target=target,
                       sort="created_at", order="asc", limit=limit)
    return (res or {}).get("objects", [])


def recent_message_count(sender, target, since_ts):
    """How many messages sender→target were created at/after since_ts. Used to
    enforce an edge's max_turns so two agents can't loop forever."""
    res = list_objects("message", sender=sender, target=target, limit=2000)
    msgs = (res or {}).get("objects", [])
    return sum(1 for m in msgs if (m.get("created_at") or 0) >= since_ts)


# --------------------------------------------------------------------------- #
# Home-directory uniqueness (one agent per place; no nesting)
# --------------------------------------------------------------------------- #
def normalize_home(path):
    """Absolute, symlink-resolved home dir, so equality/nesting checks compare
    the SAME canonical path regardless of how it was typed."""
    return os.path.realpath(os.path.expanduser(str(path)))


def _is_nested(a, b):
    """True if paths a and b overlap as workspaces: identical, or one contains the
    other. Compared with a trailing sep so '/x/app' does NOT match '/x/app2'."""
    a, b = a.rstrip(os.sep), b.rstrip(os.sep)
    if a == b:
        return True
    return a.startswith(b + os.sep) or b.startswith(a + os.sep)


def unsafe_home_reason(home):
    """Catastrophic-home guard: an agent's home gets an identity.md written into
    it, so refuse to anchor one at the filesystem root, your home directory, or any
    ANCESTOR of it — those are never dedicated agent workspaces and writing into
    them is surprising/destructive. Returns a reason string if unsafe, else None.
    (Normal project subdirectories are fine.)"""
    h = normalize_home(home)
    root = os.path.realpath(os.sep)
    home_dir = os.path.realpath(os.path.expanduser("~"))
    if h == root:
        return "refusing to use the filesystem root '/' as an agent home"
    if h == home_dir:
        return ("refusing to use your home directory (~) as an agent home — "
                "pick a dedicated subdirectory")
    if home_dir.startswith(h + os.sep):
        return (f"refusing to use {h!r} (an ancestor of your home directory) as an "
                "agent home — pick a dedicated subdirectory")
    return None


def home_conflict(home, agents=None):
    """Return the existing agent whose home collides with `home` (same dir, or one
    nested inside the other), or None if `home` is free. Enforces "one directory =
    one agent, and no agent inside another agent's tree" so two agents' work can
    never overlap on disk."""
    h = normalize_home(home)
    for a in (agents if agents is not None else list_agents()):
        ah = a.get("home")
        if ah and _is_nested(h, normalize_home(ah)):
            return a
    return None
