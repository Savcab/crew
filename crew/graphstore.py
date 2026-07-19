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
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config, guard


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
        # Self-heal schema drift: code that gained a field (merge-only schema)
        # otherwise 400s on every write until someone reruns `crew init` — push
        # the schema once and retry. _healing guards ensure_schema's own writes.
        if (e.code == 400 and "Update the schema first" in msg
                and not _req._healing):
            _req._healing = True
            try:
                from . import schema
                schema.ensure_schema()
                return _req(method, path, body=body, app=app)
            except Exception:
                pass
            finally:
                _req._healing = False
        raise GraphError(f"{e.code}: {msg}") from e
    except urllib.error.URLError as e:
        raise GraphError(
            f"cannot reach MorphDB at {config.morphdb_base()} ({e.reason}). "
            "Is it running? `morphdb status` / `morphdb start`."
        ) from e


_req._healing = False


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
                 "worktree", "status", "launch_cmd",
                 "kind", "can_edit_graph", "notes", "grants")


def create_agent(name, role="", identity="", home=None, session=None,
                 pane=None, worktree=None, launch_cmd=None, status="idle",
                 kind="agent", can_edit_graph=False, notes="", actor="human"):
    """Insert an agent node. Caller is responsible for the spawn side-effects
    (tmux session, identity.md) — this is pure data. Returns the created object.

    `actor` is who's doing the spawning ("human", or an agent name) — gated by
    crew.guard.check (op "spawn") BEFORE any validation, so a refused spawn
    never even reaches the duplicate-name/home checks. `created_by`/`blessed`
    are stamped from `actor`: human-authored rows are blessed, agent-authored
    ones are not (a human/foreman can bless them later)."""
    guard.check(actor, "spawn", name=name)
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
        "kind": kind or "agent", "can_edit_graph": bool(can_edit_graph),
        "created_by": actor, "blessed": (actor == "human"), "notes": notes or "",
    }
    obj = create_object("agent", body)
    guard.audit(actor, "spawn", {"name": name}, "applied")
    return obj


def get_agent_by_name(name):
    """The agent with this exact name, or None. Name is indexed + unique-by-convention."""
    res = list_objects("agent", name=name, limit=1)
    objs = res.get("objects") if res else None
    return objs[0] if objs else None


def list_agents():
    res = list_objects("agent", sort="created_at", order="asc", limit=1000)
    return (res or {}).get("objects", [])


def update_agent(guid, actor="human", **fields):
    """Patch an agent. Gated by guard.check (op "update_agent") on the RAW field
    names being changed — a non-foreman agent is refused outright; a foreman is
    refused only if it's touching a protected field (launch_cmd/kind/
    can_edit_graph — see crew.guard.PROTECTED_AGENT_FIELDS)."""
    guard.check(actor, "update_agent", fields=list(fields.keys()))
    body = {k: v for k, v in fields.items() if k in _AGENT_FIELDS or k == "status"}
    result = patch_object("agent", guid, body)
    guard.audit(actor, "update_agent", {"guid": guid, "fields": list(fields.keys())},
               "applied")
    return result


def set_agent_note(guid, text, actor="human"):
    """Set an agent's freeform `notes` field. Gated by guard.check (op "note")
    — per wave 1's tiers this is ALWAYS allowed, any actor, no foreman flag
    needed (crew.guard's one unconditional exception; see `crew note agent`)."""
    guard.check(actor, "note", guid=guid)
    result = patch_object("agent", guid, {"notes": text or ""})
    guard.audit(actor, "note", {"guid": guid, "on": "agent"}, "applied")
    return result


def bless_agent(guid, actor="human"):
    """Mark an agent row blessed (human review of an agent-authored change).
    Gated by guard.check (op "bless") — human-only, even for a foreman, same
    tier as remove/foreman."""
    guard.check(actor, "bless", guid=guid, on="agent")
    result = patch_object("agent", guid, {"blessed": True})
    guard.audit(actor, "bless", {"guid": guid, "on": "agent"}, "applied")
    return result


def delete_agent(guid, actor="human"):
    """Delete an agent + its edges (MorphDB cascades the edge OBJECTS only if they
    point via relations — here edges are objects, so we drop them explicitly).
    Gated by guard.check (op "remove") — human-only, even for a foreman."""
    guard.check(actor, "remove", guid=guid)
    for e in edges_touching(guid):
        delete_object("edge", e["_guid"])
    result = delete_object("agent", guid)
    guard.audit(actor, "remove", {"guid": guid}, "applied")
    return result


