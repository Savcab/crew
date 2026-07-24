"""Public webhook-node ingress.

The URL capability authorizes only one operation: convert this request into a
message and durably enqueue it across the hook's outgoing graph edges. Operator
graph controls and terminal APIs remain behind the dashboard cookie boundary.
"""
import hashlib
import json
import re
import time
import urllib.parse
import uuid

from . import config, graphstore as gs, mail


MAX_MESSAGE_CHARS = 256 * 1024
_PLACEHOLDER_RE = re.compile(r"{{\s*([^{}]+?)\s*}}")
_IDEMPOTENCY_HEADERS = (
    "idempotency-key",
    "x-github-delivery",
    "x-webhook-id",
    "webhook-id",
)


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


def _reject_json_constant(_value):
    raise ValueError("non-standard JSON constants are not allowed")


def _decode_utf8(raw):
    try:
        return bytes(raw or b"").decode("utf-8")
    except UnicodeDecodeError as error:
        raise WebhookError("webhook body must be valid UTF-8") from error


def parse_payload(raw, content_type=""):
    """Parse JSON, form, or text input into the template's payload value."""
    media_type = str(content_type or "").split(";", 1)[0].strip().lower()
    text = _decode_utf8(raw)
    if media_type == "application/json" or media_type.endswith("+json"):
        try:
            return json.loads(text, parse_constant=_reject_json_constant), text
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


def _payload_hash(raw, content_type):
    digest = hashlib.sha256()
    digest.update(str(content_type or "").encode("utf-8"))
    digest.update(b"\0")
    digest.update(bytes(raw or b""))
    return digest.hexdigest()


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


def _completed_response(delivery, *, duplicate=False):
    results = list(delivery.get("results") or [])
    accepted = sum(1 for row in results if row.get("accepted"))
    return {
        "ok": True,
        "delivery_id": delivery.get("request_id"),
        "duplicate": bool(duplicate),
        "accepted": accepted,
        "rejected": len(results) - accepted,
        "deliveries": results,
    }


def receive(token, raw, content_type="", headers=None):
    """Accept one public invocation and durably fan it out to snapshotted edges."""
    hook = gs.get_webhook_by_token(token)
    if not hook:
        raise WebhookError("webhook not found", status=404)
    payload, raw_text = parse_payload(raw, content_type)
    message = render_message(
        hook.get("webhook_template") or "", payload, raw_text, headers)
    body_hash = _payload_hash(raw, content_type)
    key_hash = _idempotency_hash(headers)

    # The lock closes only the duplicate read/create race. Fan-out happens
    # after release: its stable per-edge message request IDs are independently
    # idempotent, so concurrent retries reconcile without serializing unrelated
    # webhooks behind edge transforms.
    with gs._invariant_lock("webhook-delivery"):
        hook = gs.get_webhook_by_token(token)
        if not hook:
            raise WebhookError("webhook not found", status=404)
        delivery = _find_delivery(hook["_guid"], key_hash)
        if delivery:
            if delivery.get("payload_hash") != body_hash:
                raise WebhookError(
                    "webhook idempotency key was reused with a different body",
                    status=409)
            if delivery.get("status") == "completed":
                return _completed_response(delivery, duplicate=True)
            edge_guids = list(delivery.get("edge_guids") or [])
        else:
            edges = _outgoing_edges(hook["_guid"])
            edge_guids = [
                edge.get("_guid") for edge in edges if edge.get("_guid")
            ]
            if not edge_guids:
                raise WebhookError(
                    "webhook has no outgoing agent routes", status=409)
            request_id = uuid.uuid4().hex
            delivery = _create_delivery({
                "hook_guid": hook["_guid"],
                "request_id": request_id,
                "idempotency_key_hash": key_hash,
                "payload_hash": body_hash,
                "edge_guids": edge_guids,
                "status": "processing",
                "results": [],
                "received_at": time.time(),
                "completed_at": 0,
            })

    results = []
    for edge_guid in edge_guids:
        result = {
            "edge_guid": edge_guid,
            "target": "",
            "accepted": False,
        }
        try:
            edge = gs.get_object(edge_guid)
            if (edge.get("source") != hook["_guid"]
                    or not edge.get("directed", True)):
                raise gs.GraphError(
                    "snapshotted edge is no longer a directed hook route")
            target = gs.get_object(edge.get("target"))
            current = gs.get_agent_by_name(target.get("name"))
            if (target.get("kind") == gs.WEBHOOK_KIND
                    or not current
                    or current.get("_guid") != target.get("_guid")):
                raise gs.GraphError(
                    "snapshotted target is no longer an active agent")
            result["target"] = target.get("name") or ""
            ok, outcome = mail.enqueue(
                result["target"], message, sender=hook["name"],
                request_id=(
                    f"webhook:{delivery['request_id']}:{edge_guid}"))
            result["accepted"] = bool(ok)
            if ok:
                result["message_guid"] = outcome.get("_guid")
                result["status"] = outcome.get("status") or "queued"
            else:
                result["error"] = str(outcome)
        except Exception as error:
            result["error"] = str(error) or "webhook route failed"
        results.append(result)

    # First finalizer wins. A concurrent retry may have fanned out in parallel,
    # but stable message request IDs made that harmless; never let its later
    # observations overwrite the original completed provider response.
    with gs._invariant_lock("webhook-delivery"):
        current_delivery = gs.get_object(delivery["_guid"])
        if current_delivery.get("status") == "completed":
            return _completed_response(current_delivery, duplicate=True)
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
