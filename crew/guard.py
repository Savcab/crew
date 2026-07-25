"""crew.guard — the graph-editing permission gate + audit trail.

WAVE 1 design (settled): a human operator can do anything to the agent graph;
an agent can do almost nothing to it by default. The only way an agent gets
graph-editing power is the `can_edit_graph` ("foreman") flag on its own agent
record — and even a foreman is walled off from a few operations that stay
human-only forever (deleting an agent, blessing a change, granting the foreman
flag itself). Decisions are written to the `graph_edit` audit log
(crew.schema.GRAPH_EDIT_FIELDS) and best-effort notified, so "did the gate ever
fire, and on whom?" is answerable without grepping logs. Ordinary history is
best-effort; a pending row is the queued command itself and must persist before
Crew may tell its requester that the change was queued.

    check(actor, op, **ctx) -> None, or raises GraphError with a teaching
        refusal message (mirrors crew.mail's "BLOCKED: ..." style) explaining
        exactly what the actor would need for this to succeed.
    audit(actor, op, args, result, reason="") -> best-effort graph_edit row +
        notify(); NEVER raises (a MorphDB hiccup here must never break a real
        mutation that already succeeded, nor mask a refusal).
    _pending(...) -> required graph_edit row + best-effort notify; raises the
        persistence error instead of falsely reporting an unrecorded request.

Actor model, as of WAVE 2 (containment):
  * actor == "human" (the literal string cli.py/server/app.py resolve to for
    every non-agent caller) — unrestricted, no checks at all.
  * actor names a registered agent with can_edit_graph=True (a "foreman") —
    may spawn/connect/disconnect/up/down, but CONFINED to its own subtree:
      - spawn: refused past CREW_MAX_AGENTS agents total, or past
        CREW_SPAWN_RATE agent-actor spawns in the trailing hour (combined
        across all agent actors) — see _check_spawn_confinement.
      - connect/disconnect: the ENVELOPE rule — both endpoints must be the
        foreman itself or an agent it created; disconnect additionally
        requires the EDGE itself was created by the foreman — see
        _check_envelope.
      - connect also requires the FINITE-CAPS RULE: max_turns/token_cap/
        cost_cap must ALL be set, finite, and within the AGENT_EDGE_*_CEILING
        knobs — an agent can never hand out an unlimited edge — see
        _check_finite_caps.
      - up/down: the FOREMAN-TOUCH RULE — only agents the foreman itself
        created — see _check_foreman_touch.
    update_agent is confined to descriptive metadata on a child the foreman
    itself created; operational identity/lifecycle fields remain internal or
    human-only. update_edge/cap use wave 1's endpoint+lower-only rule, now
    applied to a foreman too — see _check_edge_update.
    remove/bless/foreman stay human-only even for a foreman.
  * any other actor name (a plain agent, or a name that isn't even a
    registered agent — never trust an unverified caller) — default-deny for
    every graph-editing op, with two narrow exceptions: an agent may note its
    own node or an incident edge, and may edit an incident edge's small set of
    "soft" fields plus LOWERING (never raising) a rate/budget cap.

check() is imported by crew.graphstore (which calls it before every guarded
mutation, then calls audit(...,"applied") itself after the write succeeds —
refusals are audited HERE, inside check(), before it raises, so every outcome
is captured exactly once).

Circular import note: crew.graphstore imports this module at the top level
(guard has no top-level dependency on graphstore, so that direction is safe).
This module needs graphstore back — for agent lookups and for writing the
audit row — so those imports are LOCAL to the functions that need them,
exactly the trick crew.schema uses to avoid importing crew.graphstore's
consumer of it at import time. config has no dependency on graphstore (or
guard), so it's imported normally at the top.
"""
import json
import math
import re
import time
import uuid

from . import config
from .notify import notify

# --------------------------------------------------------------------------- #
# op tiers
# --------------------------------------------------------------------------- #
# Topology ops: for a plain agent actor these are hard-refused; a foreman gets
# them for free THIS WAVE (no "only your own spawns" containment yet — wave 2).
TOPOLOGY_OPS = {"spawn", "connect", "disconnect", "up", "down"}

# Human-only forever (this wave): not even a foreman gets these.
HUMAN_ONLY_OPS = {
    "remove", "bless", "foreman", "approve", "reject", "revoke_grant",
    "project_create", "init", "dashboard_control",
}

# Secret-bearing webhook configuration is a foreman topology power only inside
# the immutable ownership envelope. ``webhook_read`` is included because its
# successful result contains the bearer URL; list/topology reads remain
# secret-free and need no special gate.
WEBHOOK_OPS = {
    "webhook_create", "webhook_read", "webhook_update",
    "webhook_rotate", "webhook_remove",
}

# Ops that behave like update_edge's narrow endpoint-restricted allowance —
# `cap` is reserved for a future standalone "lower a cap" verb; it shares
# update_edge's exact rule so it's defined here even though nothing calls it
# with this op name yet.
EDGE_UPDATE_OPS = {"update_edge", "cap"}

# A plain (non-foreman) agent editing an edge it's an endpoint of may touch
# these fields freely...
EDGE_SAFE_FIELDS = {"notes", "label", "description", "conditions", "target_action"}
# ...and these ONLY by lowering (never raising) the value.
EDGE_CAP_FIELDS = {"max_turns", "token_cap", "cost_cap"}

# A foreman may update only descriptive metadata on a child it created. Keep an
# explicit allowlist so a newly-added persistence field defaults to protected
# instead of silently becoming an agent-controlled lifecycle/identity channel.
FOREMAN_AGENT_FIELDS = {"role", "identity", "notes"}
PROTECTED_AGENT_FIELDS = {
    "name", "home", "session", "pane", "worktree", "status", "runtime",
    "launch_cmd", "kind", "can_edit_graph", "grants",
}

# WAVE 5: attaching/changing an edge's `transform` (code that runs on every
# message crossing that edge — see crew.mail.deliver) is human-only, same tier
# as PROTECTED_AGENT_FIELDS above but for edges: not even the foreman flag
# covers it. Checked at BOTH attach points — connect (_check_connect_transform)
# and update_edge (_check_edge_update, via this set) — since a human can attach
# a transform either at connect time or later via `crew cap`-style edits.
PROTECTED_EDGE_FIELDS = {"transform"}