# --------------------------------------------------------------------------- #
# Edges (relationships)
# --------------------------------------------------------------------------- #
def clean_conditions(v):
    """Normalize a conditions value into a clean list of non-empty strings (accepts a
    list, a single string, or None)."""
    if not v:
        return []
    if isinstance(v, str):
        v = v.splitlines()
    return [s.strip() for s in v if isinstance(s, str) and s.strip()]


def edge_conditions(edge, back=False):
    """The trigger LIST for a direction (forward source→target, or back target→source),
    falling back to the legacy singular `condition` string for old forward edges."""
    lst = edge.get("back_conditions" if back else "conditions")
    if isinstance(lst, list) and lst:
        return [s for s in lst if isinstance(s, str) and s.strip()]
    if not back:
        c = (edge.get("condition") or "").strip()
        return [c] if c else []
    return []


def edge_view(edge, agent_guid):
    """Resolve an edge into THIS agent's view: the triggers it sends on (out_*) and
    what it does when messaged (in_*), picking forward vs back fields by whether the
    agent is the edge's source or target. (Back fields only matter on a two-way edge.)"""
    if edge.get("source") == agent_guid:        # agent is source → forward is its outgoing
        return {"out_conditions": edge_conditions(edge, False),
                "out_reply": bool(edge.get("reply_expected")),
                "in_action": (edge.get("back_action") or "").strip(),
                "in_reply": bool(edge.get("back_reply"))}
    return {"out_conditions": edge_conditions(edge, True),  # agent is target → back is outgoing
            "out_reply": bool(edge.get("back_reply")),
            "in_action": (edge.get("target_action") or "").strip(),
            "in_reply": bool(edge.get("reply_expected"))}


def validate_transform_path(path):
    """WAVE 5 attach-time guard for edge.transform: refuse (GraphError) a path
    outside config.TRANSFORMS_DIR (realpath containment — resolved through
    symlinks so a symlink pointing outside the dir can't sneak past) or that
    isn't an existing file. Returns the canonical (realpath) form on success,
    or "" unchanged for an empty path (detaching / no transform).

    Called from create_edge/update_edge AFTER guard.check — by the time this
    runs, a non-human actor has already been refused (crew.guard's
    PROTECTED_EDGE_FIELDS / connect-transform checks are human-only), so this
    function only ever executes for a human attaching or changing a
    transform. It still validates unconditionally (not actor-gated) since a
    bad path is a bad path regardless of who's allowed to set it."""
    if not path:
        return ""
    real = os.path.realpath(os.path.expanduser(str(path)))
    tdir = os.path.realpath(config.TRANSFORMS_DIR)
    if real != tdir and not real.startswith(tdir + os.sep):
        raise GraphError(
            f"transform path {path!r} must be inside {config.TRANSFORMS_DIR} — "
            "put the script in var/transforms/ first")
    if not os.path.isfile(real):
        raise GraphError(
            f"transform script not found: {path!r} — put the script in "
            "var/transforms/ first")
    return real


