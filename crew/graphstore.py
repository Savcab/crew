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
import contextlib
import fcntl
import hashlib
import hmac
import json
import math
import os
import secrets
import stat
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid

from . import config, guard, runtime as runtimes


class GraphError(Exception):
    """Any MorphDB call that failed (HTTP error, bad input, server down)."""


_CURRENT_APP = object()
WEBHOOK_KIND = "webhook"
_WEBHOOK_ADMISSION_LOCK_SCOPE = "webhook-admission"


# MorphDB field indexes accelerate reads but do not enforce uniqueness on user
# fields (its object table only has a unique GUID).  The invariants below span a
# read followed by a write, so separate CLI/dashboard Python processes must
# serialize that small critical section themselves.  Filenames are hashes both
# to avoid unsafe tenant/scope characters and to keep tenant identifiers out of
# the filesystem namespace.  Call sites use stable agent/edge scopes per app
# plus one backend-wide home-claim scope, so file count is bounded by configured
# apps rather than operations.
_DEFAULT_INVARIANT_LOCK_DIR = os.path.join(
    config.RUNTIME_STATE_ROOT, "graph-invariant-locks")
_INVARIANT_LOCK_DIR = _DEFAULT_INVARIANT_LOCK_DIR
_THREAD_LOCKS = {}
_THREAD_LOCKS_GUARD = threading.Lock()
_IDENTITY_TRANSACTION_STATE = threading.local()


def _invariant_lock_path(scope, app=None):
    """Stable, filesystem-safe lock path for one MorphDB/app invariant."""
    app = config.current_app() if app is None else app
    identity = "\0".join((
        "crew-graph-invariant-v2",
        config.morphdb_base().rstrip("/"),
        str(app),
        str(scope),
    ))
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return os.path.join(_INVARIANT_LOCK_DIR, f"{digest}.lock")


def _thread_lock(path):
    """One in-process lock per flock path (flock supplies process isolation)."""
    with _THREAD_LOCKS_GUARD:
        return _THREAD_LOCKS.setdefault(path, threading.RLock())


@contextlib.contextmanager
def _invariant_lock(scope, app=None):
    """Hold an app-scoped invariant lock, always releasing on exceptions."""
    path = _invariant_lock_path(scope, app=app)
    with _thread_lock(path):
        fd = None
        try:
            try:
                if _INVARIANT_LOCK_DIR == _DEFAULT_INVARIANT_LOCK_DIR:
                    directory = config.runtime_state_dir(
                        "graph-invariant-locks")
                else:
                    directory = config.ensure_private_directory(
                        _INVARIANT_LOCK_DIR)
                directory_flags = os.O_RDONLY
                directory_flags |= getattr(os, "O_DIRECTORY", 0)
                directory_flags |= getattr(os, "O_NOFOLLOW", 0)
                directory_flags |= getattr(os, "O_CLOEXEC", 0)
                directory_fd = os.open(directory, directory_flags)
                try:
                    file_flags = os.O_CREAT | os.O_RDWR
                    file_flags |= getattr(os, "O_NOFOLLOW", 0)
                    file_flags |= getattr(os, "O_CLOEXEC", 0)
                    fd = os.open(
                        os.path.basename(path), file_flags, 0o600,
                        dir_fd=directory_fd)
                finally:
                    os.close(directory_fd)
                info = os.fstat(fd)
                uid = getattr(os, "getuid", lambda: info.st_uid)()
                if not stat.S_ISREG(info.st_mode) or info.st_uid != uid:
                    raise PermissionError(
                        "graph lock must be an owner-controlled regular file")
                os.fchmod(fd, 0o600)
                fcntl.flock(fd, fcntl.LOCK_EX)
            except OSError as error:
                if fd is not None:
                    os.close(fd)
                    fd = None
                raise GraphError(
                    f"could not acquire Crew graph lock {path!r}: {error}") \
                    from error
            yield
        finally:
            if fd is not None:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    os.close(fd)


@contextlib.contextmanager
def _identity_transaction_locks(agent_guids):
    """Serialize each agent row mutation through its final identity publish.

    One app-qualified lock deliberately covers every identity. Crew graphs are
    small and these writes are rare; the fixed scope prevents an unbounded lock
    file per historical GUID and eliminates multi-endpoint deadlock ordering.
    The GUID set is still tracked in-thread so a nested caller may reuse an
    already-held subset (needed by lifecycle helpers), while widening remains a
    programming error that could publish state outside the outer transaction's
    declared identity set.
    """
    app = config.current_app()
    requested = tuple(sorted({(app, str(guid)) for guid in agent_guids if guid}))
    depths = getattr(_IDENTITY_TRANSACTION_STATE, "depths", None)
    if depths is None:
        depths = {}
        _IDENTITY_TRANSACTION_STATE.depths = depths
    held = {key for key, depth in depths.items() if depth}
    new = [key for key in requested if key not in held]
    if held and new:
        raise GraphError(
            "cannot widen a nested identity transaction; acquire the complete "
            "sorted agent GUID set in the outer operation")
    with contextlib.ExitStack() as stack:
        if new:
            stack.enter_context(_invariant_lock("agent-identities", app=app))
        for key in requested:
            depths[key] = depths.get(key, 0) + 1
        try:
            yield
        finally:
            for key in reversed(requested):
                remaining = depths.get(key, 0) - 1
                if remaining > 0:
                    depths[key] = remaining
                else:
                    depths.pop(key, None)


@contextlib.contextmanager
def _edge_identity_transaction(agent_guids):
    """Canonical lock order for graph mutation + endpoint publication."""
    with _identity_transaction_locks(agent_guids):
        with _invariant_lock("edge-authorization"):
            yield


@contextlib.contextmanager
def _agent_identity_transaction(agent_guid):
    """Canonical lock order for one agent mutation + identity publication."""
    with _identity_transaction_locks((agent_guid,)):
        with _invariant_lock("agent"):
            yield


@contextlib.contextmanager
def _home_claim_lock():
    """Serialize physical-home claims across every app on this MorphDB backend.

    Home overlap is a backend-global invariant: two named projects use distinct
    app-level agent locks, but must still never materialize or persist the same
    directory.  Spawn holds this outer lock from its final cross-app read through
    filesystem/tmux creation and the app-locked agent insert.  The fixed synthetic
    app component deliberately makes all Crew tenants on one backend share one
    bounded lock file.  Lock ordering is always home-claim -> app agent lock;
    graphstore mutations never acquire these in the reverse order.
    """
    with _invariant_lock("home-claim", app="__all_crew_apps__"):
        yield


def normalize_edge_numeric_fields(fields):
    """Return a copy with edge caps normalized and non-finite values refused.

    This is intentionally usable by the dashboard before it calls a graphstore
    writer, while create_edge/update_edge call it again as the persistence
    boundary.  Zero remains valid and keeps its existing "unlimited" meaning.
    """
    normalized = dict(fields)
    converters = {
        "max_turns": int,
        "token_cap": int,
        "cost_cap": float,
    }
    for field, convert in converters.items():
        if field not in normalized:
            continue
        raw = normalized[field]
        if type(raw) is bool:
            raise GraphError(f"'{field}' must be a number, not a boolean")
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            raise GraphError(f"'{field}' must be a number")
        if convert is int:
            if isinstance(raw, float):
                raise GraphError(f"'{field}' must be an integer")
            if isinstance(raw, str):
                digits = raw.strip().lstrip("+-")
                if not digits or not digits.isdigit():
                    raise GraphError(f"'{field}' must be an integer")
        try:
            number = convert(raw)
        except (TypeError, ValueError, OverflowError) as error:
            raise GraphError(f"'{field}' must be a finite number") from error
        if ((isinstance(number, float) and not math.isfinite(number))
                or number < 0):
            raise GraphError(
                f"'{field}' must be zero or a positive finite number")
        normalized[field] = number
    return normalized


# --------------------------------------------------------------------------- #
# Low-level HTTP to MorphDB
# --------------------------------------------------------------------------- #
def _req(method, path, body=None, app=_CURRENT_APP):
    """One request to MorphDB. Returns parsed JSON (or None on 204). Raises
    GraphError with the server's error message on a non-2xx, or a clear
    'is it running?' on a connection failure. Omitting `app` uses the live app
    key (config.current_app); passing app=None explicitly omits the tenant header
    for app registration/deletion calls."""
    url = config.morphdb_base().rstrip("/") + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    key = config.current_app() if app is _CURRENT_APP else app
    if key:
        req.add_header("X-App-Key", key)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        try:
            raw = e.read()
            try:
                msg = json.loads(raw)["error"]["message"]
            except Exception:
                msg = raw.decode(errors="replace") or e.reason
            # Self-heal schema drift: code that gained a field (merge-only schema)
            # otherwise 400s on every write until someone reruns `crew init` —
            # push the schema once and retry.  The guard is THREAD-LOCAL: it
            # exists to stop the push's own writes from recursing, and a
            # process-global flag instead made a concurrent writer read another
            # thread's heal as its own and return a false failure.  The push is
            # schema-only — data migrations take graph locks and this path runs
            # from inside them (create_agent holds the agent lock across its
            # writes), where a second flock on the same lock file self-deadlocks.
            if (key and e.code == 400 and "Update the schema first" in msg
                    and not getattr(_HEAL_STATE, "active", False)):
                _HEAL_STATE.active = True
                try:
                    from . import schema
                    schema.push_schema(key)
                    return _req(method, path, body=body, app=key)
                except Exception:
                    pass
                finally:
                    _HEAL_STATE.active = False
            raise GraphError(f"{e.code}: {msg}") from e
        finally:
            # HTTPError is also the live response object.  Reading the body does
            # not close its file/socket, so expected 404/409 paths otherwise leak
            # descriptors until garbage collection.
            e.close()
    except urllib.error.URLError as e:
        raise GraphError(
            f"cannot reach MorphDB at {config.morphdb_base()} ({e.reason}). "
            "Is it running? `morphdb status` / `morphdb start`."
        ) from e


