"""crew.schema — bootstrap the MorphDB app + the agent/edge data model.

Idempotent: safe to run on every `crew init`. We talk to MorphDB's schema API
directly over HTTP (PUT /schema/<type> with a merge doc), so crew has no
dependency on the morphdb skill's CLI script — only on a reachable MorphDB
server. Re-running never destroys data (merge:true; lazy invalidation means
adding a field/relation is O(1) and rewrites no rows).

Run standalone:  python3 -m crew.schema   (uses $CREW_APP or 'crew')
"""
import math
import urllib.parse

from . import config
from .graphstore import (
    GraphError,
    _invariant_lock,
    _req,
    webhook_token_hash,
)

# field name -> definition. Indexed fields are the ones we actually filter/sort
# on (name lookups, home-conflict scans, status, ordering); the rest are storage.
AGENT_FIELDS = {
    "name":       {"type": "string",  "index": True},   # unique identity slug
    "role":       {"type": "string"},                    # short NL role
    "identity":   {"type": "string"},                    # freeform NL identity text
    "home":       {"type": "string",  "index": True},    # the agent's one home dir
    "session":    {"type": "string"},                    # tmux session name
    "pane":       {"type": "string"},                    # advisory; live-resolved
    "worktree":   {"type": "string"},                    # git worktree name, if any
    "status":     {"type": "string",  "index": True},    # idle/working/needs_input
    "runtime":    {"type": "string",  "index": True},    # claude|codex|custom
    "launch_cmd": {"type": "string"},
    "created_at": {"type": "number",  "index": True},
    # WAVE 1 (guard + audit) additions ------------------------------------- #
    "kind":            {"type": "string"},                 # agent|human (default "agent" in code)
    "can_edit_graph":  {"type": "boolean", "default": False},  # the "foreman" flag
    "created_by":      {"type": "string",  "index": True},     # actor name ("human" or an agent)
    "created_by_guid": {"type": "string",  "index": True},     # immutable actor identity (empty for human)
    "blessed":         {"type": "boolean", "default": False},  # human-authored == blessed
    "notes":           {"type": "string"},
    # ACTIVITY: a short self-set "what I'm doing now" presence line ---------- #
    # (`crew activity`) — shown on the graph card; ephemeral, not audited.
    "activity":        {"type": "string"},
    "activity_at":     {"type": "number"},
    # GRANTS WAVE addition ------------------------------------------------- #
    # list of {"name","path","mode":"ro"|"rw","type":"path","created_at","granted_by"}
    # — a human-only, audited exception to the agent's home boundary (see
    # crew.spawn.grant_path / crew.guard's "grant"/"revoke_grant" ops). Default
    # [] is handled in code (MorphDB has no list-default), same convention as
    # `conditions`/`back_conditions` above. `"type"` is per-entry (today only
    # "path"; a future wave may add "port" entries to the same list).
    "grants":          {"type": "json"},
    # WEBHOOK NODES -------------------------------------------------------- #
    # Webhooks share the node table so existing edge relations can connect a
    # source-only HTTP ingress node to ordinary agents. Agent-only readers
    # filter kind="webhook" in graphstore; these fields are otherwise inert.
    "webhook_token":   {"type": "string"},
    "webhook_token_hash": {"type": "string", "index": True},
    "webhook_template":{"type": "string"},
    "webhook_last_called_at": {"type": "number"},
    "webhook_last_status":    {"type": "string"},
}

EDGE_FIELDS = {
    "label":      {"type": "string"},                    # short relationship name
    "description":{"type": "string"},                    # NL: what each side does
    # FORWARD direction (source → target). `conditions` is a LIST of triggers — an
    # agent can have several reasons to message another. `condition` is the legacy
    # flattened string kept for back-compat reads.
    "condition":  {"type": "string"},                    # legacy/flattened forward trigger
    "conditions": {"type": "json"},                      # NL list: when source messages target
    "target_action": {"type": "string"},                 # NL: what target does on receipt
    "reply_expected":{"type": "boolean", "default": False},  # should target reply to source?
    # BACK direction (target → source), used only when the edge is two-way
    # (directed=false). Its own list of triggers + receiver action + reply flag, so a
    # two-way relationship describes BOTH directions independently.
    "back_conditions": {"type": "json"},                 # NL list: when target messages source
    "back_action":     {"type": "string"},               # NL: what source does on receipt
    "back_reply":      {"type": "boolean", "default": False},
    "max_turns":  {"type": "number",  "default": 0},     # rate limit: msgs/hr (0 = unlimited)
    "token_cap":  {"type": "number",  "default": 0},     # budget: target's tokens/hr (0 = uncapped)
    "cost_cap":   {"type": "number",  "default": 0},     # budget: target's $/hr (0 = uncapped)
    "directed":   {"type": "boolean", "default": True},  # false → either may message (two-way)
    "created_at": {"type": "number",  "index": True},
    # WAVE 1 (guard + audit) additions ------------------------------------- #
    "created_by": {"type": "string",  "index": True},    # actor name ("human" or an agent)
    "created_by_guid": {"type": "string", "index": True}, # immutable actor identity
    "blessed":    {"type": "boolean", "default": False}, # human-authored == blessed
    "transform":  {"type": "string"},                    # reserved for a later wave
    "notes":      {"type": "string"},
}

