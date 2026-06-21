"""crew.schema — bootstrap the MorphDB app + the agent/edge data model.

Idempotent: safe to run on every `crew init`. We talk to MorphDB's schema API
directly over HTTP (PUT /schema/<type> with a merge doc), so crew has no
dependency on the morphdb skill's CLI script — only on a reachable MorphDB
server. Re-running never destroys data (merge:true; lazy invalidation means
adding a field/relation is O(1) and rewrites no rows).

Run standalone:  python3 -m crew.schema   (uses $CREW_APP or 'crew')
"""
from . import config
from .graphstore import GraphError, _req

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
    "launch_cmd": {"type": "string"},
    "created_at": {"type": "number",  "index": True},
}

EDGE_FIELDS = {
    "label":      {"type": "string"},                    # short relationship name
    "description":{"type": "string"},                    # NL: what each side does
    "condition":  {"type": "string"},                    # NL: when source messages target
    "directed":   {"type": "boolean", "default": True},  # false → either may message
    "created_at": {"type": "number",  "index": True},
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


def ensure_schema(app=None):
    """Create/merge the agent + edge types. Idempotent. Returns the app key used."""
    app = app or config.current_app()
    ensure_app(app)
    _req("PUT", "/schema/agent", {"merge": True, "fields": AGENT_FIELDS}, app=app)
    _req("PUT", "/schema/edge",
         {"merge": True, "fields": EDGE_FIELDS, "relations": EDGE_RELATIONS}, app=app)
    return app


if __name__ == "__main__":
    used = ensure_schema()
    print(f"crew schema ready in MorphDB app '{used}' at {config.morphdb_base()}")