_HEAL_STATE = threading.local()


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
                 offset=None, app=_CURRENT_APP, **filters):
    """List/query objects. Field filters AND relation filters both ride in as
    plain kwargs (e.g. name='x' for a field, source=guid for a relation) — MorphDB
    resolves which is which. Returns the raw {objects,total,limit,offset} dict."""
    params = dict(filters)
    params.update({"include": include, "sort": sort, "order": order,
                   "limit": limit, "offset": offset})
    return _req("GET", f"/objects/{otype}{_qs(params)}", app=app)


def _list_all_exact(otype, include=None, sort="created_at", order="asc",
                    app=_CURRENT_APP, **filters):
    """Every matching object, paged until MorphDB's exact total is exhausted.

    A fixed `limit` answers "the first N", never "all". Crew's invariants —
    foreman singleton, one home per directory, cascade deletion, pair
    authorization, spawn quota — are semantic "all" decisions, so they read
    through here and fail loudly rather than deciding on a first page.
    """
    rows = []
    seen = set()
    offset = 0
    page_size = 1000
    while True:
        result = list_objects(otype, include=include, sort=sort, order=order,
                              limit=page_size, offset=offset, app=app,
                              **filters)
        page = (result or {}).get("objects")
        try:
            total = int((result or {}).get("total"))
        except (TypeError, ValueError, OverflowError) as error:
            raise GraphError(
                f"MorphDB returned an invalid {otype} count") from error
        if total < 0 or not isinstance(page, list):
            raise GraphError(f"MorphDB returned an invalid {otype} page")
        if not page:
            if offset < total:
                raise GraphError(
                    f"MorphDB returned an incomplete {otype} page")
            return rows
        for row in page:
            guid = (row or {}).get("_guid")
            # A row inserted/removed under us can shift a page boundary; the
            # GUID set stays correct even when the offsets do not.
            if guid and guid in seen:
                continue
            if guid:
                seen.add(guid)
            rows.append(row)
        offset += len(page)
        if offset >= total:
            return rows


def patch_object(otype, guid, body):
    return _req("PATCH", f"/objects/{otype}/{guid}", body)


def delete_object(otype, guid):
    return _req("DELETE", f"/objects/{otype}/{guid}")


def _object_snapshot_body(snapshot):
    """Return only persisted fields from a MorphDB object snapshot."""
    return {
        key: value for key, value in dict(snapshot or {}).items()
        if not str(key).startswith("_")
    }


def _object_has_fields(obj, fields):
    return bool(obj) and all(obj.get(key) == value for key, value in fields.items())


def _patch_object_verified(otype, guid, body):
    """PATCH and reconcile a response lost after a server-side commit."""
    try:
        return patch_object(otype, guid, body)
    except Exception as primary_error:
        try:
            current = get_object(guid)
        except Exception as verification_error:
            raise primary_error from verification_error
        if _object_has_fields(current, body):
            return current
        raise


def _create_edge_verified(body):
    """POST one invariant-unique edge and recover a lost success response."""
    try:
        return create_object("edge", body)
    except Exception as primary_error:
        try:
            candidates = edges_from_to(body.get("source"), body.get("target"))
        except Exception as verification_error:
            raise primary_error from verification_error
        matches = [edge for edge in candidates if _object_has_fields(edge, body)]
        if len(matches) == 1:
            return matches[0]
        raise


def _create_agent_verified(body):
    """POST one name-unique graph node and recover a lost success response.

    Callers hold the app-wide agent lock, so an exact-name row observed after a
    transport failure can only be this attempted commit.  Still require every
    persisted field to match before accepting it; a partial/corrupt row is not
    proof of success.
    """
    try:
        return create_object("agent", body)
    except Exception as primary_error:
        try:
            current = get_node_by_name(body.get("name"))
        except Exception as verification_error:
            raise primary_error from verification_error
        if _object_has_fields(current, body):
            return current
        raise


def _restore_object_snapshot(otype, snapshot):
    """Idempotently restore one object under its original GUID.

    MorphDB PATCH is an upsert, which is exactly what compensation needs after
    a delete.  If the PATCH transport result is ambiguous, accept it only when
    a refetch proves every snapshotted field landed; otherwise preserve the
    rollback exception for the transaction's error detail.
    """
    guid = (snapshot or {}).get("_guid")
    if not guid:
        raise GraphError(f"cannot restore {otype}: snapshot has no GUID")
    body = _object_snapshot_body(snapshot)
    try:
        return _patch_object_verified(otype, guid, body)
    except Exception:
        current = get_object(guid)
        if all(current.get(key) == value for key, value in body.items()):
            return current
        raise


def _delete_object_verified(otype, guid):
    """Delete for compensation, tolerating an ambiguous successful response."""
    try:
        return delete_object(otype, guid)
    except Exception:
        try:
            get_object(guid)
        except GraphError as verification_error:
            # GraphError also represents timeouts, connection failures, and
            # server errors.  Only a concrete MorphDB not-found response proves
            # the ambiguous DELETE actually removed the object.
            if str(verification_error).lstrip().startswith("404:"):
                return None
            raise
        raise


def _get_object_if_present(guid):
    """Return None only for a proven 404; backend uncertainty must propagate."""
    try:
        return get_object(guid)
    except GraphError as error:
        if str(error).lstrip().startswith("404:"):
            return None
        raise


def _rewrite_agent_identities(agent_guids, identity_rewriter, *, notify):
    """Refetch and rewrite each distinct live agent in stable input order."""
    for guid in dict.fromkeys(filter(None, agent_guids)):
        identity_rewriter(get_object(guid), notify=notify)


def _notify_agent_identity_changes(agent_guids, identity_notifier):
    """Best-effort post-commit notice for each distinct surviving endpoint.

    Refetch by immutable GUID so a deleted agent name cannot redirect a notice
    to a replacement identity.  Every endpoint is isolated: a stale row or a
    broken notifier must not fail the committed graph operation or suppress a
    later endpoint's notice.
    """
    if identity_notifier is None:
        return
    for guid in dict.fromkeys(filter(None, agent_guids)):
        try:
            identity_notifier(get_object(guid))
        except Exception:
            pass


def _transaction_error(operation, error, rollback_errors):
    """Keep the primary failure visible while reporting compensation trouble."""
    reason = f"{operation} failed: {error}"
    if rollback_errors:
        reason += "; rollback incomplete: " + "; ".join(rollback_errors)
    return GraphError(reason)


# --------------------------------------------------------------------------- #
# Agents
# --------------------------------------------------------------------------- #
_AGENT_FIELDS = ("name", "role", "identity", "home", "session", "pane",
                 "worktree", "status", "runtime", "launch_cmd",
                 "can_edit_graph", "notes", "grants")
WEBHOOK_DESCRIPTION_MAX = 500
WEBHOOK_TEMPLATE_MAX = 16 * 1024


def _resolve_actor_guid(actor):
    """Immutable identity for an agent actor; humans have no agent GUID."""
    if actor == "human":
        return ""
    resolved = get_agent_by_name(actor)
    if not resolved:
        raise GraphError(f"no registered agent identity for actor {actor!r}")
    return resolved.get("_guid") or ""


def _require_actor_guid(actor, expected_guid):
    """Revalidate a mutable actor name against its immutable transaction pin."""
    resolved = get_agent_by_name(actor) if actor and actor != "human" else None
    if (not resolved or not expected_guid
            or resolved.get("_guid") != expected_guid):
        raise GraphError(
            f"actor identity for {actor!r} changed or no longer exists while "
            "the graph transaction was waiting; submit the operation again")
    return resolved


def _audit_actor_kwargs(actor_guid):
    """Preserve exact legacy mock/caller shape for humans, pin agents."""
    return {"actor_guid": actor_guid} if actor_guid else {}


def create_agent(name, role="", identity="", home=None, session=None,
                 pane=None, worktree=None, runtime=None, launch_cmd=None, status="idle",
                 kind="agent", can_edit_graph=False, notes="", actor="human",
                 _actor_guid=None):
    """Insert an agent node. Caller is responsible for the spawn side-effects
    (tmux session, identity.md) — this is pure data. Returns the created object.

    `actor` is who's doing the spawning ("human", or an agent name) — gated by
    crew.guard.check (op "spawn") BEFORE any validation, so a refused spawn
    never even reaches the duplicate-name/home checks. `created_by`/`blessed`
    are stamped from `actor`: human-authored rows are blessed, agent-authored
    ones are not (a human/foreman can bless them later)."""
    guard.check(actor, "spawn", name=name)
    if kind == WEBHOOK_KIND:
        raise GraphError(
            "webhook nodes must be created through create_webhook")
    if not config.valid_agent_name(name):
        raise GraphError(
            f"invalid agent name {name!r}: letters, digits, '_', '-' only "
            "(no dots/slashes/spaces), max 64 chars")
    try:
        runtime_key = runtimes.resolve_runtime(runtime, launch_cmd)
    except ValueError as e:
        raise GraphError(str(e)) from e
    creator_guid = (
        "" if actor == "human"
        else (_actor_guid or _resolve_actor_guid(actor)))
    body = {
        "name": name, "role": role or "", "identity": identity or "",
        "home": home or "", "session": session or name, "pane": pane or "",
        "worktree": worktree or "", "status": status or "idle",
        "runtime": runtime_key,
        "launch_cmd": launch_cmd or "", "created_at": int(time.time()),
        "kind": kind or "agent", "can_edit_graph": bool(can_edit_graph),
        "created_by": actor, "created_by_guid": creator_guid,
        "blessed": (actor == "human"), "notes": notes or "",
    }
    # MorphDB's `index: true` makes name filterable, not unique.  Hold the
    # app-wide agent-invariant lock across exactly the read + POST so two
    # CLI/dashboard processes cannot both pass the same-name check.  One stable
    # file per app avoids unbounded lock-file growth as agent names come and go.
    with _invariant_lock("agent"):
        if get_node_by_name(name):
            raise GraphError(f"a graph node named '{name}' already exists")
        # The earlier check is a side-effect-free preflight.  Recheck every
        # graph-wide creation invariant at the serialized commit point: two
        # distinct names can otherwise both consume the final total/rate slot,
        # or both become foreman after observing an empty singleton set.
        if actor != "human":
            _require_actor_guid(actor, creator_guid)
        guard.check(actor, "spawn", name=name)
        if can_edit_graph:
            guard.check(actor, "foreman", name=name, revoke=False)
        obj = _create_agent_verified(body)
        # Keep the applied receipt ordered with the durable commit even though
        # quota authority comes from the row itself (guard.audit is best-effort).
        guard.audit(
            actor, "spawn", {"name": name}, "applied",
            **_audit_actor_kwargs(creator_guid))
    return obj


