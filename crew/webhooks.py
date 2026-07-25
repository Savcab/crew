"""Public webhook-node ingress.

The URL capability authorizes only one operation: convert this request into a
message and durably enqueue it across the hook's outgoing graph edges. Operator
graph controls and terminal APIs remain behind the dashboard cookie boundary.
"""
import hashlib
import json
import math
import re
import time
import urllib.parse
import uuid

from . import config, graphstore as gs, mail


MAX_MESSAGE_CHARS = 256 * 1024
RECEIPT_VERSION = 1
_ATTEMPT_LOCK_STRIPES = 64
_PLACEHOLDER_RE = re.compile(r"{{\s*([^{}]+?)\s*}}")
_IDEMPOTENCY_HEADERS = (
    "idempotency-key",
    "x-github-delivery",
    "x-webhook-id",
    "webhook-id",
)
_PRIVATE_CREDENTIAL_HEADERS = frozenset((
    "authorization",
    "cookie",
    "cookie2",
    "proxy-authorization",
    "proxy-authenticate",
    "set-cookie",
    "set-cookie2",
))
_PUBLIC_IDENTIFIER_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_-]{0,255}\Z")
_PUBLIC_ACCEPTED_STATUSES = frozenset((
    "queued",
    "submitting",
    "delivered",
    "runtime_queued",
    "delivery_uncertain",
))
_PUBLIC_REJECTION_CODE = "route_delivery_failed"
_PUBLIC_REJECTION_MESSAGE = "Webhook route delivery failed."


class WebhookError(gs.GraphError):
    """Expected public-request failure with an HTTP response code."""

    def __init__(self, message, status=422):
        super().__init__(message)
        self.status = int(status)


def public_path(hook):
    token = str((hook or {}).get("webhook_token") or "")
    return "/hooks/" + urllib.parse.quote(token, safe="")


def public_url(hook):
    path = public_path(hook)
    return (
        config.WEBHOOK_PUBLIC_BASE_URL + path
        if config.WEBHOOK_PUBLIC_BASE_URL else path
    )


def for_operator(hook):
    """Return a graph/API-safe hook row without a standalone token field."""
    result = dict(hook or {})
    result.pop("webhook_token", None)
    result["public_url"] = public_url(hook)
    return result


def capability_exists(token):
    """Admit a capability before an HTTP handler reads its request body.

    This is only an early resource-use guard. ``receive`` deliberately resolves
    the capability again before parsing and at the serialized dispatch point,
    so deletion or rotation after admission still fails closed.
    """
    return gs.get_webhook_by_token(token) is not None


def _reject_json_constant(_value):
    raise ValueError("non-standard JSON constants are not allowed")


def _parse_json_float(value):
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("non-finite JSON numbers are not allowed")
    return parsed


def _decode_utf8(raw):
    try:
        return bytes(raw or b"").decode("utf-8")
    except UnicodeDecodeError as error:
        raise WebhookError("webhook body must be valid UTF-8") from error


def _media_type(content_type):
    """Canonicalize the request media type exactly as payload parsing does."""
    return str(content_type or "").split(";", 1)[0].strip().lower()


def parse_payload(raw, content_type=""):
    """Parse JSON, form, or text input into the template's payload value."""
    media_type = _media_type(content_type)
    text = _decode_utf8(raw)
    if media_type == "application/json" or media_type.endswith("+json"):
        try:
            return json.loads(
                text,
                parse_constant=_reject_json_constant,
                parse_float=_parse_json_float,
            ), text
        except (TypeError, ValueError) as error:
            raise WebhookError(f"webhook body is not valid JSON: {error}") from error
    if media_type == "application/x-www-form-urlencoded":
        try:
            parsed = urllib.parse.parse_qs(
                text, keep_blank_values=True, strict_parsing=False,
                max_num_fields=1000)
        except ValueError as error:
            raise WebhookError(f"webhook form body is invalid: {error}") from error
        return {
            key: values[0] if len(values) == 1 else values
            for key, values in parsed.items()
        }, text
    return text, text