def create_edge(source_guid, target_guid, label="", description="",
                conditions=None, target_action="", reply_expected=False,
                back_conditions=None, back_action="", back_reply=False,
                max_turns=0, token_cap=0, cost_cap=0, directed=True, condition="",
                transform="", actor="human", _pre_approved=False):
    """Connect two agents. `directed=True` → only source→target may message;
    `directed=False` (two-way) → either may message the other, and the BACK fields
    describe the target→source direction independently.

    Each direction captures: a LIST of trigger `conditions` (an agent can have several
    reasons to message a peer), the receiver's `action` on receipt, and a reply flag.
    `max_turns` is an hourly RATE LIMIT (0 = unlimited) so a tight loop can't run away;
    `token_cap`/`cost_cap` budget the TARGET's hourly claude spend (0 = uncapped —
    enforced at delivery time, see crew.mail).

    `actor` is who's connecting them — gated by guard.check (op "connect") BEFORE
    the self-edge check. `created_by`/`blessed` are stamped from `actor`, same rule
    as create_agent. Every other kwarg ALSO rides along in the check's ctx — an
    agent actor's FINITE-CAPS RULE needs the caps to decide, and a WAVE 4 pending
    row (crew.guard._check_envelope) needs the full set to replay this exact call
    later via approve_pending.

    `transform` (WAVE 5) is a path to a script (must live under
    config.TRANSFORMS_DIR — see validate_transform_path) that runs ONCE per
    message crossing this edge, at delivery accept-time (see crew.mail.deliver).
    Attaching one is human-only — guard.check refuses a non-human actor's
    connect outright when `transform` is set (before it ever reaches the
    envelope/pending logic), so an agent/foreman can never queue one for
    approval either.

    `_pre_approved=True` (WAVE 4, guard.approve_pending's escape hatch ONLY) skips
    the guard.check call entirely — the caller already ran check("human", ...)
    itself and is replaying a stored request, stamping created_by/blessed from
    the ORIGINAL requester (`actor`) rather than "human"."""
    if not _pre_approved:
        guard.check(actor, "connect", source=source_guid, target=target_guid,
                   label=label, description=description, conditions=conditions,
                   target_action=target_action, reply_expected=reply_expected,
                   back_conditions=back_conditions, back_action=back_action,
                   back_reply=back_reply, max_turns=max_turns, token_cap=token_cap,
                   cost_cap=cost_cap, directed=directed, condition=condition,
                   transform=transform)
    if source_guid == target_guid:
        raise GraphError("an agent cannot have an edge to itself")
    transform = validate_transform_path(transform)
    fwd = clean_conditions(conditions if conditions is not None else condition)
    bwd = clean_conditions(back_conditions)
    body = {
        "source": source_guid, "target": target_guid,
        "label": label or "", "description": description or "",
        "conditions": fwd, "condition": "; ".join(fwd),
        "target_action": target_action or "", "reply_expected": bool(reply_expected),
        "back_conditions": bwd, "back_action": back_action or "", "back_reply": bool(back_reply),
        "max_turns": int(max_turns or 0), "token_cap": int(token_cap or 0),
        "cost_cap": float(cost_cap or 0), "directed": bool(directed),
        "transform": transform,
        "created_at": int(time.time()),
        "created_by": actor, "blessed": (actor == "human"),
    }
    edge = create_object("edge", body)
    guard.audit(actor, "connect", {"source": source_guid, "target": target_guid},
               "applied")
    return edge


def update_edge(guid, fields, actor="human", _pre_approved=False):
    """Patch an edge, normalizing the condition lists and keeping the legacy flattened
    `condition` string in sync. `fields` may carry conditions/back_conditions as lists
    (or strings) plus any scalar edge fields.

    Gated by guard.check (op "update_edge") against the CURRENT edge (for the
    endpoint + cap-lowering rule a non-foreman agent is held to) and the raw
    `fields` being applied. `_pre_approved=True` (WAVE 4, guard.approve_pending's
    escape hatch ONLY) skips that guard.check — the caller already ran
    check("human", ...) itself and is replaying a stored cap-raise request."""
    cur = get_object(guid)
    if not _pre_approved:
        guard.check(actor, "update_edge", edge=cur, changes=fields)
    body = dict(fields)
    if "transform" in body:
        # WAVE 5: reaching here at all means a human is setting it (guard's
        # PROTECTED_EDGE_FIELDS refuses any other actor before this point) —
        # still validate unconditionally, same as create_edge.
        body["transform"] = validate_transform_path(body["transform"])
    if "conditions" in body:
        body["conditions"] = clean_conditions(body["conditions"])
        body["condition"] = "; ".join(body["conditions"])
    if "back_conditions" in body:
        body["back_conditions"] = clean_conditions(body["back_conditions"])
    result = patch_object("edge", guid, body)
    guard.audit(actor, "update_edge", {"guid": guid, "fields": fields}, "applied")
    return result


def bless_edge(guid, actor="human"):
    """Mark an edge row blessed. Gated by guard.check (op "bless") — same
    human-only tier as bless_agent."""
    guard.check(actor, "bless", guid=guid, on="edge")
    result = patch_object("edge", guid, {"blessed": True})
    guard.audit(actor, "bless", {"guid": guid, "on": "edge"}, "applied")
    return result


def set_edge_note(guid, text, actor="human"):
    """Set an edge's freeform `notes` field. Gated by guard.check (op "note")
    — same unconditional-allow tier as set_agent_note (see `crew note edge`)."""
    guard.check(actor, "note", guid=guid)
    result = patch_object("edge", guid, {"notes": text or ""})
    guard.audit(actor, "note", {"guid": guid, "on": "edge"}, "applied")
    return result


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