def get_node_by_name(name, app=_CURRENT_APP):
    """Any graph node with this name, including webhook nodes.

    Crew serializes its own creates, but MorphDB's name index is filterable,
    not unique — legacy/imported storage can hold two rows with one name. A
    name is an AUTHORITY here (mail, lifecycle, pane ownership, guard), so an
    ambiguous one fails closed instead of silently electing the first row.
    """
    # _qs deliberately drops None query values. Without this guard a corrupt
    # caller identity (for example a sparse row's null name) becomes an
    # unfiltered ``GET /objects/agent?limit=1`` and resolves an unrelated agent.
    if not isinstance(name, str) or not name.strip():
        return None
    res = list_objects("agent", name=name, limit=2, app=app)
    objs = (res or {}).get("objects") or []
    if len(objs) > 1:
        raise GraphError(
            f"graph node name {name!r} is ambiguous: {len(objs)} nodes share "
            "it, so no name-authorized action can run — delete or rename the "
            "duplicate until one identity remains")
    return objs[0] if objs else None


def get_agent_by_name(name, app=_CURRENT_APP):
    """The runtime agent with this exact name, excluding webhook nodes."""
    node = get_node_by_name(name, app=app)
    return None if (node or {}).get("kind") == WEBHOOK_KIND else node


def get_agent_by_guid(guid):
    """The live runtime agent for an immutable GUID, or None after deletion."""
    if not guid:
        return None
    node = _get_object_if_present(guid)
    return None if (node or {}).get("kind") == WEBHOOK_KIND else node


def get_webhook_by_name(name, app=_CURRENT_APP):
    """The webhook node with this exact name, or None."""
    node = get_node_by_name(name, app=app)
    return node if (node or {}).get("kind") == WEBHOOK_KIND else None


def webhook_token_hash(token):
    """Return a domain-separated non-bearer lookup key for one capability."""
    if not isinstance(token, str) or not token:
        return ""
    digest = hashlib.sha256()
    digest.update(b"crew-webhook-token-v1\0")
    digest.update(token.encode("utf-8"))
    return digest.hexdigest()


def get_webhook_by_token(token, app=_CURRENT_APP):
    """Resolve one opaque capability without placing it in a request URL."""
    if not isinstance(token, str) or not token:
        return None
    token_hash = webhook_token_hash(token)
    res = list_objects(
        "agent", webhook_token_hash=token_hash, limit=3, app=app)
    rows = [
        row for row in (res or {}).get("objects", [])
        if row.get("kind") == WEBHOOK_KIND
        and row.get("webhook_token_hash") == token_hash
        and isinstance(row.get("webhook_token"), str)
        and hmac.compare_digest(row["webhook_token"], token)
    ]
    if len(rows) > 1:
        raise GraphError(
            "webhook capability is ambiguous; rotate or delete the duplicate "
            "hook nodes before accepting requests")
    return rows[0] if rows else None


def list_nodes(app=_CURRENT_APP):
    return _list_all_exact("agent", app=app)


def list_agents(app=_CURRENT_APP):
    """Runtime-backed agent nodes only (webhooks have no home or tmux state)."""
    return [
        row for row in list_nodes(app=app)
        if row.get("kind") != WEBHOOK_KIND
    ]


def list_nodes_by_owner(owner_guid, app=_CURRENT_APP):
    """Every node carrying one immutable creator GUID, without canvas limits."""
    if not owner_guid:
        return []
    return _list_all_exact("agent", app=app, created_by_guid=owner_guid)


def list_webhooks(app=_CURRENT_APP):
    """Every webhook via the indexed discriminator, paged past UI limits."""
    return _list_all_exact("agent", app=app, kind=WEBHOOK_KIND)


def count_webhooks_by_owner(owner_guid, app=_CURRENT_APP):
    """Exact per-owner webhook count, independent of graph-list pagination."""
    if not owner_guid:
        return 0
    result = list_objects(
        "agent", created_by_guid=owner_guid, kind=WEBHOOK_KIND,
        limit=1, app=app)
    try:
        total = int((result or {}).get("total"))
    except (TypeError, ValueError, OverflowError) as error:
        raise GraphError(
            "MorphDB returned an invalid owned-webhook count") from error
    if total < 0:
        raise GraphError("MorphDB returned an invalid owned-webhook count")
    return total


def _clean_webhook_fields(description, template):
    description = str(description or "").strip()
    template = str(template or "")
    if len(description) > WEBHOOK_DESCRIPTION_MAX:
        raise GraphError(
            f"webhook description exceeds {WEBHOOK_DESCRIPTION_MAX} characters")
    if len(template) > WEBHOOK_TEMPLATE_MAX:
        raise GraphError(
            f"webhook template exceeds {WEBHOOK_TEMPLATE_MAX} characters")
    return description, template


def _new_webhook_token():
    """A URL-safe capability carrying at least 256 random bits."""
    return secrets.token_urlsafe(32)


def _webhook_ownership_view(hook):
    """Non-secret authorization projection for the webhook guard.

    The complete row also contains the bearer token, its lookup hash, and the
    payload template. Guard refusal contexts are durably audited, so passing
    the full row across that boundary would turn an access denial into a secret
    disclosure.
    """
    return {
        "_guid": (hook or {}).get("_guid"),
        "kind": (hook or {}).get("kind"),
        "created_by_guid": (hook or {}).get("created_by_guid"),
    }


def create_webhook(name, description="", template="", actor="human"):
    """Create a source-only HTTP ingress node in the shared graph namespace."""
    actor_guid = (
        "" if actor == "human" else _resolve_actor_guid(actor))
    guard.check(actor, "webhook_create", name=name)
    if not config.valid_agent_name(name):
        raise GraphError(
            f"invalid webhook name {name!r}: letters, digits, '_', '-' only "
            "(no dots/slashes/spaces), max 64 chars")
    description, template = _clean_webhook_fields(description, template)
    with _invariant_lock("agent"):
        if get_node_by_name(name):
            raise GraphError(f"a graph node named '{name}' already exists")
        if actor != "human":
            _require_actor_guid(actor, actor_guid)
        # Serialize the per-owner quota decision with the durable insert.
        guard.check(actor, "webhook_create", name=name)
        token = _new_webhook_token()
        while get_webhook_by_token(token):
            token = _new_webhook_token()
        body = {
            "name": name, "role": description, "identity": "",
            "home": "", "session": "", "pane": "", "worktree": "",
            "status": "listening", "runtime": WEBHOOK_KIND, "launch_cmd": "",
            "created_at": int(time.time()), "kind": WEBHOOK_KIND,
            "can_edit_graph": False, "created_by": actor,
            "created_by_guid": actor_guid, "blessed": (actor == "human"),
            "notes": "", "grants": [],
            "webhook_token": token,
            "webhook_token_hash": webhook_token_hash(token),
            "webhook_template": template,
            "webhook_last_called_at": 0, "webhook_last_status": "",
        }
        result = _create_agent_verified(body)
        guard.audit(
            actor, "webhook_create", {"name": name}, "applied",
            **_audit_actor_kwargs(actor_guid))
    return result


def read_webhook(guid, actor="human"):
    """Return one webhook's secret configuration after an ownership check."""
    actor_guid = (
        "" if actor == "human" else _resolve_actor_guid(actor))
    with _invariant_lock("agent"):
        if actor != "human":
            _require_actor_guid(actor, actor_guid)
        current = get_object(guid)
        if current.get("kind") != WEBHOOK_KIND:
            raise GraphError(f"node {guid!r} is not a webhook")
        guard.check(
            actor, "webhook_read",
            guid=guid, ownership=_webhook_ownership_view(current))
        result = dict(current)
    # Successful secret reads are decisions too. Keep the receipt useful but
    # deliberately omit every secret/configuration value.
    guard.audit(
        actor, "webhook_read", {"guid": guid}, "applied",
        **_audit_actor_kwargs(actor_guid))
    return result