_TRANSFORM_HUMAN_ONLY_MSG = (
    "attaching or changing a transform is human-only (not even the foreman "
    "flag covers this) — put the script in var/transforms/ first, then ask "
    "the user to run `crew connect <source> <target> --transform <file>`")


def _foreman_msg(actor):
    return (f"graph editing requires the foreman flag — ask the user: "
            f"crew foreman {actor}")


# WAVE 3: verbatim sentence surfaced in a foreman's identity.md "Graph powers"
# section (crew.identity.render_graph_powers) — keep this exact wording, it's
# asserted verbatim by tests and read by the agent as the rule's statement.
FOREMAN_ENVELOPE_SENTENCE = "you may wire only nodes you created, plus yourself"


_HUMAN_ONLY_REASONS = {
    "remove": ("removing an agent requires a human (the foreman flag doesn't "
              "cover this) — ask the user to run `crew remove-agent <name>`"),
    "bless": ("blessing a change requires a human (the foreman flag doesn't "
             "cover this) — ask the user"),
    "foreman": ("granting the foreman flag requires a human — ask the user to "
               "run `crew foreman <name>`"),
    "approve": ("approving a pending request requires a human — ask the user "
               "to run `crew approve <id>`"),
    "reject": ("rejecting a pending request requires a human — ask the user "
              "to run `crew reject <id>`"),
    "revoke_grant": ("revoking a file grant requires a human (the foreman flag "
                     "doesn't cover this) — ask the user to run "
                     "`crew revoke-grant <agent> <name>`"),
    "project_create": ("creating a project requires a human operator (the "
                       "foreman flag doesn't cover control-plane changes) — "
                       "ask the user to run `crew project create <name>`"),
    "init": ("initializing MorphDB or the Crew dashboard requires a human "
             "operator (the foreman flag doesn't cover control-plane changes) "
             "— ask the user to run `crew init`"),
    "dashboard_control": ("starting, stopping, or opening the operator dashboard "
                          "requires a human operator (the foreman flag doesn't "
                          "cover control-plane changes) — ask the user"),
}


# --------------------------------------------------------------------------- #
# audit — best-effort for history, REQUIRED for pending requests
# --------------------------------------------------------------------------- #
_AUDIT_REDACTED = "[REDACTED]"
_AUDIT_SECRET_KEYS = {
    "access_key", "api_key", "authorization", "capability", "cookie",
    "credential", "bearer", "password", "secret", "template", "token",
    "token_hash", "url", "webhook_template", "webhook_token",
    "webhook_token_hash",
}
_AUDIT_SECRET_SUFFIXES = (
    "_access_key", "_api_key", "_authorization", "_capability", "_cookie",
    "_credential", "_password", "_secret", "_template", "_token",
    "_token_hash", "_url",
)
_AUDIT_HOOK_PATH = re.compile(
    r"(?i)(/hooks/)[A-Za-z0-9_-]{16,}")
_AUDIT_CAPABILITY_KEY = re.compile(r"[A-Za-z0-9_-]{24,}")


def _audit_key_is_secret(key):
    if not isinstance(key, str):
        return False
    normalized = key.strip().lower().replace("-", "_")
    return (
        normalized in _AUDIT_SECRET_KEYS
        or normalized.endswith(_AUDIT_SECRET_SUFFIXES)
    )


def _redact_audit_text(value):
    """Remove a webhook capability from otherwise useful audit prose."""
    return _AUDIT_HOOK_PATH.sub(r"\1[REDACTED]", str(value))


def _audit_mapping_key(value):
    """JSON-safe mapping key without invoking user-defined ``__str__``."""
    if isinstance(value, str):
        redacted = _redact_audit_text(value)
        if _AUDIT_CAPABILITY_KEY.fullmatch(redacted):
            return "[REDACTED_KEY]"
        return redacted
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(value) if math.isfinite(value) else "<non-finite-key>"
    return f"<{type(value).__name__}-key>"


def _redact_audit_value(value, *, _key="", _seen=None, _depth=0):
    """Recursively produce a JSON-safe, capability-free audit value.

    Permission callers should pass only the minimum authorization view, but
    this persistence boundary is deliberately defensive: a future nested
    context cannot accidentally turn the durable audit table into a secret
    store. Unknown Python objects are represented by type, never ``repr``,
    because repr output can itself contain credentials.
    """
    if _audit_key_is_secret(_key):
        return _AUDIT_REDACTED
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else f"<{value}>"
    if isinstance(value, str):
        return _redact_audit_text(value)
    if _depth >= 12:
        return "<max-depth>"
    if _seen is None:
        _seen = set()
    identity = id(value)
    if identity in _seen:
        return "<cycle>"
    _seen.add(identity)
    try:
        if isinstance(value, dict):
            return {
                _audit_mapping_key(key): _redact_audit_value(
                    item, _key=key, _seen=_seen, _depth=_depth + 1)
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple, set, frozenset)):
            return [
                _redact_audit_value(
                    item, _seen=_seen, _depth=_depth + 1)
                for item in value
            ]
        return f"<{type(value).__name__}>"
    finally:
        _seen.discard(identity)


def _audit_body(actor, op, args, result, reason="", actor_guid=None):
    """Build the JSON-safe graph_edit body shared by both write policies."""
    safe_args = _redact_audit_value(args)
    try:
        json.dumps(safe_args, allow_nan=False)
    except (TypeError, ValueError):
        safe_args = {"value": "<unserializable>"}
    if actor_guid is None:
        actor_guid = ""
        if actor and actor != "human":
            try:
                from . import graphstore as gs
                resolved = gs.get_agent_by_name(actor)
                actor_guid = (resolved or {}).get("_guid") or ""
            except Exception:
                pass
    return {
        "actor": actor, "op": op, "args": safe_args, "result": result,
        "actor_guid": actor_guid or "",
        "reason": _redact_audit_text(reason or ""),
        "created_at": int(time.time()),
    }


def _notify_audit(actor, op, result, reason=""):
    """Best-effort notification half of an audit decision."""
    try:
        detail = f"{op} {result}" + (f": {reason}" if reason else "")
        notify("graph_edit", actor, detail)
    except Exception:
        pass