def _template_paths(template):
    template = str(template or "")
    remainder = _PLACEHOLDER_RE.sub("", template)
    if "{{" in remainder or "}}" in remainder:
        raise WebhookError(
            "webhook template has unmatched '{{' or '}}'")
    paths = []
    for match in _PLACEHOLDER_RE.finditer(template):
        path = match.group(1).strip()
        segments = path.split(".") if path else []
        if (not segments or segments[0] not in ("payload", "headers", "raw")
                or any(not segment for segment in segments)):
            raise WebhookError(
                f"unsupported webhook template placeholder: {path!r}")
        if segments[0] == "raw" and len(segments) != 1:
            raise WebhookError(
                "the raw webhook template placeholder cannot have child fields")
        paths.append((match, segments))
    return paths


def validate_template(template):
    template = str(template or "")
    if len(template) > gs.WEBHOOK_TEMPLATE_MAX:
        raise WebhookError(
            f"webhook template exceeds {gs.WEBHOOK_TEMPLATE_MAX} characters")
    _template_paths(template)
    return template


def _lookup_path(context, segments):
    value = context[segments[0]]
    walked = [segments[0]]
    for segment in segments[1:]:
        walked.append(segment)
        if isinstance(value, dict) and segment in value:
            value = value[segment]
            continue
        if isinstance(value, list) and segment.isdigit():
            index = int(segment)
            if index < len(value):
                value = value[index]
                continue
        raise WebhookError(
            "webhook template field was not found: " + ".".join(walked))
    return value


def _message_text(value):
    if isinstance(value, str):
        return value
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False,
        separators=(",", ":"))