def update_webhook(guid, *, description=None, template=None, rotate=False,
                   actor="human"):
    """Update an authorized webhook configuration and optionally rotate URL."""
    if description is None and template is None and not rotate:
        raise GraphError(
            "nothing to change — provide a description or template, or rotate")
    op = "webhook_rotate" if rotate else "webhook_update"
    actor_guid = (
        "" if actor == "human" else _resolve_actor_guid(actor))
    body = {}
    # Configuration changes and public receipt creation share one outer
    # admission lock.  Rotation can therefore return only after every request
    # admitted with the old token has either created/reused its durable receipt
    # or failed closed.  The fixed lock order is admission -> agent/delivery.
    with _invariant_lock(_WEBHOOK_ADMISSION_LOCK_SCOPE):
        with _invariant_lock("agent"):
            if actor != "human":
                _require_actor_guid(actor, actor_guid)
            current = get_object(guid)
            if current.get("kind") != WEBHOOK_KIND:
                raise GraphError(f"node {guid!r} is not a webhook")
            guard.check(
                actor, op, guid=guid,
                ownership=_webhook_ownership_view(current))
            next_description = (
                current.get("role") if description is None else description)
            next_template = (
                current.get("webhook_template")
                if template is None else template)
            next_description, next_template = _clean_webhook_fields(
                next_description, next_template)
            if description is not None:
                body["role"] = next_description
            if template is not None:
                body["webhook_template"] = next_template
            if rotate:
                token = _new_webhook_token()
                while get_webhook_by_token(token):
                    token = _new_webhook_token()
                body["webhook_token"] = token
                body["webhook_token_hash"] = webhook_token_hash(token)
            if not body:
                return current
            result = _patch_object_verified("agent", guid, body)
    guard.audit(
        actor, op, {"guid": guid, "fields": sorted(body)}, "applied",
        **_audit_actor_kwargs(actor_guid))
    return result


def mark_webhook_called(guid, status):
    """Best-effort operational presence for the graph card/config modal."""
    with _invariant_lock("agent"):
        current = _get_object_if_present(guid)
        if current is None:
            return None
        if current.get("kind") != WEBHOOK_KIND:
            raise GraphError(f"node {guid!r} is not a webhook")
        return _patch_object_verified(
            "agent", guid, {
                "webhook_last_called_at": time.time(),
                "webhook_last_status": str(status or "")[:240],
            })


def delete_webhook(guid, actor="human", _identity_projector=None,
                   _identity_rewriter=None, _identity_notifier=None):
    """Remove a webhook and its routes while preserving historical receipts."""
    # Keep deletion on the same linearization boundary as public admission.
    # A request cannot validate a soon-to-be-deleted capability and create its
    # receipt after deletion has returned.
    with _invariant_lock(_WEBHOOK_ADMISSION_LOCK_SCOPE):
        return _delete_node(
            guid, actor=actor, _identity_projector=_identity_projector,
            _identity_rewriter=_identity_rewriter,
            _identity_notifier=_identity_notifier,
            _operation="webhook_remove", _expected_kind=WEBHOOK_KIND)


def agent_row_problem(agent):
    """Why a persisted row cannot safely act as an operational agent.

    Storage/invariant scans intentionally keep seeing raw rows. Operator, UI,
    lifecycle, and terminal boundaries use this classifier before turning a
    row into an actionable identity. Sparse legacy rows remain supported as
    long as their immutable identity pair (GUID + valid name) is usable.
    """
    if not isinstance(agent, dict):
        return "row is not an object"
    guid = agent.get("_guid")
    if not isinstance(guid, str) or not guid.strip():
        return "missing a valid GUID"
    if not config.valid_agent_name(agent.get("name")):
        return "missing or invalid agent name"
    return None


def partition_operational_agents(agents):
    """Return ``(usable, malformed)`` without mutating or hiding storage."""
    usable, malformed = [], []
    for agent in agents or ():
        (malformed if agent_row_problem(agent) else usable).append(agent)
    return usable, malformed


def update_agent(guid, actor="human", **fields):
    """Patch an agent through the public governance boundary.

    A human may patch any persisted field. A foreman is limited to descriptive
    metadata on a child it created. The target is fetched and authorized while
    holding the agent invariant lock, preventing a concurrent rename/delete
    from detaching the permission decision from the row that is patched.
    """
    body = {k: v for k, v in fields.items() if k in _AGENT_FIELDS}
    actor_guid = (
        "" if actor == "human" else _resolve_actor_guid(actor))
    with _invariant_lock("agent"):
        if actor != "human":
            _require_actor_guid(actor, actor_guid)
        target = get_object(guid)  # PATCH upserts; require a live agent row.
        guard.check(
            actor, "update_agent", target=target, fields=list(fields.keys()))
        if target.get("kind") == WEBHOOK_KIND:
            raise GraphError(
                "webhook nodes must be changed through update_webhook")
        if "kind" in fields:
            raise GraphError(
                "agent kind is immutable; use dedicated agent or webhook "
                "creation")
        if "name" in body:
            new_name = body.get("name")
            if not config.valid_agent_name(new_name):
                raise GraphError(
                    f"invalid agent name {new_name!r}: letters, digits, '_', '-' only "
                    "(no dots/slashes/spaces), max 64 chars")
            # A rename claims the same identity namespace as create_agent, so
            # it must take the identical app-wide agent lock.  The
            # current row itself is the only acceptable duplicate lookup.
            existing = get_node_by_name(new_name)
            if existing and existing.get("_guid") != guid:
                raise GraphError(f"an agent named '{new_name}' already exists")
            result = patch_object("agent", guid, body)
        else:
            result = patch_object("agent", guid, body)
    guard.audit(
        actor, "update_agent", {"guid": guid, "fields": list(fields.keys())},
        "applied", **_audit_actor_kwargs(actor_guid))
    return result


def set_foreman(guid, revoke=False, actor="human", _identity_rewriter=None):
    """Publish the foreman flag and its durable identity as one outcome.

    The flag changes what an agent is authorized to do immediately, while the
    managed identity tells that runtime about the power.  A rewrite failure
    therefore compensates the row back to its complete prior snapshot and
    republishes that old truth before reporting failure.
    """
    with _agent_identity_transaction(guid):
        target = get_object(guid)
        name = target.get("name")
        guard.check(actor, "foreman", name=name, revoke=bool(revoke))
        updated = _patch_object_verified(
            "agent", guid, {"can_edit_graph": not bool(revoke)})
        if _identity_rewriter is not None:
            try:
                _identity_rewriter(updated, notify=True)
            except Exception as error:
                rollback_errors = []
                try:
                    _restore_object_snapshot("agent", target)
                except Exception as rollback_error:
                    rollback_errors.append(f"foreman row: {rollback_error}")
                try:
                    _rewrite_agent_identities(
                        (guid,), _identity_rewriter, notify=False)
                except Exception as rollback_error:
                    rollback_errors.append(f"identity: {rollback_error}")
                failure = _transaction_error("foreman", error, rollback_errors)
                guard.audit(
                    actor, "foreman", {"name": name, "revoke": bool(revoke)},
                    "failed", str(failure))
                raise failure from error
    guard.audit(
        actor, "foreman", {"name": name, "revoke": bool(revoke)}, "applied")
    return updated


_AGENT_FIELD_UNSET = object()


def update_agent_runtime_state(
        guid, *, pane=_AGENT_FIELD_UNSET, status=_AGENT_FIELD_UNSET):
    """Internal persistence primitive for an already-authorized lifecycle op.

    The deliberately narrow signature cannot change session/home/name/runtime
    identity. Callers must first pass their operation-specific guard (`up` or a
    successful spawn); those paths write their own lifecycle audit row.
    """
    body = {}
    if pane is not _AGENT_FIELD_UNSET:
        body["pane"] = pane
    if status is not _AGENT_FIELD_UNSET:
        body["status"] = status
    with _invariant_lock("agent"):
        get_object(guid)
        return patch_object("agent", guid, body)


def update_agent_grants(guid, grants):
    """Internal grants-only patch after `grant`/`revoke_grant` authorization."""
    with _invariant_lock("agent"):
        get_object(guid)
        return patch_object("agent", guid, {"grants": list(grants or [])})


def set_agent_note(guid, text, actor="human"):
    """Set an agent's freeform note.

    Humans may target any agent; an agent actor may target only its own row.
    Fetch and authorize under the agent invariant lock so a concurrent rename
    or delete cannot change which row the permission check refers to.
    """
    actor_guid = (
        "" if actor == "human" else _resolve_actor_guid(actor))
    with _invariant_lock("agent"):
        if actor != "human":
            _require_actor_guid(actor, actor_guid)
        target = get_object(guid)
        guard.check(actor, "note", guid=guid, on="agent", target=target)
        result = patch_object("agent", guid, {"notes": text or ""})
    guard.audit(
        actor, "note", {"guid": guid, "on": "agent"}, "applied",
        **_audit_actor_kwargs(actor_guid))
    return result


def set_agent_activity(guid, text, actor="human"):
    """Set an agent's ephemeral activity line ("working on website…").

    Presence, not a graph edit: a human may target any agent, an agent only
    its own row (guard op "activity"), and APPLIED updates are deliberately
    not audited — agents may update every few minutes and the audit log is
    for topology/authority changes. Refusals still audit via guard._refuse.
    Text is trimmed and capped at 200 chars; empty clears the line.
    """
    text = (text or "").strip()[:200]
    actor_guid = (
        "" if actor == "human" else _resolve_actor_guid(actor))
    with _invariant_lock("agent"):
        if actor != "human":
            _require_actor_guid(actor, actor_guid)
        target = get_object(guid)
        guard.check(actor, "activity", guid=guid, on="agent", target=target)
        result = patch_object(
            "agent", guid, {"activity": text, "activity_at": time.time()})
    return result


def bless_agent(guid, actor="human"):
    """Mark an agent row blessed (human review of an agent-authored change).
    Gated by guard.check (op "bless") — human-only, even for a foreman, same
    tier as remove/foreman."""
    guard.check(actor, "bless", guid=guid, on="agent")
    with _invariant_lock("agent"):
        get_object(guid)
        result = patch_object("agent", guid, {"blessed": True})
    guard.audit(actor, "bless", {"guid": guid, "on": "agent"}, "applied")
    return result