# A durable log of every agent→agent message. This is what makes delivery
# OBSERVABLE (queued/delivered/failed instead of fire-and-forget), lets the
# background flusher retry a message whose target was busy, gives messages a
# provenance trail, and lets the gate enforce per-edge `max_turns` (count recent
# exchanges). Sender/target are immutable display-name snapshots; the GUID
# fields below are the authoritative identity bindings for queued delivery.
MESSAGE_FIELDS = {
    "sender":     {"type": "string",  "index": True},
    "target":     {"type": "string",  "index": True},
    # Immutable identity snapshots.  Names are display/provenance snapshots and
    # may be reused after deletion; GUIDs bind queued work to the identities that
    # existed when Crew accepted it.  Strings (not relations) intentionally keep
    # the audit row after an endpoint or edge is deleted.
    "sender_guid":{"type": "string",  "index": True},
    "target_guid":{"type": "string",  "index": True},
    "edge_guid":  {"type": "string",  "index": True},
    # Per-attempt idempotency key reconciles a POST whose success response was
    # lost; created_order is a lock-serialized microsecond sequence used for
    # deterministic FIFO even when many messages share created_at.
    "request_id": {"type": "string",  "index": True},
    "created_order":{"type": "number", "index": True},
    "body":       {"type": "string"},
    "no_prefix":  {"type": "boolean", "default": False},
    # queued | delivered | failed, plus refusal audit statuses from
    # graphstore.REFUSAL_STATUSES (including budget_unavailable)
    "status":     {"type": "string",  "index": True},
    "status_detail":{"type": "string"},
    "created_at": {"type": "number",  "index": True},
    "delivered_at":{"type": "number"},
}

# A durable audit log of every graph-editing DECISION (not just successful
# writes) — applied, refused, and pending resolution states. ``applying`` is a
# durable exactly-once claim; ``approval_failed`` is its non-replayable failure
# terminal, alongside pending/approved/rejected. This
# is what makes the guard OBSERVABLE: "was this agent ever refused?" is a
# `crew audit` query, not a grep through logs. `args` is the op's raw kwargs
# (JSON), `result` is the outcome, `reason` is the (possibly empty) teaching
# message shown to the actor.
GRAPH_EDIT_FIELDS = {
    "actor":      {"type": "string",  "index": True},    # "human" or an agent name
    "actor_guid": {"type": "string",  "index": True},    # immutable requester identity
    "op":         {"type": "string",  "index": True},    # spawn|connect|disconnect|...
    "args":       {"type": "json"},
    "result":     {"type": "string",  "index": True},    # applied|refused|pending|applying|approval_failed|approved|rejected
    "reason":     {"type": "string"},
    # Required pending-command writes use this per-attempt key to reconcile a
    # POST whose success response was lost. Ordinary best-effort audit rows may
    # leave it empty.
    "request_id": {"type": "string", "index": True},
    "created_at": {"type": "number",  "index": True},
    # Lock-serialized microsecond order. ``created_at`` remains the human wall
    # clock, while this field makes newest-first pagination deterministic when
    # dozens of decisions land within the same second.
    "created_order":{"type": "number", "index": True},
}

# One durable receipt per inbound webhook invocation. The request row is
# created before fan-out and records a stable edge snapshot, allowing a retry
# after a process/persistence failure to reconcile each message by request ID.
WEBHOOK_DELIVERY_FIELDS = {
    "hook_guid":       {"type": "string", "index": True},
    "request_id":      {"type": "string", "index": True},
    "idempotency_key_hash": {"type": "string", "index": True},
    "payload_hash":    {"type": "string"},
    "receipt_version": {"type": "number"},
    "rendered_message":{"type": "string"},
    "routes":          {"type": "json"},
    "edge_guids":      {"type": "json"},
    "status":          {"type": "string", "index": True},
    "results":         {"type": "json"},
    "received_at":     {"type": "number", "index": True},
    "completed_at":    {"type": "number"},
}

# Edges are objects with two relations to agent. The inverse names (out_edges /
# in_edges) appear automatically on the agent type — one read traverses both ways.
EDGE_RELATIONS = {
    "source": {"to": "agent", "cardinality": "many_to_one", "inverse": "out_edges"},
    "target": {"to": "agent", "cardinality": "many_to_one", "inverse": "in_edges"},
}


def ensure_app(app=None):
    """Create the MorphDB app (tenant) if absent. A 409 means it already exists —
    that's success for an idempotent bootstrap, not an error."""
    app = app or config.current_app()
    try:
        _req("POST", "/app", {"key": app}, app=None)
    except GraphError as e:
        if e.args and str(e.args[0]).startswith("409"):
            return False  # already there
        raise
    return True