def delete_edge(guid, actor="human"):
    """Drop an edge. Gated by guard.check (op "disconnect") — a topology op,
    so a non-foreman agent is refused outright (no endpoint exception here,
    unlike update_edge). The CURRENT edge is fetched first (same pattern as
    update_edge) so a foreman's ENVELOPE rule can see its endpoints +
    created_by before the delete happens."""
    try:
        edge = get_object(guid)
    except GraphError:
        edge = None
    guard.check(actor, "disconnect", guid=guid, edge=edge)
    result = delete_object("edge", guid)
    guard.audit(actor, "disconnect", {"guid": guid}, "applied")
    return result


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


def _neighbors(agent_guid, near, far):
    """Edges where the agent sits on the `near` end (any direction) PLUS undirected
    edges where it sits on the `far` end — i.e. every link that authorizes a message
    in one chosen direction. `near`/`far` are the relation field names
    ('source'/'target'); each result is (neighbor_guid, edge), deduped by neighbor."""
    out = []
    seen = set()
    for e in (list_objects("edge", limit=2000, **{near: agent_guid}) or {}).get("objects", []):
        g = e.get(far)
        if g and g not in seen:
            seen.add(g)
            out.append((g, e))
    for e in (list_objects("edge", limit=2000, **{far: agent_guid}) or {}).get("objects", []):
        if e.get("directed", True):
            continue
        g = e.get(near)
        if g and g not in seen:
            seen.add(g)
            out.append((g, e))
    return out


def messageable_targets(agent_guid):
    """The agents this agent may message, each with the edge that authorizes it
    (every edge with source=agent, plus every UNDIRECTED edge with target=agent).
    Returns (target_agent_guid, edge). Used to render identity.md."""
    return _neighbors(agent_guid, "source", "target")


def incoming_edges(agent_guid):
    """The agents that may message THIS agent, each with the authorizing edge (every
    edge with target=agent, plus every UNDIRECTED edge with source=agent). Returns
    (source_agent_guid, edge). Renders the receiver's half of the contract."""
    return _neighbors(agent_guid, "target", "source")


# --------------------------------------------------------------------------- #
# Message log (durable, observable delivery — queued/delivered/failed, plus
# refusal AUDIT rows: sends the gate turned away, kept so "did the gate ever
# fire?" is answerable from the log)
# --------------------------------------------------------------------------- #
REFUSAL_STATUSES = ("blocked", "ratelimited", "budget", "filtered")


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
    enforce an edge's max_turns so two agents can't loop forever.

    Ordered NEWEST-FIRST on purpose: a runaway loop is exactly what overflows the
    fetch limit, and an unordered truncation would drop the most-recent rows — the
    in-window ones — and silently blind the limiter precisely when it's needed. With
    desc order the retained rows ARE the recent ones, so the window count stays
    correct however large the log grows."""
    res = list_objects("message", sender=sender, target=target,
                       sort="created_at", order="desc", limit=2000)
    msgs = (res or {}).get("objects", [])
    # refusal audit rows (blocked/ratelimited/budget) are attempts that never
    # delivered — counting them would let a retrying sender pin its own window
    # full forever
    return sum(1 for m in msgs if (m.get("created_at") or 0) >= since_ts
               and m.get("status") not in REFUSAL_STATUSES)


# --------------------------------------------------------------------------- #
# Home-directory uniqueness (one agent per place; no nesting)
# --------------------------------------------------------------------------- #
# Case-insensitive filesystem? On macOS (APFS/HFS+ default) and Windows (NTFS),
# `~/crew/Foo` and `~/crew/foo` are the SAME physical dir, so home equality/nesting
# must compare case-insensitively — else two agents share one home and clobber each
# other's identity.md/CLAUDE.md. NOTE: os.path.normcase does NOT do this — on macOS
# Python uses posixpath, whose normcase is the identity function (it only folds on
# Windows). So we lowercase ourselves, gated on platform.
# ponytail: platform heuristic, not a live FS probe — a case-SENSITIVE APFS volume
# (rare, opt-in) would be over-strict here, and a case-insensitive mount on Linux
# under-strict; switch to probing the actual mount if that ever bites.
_CASE_INSENSITIVE_FS = sys.platform in ("darwin", "win32")


def normalize_home(path):
    """Absolute, symlink-resolved, case-folded (on case-insensitive FSes) home dir,
    so equality/nesting checks compare the SAME canonical path regardless of how it
    was typed."""
    p = os.path.realpath(os.path.expanduser(str(path)))
    return p.lower() if _CASE_INSENSITIVE_FS else p


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
    # Compare against the SAME canonical form (normalize_home casefolds), else a
    # case-insensitive FS would make /Users/Felix vs /users/felix miss this guard.
    root = normalize_home(os.sep)
    home_dir = normalize_home("~")
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