def _create_graph_edit(gs, body):
    """Persist one decision with a unique app-local chronological order."""
    with gs._invariant_lock("graph-edit-order"):
        latest = gs.list_objects(
            "graph_edit", sort="created_order", order="desc", limit=1)
        rows = (latest or {}).get("objects", [])
        try:
            previous = int((rows[0] if rows else {}).get("created_order") or 0)
        except (TypeError, ValueError, OverflowError):
            previous = 0
        ordered = dict(body)
        ordered["created_order"] = max(
            time.time_ns() // 1000, previous + 1)
        try:
            return gs.create_object("graph_edit", ordered)
        except Exception as primary_error:
            request_id = ordered.get("request_id")
            if not request_id:
                raise
            try:
                found = gs.list_objects(
                    "graph_edit", request_id=request_id, limit=2)
            except Exception as verification_error:
                raise primary_error from verification_error
            rows = (found or {}).get("objects", [])
            matches = [
                row for row in rows
                if all(row.get(key) == value
                       for key, value in ordered.items())
            ]
            if len(rows) == 1 and len(matches) == 1:
                return matches[0]
            raise


def audit(actor, op, args, result, reason="", *, actor_guid=None):
    """Write a graph_edit row + fire a notify(), best-effort. Never raises —
    an audit-log hiccup must never mask (or undo) an outcome already decided.

    A PENDING decision is different: its row is the operation itself, not just
    history.  `_pending` therefore uses the same body builder but requires its
    create to succeed before it tells the requester the change was queued.
    """
    try:
        from . import graphstore as gs
        _create_graph_edit(
            gs, _audit_body(
                actor, op, args, result, reason, actor_guid=actor_guid))
    except Exception:
        pass
    _notify_audit(actor, op, result, reason)


def _refuse(actor, op, ctx, reason):
    audit(
        actor, op, ctx, "refused", reason,
        actor_guid=ctx.get("_actor_guid"))
    from . import graphstore as gs
    raise gs.GraphError(reason)


# --------------------------------------------------------------------------- #
# WAVE 4: the pending-approval queue
# --------------------------------------------------------------------------- #
# Exactly two op-cases route here instead of _refuse: (a) a foreman's connect
# whose out-of-envelope endpoint is a HUMAN-created agent (_check_envelope),
# and (b) any agent's cap RAISE on an edge it's an endpoint of
# (_check_edge_update). `args` carries everything approve_pending needs to
# replay the exact requested op later — see graphstore.create_edge/
# update_edge's `_pre_approved` escape hatch, which lets approve_pending
# execute the stored op stamped with the ORIGINAL requester as `actor`
# (created_by/audit trail), while the actual authorization gate it re-runs is
# `check("human", ...)` (always clear).
def _pending(actor, op, ctx, args, reason):
    from . import graphstore as gs
    # Unlike an ordinary audit row, this row IS the queued command.  Swallowing
    # its write failure while reporting "queued" would silently lose the user's
    # only chance to approve it.  The guarded mutation has not started yet, so
    # propagating the persistence error is the safe, fail-closed outcome.
    try:
        body = _audit_body(
            actor, op, args, "pending", reason,
            actor_guid=ctx.get("_actor_guid"))
        body["request_id"] = uuid.uuid4().hex
        _create_graph_edit(gs, body)
    except gs.GraphError:
        raise
    except Exception as error:
        raise gs.GraphError(
            f"could not persist pending request: {error}") from error
    _notify_audit(actor, op, "pending", reason)
    raise gs.GraphError(reason)


# --------------------------------------------------------------------------- #
# check — the gate
# --------------------------------------------------------------------------- #
def check(actor, op, **ctx):
    """Refuse (raise GraphError) or allow (return None) `actor` performing
    `op`. `ctx` carries whatever the caller has on hand that the op's rule
    needs — see each branch below for what it reads.

    ctx keys used:
      note: on ("agent"/"edge"), target (the CURRENT target dict)
      update_agent: target (the CURRENT agent dict), fields (changed names)
      update_edge/cap: edge (the CURRENT edge dict, for source/target + old
        cap values), changes (the raw fields dict being applied)
      foreman: name (the agent being granted/revoked), revoke (bool)

    WAVE 3: "foreman" is handled BEFORE the `actor == "human"` bypass — unlike
    every other op, a human is not unconditionally clear here: granting the
    flag is still gated by the SINGLETON rule (only one foreman at a time).
    Revoking is never blocked by the singleton rule (it can only shrink the
    foreman set, never violate it), and a non-human actor is refused outright
    regardless (still human-only to GRANT/revoke) — see _check_foreman_singleton.
    """
    if op == "foreman":
        _check_foreman_singleton(actor, ctx)
        return
    if actor == "human":
        return

    from . import graphstore as gs
    try:
        agent = gs.get_agent_by_name(actor)
    except gs.GraphError:
        agent = None
    is_foreman = bool(agent and agent.get("can_edit_graph"))
    if agent and agent.get("_guid"):
        ctx["_actor_guid"] = agent["_guid"]

    if op == "activity":
        # Presence, not topology: any registered agent may update ITS OWN
        # activity line, nothing else. (Humans passed the bypass above.)
        target = ctx.get("target") or {}
        aguid = agent.get("_guid") if agent else None
        if not aguid:
            _refuse(actor, op, ctx,
                    "only a registered agent may set an activity status")
            return
        if target.get("_guid") == aguid:
            return
        _refuse(actor, op, ctx, "an agent may set only its own activity")
        return

    if op == "note":
        target = ctx.get("target") or {}
        aguid = agent.get("_guid") if agent else None
        if not aguid:
            _refuse(actor, op, ctx,
                    "only a registered agent may write an agent note")
            return
        if ctx.get("on") == "agent" and target.get("_guid") == aguid:
            return
        if (ctx.get("on") == "edge"
                and aguid in (target.get("source"), target.get("target"))):
            return
        scope = "its own node" if ctx.get("on") == "agent" else "an incident edge"
        _refuse(actor, op, ctx, f"an agent may note only {scope}")
        return

    if op == "grant":
        _check_grant(actor, is_foreman, ctx)
        return

    if op in WEBHOOK_OPS:
        _check_webhook(actor, agent, is_foreman, op, ctx)
        return

    if op in HUMAN_ONLY_OPS:
        _refuse(actor, op, ctx, _HUMAN_ONLY_REASONS.get(op, _foreman_msg(actor)))
        return

    if op in TOPOLOGY_OPS:
        if not is_foreman:
            _refuse(actor, op, ctx, _foreman_msg(actor))
            return
        if op == "spawn":
            _check_spawn_confinement(actor, ctx)
        elif op in ("connect", "disconnect"):
            if op == "connect" and ctx.get("transform"):
                _refuse(actor, op, ctx, _TRANSFORM_HUMAN_ONLY_MSG)
                return
            if op == "connect":
                # Cap validity is a prerequisite for creating a request at
                # all.  `_check_envelope` may terminate by queueing a pending
                # request, so it must never run before this check.
                _check_finite_caps(actor, ctx)
            _check_envelope(actor, agent, op, ctx)
        else:  # up / down
            _check_foreman_touch(actor, op, ctx)
        return

    if op == "update_agent":
        if not is_foreman:
            _refuse(actor, op, ctx, _foreman_msg(actor))
            return
        target = ctx.get("target") or {}
        if (not target
                or not target.get("created_by_guid")
                or target.get("created_by_guid") != (agent or {}).get("_guid")):
            _refuse(
                actor, op, ctx,
                "a foreman may update only an agent it created — this node "
                "belongs to the user or another branch")
            return
        blocked = set(ctx.get("fields") or ()) - FOREMAN_AGENT_FIELDS
        if blocked:
            _refuse(actor, op, ctx,
                   f"foreman may change only descriptive fields "
                   f"({', '.join(sorted(FOREMAN_AGENT_FIELDS))}); "
                   f"{', '.join(sorted(blocked))} is operational or "
                   "human-controlled — ask the user")
        return

    if op in EDGE_UPDATE_OPS:
        _check_edge_update(actor, op, agent, is_foreman, ctx)
        return

    # Unknown op: default-deny for safety (a typo'd op string must never
    # silently become a free pass).
    _refuse(actor, op, ctx, _foreman_msg(actor))


def _check_edge_update(actor, op, agent, is_foreman, ctx):
    """WAVE 2: the endpoint + lower-only-cap rule from wave 1, now applied to a
    foreman too (no more blanket pass-through) — this IS the DOWNHILL-ONLY
    rule. A foreman may also edit an edge it created wholly inside its immutable
    envelope, which lets it maintain routes between owned webhook/agent nodes
    without granting authority over user-drawn edges.

    WAVE 4: a cap RAISE (any EDGE_CAP_FIELDS value going up, or to 0/unlimited)
    no longer hard-refuses — it routes to PENDING instead (case (b) of the
    spec), for any actor allowed to edit the edge. Safe fields apply
    immediately, lowering applies immediately, and any other field is
    refused."""
    edge = ctx.get("edge") or {}
    changes = ctx.get("changes") or {}

    # WAVE 5: transform is human-only regardless of endpoint/foreman status —
    # checked FIRST so the refusal reason is the accurate teaching message,
    # not the generic "you can only edit edges you're an endpoint of" one an
    # outsider would get, or the misleading "...or a human" one the generic
    # disallowed-field fallback below would otherwise produce.
    blocked = set(changes) & PROTECTED_EDGE_FIELDS
    if blocked:
        _refuse(actor, op, ctx, _TRANSFORM_HUMAN_ONLY_MSG)
        return

    aguid = agent.get("_guid") if agent else None
    is_endpoint = bool(
        aguid and aguid in (edge.get("source"), edge.get("target")))
    envelope = _envelope_guids(actor, agent) if is_foreman else set()
    owns_enveloped_edge = bool(
        aguid
        and is_foreman
        and edge.get("created_by_guid") == aguid
        and edge.get("source") in envelope
        and edge.get("target") in envelope)
    if not is_endpoint and not owns_enveloped_edge:
        _refuse(actor, op, ctx,
               "you can only edit an edge where you are an endpoint, or a "
               "foreman-owned edge inside your envelope — ask the user")
        return

    # Validate the WHOLE requested patch before a cap raise can enqueue it.
    # Dict iteration order must never let an early pending-eligible cap smuggle
    # a later topology/protected field into the human-approved replay.
    disallowed = (
        set(changes) - EDGE_SAFE_FIELDS - EDGE_CAP_FIELDS
        - PROTECTED_EDGE_FIELDS)
    if disallowed:
        field = sorted(disallowed)[0]
        _refuse(actor, op, ctx,
               f"agents may only edit {', '.join(sorted(EDGE_SAFE_FIELDS))} "
               f"(or lower a cap) on their own edges — '{field}' requires the "
               "foreman flag or a human, ask the user")
        return

    # Use the persistence layer's canonical numeric parser before deciding
    # whether any cap is a pending-eligible raise.  A permissive float()
    # conversion here used to let values such as ``"1.5"`` queue an approval
    # that could never be persisted, while graphstore's earlier validation of
    # values such as ``"nan"`` bypassed this gate (and its refusal audit)
    # entirely.  Validate the complete cap patch as one unit so dict order can
    # never queue an earlier raise before discovering a later invalid value.
    from . import graphstore as gs
    cap_fields = set(changes) & EDGE_CAP_FIELDS
    try:
        normalized_changes = gs.normalize_edge_numeric_fields({
            field: changes[field] for field in cap_fields})
    except gs.GraphError as error:
        _refuse(
            actor, op, ctx,
            f"{error} — ask the user to provide valid cap values")
        return
    try:
        normalized_old = gs.normalize_edge_numeric_fields({
            field: edge.get(field) or 0 for field in cap_fields})
    except gs.GraphError as error:
        _refuse(actor, op, ctx, f"{error} — ask the user to repair this edge")
        return

    pending_raise = None
    for field, new_val in changes.items():
        if field in EDGE_SAFE_FIELDS:
            continue
        if field in EDGE_CAP_FIELDS:
            old_val = normalized_old[field]
            new_num = normalized_changes[field]
            if _is_cap_raise(old_val, new_num):
                pending_raise = pending_raise or (field, old_val, new_num)
            continue
    if pending_raise:
        field, old_val, new_num = pending_raise
        _pending(actor, op, ctx, {"guid": edge.get("_guid"), "fields": changes},
                 f"cap raise requested — queued for approval ('{field}' "
                 f"would go from {old_val:g} to {new_num:g})")


def _is_cap_raise(old_value, new_value):
    """Compare cap values where zero means unlimited rather than zero budget."""
    return ((new_value == 0 and old_value != 0)
            or (old_value != 0 and new_value > old_value))


def _check_webhook(actor, agent, is_foreman, op, ctx):
    """Authorize only immutable-GUID-owned webhook capabilities.

    ``ownership`` is intentionally a non-secret view constructed by
    graphstore. Never accept a full webhook row here: refusal auditing stores
    the check context, and the row contains the bearer token and message
    template.
    """
    if not is_foreman:
        _refuse(actor, op, ctx, _foreman_msg(actor))
        return

    from . import graphstore as gs
    actor_guid = (agent or {}).get("_guid")
    if op == "webhook_create":
        owned_count = gs.count_webhooks_by_owner(actor_guid)
        limit = max(0, config.MAX_WEBHOOKS_PER_FOREMAN)
        if owned_count >= limit:
            _refuse(
                actor, op, ctx,
                f"foreman webhook limit reached ({owned_count}/{limit}) — "
                "remove one of your hooks or ask the user to raise "
                "CREW_MAX_WEBHOOKS_PER_FOREMAN")
        return

    ownership = ctx.get("ownership") or {}
    if ownership.get("kind") != gs.WEBHOOK_KIND:
        _refuse(actor, op, ctx, "the target is not a live webhook node")
        return
    if (not ownership.get("created_by_guid")
            or ownership.get("created_by_guid") != actor_guid):
        _refuse(
            actor, op, ctx,
            "a foreman may configure only webhook nodes it created — this "
            "hook is human-managed or belongs to another immutable owner")
        return


# --------------------------------------------------------------------------- #
# WAVE 2 containment
# --------------------------------------------------------------------------- #
def _envelope_guids(actor, agent):
    """Foreman GUID plus every agent/webhook node it created."""
    from . import graphstore as gs
    guids = set()
    if agent:
        guids.add(agent["_guid"])
    creator_guid = (agent or {}).get("_guid")
    for node in gs.list_nodes_by_owner(creator_guid):
        guids.add(node["_guid"])
    return guids


def _agent_name(guid):
    from . import graphstore as gs
    if not guid:
        return "?"
    try:
        obj = gs.get_object(guid)
    except gs.GraphError:
        obj = None
    return (obj or {}).get("name") or guid


_ENVELOPE_MSG = "the {name} node was drawn by the user — ask them"

# WAVE 4: verbatim per the spec — surfaced whenever a foreman's connect to a
# human-created, out-of-envelope endpoint gets queued instead of refused.
_PENDING_CONNECT_MSG = ("request queued for the user's approval — crew "
                        "pending / the dashboard tray will show it. Nothing "
                        "was created yet.")

# The full set of graphstore.create_edge kwargs threaded through guard.check's
# ctx for a "connect" op — this IS "args = full connect args incl. caps" from
# the wave-4 spec: what a pending row needs to replay the exact requested
# create_edge call later (see approve_pending).
_PENDING_CONNECT_FIELDS = (
    "source", "target", "label", "description", "conditions", "target_action",
    "reply_expected", "back_conditions", "back_action", "back_reply",
    "max_turns", "token_cap", "cost_cap", "directed", "condition",
)


def _created_by_human(guid):
    """True iff `guid` resolves to an agent whose created_by == "human" — the
    WAVE 4 test for "does this out-of-envelope connect endpoint route to
    PENDING (case (a)) instead of a hard refusal?" An unresolvable guid (bad
    input, or an agent created by some OTHER agent — impossible in today's
    single-foreman system, but not assumed) is never pending-eligible."""
    from . import graphstore as gs
    if not guid:
        return False
    try:
        obj = gs.get_object(guid)
    except gs.GraphError:
        return False
    return bool(obj) and obj.get("created_by") == "human"


def _check_envelope(actor, agent, op, ctx):
    """ENVELOPE rule (connect/disconnect by foreman F): both endpoints must be
    in {F} ∪ {nodes F created}. disconnect additionally requires the EDGE
    itself was created_by F (an edge inside the envelope that a HUMAN drew
    between two of F's own agents is still not F's to remove).

    WAVE 4: for `connect` specifically, an out-of-envelope endpoint that is
    itself HUMAN-created (case (a) of the spec) no longer hard-refuses — it
    routes to PENDING instead. An out-of-envelope endpoint owned by some
    OTHER agent (not this foreman, not human) still hard-refuses; disconnect
    is unchanged (never pending-eligible)."""
    envelope = _envelope_guids(actor, agent)
    if op == "connect":
        source, target = ctx.get("source"), ctx.get("target")
    else:  # disconnect
        edge = ctx.get("edge") or {}
        source, target = edge.get("source"), edge.get("target")

    for guid in (source, target):
        if guid not in envelope:
            if op == "connect" and _created_by_human(guid):
                args = {k: ctx.get(k) for k in _PENDING_CONNECT_FIELDS}
                _pending(actor, op, ctx, args, _PENDING_CONNECT_MSG)
                return
            _refuse(actor, op, ctx, _ENVELOPE_MSG.format(name=_agent_name(guid)))
            return

    if op == "disconnect":
        edge = ctx.get("edge") or {}
        if (not edge.get("created_by_guid")
                or edge.get("created_by_guid") != (agent or {}).get("_guid")):
            _refuse(actor, op, ctx, "this edge was drawn by the user — ask them")
            return


def _check_finite_caps(actor, ctx):
    """FINITE-CAPS RULE (connect by agent actor): max_turns/token_cap/cost_cap
    must ALL be set, finite, and within their ceilings — an agent can never
    hand out an unlimited/uncapped edge, only a human can."""
    fields = (
        ("max_turns", ctx.get("max_turns"), config.AGENT_EDGE_MAX_TURNS_CEILING),
        ("token_cap", ctx.get("token_cap"), config.AGENT_EDGE_TOKEN_CAP_CEILING),
        ("cost_cap", ctx.get("cost_cap"), config.AGENT_EDGE_COST_CAP_CEILING),
    )
    for name, val, ceiling in fields:
        invalid_type = type(val) is bool
        try:
            num = float(val or 0) if not invalid_type else float("nan")
        except (TypeError, ValueError, OverflowError):
            num = float("nan")
        if not math.isfinite(num) or not (0 < num <= ceiling):
            _refuse(actor, "connect", ctx,
                   f"agents must set a finite '{name}' greater than 0 and at "
                   f"most {ceiling:g} (got {val!r}) — ask the user for an "
                   "unlimited edge")
            return


def _validate_pending_connect(requester, args):
    """Revalidate an untrusted stored connect request before approved replay.

    Only a foreman connect can legitimately produce this pending-operation
    shape. Human approval may authorize crossing the original envelope, but it
    must not erase the requester's finite-cap policy or accept a stale/non-
    foreman requester.
    """
    from . import graphstore as gs
    if not isinstance(args, dict):
        raise gs.GraphError(
            "pending connect request has malformed stored args; expected a mapping")
    requester_agent = (
        gs.get_agent_by_name(requester)
        if requester and requester != "human" else None)
    if not requester_agent or not requester_agent.get("can_edit_graph"):
        raise gs.GraphError(
            "pending connect requester is no longer a foreman; reject the "
            "stale request and ask them to submit a new one")
    _check_finite_caps(requester, args)


def _check_spawn_confinement(actor, ctx):
    """SPAWN CONFINEMENT (agent actor): refuse past CREW_MAX_AGENTS agents
    total, or past CREW_SPAWN_RATE agent-actor spawns in the trailing hour
    (combined across every agent actor, not just this one)."""
    from . import graphstore as gs
    n = len(gs.list_agents())
    if n >= config.MAX_AGENTS:
        _refuse(actor, "spawn", ctx,
               f"the crew already has {n} agents (limit {config.MAX_AGENTS}) "
               "— ask the user to raise CREW_MAX_AGENTS or remove one first")
        return
    since = time.time() - 3600
    if _agent_spawn_count_since(since) >= config.SPAWN_RATE:
        _refuse(actor, "spawn", ctx,
               f"agents may only spawn {config.SPAWN_RATE} new agent(s) per "
               "hour, combined — ask the user to spawn directly, or wait")
        return


def _agent_spawn_count_since(since_ts):
    """How many durable agent-actor-created rows landed at/after ``since_ts``.

    Agent rows, not best-effort audit receipts, are the quota authority.  A
    committed spawn must consume the hourly slot even if its applied audit
    write was lost, while refused attempts create no row and never count.
    """
    from . import graphstore as gs
    count = 0
    for agent in gs.list_agents():
        if agent.get("created_by") in (None, "", "human"):
            continue
        try:
            created_at = float(agent.get("created_at") or 0)
        except (TypeError, ValueError, OverflowError):
            continue
        if created_at >= since_ts:
            count += 1
    return count


def _check_foreman_touch(actor, op, ctx):
    """FOREMAN-TOUCH RULE (up/down by foreman F): F may only bring up/down an
    agent IT created — not any agent in the graph."""
    from . import graphstore as gs
    name = ctx.get("name")
    target = gs.get_agent_by_name(name) if name else None
    actor_agent = gs.get_agent_by_name(actor)
    creator = (target or {}).get("created_by")
    creator_guid = (target or {}).get("created_by_guid")
    if (not target or not creator_guid
            or creator_guid != (actor_agent or {}).get("_guid")):
        _refuse(actor, op, ctx,
               f"'{name}' was created by {creator or 'someone else'}, not "
               "you — ask the user, or only manage agents you created")


# --------------------------------------------------------------------------- #
# WAVE 3: foreman singleton + live quota state
# --------------------------------------------------------------------------- #
def _check_foreman_singleton(actor, ctx):
    """SINGLETON RULE (op "foreman"): still human-only (a non-human actor is
    refused outright, same message as the other HUMAN_ONLY_OPS), and granting
    the flag ("revoke" falsy) is refused if any OTHER agent already holds it —
    the message names that agent and the exact revoke command, per WAVE 3 spec.
    Revoking, or re-granting to the CURRENT holder (idempotent), is never
    blocked by this rule."""
    if actor != "human":
        _refuse(actor, "foreman", ctx, _HUMAN_ONLY_REASONS["foreman"])
        return
    if ctx.get("revoke"):
        return
    name = ctx.get("name")
    from . import graphstore as gs
    for a in gs.list_agents():
        if a.get("can_edit_graph") and a.get("name") != name:
            _refuse(actor, "foreman", ctx,
                   f"'{a['name']}' is already foreman — revoke first: "
                   f"crew foreman {a['name']} --revoke")
            return


# --------------------------------------------------------------------------- #
# GRANTS WAVE: op "grant" — human-only, but a FOREMAN's attempt is queued to
# the pending queue instead of refused outright (reusing the wave-4 _pending
# machinery); a non-foreman/unregistered actor is a plain refusal. Actual
# grant mechanics (path normalization, own-home refusal, symlink, the
# agent.grants entry, identity rewrite, audit) all live in
# crew.spawn.grant_path — this function is ONLY the permission gate.
# `revoke_grant` has no such exception (see HUMAN_ONLY_OPS above) — it's
# human-only forever, same tier as remove/bless/foreman.
# --------------------------------------------------------------------------- #
_PENDING_GRANT_FIELDS = ("name", "agent_guid", "path", "mode")

_PENDING_GRANT_MSG = ("request queued for the user's approval — crew pending "
                     "/ the dashboard tray will show it. Nothing was granted "
                     "yet.")


def _check_grant(actor, is_foreman, ctx):
    if not is_foreman:
        _refuse(actor, "grant", ctx, _foreman_msg(actor))
        return
    args = {k: ctx.get(k) for k in _PENDING_GRANT_FIELDS}
    _pending(actor, "grant", ctx, args, _PENDING_GRANT_MSG)


# --------------------------------------------------------------------------- #
# WAVE 4: resolving a pending request — approve / reject
# --------------------------------------------------------------------------- #
def _notice(agent_name, body):
    """Best-effort system notice to the ORIGINAL requester, via the same
    reserved-"crew"-sender path spawn.rewrite_identity uses for its
    "your connections changed" nudge: a durable, queued message row that the
    background flusher (or the next inline flush) delivers once the agent is
    idle. Never raises — a notify hiccup must never mask (or undo) an
    approve/reject that already landed."""
    if not agent_name or agent_name == "human":
        return
    try:
        from . import graphstore as gs
        gs.create_message("crew", agent_name, body, status="queued")
    except Exception:
        pass


def _summarize(op, args):
    """Human-readable one-liner for a pending row's op+args — used in the
    approve/reject notice text, and available to callers (CLI/dashboard) that
    want the same wording rather than re-deriving it from raw guids."""
    if not isinstance(args, dict):
        return f"{op or 'request'} (malformed stored args)"
    if op == "connect":
        return f"connect {_agent_name(args.get('source'))} → {_agent_name(args.get('target'))}"
    if op == "grant":
        return (f"grant {args.get('name')} {args.get('mode')} access to "
                f"{args.get('path')}")
    if op == "update_edge":
        fields = args.get("fields") or {}
        if not isinstance(fields, dict):
            return "an edge update (malformed stored fields)"
        chs = ", ".join(f"{k}={v}" for k, v in fields.items())
        return f"raise edge cap(s) ({chs})" if chs else "an edge update"
    return op or "a request"


_PENDING_APPLYING = "applying"
_PENDING_APPROVAL_FAILED = "approval_failed"
PENDING_ATTENTION_RESULTS = (
    "pending", _PENDING_APPLYING, _PENDING_APPROVAL_FAILED)


def _validate_pending_approval(gs, row):
    """Validate stored request data before making the durable replay claim.

    Validation failures intentionally leave the row pending so an operator can
    repair or reject a malformed/stale request.  Once replay can begin, the
    caller moves the row to ``applying`` first; every later failure must remain
    non-pending because a transport error cannot prove the mutation did not
    reach MorphDB (or, for grants, the filesystem).
    """
    op = row.get("op")
    args = row.get("args")
    if args is None:
        args = {}
    requester = row.get("actor")
    requester_guid = row.get("actor_guid")
    current_requester = None
    if requester and requester != "human":
        current_requester = gs.get_agent_by_name(requester)
        if not requester_guid:
            raise gs.GraphError(
                "pending request has no immutable requester identity; reject "
                "this legacy request and submit a new one")
        if ((current_requester or {}).get("_guid") != requester_guid):
            raise gs.GraphError(
                "pending requester identity is stale: that agent name was "
                "deleted or replaced; reject and submit a new request")

    if op == "connect":
        _validate_pending_connect(requester, args)
        source, target = args.get("source"), args.get("target")
        if source == target:
            raise gs.GraphError("an agent cannot have an edge to itself")
        # Prove both stored identities still exist and numeric persistence will
        # accept the finite caps before claiming the request.
        gs.get_object(source)
        gs.get_object(target)
        gs.normalize_edge_numeric_fields({
            "max_turns": args.get("max_turns"),
            "token_cap": args.get("token_cap"),
            "cost_cap": args.get("cost_cap"),
        })
        check("human", "connect", source=source, target=target,
              max_turns=args.get("max_turns"),
              token_cap=args.get("token_cap"),
              cost_cap=args.get("cost_cap"))
    elif op == "update_edge":
        if not isinstance(args, dict):
            raise gs.GraphError(
                "pending edge update has malformed stored args; expected a mapping")
        edge_guid = args.get("guid")
        fields = args.get("fields") or {}
        if not isinstance(fields, dict):
            # Preserve the dashboard's defensive corrupt-row contract: this
            # is malformed persisted data (HTTP 500), not an ordinary denied
            # graph request (the GraphError/200 response path).
            raise TypeError(
                "pending edge update has malformed stored fields; expected a mapping")
        if not fields:
            raise gs.GraphError(
                "pending edge update has no cap raise to approve")
        disallowed = set(fields) - EDGE_CAP_FIELDS
        if disallowed:
            raise gs.GraphError(
                "pending edge update may contain cap fields only; stored "
                f"request included {', '.join(sorted(disallowed))}")
        edge = gs.get_object(edge_guid)
        if (not requester_guid
                or requester_guid not in (
                    edge.get("source"), edge.get("target"))):
            raise gs.GraphError(
                "pending edge update requester is no longer an endpoint of "
                "the stored edge; reject and submit a new request")
        normalized = gs.normalize_edge_numeric_fields(fields)
        old_values = gs.normalize_edge_numeric_fields({
            field: edge.get(field) or 0 for field in fields})
        if not any(_is_cap_raise(old_values[field], normalized[field])
                   for field in fields):
            raise gs.GraphError(
                "pending edge update no longer contains an actual cap raise; "
                "reject and submit a new request")
    elif op == "grant":
        if not isinstance(args, dict):
            raise gs.GraphError(
                "pending grant request has malformed stored args; expected a mapping")
        if (args.get("mode") or "ro") not in ("ro", "rw"):
            raise gs.GraphError(
                f"invalid grant mode {args.get('mode')!r}: use --ro or --rw")
        if not (current_requester or {}).get("can_edit_graph"):
            raise gs.GraphError(
                "pending grant requester is no longer a foreman; reject the "
                "stale request and ask them to submit a new one")
        # Pin approval to the immutable row selected when the request queued.
        # Agent names are reusable after deletion; silently resolving a reused
        # name would grant a different GUID/home than the foreman requested.
        target = gs.get_agent_by_name(args.get("name"))
        if not target:
            raise gs.GraphError(f"no such agent: {args.get('name')}")
        expected_guid = args.get("agent_guid")
        if not expected_guid:
            raise gs.GraphError(
                "pending grant has no immutable agent identity; reject this "
                "legacy request and submit a new one")
        if target.get("_guid") != expected_guid:
            raise gs.GraphError(
                "pending grant target is stale: that agent name now belongs "
                "to a replacement identity; reject and submit a new request")
        check("human", "grant", name=args.get("name"), path=args.get("path"),
              mode=args.get("mode"), agent_guid=expected_guid)
    else:
        raise gs.GraphError(f"don't know how to approve op '{op}'")
    return op, args, requester, requester_guid


def _replay_pending_approval(gs, op, args, requester, requester_guid):
    """Execute one already-validated, durably claimed request."""
    from . import spawn as sp
    if op == "connect":
        gs.create_edge(
            args.get("source"), args.get("target"),
            label=args.get("label") or "", description=args.get("description") or "",
            conditions=args.get("conditions"), target_action=args.get("target_action") or "",
            reply_expected=bool(args.get("reply_expected")),
            back_conditions=args.get("back_conditions"),
            back_action=args.get("back_action") or "", back_reply=bool(args.get("back_reply")),
            max_turns=args.get("max_turns") or 0, token_cap=args.get("token_cap") or 0,
            cost_cap=args.get("cost_cap") or 0, directed=args.get("directed", True),
            condition=args.get("condition") or "",
            actor=requester, _pre_approved=True,
            _identity_rewriter=sp.rewrite_identity,
            _identity_notifier=sp.notify_connection_change,
            _actor_guid=requester_guid)
    elif op == "update_edge":
        gs.update_edge(
            args.get("guid"), args.get("fields") or {}, actor=requester,
            _pre_approved=True, _identity_rewriter=sp.rewrite_identity,
            _identity_notifier=sp.notify_connection_change,
            _actor_guid=requester_guid)
    else:  # grant — the validator rejects every unsupported op first
        sp.grant_path(
            args.get("name"), args.get("path"), mode=args.get("mode") or "ro",
            actor=requester, _pre_approved=True,
            _expected_guid=args.get("agent_guid"),
            _actor_guid=requester_guid)


def _mark_approval_failed(gs, guid, stage, error):
    """Best-effort terminal marker after a claimed replay becomes uncertain.

    The preceding ``applying`` claim is already durable, so even if this patch
    also fails the request remains excluded from pending listings and cannot be
    replayed.  Never mask the mutation/finalization exception with this detail
    update.
    """
    detail = f"approval {stage} failed: {error}"
    try:
        gs.patch_object("graph_edit", guid, {
            "result": _PENDING_APPROVAL_FAILED,
            "reason": detail[:2000],
        })
    except Exception:
        pass


def approve_pending(guid, actor="human"):
    """Resolve a pending graph_edit row: validate it's still result="pending",
    replay the exact op it recorded (create_edge for "connect" / update_edge
    for "update_edge"), mark the row "approved", and queue a best-effort
    notice to the ORIGINAL requester. Human-only (guard op "approve").

    The replay re-runs check() as "human" (always clear) rather than as the
    original requester — re-checking as the requester would just hit the same
    envelope/cap-raise rule and re-queue another pending row. The actual
    write still executes with `actor=<original requester>` (via each
    graphstore function's `_pre_approved=True` escape hatch, which skips ITS
    OWN internal guard.check call since we already ran one here) so
    created_by/blessed/the audit trail all read as if the requester's action
    had gone straight through — exactly the shape a human clicking "approve"
    should produce."""
    check(actor, "approve", guid=guid)
    from . import graphstore as gs
    # One app/GUID-scoped flock spans read, claim, replay, and finalization.
    # The durable applying transition is the cross-crash claim; the lock makes
    # that transition exclusive across dashboard and CLI processes.
    with gs._invariant_lock("pending-resolution"):
        row = gs.get_object(guid)
        if not row or row.get("result") != "pending":
            raise gs.GraphError(f"no pending request '{guid}' (already resolved, "
                                "or not a pending row)")
        op, args, requester, requester_guid = _validate_pending_approval(gs, row)
        gs.patch_object("graph_edit", guid, {"result": _PENDING_APPLYING})
        try:
            _replay_pending_approval(
                gs, op, args, requester, requester_guid)
        except Exception as error:
            _mark_approval_failed(gs, guid, "mutation", error)
            raise
        try:
            gs.patch_object("graph_edit", guid, {"result": "approved"})
        except Exception as error:
            _mark_approval_failed(gs, guid, "finalization", error)
            raise
        row = dict(row)
        row["result"] = "approved"
    _notice(requester, f"your request to {_summarize(op, args)} was approved")
    return row


def reject_pending(guid, reason="", actor="human"):
    """Resolve a pending graph_edit row without executing it: mark it
    "rejected" (+ the given reason), and queue a best-effort notice to the
    original requester. Human-only (guard op "reject")."""
    check(actor, "reject", guid=guid)
    from . import graphstore as gs
    reason = reason or ""
    with gs._invariant_lock("pending-resolution"):
        row = gs.get_object(guid)
        if not row or row.get("result") != "pending":
            raise gs.GraphError(f"no pending request '{guid}' (already resolved, "
                                "or not a pending row)")
        gs.patch_object(
            "graph_edit", guid, {"result": "rejected", "reason": reason})
        row = dict(row)
        row["result"], row["reason"] = "rejected", reason
    tail = f": {reason}" if reason else ""
    _notice(row.get("actor"),
           f"your request to {_summarize(row.get('op'), row.get('args') or {})} "
           f"was rejected{tail}")
    return row


def quota_state(foreman_guid=None):
    """Live quota numbers for a foreman's identity.md "Graph powers" section
    (crew.identity.render_graph_powers): agents used / MAX_AGENTS, owned
    webhooks / MAX_WEBHOOKS_PER_FOREMAN, agent-actor spawns in the trailing
    hour / SPAWN_RATE, and the edge-cap ceilings a foreman's `connect` is held
    to. Computed fresh on every call so identity text reflects durable rows."""
    from . import graphstore as gs
    if foreman_guid:
        webhook_count = gs.count_webhooks_by_owner(foreman_guid)
    else:
        # Backward-compatible aggregate for callers that do not render one
        # concrete foreman. Human-owned hooks never consume a foreman quota.
        hooks = gs.list_webhooks()
        hooks = [
            hook for hook in hooks
            if hook.get("created_by_guid")
        ]
        webhook_count = len(hooks)
    return {
        "agents_used": len(gs.list_agents()),
        "max_agents": config.MAX_AGENTS,
        "webhooks_used": webhook_count,
        "max_webhooks": max(0, config.MAX_WEBHOOKS_PER_FOREMAN),
        "spawns_this_hour": _agent_spawn_count_since(time.time() - 3600),
        "spawn_rate": config.SPAWN_RATE,
        "max_turns_ceiling": config.AGENT_EDGE_MAX_TURNS_CEILING,
        "token_cap_ceiling": config.AGENT_EDGE_TOKEN_CAP_CEILING,
        "cost_cap_ceiling": config.AGENT_EDGE_COST_CAP_CEILING,
    }