def _all_objects(app, object_type):
    """Read one object type in bounded pages for idempotent migrations."""
    rows = []
    offset = 0
    while True:
        query = urllib.parse.urlencode({"limit": 1000, "offset": offset})
        page = _req(
            "GET", f"/objects/{object_type}?{query}", app=app) or {}
        batch = page.get("objects") or []
        rows.extend(batch)
        if len(batch) < 1000:
            return rows
        offset += len(batch)


def _legacy_creator_guid(row, owners_by_name):
    """Resolve a legacy mutable creator name only when chronology proves it."""
    if not isinstance(row, dict) or row.get("created_by_guid"):
        return ""
    creator_name = row.get("created_by")
    if not creator_name or creator_name == "human":
        return ""
    candidates = owners_by_name.get(creator_name) or []
    if len(candidates) != 1:
        return ""
    owner = candidates[0]
    owner_guid = owner.get("_guid") or ""
    if not owner_guid or owner_guid == row.get("_guid"):
        return ""
    try:
        owner_created = float(owner.get("created_at"))
        row_created = float(row.get("created_at"))
    except (TypeError, ValueError, OverflowError):
        return ""
    if (not math.isfinite(owner_created) or not math.isfinite(row_created)
            or owner_created >= row_created):
        # A current agent created after the legacy row is a reused name, not
        # evidence of original ownership. Equal second-resolution timestamps
        # are ambiguous too: a rapid delete/recreate can share that value.
        # Leave both cases unbound/fail-closed.
        return ""
    return owner_guid


def _backfill_legacy_creator_guids(app):
    """Restore pre-GUID ownership without granting rights to name reuse.

    Older Crew rows stored only ``created_by=<mutable name>``.  Merge-adding the
    GUID field would otherwise make every existing foreman child/edge
    unmanageable.  Bind only a unique current name whose creation timestamp is
    no later than the row, and revalidate both rows immediately before PATCH.
    Ambiguous/missing/newer owners remain deliberately unbound.
    """
    with _invariant_lock("ownership-backfill-v1", app=app):
        agents = _all_objects(app, "agent")
        owners_by_name = {}
        for agent in agents:
            owners_by_name.setdefault(agent.get("name"), []).append(agent)
        candidates = []
        for object_type, rows in (
                ("agent", agents), ("edge", _all_objects(app, "edge"))):
            for row in rows:
                owner_guid = _legacy_creator_guid(row, owners_by_name)
                if owner_guid:
                    candidates.append((object_type, row, owner_guid))

        for object_type, snapshot, owner_guid in candidates:
            guid = snapshot.get("_guid")
            try:
                current = _req("GET", f"/object/{guid}", app=app)
                owner = _req("GET", f"/object/{owner_guid}", app=app)
            except GraphError as error:
                if str(error).lstrip().startswith("404:"):
                    continue
                raise
            if (current.get("created_by_guid")
                    or current.get("created_by") != snapshot.get("created_by")
                    or owner.get("name") != snapshot.get("created_by")
                    or _legacy_creator_guid(current, {
                        owner.get("name"): [owner]}) != owner_guid):
                continue
            _req(
                "PATCH", f"/objects/{object_type}/{guid}",
                {"created_by_guid": owner_guid}, app=app)


def _backfill_webhook_token_hashes(app):
    """Add non-bearer lookup keys without ever querying by raw capability."""
    with _invariant_lock("agent", app=app):
        for row in _all_objects(app, "agent"):
            if row.get("kind") != "webhook":
                continue
            token = row.get("webhook_token")
            expected = webhook_token_hash(token)
            if not expected or row.get("webhook_token_hash") == expected:
                continue
            _req(
                "PATCH", f"/objects/agent/{row['_guid']}",
                {"webhook_token_hash": expected}, app=app)


def ensure_schema(app=None):
    """Create/merge the agent + edge types. Idempotent. Returns the app key used."""
    app = app or config.current_app()
    ensure_app(app)
    _req("PUT", "/schema/agent", {"merge": True, "fields": AGENT_FIELDS}, app=app)
    _backfill_webhook_token_hashes(app)
    _req("PUT", "/schema/edge",
         {"merge": True, "fields": EDGE_FIELDS, "relations": EDGE_RELATIONS}, app=app)
    _req("PUT", "/schema/message", {"merge": True, "fields": MESSAGE_FIELDS}, app=app)
    _req("PUT", "/schema/graph_edit", {"merge": True, "fields": GRAPH_EDIT_FIELDS}, app=app)
    _req(
        "PUT", "/schema/webhook_delivery",
        {"merge": True, "fields": WEBHOOK_DELIVERY_FIELDS}, app=app)
    _backfill_legacy_creator_guids(app)
    return app


if __name__ == "__main__":
    used = ensure_schema()
    print(f"crew schema ready in MorphDB app '{used}' at {config.morphdb_base()}")