def render_message(template, payload, raw_text, headers=None):
    """Render one deterministic message from parsed request context."""
    template = validate_template(template)
    normalized_headers = {
        str(key).lower(): str(value)
        for key, value in dict(headers or {}).items()
        if str(key).lower() not in _PRIVATE_CREDENTIAL_HEADERS
    }
    if not template:
        if isinstance(payload, dict):
            for key in ("message", "text"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    result = value
                    break
            else:
                result = _message_text(payload)
        else:
            result = _message_text(payload)
    else:
        context = {
            "payload": payload,
            "headers": normalized_headers,
            "raw": raw_text,
        }
        pieces = []
        cursor = 0
        for match, segments in _template_paths(template):
            pieces.append(template[cursor:match.start()])
            pieces.append(_message_text(_lookup_path(context, segments)))
            cursor = match.end()
        pieces.append(template[cursor:])
        result = "".join(pieces)
    result = result.strip()
    if not result:
        raise WebhookError("webhook template produced an empty message")
    if len(result) > MAX_MESSAGE_CHARS:
        raise WebhookError(
            f"webhook message exceeds {MAX_MESSAGE_CHARS} characters")
    return result


def _idempotency_hash(headers):
    normalized = {
        str(key).lower(): str(value)
        for key, value in dict(headers or {}).items()
    }
    for name in _IDEMPOTENCY_HEADERS:
        value = normalized.get(name, "").strip()
        if value:
            return hashlib.sha256(
                (name + "\0" + value).encode("utf-8")).hexdigest()
    return ""


def _payload_hash(raw, _content_type=None):
    """Hash provider payload bytes; interpretation headers freeze separately."""
    return hashlib.sha256(bytes(raw or b"")).hexdigest()


def _find_delivery(hook_guid, key_hash):
    if not key_hash:
        return None
    rows = (gs.list_objects(
        "webhook_delivery", hook_guid=hook_guid,
        idempotency_key_hash=key_hash, limit=2) or {}).get("objects", [])
    if len(rows) > 1:
        raise WebhookError(
            "duplicate webhook idempotency records require operator repair",
            status=500)
    return rows[0] if rows else None


def _create_delivery(body):
    try:
        return gs.create_object("webhook_delivery", body)
    except Exception as primary_error:
        try:
            rows = (gs.list_objects(
                "webhook_delivery", request_id=body["request_id"],
                limit=2) or {}).get("objects", [])
        except Exception as verification_error:
            raise primary_error from verification_error
        matches = [
            row for row in rows
            if all(row.get(key) == value for key, value in body.items())
        ]
        if len(rows) == 1 and len(matches) == 1:
            return matches[0]
        raise


def _outgoing_edges(hook_guid):
    rows = (gs.list_objects(
        "edge", source=hook_guid, sort="created_at",
        order="asc", limit=2000) or {}).get("objects", [])
    return [
        row for row in rows
        if row.get("source") == hook_guid and row.get("directed", True)
    ]


def _snapshot_routes(hook):
    """Freeze exact edge and endpoint identities before accepting a receipt."""
    hook_guid = hook.get("_guid")
    if (
        not isinstance(hook_guid, str)
        or not _PUBLIC_IDENTIFIER_RE.fullmatch(hook_guid)
        or not config.valid_agent_name(hook.get("name"))
    ):
        raise gs.GraphError(
            "webhook node is missing an immutable identity")
    edges = _outgoing_edges(hook_guid)
    if not edges:
        raise WebhookError(
            "webhook has no outgoing agent routes", status=409)
    routes = []
    for edge in edges:
        edge_guid = edge.get("_guid")
        target_guid = edge.get("target")
        if (
            not isinstance(edge_guid, str)
            or not _PUBLIC_IDENTIFIER_RE.fullmatch(edge_guid)
            or edge.get("source") != hook_guid
            or not isinstance(target_guid, str)
            or not _PUBLIC_IDENTIFIER_RE.fullmatch(target_guid)
            or not edge.get("directed", True)
        ):
            raise gs.GraphError(
                "webhook route is missing an immutable identity")
        target = gs._get_object_if_present(target_guid)
        if target is None:
            raise gs.GraphError(
                "webhook route target no longer exists")
        target_name = target.get("name")
        current = gs.get_agent_by_name(target_name)
        if (
            target.get("kind") == gs.WEBHOOK_KIND
            or not config.valid_agent_name(target_name)
            or not current
            or current.get("_guid") != target_guid
        ):
            raise gs.GraphError(
                "webhook route target is not a current runtime agent")
        routes.append({
            "edge_guid": edge_guid,
            "source_guid": hook_guid,
            "target_guid": target_guid,
            "target_name": target_name,
        })
    return routes


def _processing_snapshot(delivery):
    """Return a safe resumable receipt snapshot or fail closed."""
    if delivery.get("receipt_version") != RECEIPT_VERSION:
        raise WebhookError(
            "webhook temporarily unavailable", status=503)
    message = delivery.get("rendered_message")
    routes = delivery.get("routes")
    request_id = delivery.get("request_id")
    if (
        not isinstance(message, str)
        or not message
        or len(message) > MAX_MESSAGE_CHARS
        or not isinstance(request_id, str)
        or not _PUBLIC_IDENTIFIER_RE.fullmatch(request_id)
        or not isinstance(routes, list)
    ):
        raise WebhookError(
            "webhook temporarily unavailable", status=503)
    normalized = []
    required = (
        "edge_guid", "source_guid", "target_guid", "target_name")
    for route in routes:
        if (
            not isinstance(route, dict)
            or any(
                not isinstance(route.get(field), str)
                or not _PUBLIC_IDENTIFIER_RE.fullmatch(route[field])
                for field in required[:3]
            )
            or not config.valid_agent_name(route.get("target_name"))
        ):
            raise WebhookError(
                "webhook temporarily unavailable", status=503)
        normalized.append({
            field: route[field]
            for field in required
        })
    if not normalized:
        raise WebhookError(
            "webhook temporarily unavailable", status=503)
    return message, normalized


def _delivery_rejection(route, detail):
    return {
        "edge_guid": route["edge_guid"],
        "target": route["target_name"],
        "accepted": False,
        "error": str(detail or "webhook route failed"),
    }


def _deliver_route(hook, delivery, route, message):
    """Reconcile or durably reserve one frozen route's exact message."""
    ok, outcome = mail.enqueue(
        route["target_name"], message, sender=hook["name"],
        request_id=(
            f"webhook:{delivery['request_id']}:{route['edge_guid']}"),
        expected_edge_guid=route["edge_guid"],
        expected_sender_guid=route["source_guid"],
        expected_target_guid=route["target_guid"],
        raise_graph_errors=True)
    if not ok:
        return _delivery_rejection(route, outcome)
    if (
        not isinstance(outcome, dict)
        or not isinstance(outcome.get("_guid"), str)
        or not outcome.get("_guid")
    ):
        raise gs.GraphError(
            "webhook mail acceptance returned an invalid durable row")
    return {
        "edge_guid": route["edge_guid"],
        "target": route["target_name"],
        "accepted": True,
        "message_guid": outcome.get("_guid"),
        "status": outcome.get("status") or "queued",
    }


def _public_identifier(value):
    """Return a bounded identifier safe to reflect through public ingress."""
    value = value if isinstance(value, str) else ""
    return value if _PUBLIC_IDENTIFIER_RE.fullmatch(value) else ""


def _public_delivery_result(result):
    """Project a durable result onto the strict public response schema."""
    accepted = bool((result or {}).get("accepted"))
    public = {
        "edge_guid": _public_identifier((result or {}).get("edge_guid")),
        "target": _public_identifier((result or {}).get("target")),
        "accepted": accepted,
    }
    if accepted:
        public["message_guid"] = _public_identifier(
            (result or {}).get("message_guid"))
        status = (result or {}).get("status")
        public["status"] = (
            status if status in _PUBLIC_ACCEPTED_STATUSES else "accepted")
    else:
        public.update({
            "status": "rejected",
            "error": {
                "code": _PUBLIC_REJECTION_CODE,
                "message": _PUBLIC_REJECTION_MESSAGE,
            },
        })
    return public


def _completed_response(delivery, *, duplicate=False):
    results = list(delivery.get("results") or [])
    accepted = sum(1 for row in results if row.get("accepted"))
    return {
        "ok": True,
        "delivery_id": _public_identifier(delivery.get("request_id")),
        "duplicate": bool(duplicate),
        "accepted": accepted,
        "rejected": len(results) - accepted,
        "deliveries": [
            _public_delivery_result(result) for result in results
        ],
    }


def _existing_delivery(hook_guid, key_hash, body_hash):
    """Resolve a keyed receipt under the caller's delivery lock."""
    delivery = _find_delivery(hook_guid, key_hash)
    if not delivery:
        return None
    if delivery.get("payload_hash") != body_hash:
        raise WebhookError(
            "webhook idempotency key was reused with a different body",
            status=409)
    if delivery.get("status") == "completed":
        return _completed_response(delivery, duplicate=True)
    message, routes = _processing_snapshot(delivery)
    return delivery, message, routes


def _attempt_lock_scope(delivery):
    """Choose one bounded lock stripe for a durable delivery receipt."""
    request_id = str((delivery or {}).get("request_id") or "")
    if not _PUBLIC_IDENTIFIER_RE.fullmatch(request_id):
        raise gs.GraphError(
            "webhook delivery is missing an immutable identity")
    digest = hashlib.sha256(request_id.encode("utf-8")).digest()
    stripe = int.from_bytes(digest[:4], "big") % _ATTEMPT_LOCK_STRIPES
    return f"webhook-fanout-{stripe}"


def receive(token, raw, content_type="", headers=None):
    """Accept one public invocation and durably fan it out to snapshotted edges."""
    hook = gs.get_webhook_by_token(token)
    if not hook:
        raise WebhookError("webhook not found", status=404)
    body_hash = _payload_hash(raw, content_type)
    key_hash = _idempotency_hash(headers)

    # A completed or in-flight keyed receipt is authoritative before parsing or
    # rendering. Provider retries therefore cannot be changed by a later
    # template/header update, and completed duplicates avoid unnecessary work.
    with gs._invariant_lock(gs._WEBHOOK_ADMISSION_LOCK_SCOPE):
        with gs._invariant_lock("webhook-delivery"):
            hook = gs.get_webhook_by_token(token)
            if not hook:
                raise WebhookError("webhook not found", status=404)
            existing = _existing_delivery(
                hook["_guid"], key_hash, body_hash)
    if isinstance(existing, dict):
        return existing
    if existing is not None:
        delivery, message, routes = existing
    else:
        payload, raw_text = parse_payload(raw, content_type)

        # Recheck after parsing because another process may have created the
        # same keyed receipt. Only the winner snapshots current template and
        # topology; every retry reuses that durable snapshot.
        with gs._invariant_lock(gs._WEBHOOK_ADMISSION_LOCK_SCOPE):
            with gs._invariant_lock("webhook-delivery"):
                hook = gs.get_webhook_by_token(token)
                if not hook:
                    raise WebhookError("webhook not found", status=404)
                existing = _existing_delivery(
                    hook["_guid"], key_hash, body_hash)
                if isinstance(existing, dict):
                    return existing
                if existing is not None:
                    delivery, message, routes = existing
                else:
                    message = render_message(
                        hook.get("webhook_template") or "",
                        payload, raw_text, headers)
                    with gs._invariant_lock("edge-authorization"):
                        routes = _snapshot_routes(hook)
                    request_id = uuid.uuid4().hex
                    delivery = _create_delivery({
                        "hook_guid": hook["_guid"],
                        "request_id": request_id,
                        "idempotency_key_hash": key_hash,
                        "payload_hash": body_hash,
                        "receipt_version": RECEIPT_VERSION,
                        "rendered_message": message,
                        "routes": routes,
                        # Retained for operator/backward-readable receipts.
                        "edge_guids": [
                            route["edge_guid"] for route in routes
                        ],
                        "status": "processing",
                        "results": [],
                        "received_at": time.time(),
                        "completed_at": 0,
                    })

    # Serialize fan-out and finalization for this receipt. The bounded stripes
    # avoid an unbounded lock file per provider delivery while letting unrelated
    # receipts proceed concurrently. A waiter re-reads after taking the stripe,
    # so it cannot race a terminal rejection against another retry's durable
    # acceptance. Infrastructure failures escape and leave the receipt
    # processing; a same-key retry resumes its frozen snapshot.
    with gs._invariant_lock(_attempt_lock_scope(delivery)):
        with gs._invariant_lock("webhook-delivery"):
            current_delivery = gs.get_object(delivery["_guid"])
            if current_delivery.get("status") == "completed":
                return _completed_response(
                    current_delivery, duplicate=True)
            message, routes = _processing_snapshot(current_delivery)
        results = [
            _deliver_route(hook, current_delivery, route, message)
            for route in routes
        ]
        with gs._invariant_lock("webhook-delivery"):
            current_delivery = gs.get_object(delivery["_guid"])
            if current_delivery.get("status") == "completed":
                return _completed_response(
                    current_delivery, duplicate=True)
            completed = gs._patch_object_verified(
                "webhook_delivery", delivery["_guid"], {
                    "status": "completed",
                    "results": results,
                    "completed_at": time.time(),
                })
    accepted = sum(1 for row in results if row.get("accepted"))
    try:
        gs.mark_webhook_called(
            hook["_guid"],
            f"{accepted}/{len(results)} route(s) accepted")
    except Exception:
        pass
    return _completed_response(completed)