def _delete_node(guid, actor="human", _identity_projector=None,
                 _identity_rewriter=None, _identity_notifier=None,
                 _operation="remove", _expected_kind=None):
    """Delete one validated graph node after survivor identities are writable.

    The target is irreversible once deleted: MorphDB cannot recreate an agent
    with its old GUID after its relation endpoints vanish.  Therefore survivor
    identities are first rendered with the target excluded, while the existing
    agent→edge locks still exclude topology changes.  Incident edges are then
    deleted with verified outcomes and the target is deleted last.  If a delete
    fails, the still-live target makes same-GUID edge restoration valid and
    regular identity rewrites republish the graph state compensation left.
    Public callers must use ``delete_agent`` or ``delete_webhook`` so the node
    kind, operation gate, and webhook admission boundary cannot diverge.
    """
    # Runtime-agent deletion remains human-only and can fail before doing any
    # graph discovery. Webhook authorization needs the current ownership row,
    # so it is checked later under the same agent lock as the delete.
    if _operation == "remove":
        guard.check(actor, _operation, guid=guid)
        actor_guid = ""
    else:
        actor_guid = (
            "" if actor == "human" else _resolve_actor_guid(actor))
        # Fail before incident-edge discovery, then recheck at the durable
        # mutation point below because this optimistic decision may wait behind
        # another identity transaction.
        with _invariant_lock("agent"):
            if actor != "human":
                _require_actor_guid(actor, actor_guid)
            target = get_object(guid)
            if (
                    _expected_kind == WEBHOOK_KIND
                    and target.get("kind") != WEBHOOK_KIND):
                raise GraphError(f"node {guid!r} is not a webhook")
            guard.check(
                actor, _operation, guid=guid,
                ownership=_webhook_ownership_view(target))
    affected = ()
    # Endpoint identity locks must be outermost.  Discover optimistically, then
    # re-scan under the target+survivor set; if an edge committed in between,
    # release and retry with the complete sorted set rather than widening a
    # nested transaction (which could deadlock another multi-agent mutation).
    for _attempt in range(8):
        planned_edges = list(edges_touching(guid))
        planned_guids = tuple(dict.fromkeys(
            endpoint for edge in planned_edges
            for endpoint in (guid, edge.get("source"), edge.get("target"))
            if endpoint
        )) or (guid,)
        retry = False
        with _identity_transaction_locks(planned_guids):
            with _invariant_lock("agent"):
                if actor != "human" and _operation != "remove":
                    _require_actor_guid(actor, actor_guid)
                target = get_object(guid)
                if (
                        _expected_kind == WEBHOOK_KIND
                        and target.get("kind") != WEBHOOK_KIND):
                    raise GraphError(f"node {guid!r} is not a webhook")
                if (
                        _expected_kind != WEBHOOK_KIND
                        and target.get("kind") == WEBHOOK_KIND):
                    raise GraphError(
                        "webhook nodes must be removed through delete_webhook")
                if _operation != "remove":
                    guard.check(
                        actor, _operation, guid=guid,
                        ownership=_webhook_ownership_view(target))
                # Keep the identity name reserved until the old row is gone,
                # and block edge create/update during projection + deletion.
                with _invariant_lock("edge-authorization"):
                    edges = list(edges_touching(guid))
                    locked_guids = {
                        endpoint for edge in edges
                        for endpoint in (
                            guid, edge.get("source"), edge.get("target"))
                        if endpoint
                    } or {guid}
                    if locked_guids != set(planned_guids):
                        retry = True
                    else:
                        affected = tuple(
                            candidate for candidate in planned_guids
                            if candidate != guid)
                        delete_started = False
                        try:
                            if _identity_projector is not None:
                                _rewrite_agent_identities(
                                    affected, _identity_projector,
                                    notify=False)
                            delete_started = True
                            for edge in edges:
                                _delete_object_verified(
                                    "edge", edge["_guid"])
                            result = _delete_object_verified("agent", guid)
                        except Exception as error:
                            rollback_errors = []
                            if delete_started:
                                for edge in edges:
                                    try:
                                        _restore_object_snapshot("edge", edge)
                                    except Exception as rollback_error:
                                        rollback_errors.append(
                                            f"edge {edge.get('_guid')}: "
                                            f"{rollback_error}")
                            if _identity_rewriter is not None:
                                try:
                                    _rewrite_agent_identities(
                                        affected, _identity_rewriter,
                                        notify=False)
                                except Exception as rollback_error:
                                    rollback_errors.append(
                                        "surviving identities: "
                                        f"{rollback_error}")
                            failure = _transaction_error(
                                ("remove webhook"
                                 if _expected_kind == WEBHOOK_KIND
                                 else "remove agent"),
                                error, rollback_errors)
                            guard.audit(
                                actor, _operation, {"guid": guid}, "failed",
                                str(failure),
                                **_audit_actor_kwargs(actor_guid))
                            raise failure from error
        if retry:
            continue
        break
    else:
        raise GraphError(
            "agent connections kept changing during removal; retry once graph "
            "edits settle")
    guard.audit(
        actor, _operation, {"guid": guid}, "applied",
        **_audit_actor_kwargs(actor_guid))
    _notify_agent_identity_changes(affected, _identity_notifier)
    return result


def delete_agent(guid, actor="human", _identity_projector=None,
                 _identity_rewriter=None, _identity_notifier=None):
    """Delete a runtime agent; webhook nodes use ``delete_webhook`` instead."""
    return _delete_node(
        guid, actor=actor, _identity_projector=_identity_projector,
        _identity_rewriter=_identity_rewriter,
        _identity_notifier=_identity_notifier,
        _operation="remove", _expected_kind="agent")


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
    outside config.TRANSFORMS_DIR, any symlink at or below that trusted root,
    or anything other than an existing regular file. Returns the canonical
    absolute form on success,
    or "" unchanged for an empty path (detaching / no transform).

    Called from create_edge/update_edge AFTER guard.check — by the time this
    runs, a non-human actor has already been refused (crew.guard's
    PROTECTED_EDGE_FIELDS / connect-transform checks are human-only), so this
    function only ever executes for a human attaching or changing a
    transform. It still validates unconditionally (not actor-gated) since a
    bad path is a bad path regardless of who's allowed to set it."""
    if not path:
        return ""
    candidate = os.path.abspath(os.path.expanduser(str(path)))
    configured_dir = os.path.abspath(
        os.path.expanduser(config.TRANSFORMS_DIR))
    try:
        contained = os.path.commonpath((configured_dir, candidate)) == configured_dir
    except ValueError:
        contained = False
    if not contained or candidate == configured_dir:
        raise GraphError(
            f"transform path {path!r} must be inside {config.TRANSFORMS_DIR} — "
            "put the script in var/transforms/ first")

    # realpath containment still protects a configured root that is itself a
    # symlink, while the lstat walk rejects every attacker-controlled symlink
    # component beneath it (including an otherwise in-directory alias).
    real_root = os.path.realpath(configured_dir)
    real_candidate = os.path.realpath(candidate)
    try:
        real_contained = (
            os.path.commonpath((real_root, real_candidate)) == real_root)
    except ValueError:
        real_contained = False
    if not real_contained or real_candidate == real_root:
        raise GraphError(
            f"transform path {path!r} must be inside {config.TRANSFORMS_DIR} — "
            "put the script in var/transforms/ first")

    current = configured_dir
    try:
        for component in os.path.relpath(candidate, configured_dir).split(os.sep):
            current = os.path.join(current, component)
            info = os.lstat(current)
            if stat.S_ISLNK(info.st_mode):
                raise GraphError(
                    f"transform path {path!r} cannot contain symlinks")
    except FileNotFoundError:
        raise GraphError(
            f"transform script not found: {path!r} — put the script in "
            "var/transforms/ first") from None
    except NotADirectoryError:
        raise GraphError(
            f"transform script not found: {path!r} — put the script in "
            "var/transforms/ first") from None
    if not stat.S_ISREG(info.st_mode):
        raise GraphError(
            f"transform path {path!r} must name a regular file")
    return real_candidate


def _edge_authorizations(source_guid, target_guid, directed):
    """Ordered sender/receiver pairs authorized by one candidate edge."""
    pairs = {(source_guid, target_guid)}
    if not directed:
        pairs.add((target_guid, source_guid))
    return pairs


def _validate_edge_node_kinds(source_node, target_node, directed):
    """Enforce source-only semantics for webhook graph nodes."""
    source_kind = (source_node or {}).get("kind")
    target_kind = (target_node or {}).get("kind")
    if source_kind == WEBHOOK_KIND:
        if target_kind == WEBHOOK_KIND:
            raise GraphError("a webhook cannot route to another webhook")
        if not directed:
            raise GraphError(
                "webhook routes are one-way; a webhook cannot receive replies")
        return
    if target_kind == WEBHOOK_KIND:
        raise GraphError(
            "a webhook is source-only; connect webhook → agent instead")


def _validate_edge_contract(source_guid, target_guid, directed,
                            reply_expected=False, back_reply=False,
                            exclude_guid=None, source_node=None,
                            target_node=None):
    """Keep an edge's instructions and authorization unambiguous.

    A reply instruction is only coherent on a two-way edge.  Separately, an
    ordered sender→receiver pair may be authorized by at most one edge; mail
    caps, transforms, notes and identity rendering must never depend on which
    duplicate MorphDB happens to return first.
    """
    if directed and (reply_expected or back_reply):
        raise GraphError(
            "reply instructions require a two-way edge; use directed=false "
            "(`crew connect --undirected`) before requiring a reply")
    if source_node is not None or target_node is not None:
        _validate_edge_node_kinds(source_node, target_node, directed)
    candidate = _edge_authorizations(source_guid, target_guid, directed)
    for edge in list_edges():
        if exclude_guid and edge.get("_guid") == exclude_guid:
            continue
        existing = _edge_authorizations(
            edge.get("source"), edge.get("target"),
            bool(edge.get("directed", True)))
        overlap = candidate & existing
        if overlap:
            sender, target = next(iter(overlap))
            raise GraphError(
                "an edge already authorizes this sender→target pair "
                f"({sender} → {target}); duplicate/overlapping edges are not allowed")


# Generic edge updates may change the relationship contract, but never its
# durable provenance. Blessing has its own gated operation, and creation
# metadata remains immutable even to a human library caller.
_EDGE_MUTABLE_FIELDS = {
    "source", "target", "label", "description", "condition", "conditions",
    "target_action", "reply_expected", "back_conditions", "back_action",
    "back_reply", "max_turns", "token_cap", "cost_cap", "directed",
    "transform", "notes",
}


def create_edge(source_guid, target_guid, label="", description="",
                conditions=None, target_action="", reply_expected=False,
                back_conditions=None, back_action="", back_reply=False,
                max_turns=0, token_cap=0, cost_cap=0, directed=True, condition="",
                transform="", actor="human", _pre_approved=False,
                _identity_rewriter=None, _identity_notifier=None,
                _actor_guid=None):
    """Connect two agents. `directed=True` → only source→target may message;
    `directed=False` (two-way) → either may message the other, and the BACK fields
    describe the target→source direction independently.

    Each direction captures: a LIST of trigger `conditions` (an agent can have several
    reasons to message a peer), the receiver's `action` on receipt, and a reply flag.
    `max_turns` is an hourly RATE LIMIT (0 = unlimited) so a tight loop can't run away;
    `token_cap`/`cost_cap` budget the TARGET runtime's hourly usage (0 = uncapped;
    an unavailable configured metric fails closed at delivery time, see crew.mail).

    `actor` is who's connecting them — gated by guard.check (op "connect") BEFORE
    the self-edge check. `created_by`/`blessed` are stamped from `actor`, same rule
    as create_agent. Every other kwarg ALSO rides along in the check's ctx — an
    agent actor's FINITE-CAPS RULE needs the caps to decide, and a WAVE 4 pending
    row (crew.guard._check_envelope) needs the full set to replay this exact call
    later via approve_pending.

    `transform` (WAVE 5) is a path to a script (must live under
    config.TRANSFORMS_DIR — see validate_transform_path) that runs at delivery
    accept-time (see crew.mail.deliver). Durable retries reuse its result;
    scripts must make external side effects idempotent across a crash before
    the result row is stored.
    Attaching one is human-only — guard.check refuses a non-human actor's
    connect outright when `transform` is set (before it ever reaches the
    envelope/pending logic), so an agent/foreman can never queue one for
    approval either.

    `_pre_approved=True` (WAVE 4, guard.approve_pending's escape hatch ONLY) skips
    the guard.check call entirely — the caller already ran check("human", ...)
    itself and is replaying a stored request, stamping created_by/blessed from
    the ORIGINAL requester (`actor`) rather than "human"."""
    check_ctx = {
        "source": source_guid, "target": target_guid,
        "label": label, "description": description,
        "conditions": conditions, "target_action": target_action,
        "reply_expected": reply_expected,
        "back_conditions": back_conditions, "back_action": back_action,
        "back_reply": back_reply, "max_turns": max_turns,
        "token_cap": token_cap, "cost_cap": cost_cap,
        "directed": directed, "condition": condition,
        "transform": transform,
    }
    if not _pre_approved:
        guard.check(actor, "connect", **check_ctx)
    if source_guid == target_guid:
        raise GraphError("an agent cannot have an edge to itself")
    transform = validate_transform_path(transform)
    fwd = clean_conditions(conditions if conditions is not None else condition)
    bwd = clean_conditions(back_conditions)
    caps = normalize_edge_numeric_fields({
        "max_turns": max_turns, "token_cap": token_cap, "cost_cap": cost_cap,
    })
    creator_guid = (
        _resolve_actor_guid(actor) if _actor_guid is None else _actor_guid)
    if actor == "human":
        creator_guid = ""
    body = {
        "source": source_guid, "target": target_guid,
        "label": label or "", "description": description or "",
        "conditions": fwd, "condition": "; ".join(fwd),
        "target_action": target_action or "", "reply_expected": bool(reply_expected),
        "back_conditions": bwd, "back_action": back_action or "", "back_reply": bool(back_reply),
        "max_turns": caps["max_turns"], "token_cap": caps["token_cap"],
        "cost_cap": caps["cost_cap"], "directed": bool(directed),
        "transform": transform,
        "created_at": int(time.time()),
        "created_by": actor, "created_by_guid": creator_guid,
        "blessed": (actor == "human"),
    }
    # Any edge can overlap a two-way edge in either orientation, so use one
    # app-scoped authorization lock rather than independent directed-pair locks.
    # Hold it only across the invariant scan and POST; all pure normalization is
    # complete before entering the critical section.
    with _edge_identity_transaction((
            source_guid, target_guid, creator_guid)):
        # Actor names and endpoint names are reusable.  The optimistic guard
        # decision above may wait behind a removal that owns the same immutable
        # identity lock, so attach it to the commit only after proving the same
        # actor and both endpoint rows are still live under those locks.
        if actor != "human":
            locked_actor = _require_actor_guid(actor, creator_guid)
            if _pre_approved:
                if not locked_actor.get("can_edit_graph"):
                    raise GraphError(
                        "pending connect requester is no longer a foreman; "
                        "submit a new request")
            else:
                guard.check(actor, "connect", **check_ctx)
        source_node = get_object(source_guid)
        target_node = get_object(target_guid)
        _validate_edge_contract(
            source_guid, target_guid, bool(directed),
            reply_expected=bool(reply_expected), back_reply=bool(back_reply),
            source_node=source_node, target_node=target_node)
        edge = _create_edge_verified(body)
        if _identity_rewriter is not None:
            try:
                _rewrite_agent_identities(
                    (source_guid, target_guid), _identity_rewriter, notify=False)
            except Exception as error:
                rollback_errors = []
                try:
                    _delete_object_verified("edge", edge["_guid"])
                except Exception as rollback_error:
                    rollback_errors.append(f"edge row: {rollback_error}")
                # Render from the graph state that compensation actually left,
                # whether the delete succeeded or remained ambiguous.
                try:
                    _rewrite_agent_identities(
                        (source_guid, target_guid), _identity_rewriter,
                        notify=False)
                except Exception as rollback_error:
                    rollback_errors.append(f"identities: {rollback_error}")
                failure = _transaction_error("connect", error, rollback_errors)
                guard.audit(
                    actor, "connect",
                    {"source": source_guid, "target": target_guid},
                    "failed", str(failure),
                    **_audit_actor_kwargs(creator_guid))
                raise failure from error
    guard.audit(actor, "connect", {"source": source_guid, "target": target_guid},
               "applied", **_audit_actor_kwargs(creator_guid))
    _notify_agent_identity_changes(
        (source_guid, target_guid), _identity_notifier)
    return edge


def update_edge(guid, fields, actor="human", _pre_approved=False,
                _identity_rewriter=None, _identity_notifier=None,
                _actor_guid=None):
    """Patch an edge, normalizing the condition lists and keeping the legacy flattened
    `condition` string in sync. `fields` may carry conditions/back_conditions as lists
    (or strings) plus any scalar edge fields.

    Gated by guard.check (op "update_edge") against the CURRENT edge (for the
    endpoint + cap-lowering rule a non-foreman agent is held to) and the raw
    `fields` being applied. `_pre_approved=True` (WAVE 4, guard.approve_pending's
    escape hatch ONLY) skips that guard.check — the caller already ran
    check("human", ...) itself and is replaying a stored cap-raise request."""
    if not isinstance(fields, dict):
        raise GraphError("edge update fields must be a mapping")
    body = None
    actor_guid = "" if actor == "human" else _actor_guid
    affected = ()
    for attempt in range(8):
        cur = get_object(guid)
        if not _pre_approved:
            guard.check(actor, "update_edge", edge=cur, changes=fields)
        immutable = set(fields) - _EDGE_MUTABLE_FIELDS
        if immutable:
            raise GraphError(
                "edge provenance/unknown fields are immutable and cannot be "
                "updated here: " + ", ".join(sorted(immutable)))
        if actor != "human" and not actor_guid:
            actor_guid = _resolve_actor_guid(actor)
        # Authorization deliberately precedes persistence normalization.  The
        # guard validates cap values with this same canonical parser, so an
        # invalid agent-authored patch is refused and audited instead of
        # escaping through a pre-gate GraphError.
        if body is None:
            body = normalize_edge_numeric_fields(fields)
            if "transform" in body:
                # WAVE 5: reaching here at all means a human is setting it
                # (guard's PROTECTED_EDGE_FIELDS refuses any other actor
                # before this point) — still validate unconditionally, same
                # as create_edge.
                body["transform"] = validate_transform_path(body["transform"])
            if "conditions" in body:
                body["conditions"] = clean_conditions(body["conditions"])
                body["condition"] = "; ".join(body["conditions"])
            if "back_conditions" in body:
                body["back_conditions"] = clean_conditions(
                    body["back_conditions"])
        planned = tuple(filter(None, (
            cur.get("source"), cur.get("target"),
            body.get("source"), body.get("target"), actor_guid)))
        retry = False
        with _edge_identity_transaction(planned):
            # Refetch only after acquiring the same lock as create_edge. If an
            # intervening endpoint update introduced an identity outside the
            # optimistic plan, this transaction does not own that identity.
            # Release every lock and retry from the fresh row; nested widening
            # would violate the global sorted-GUID lock order.
            locked_cur = get_object(guid)
            live_endpoints = {
                locked_cur.get("source"), locked_cur.get("target")}
            if not live_endpoints.issubset(set(planned)):
                retry = True
            else:
                if actor != "human":
                    _require_actor_guid(actor, actor_guid)
                    if _pre_approved:
                        if actor_guid not in live_endpoints:
                            raise GraphError(
                                "pending edge update requester is no longer "
                                "an endpoint; submit a new request")
                    else:
                        # Recheck unconditionally: actor authority can change
                        # while the edge itself remains byte-for-byte stable.
                        guard.check(
                            actor, "update_edge", edge=locked_cur,
                            changes=fields)
                elif not _pre_approved and locked_cur != cur:
                    guard.check(
                        actor, "update_edge", edge=locked_cur, changes=fields)
                candidate = dict(locked_cur)
                candidate.update(body)
                source_node = get_object(candidate.get("source"))
                target_node = get_object(candidate.get("target"))
                _validate_edge_contract(
                    candidate.get("source"), candidate.get("target"),
                    bool(candidate.get("directed", True)),
                    reply_expected=bool(candidate.get("reply_expected", False)),
                    back_reply=bool(candidate.get("back_reply", False)),
                    exclude_guid=guid, source_node=source_node,
                    target_node=target_node)
                result = _patch_object_verified("edge", guid, body)
                affected = (
                    locked_cur.get("source"), locked_cur.get("target"),
                    result.get("source"), result.get("target"))
                if _identity_rewriter is not None:
                    try:
                        _rewrite_agent_identities(
                            affected, _identity_rewriter, notify=False)
                    except Exception as error:
                        rollback_errors = []
                        try:
                            _restore_object_snapshot("edge", locked_cur)
                        except Exception as rollback_error:
                            rollback_errors.append(
                                f"edge row: {rollback_error}")
                        try:
                            _rewrite_agent_identities(
                                affected, _identity_rewriter, notify=False)
                        except Exception as rollback_error:
                            rollback_errors.append(
                                f"identities: {rollback_error}")
                        failure = _transaction_error(
                            "update edge", error, rollback_errors)
                        guard.audit(
                            actor, "update_edge",
                            {"guid": guid, "fields": fields},
                            "failed", str(failure),
                            **_audit_actor_kwargs(actor_guid))
                        raise failure from error
        if not retry:
            break
    else:
        raise GraphError(
            "edge endpoints kept changing while update locks were acquired; "
            "retry the update")
    guard.audit(
        actor, "update_edge", {"guid": guid, "fields": fields}, "applied",
        **_audit_actor_kwargs(actor_guid))
    _notify_agent_identity_changes(affected, _identity_notifier)
    return result


def bless_edge(guid, actor="human"):
    """Mark an edge row blessed. Gated by guard.check (op "bless") — same
    human-only tier as bless_agent."""
    guard.check(actor, "bless", guid=guid, on="edge")
    with _invariant_lock("edge-authorization"):
        # MorphDB PATCH upserts a missing GUID. Verify existence under the same
        # lock used by edge create/update/delete so blessing an unknown or
        # concurrently deleted GUID cannot materialize a relation-less edge.
        get_object(guid)
        result = patch_object("edge", guid, {"blessed": True})
    guard.audit(actor, "bless", {"guid": guid, "on": "edge"}, "applied")
    return result


def set_edge_note(guid, text, actor="human"):
    """Set an edge's freeform note.

    Humans may target any edge; an agent actor must be one of its endpoints.
    The shared edge-authorization lock keeps that endpoint check attached to
    the exact edge state patched below.
    """
    actor_guid = (
        "" if actor == "human" else _resolve_actor_guid(actor))
    with _invariant_lock("edge-authorization"):
        if actor != "human":
            _require_actor_guid(actor, actor_guid)
        target = get_object(guid)
        guard.check(actor, "note", guid=guid, on="edge", target=target)
        result = patch_object("edge", guid, {"notes": text or ""})
    guard.audit(
        actor, "note", {"guid": guid, "on": "edge"}, "applied",
        **_audit_actor_kwargs(actor_guid))
    return result


def list_edges(include=None):
    return _list_all_exact("edge", include=include)


def edges_from_to(source_guid, target_guid):
    """Every edge with this exact source AND target (index-backed relation filter)."""
    return _list_all_exact("edge", source=source_guid, target=target_guid)


def edges_touching(agent_guid):
    """All edges with this agent on either end (for cascade-delete / neighbor scans)."""
    out = {}
    for key in ("source", "target"):
        for e in _list_all_exact("edge", **{key: agent_guid}):
            out[e["_guid"]] = e
    return list(out.values())


def delete_edge(guid, actor="human", _identity_rewriter=None,
                _identity_notifier=None):
    """Drop an edge. Gated by guard.check (op "disconnect") — a topology op,
    so a non-foreman agent is refused outright (no endpoint exception here,
    unlike update_edge). The CURRENT edge is fetched first (same pattern as
    update_edge) so a foreman's ENVELOPE rule can see its endpoints +
    created_by before the delete happens."""
    actor_guid = "" if actor == "human" else None
    affected = ()
    for attempt in range(8):
        edge = _get_object_if_present(guid)
        guard.check(actor, "disconnect", guid=guid, edge=edge)
        if actor != "human" and not actor_guid:
            actor_guid = _resolve_actor_guid(actor)
        planned = tuple(filter(None, (
            (edge or {}).get("source"), (edge or {}).get("target"),
            actor_guid)))
        retry = False
        with _edge_identity_transaction(planned):
            # Serialize deletion with create/update.  MorphDB PATCH upserts a
            # missing GUID, so an unlocked delete between update_edge's refetch
            # and PATCH could otherwise resurrect a relation-less phantom edge.
            locked_edge = _get_object_if_present(guid)
            live_endpoints = {
                (locked_edge or {}).get("source"),
                (locked_edge or {}).get("target")}
            if not live_endpoints.issubset(set(planned) | {None}):
                retry = True
            else:
                if actor != "human":
                    _require_actor_guid(actor, actor_guid)
                    # A foreman can disconnect two children without being an
                    # endpoint. Its flag/ownership may change while this
                    # transaction waits even when the edge row does not.
                    guard.check(
                        actor, "disconnect", guid=guid, edge=locked_edge)
                elif locked_edge != edge:
                    guard.check(
                        actor, "disconnect", guid=guid, edge=locked_edge)
                result = _delete_object_verified("edge", guid)
                if locked_edge:
                    affected = (
                        locked_edge.get("source"), locked_edge.get("target"))
                if _identity_rewriter is not None and locked_edge:
                    try:
                        _rewrite_agent_identities(
                            affected, _identity_rewriter, notify=False)
                    except Exception as error:
                        rollback_errors = []
                        try:
                            _restore_object_snapshot("edge", locked_edge)
                        except Exception as rollback_error:
                            rollback_errors.append(
                                f"edge row: {rollback_error}")
                        try:
                            _rewrite_agent_identities(
                                affected, _identity_rewriter, notify=False)
                        except Exception as rollback_error:
                            rollback_errors.append(
                                f"identities: {rollback_error}")
                        failure = _transaction_error(
                            "disconnect", error, rollback_errors)
                        guard.audit(
                            actor, "disconnect", {"guid": guid}, "failed",
                            str(failure), **_audit_actor_kwargs(actor_guid))
                        raise failure from error
        if not retry:
            break
    else:
        raise GraphError(
            "edge endpoints kept changing while delete locks were acquired; "
            "retry the delete")
    guard.audit(
        actor, "disconnect", {"guid": guid}, "applied",
        **_audit_actor_kwargs(actor_guid))
    _notify_agent_identity_changes(affected, _identity_notifier)
    return result


def disconnect_between(source_guid, target_guid, actor="human",
                       _identity_rewriter=None, _identity_notifier=None):
    """Delete every directed orientation between two agents as one batch.

    Collection, permission checks, and deletes share the same app-wide edge
    lock as create/update/delete. Every matching row is authorized before the
    first DELETE, so a mixed-ownership pair cannot be left half-connected just
    because the permitted orientation happened to be listed first. Returns the
    deleted edge snapshots for CLI counts; an empty list is an ordinary no-op.
    """
    actor_guid = (
        "" if actor == "human" else _resolve_actor_guid(actor))
    with _edge_identity_transaction((
            source_guid, target_guid, actor_guid)):
        if actor != "human":
            _require_actor_guid(actor, actor_guid)
        candidates = (
            edges_from_to(source_guid, target_guid)
            + edges_from_to(target_guid, source_guid))
        edges = list({e.get("_guid"): e for e in candidates}.values())

        # Preflight the complete batch before the first irreversible write.
        for edge in edges:
            guard.check(
                actor, "disconnect", guid=edge.get("_guid"), edge=edge)
        affected = tuple(
            guid for edge in edges
            for guid in (edge.get("source"), edge.get("target")))
        attempted = []
        try:
            for edge in edges:
                # Include the in-flight row before DELETE: if transport fails
                # ambiguously, restoring its snapshot is safe and idempotent.
                attempted.append(edge)
                _delete_object_verified("edge", edge["_guid"])
            if edges and _identity_rewriter is not None:
                _rewrite_agent_identities(
                    affected, _identity_rewriter, notify=False)
        except Exception as error:
            rollback_errors = []
            for edge in attempted:
                try:
                    _restore_object_snapshot("edge", edge)
                except Exception as rollback_error:
                    rollback_errors.append(
                        f"edge {edge.get('_guid')}: {rollback_error}")
            if _identity_rewriter is not None:
                try:
                    _rewrite_agent_identities(
                        affected, _identity_rewriter, notify=False)
                except Exception as rollback_error:
                    rollback_errors.append(f"identities: {rollback_error}")
            failure = _transaction_error(
                "disconnect", error, rollback_errors)
            for edge in edges:
                guard.audit(
                    actor, "disconnect", {"guid": edge["_guid"]},
                    "failed", str(failure),
                    **_audit_actor_kwargs(actor_guid))
            raise failure from error

    for edge in edges:
        guard.audit(
            actor, "disconnect", {"guid": edge["_guid"]}, "applied",
            **_audit_actor_kwargs(actor_guid))
    if edges:
        _notify_agent_identity_changes(affected, _identity_notifier)
    return edges


# --------------------------------------------------------------------------- #
# The delivery gate — "you can only message agents you're connected to"
# --------------------------------------------------------------------------- #
def authorizing_edges(sender_name, target_name):
    """All edges that authorize this ordered sender→target pair.

    New writes enforce a single-edge invariant. Returning the full list here
    lets old/raced data be detected explicitly instead of making policy depend
    on MorphDB's row order.
    """
    sender = get_node_by_name(sender_name)
    target = get_node_by_name(target_name)
    if not sender or not target:
        return []
    sg, tg = sender["_guid"], target["_guid"]
    out = list(edges_from_to(sg, tg))
    out.extend(e for e in edges_from_to(tg, sg)
               if not e.get("directed", True))
    by_guid = {e.get("_guid"): e for e in out}
    return list(by_guid.values())


def authorizing_edge(sender_name, target_name):
    """The unique authorizing edge, None, or a fail-closed ambiguity error."""
    edges = authorizing_edges(sender_name, target_name)
    if len(edges) > 1:
        ids = ", ".join(str(e.get("_guid") or "?") for e in edges)
        raise GraphError(
            f"ambiguous duplicate authorization for {sender_name} → "
            f"{target_name}; repair/delete one of these edges: {ids}")
    return edges[0] if edges else None


def can_message(sender_name, target_name):
    """Is sender→target authorized? True iff a directed edge source=sender,
    target=target exists, OR an UNDIRECTED edge connects them in either
    orientation. This is the hard wall enforced at delivery time (crew.mail)."""
    return authorizing_edge(sender_name, target_name) is not None


def _neighbors(agent_guid, near, far):
    """Edges where the agent sits on the `near` end (any direction) PLUS undirected
    edges where it sits on the `far` end — i.e. every link that authorizes a message
    in one chosen direction. `near`/`far` are the relation field names
    ('source'/'target'); each result is (neighbor_guid, edge), deduped by neighbor."""
    out = []
    seen = set()
    for e in _list_all_exact("edge", **{near: agent_guid}):
        g = e.get(far)
        if g and g not in seen:
            seen.add(g)
            out.append((g, e))
    for e in _list_all_exact("edge", **{far: agent_guid}):
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
REFUSAL_STATUSES = (
    "blocked", "ratelimited", "budget", "budget_unavailable", "filtered")


def create_message(sender, target, body, status="queued", *,
                   sender_guid=None, target_guid=None, edge_guid=None,
                   no_prefix=False, status_detail="", request_id=None):
    # Resolve endpoint snapshots for internal/system call sites that predate the
    # explicit arguments.  The accepted peer-mail path passes all three GUIDs
    # directly, including the exact authorizing edge.
    if sender_guid is None and sender and sender != "crew":
        sender_agent = get_node_by_name(sender)
        sender_guid = ((sender_agent or {}).get("_guid") or "")
    if target_guid is None and target:
        target_agent = get_agent_by_name(target)
        target_guid = ((target_agent or {}).get("_guid") or "")
    supplied_request_id = request_id is not None
    if supplied_request_id:
        request_id = str(request_id).strip()
        if not request_id:
            raise GraphError("message request_id must be a nonempty string")
    else:
        request_id = uuid.uuid4().hex
    # A single app-wide sequence lock gives every durable row a unique numeric
    # order across threads and CLI/dashboard processes. Microseconds fit exactly
    # in MorphDB/JSON's IEEE-754 integer range until well beyond Crew's lifetime.
    with _invariant_lock("message-order"):
        request_body = {
            "sender": sender, "target": target, "body": body,
            "sender_guid": sender_guid or "", "target_guid": target_guid or "",
            "edge_guid": edge_guid or "", "request_id": request_id,
            "no_prefix": bool(no_prefix),
        }
        stable_body = {
            **request_body, "status": status,
            "status_detail": status_detail or "", "delivered_at": 0,
        }
        progressed_statuses = {
            "queued", "submitting", "delivered", "runtime_queued",
            "delivery_uncertain", "failed",
        }

        def request_matches(row):
            if not _object_has_fields(row, request_body):
                return False
            # Queue status/detail/delivery time are mutable state, not request
            # payload. A retry after a worker already advanced the row must
            # still return that original logical send. Refusal/audit rows do
            # not enter the queue state machine and must retain their status.
            current_status = row.get("status")
            return (current_status in progressed_statuses
                    if status == "queued" else current_status == status)
        # A caller retrying the same logical send supplies the same request id.
        # The app-wide message lock makes this read + potential POST a true
        # idempotency boundary even when two retries arrive concurrently.
        if supplied_request_id:
            existing = list_objects(
                "message", request_id=request_id, limit=2)
            rows = (existing or {}).get("objects", [])
            matches = [row for row in rows if request_matches(row)]
            if len(rows) == 1 and len(matches) == 1:
                return matches[0]
            if rows:
                raise GraphError(
                    "message request_id already exists with different content")
        latest = list_objects(
            "message", sort="created_order", order="desc", limit=1)
        rows = (latest or {}).get("objects", [])
        try:
            previous = int((rows[0] if rows else {}).get("created_order") or 0)
        except (TypeError, ValueError, OverflowError):
            previous = 0
        created_order = max(time.time_ns() // 1000, previous + 1)
        message_body = dict(
            stable_body, created_at=int(time.time()),
            created_order=created_order)
        try:
            return create_object("message", message_body)
        except Exception as primary_error:
            # A transport error can arrive after MorphDB committed the POST.
            # The per-attempt request id makes that outcome unambiguous without
            # treating an older equal-content message as this send.
            try:
                found = list_objects(
                    "message", request_id=request_id, limit=2)
            except Exception as verification_error:
                raise primary_error from verification_error
            matches = [row for row in (found or {}).get("objects", [])
                       if request_matches(row)]
            if len(matches) == 1:
                return matches[0]
            raise


def mark_message(guid, status, delivered=False, detail=None):
    body = {"status": status}
    if detail is not None:
        body["status_detail"] = detail
    if delivered:
        body["delivered_at"] = int(time.time())
    return _patch_object_verified("message", guid, body)


def list_messages(status=None, target=None, limit=200, offset=None):
    res = list_objects("message", status=status, target=target,
                       sort="created_order", order="asc", limit=limit,
                       offset=offset)
    return (res or {}).get("objects", [])


def recent_message_count(sender, target, since_ts, *, edge_guid=None,
                         sender_guid=None, target_guid=None, ceiling=None):
    """How many messages sender→target were created at/after since_ts. Used to
    enforce an edge's max_turns so two agents can't loop forever.

    Ordered NEWEST-FIRST on purpose: a runaway loop is exactly what overflows the
    fetch limit, and an unordered truncation would drop the most-recent rows — the
    in-window ones — and silently blind the limiter precisely when it's needed. With
    desc order the retained rows ARE the recent ones, so the window count stays
    correct however large the log grows."""
    identity_filters = {}
    if edge_guid and sender_guid and target_guid:
        identity_filters = {
            "edge_guid": edge_guid, "sender_guid": sender_guid,
            "target_guid": target_guid,
        }
    else:
        # Compatibility for diagnostics/tests over legacy unbound rows. The
        # enforcement path always supplies immutable edge + endpoint GUIDs.
        identity_filters = {"sender": sender, "target": target}
    if ceiling is not None:
        try:
            ceiling = int(ceiling)
        except (TypeError, ValueError, OverflowError) as error:
            raise GraphError("message count ceiling must be an integer") from error
        if ceiling <= 0:
            return 0

    # MorphDB list responses are bounded. Page until exhaustion (or until the
    # enforcement caller's exact cap is reached) instead of silently treating
    # the first 2,000 rows as the whole history.
    page_size = 1000
    offset = 0
    count = 0
    while True:
        res = list_objects(
            "message", sort="created_order", order="desc", limit=page_size,
            offset=offset, **identity_filters)
        msgs = (res or {}).get("objects", [])
        for message in msgs:
            # Refusal audit rows are attempts that never delivered — counting
            # them would let a retrying sender pin its own window full forever.
            if ((message.get("created_at") or 0) >= since_ts
                    and message.get("status") not in REFUSAL_STATUSES):
                count += 1
                if ceiling is not None and count >= ceiling:
                    return ceiling
        if len(msgs) < page_size:
            return count
        offset += len(msgs)


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
    if _CASE_INSENSITIVE_FS:
        # Default macOS filesystems treat canonically equivalent Unicode names
        # (for example NFC ``é`` and NFD ``é``) as the same directory even
        # though realpath preserves the caller's spelling.  casefold is also a
        # stronger representation of the filesystem's case-insensitive identity
        # than lower().  Do not normalize on case-sensitive platforms: common
        # Linux filesystems legitimately allow those byte-distinct names to be
        # separate directories.
        return unicodedata.normalize("NFC", p).casefold()
    return p


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


def home_conflict_across_apps(home):
    """Find an overlapping agent home across every Crew app we can address.

    MorphDB deliberately has no list-apps endpoint, so Crew's durable project
    registry is the authority for project-derived tenants.  Include the current
    app explicitly as well so a pinned CREW_APP test/custom tenant participates.
    Stale registered projects whose apps were deleted are harmless; any other
    read failure is fatal because silently skipping a live app would defeat the
    global one-agent-per-directory invariant.
    """
    apps = [config.current_app()]
    try:
        projects = config.list_known_projects()
    except (OSError, ValueError) as error:
        raise GraphError(str(error)) from error
    for project in projects:
        app = config.project_app(project)
        if app not in apps:
            apps.append(app)

    for app in apps:
        try:
            agents = list_agents(app=app)
        except GraphError as e:
            message = str(e)
            if "Unknown app" in message or message.startswith("404"):
                continue
            raise GraphError(
                f"cannot verify agent-home ownership in Crew app {app!r}: "
                f"{message}") from e
        conflict = home_conflict(home, agents=agents)
        if conflict:
            result = dict(conflict)
            result["_app"] = app
            return result
    return None
