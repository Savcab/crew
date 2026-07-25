"""Pure contracts for public webhook parsing, templating, and URL exposure."""
import contextlib
import concurrent.futures
import json
import os
import sys
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from crew import graphstore as gs, webhooks  # noqa: E402


class PayloadParsingTests(unittest.TestCase):
    def test_json_and_vendor_json_accept_any_standard_json_value(self):
        payload, raw = webhooks.parse_payload(
            b'{"issue":{"title":"Ship it"},"labels":["bug","p1"]}',
            "application/json; charset=utf-8")
        self.assertEqual(payload["issue"]["title"], "Ship it")
        self.assertEqual(payload["labels"][1], "p1")
        self.assertIn('"issue"', raw)

        payload, _ = webhooks.parse_payload(
            b'["one",2,true,null]', "application/vnd.example+json")
        self.assertEqual(payload, ["one", 2, True, None])

    def test_form_values_preserve_repeated_fields(self):
        payload, raw = webhooks.parse_payload(
            b"tag=bug&tag=urgent&message=hello+world&blank=",
            "application/x-www-form-urlencoded")
        self.assertEqual(payload, {
            "tag": ["bug", "urgent"],
            "message": "hello world",
            "blank": "",
        })
        self.assertIn("tag=bug", raw)

    def test_other_content_types_are_utf8_text(self):
        payload, raw = webhooks.parse_payload(
            "snowman \N{SNOWMAN}".encode(), "text/plain")
        self.assertEqual(payload, "snowman \N{SNOWMAN}")
        self.assertEqual(raw, payload)

    def test_invalid_utf8_json_and_nonstandard_constants_are_rejected(self):
        cases = (
            (b"\xff", "text/plain", "UTF-8"),
            (b'{"broken":', "application/json", "valid JSON"),
            (b'{"value":NaN}', "application/json", "valid JSON"),
            (b'{"value":Infinity}', "application/json", "valid JSON"),
            (b'{"value":1e400}', "application/json", "valid JSON"),
        )
        for raw, media_type, phrase in cases:
            with self.subTest(raw=raw), self.assertRaisesRegex(
                    webhooks.WebhookError, phrase):
                webhooks.parse_payload(raw, media_type)

    def test_payload_identity_ignores_content_type_interpretation_headers(self):
        raw = b'{"message":"same payload"}'
        json_hashes = {
            webhooks._payload_hash(raw, content_type)
            for content_type in (
                "application/json",
                " Application/JSON ; charset=utf-8",
                "application/problem+json",
            )
        }
        text_hashes = {
            webhooks._payload_hash(b"same text", content_type)
            for content_type in (None, "", "text/plain")
        }
        self.assertEqual(len(json_hashes), 1)
        self.assertEqual(len(text_hashes), 1)


class MessageTemplateTests(unittest.TestCase):
    def test_nested_payload_array_header_and_raw_placeholders(self):
        rendered = webhooks.render_message(
            "Issue {{ payload.issue.title }} "
            "[{{ payload.issue.labels.1.name }}] "
            "event={{ headers.x-github-event }} raw={{ raw }}",
            {
                "issue": {
                    "title": "Fix queue",
                    "labels": [{"name": "bug"}, {"name": "urgent"}],
                },
            },
            '{"issue":"raw"}',
            {"X-GitHub-Event": "issues"},
        )
        self.assertEqual(
            rendered,
            'Issue Fix queue [urgent] event=issues raw={"issue":"raw"}')

    def test_non_string_values_are_compact_json(self):
        rendered = webhooks.render_message(
            "meta={{ payload.meta }} active={{ payload.active }}",
            {"meta": {"count": 2}, "active": True}, "")
        self.assertEqual(rendered, 'meta={"count":2} active=true')

    def test_standard_credentials_are_never_available_to_templates(self):
        headers = {
            "X-Provider-Event": "push",
            "Stripe-Signature": "provider-signature",
            "aUtHoRiZaTiOn": "Bearer operator-secret",
            "Cookie": "session=operator-secret",
            "Proxy-Authorization": "Basic operator-secret",
            "Set-Cookie": "session=operator-secret",
        }
        self.assertEqual(
            webhooks.render_message(
                "{{ headers.x-provider-event }} "
                "{{ headers.stripe-signature }}",
                {}, "", headers),
            "push provider-signature",
        )
        for name in (
                "authorization", "cookie", "proxy-authorization",
                "set-cookie"):
            with self.subTest(name=name), self.assertRaisesRegex(
                    webhooks.WebhookError, "not found"):
                webhooks.render_message(
                    "{{ headers." + name + " }}", {}, "", headers)

    def test_blank_template_uses_message_text_then_full_payload(self):
        self.assertEqual(
            webhooks.render_message(
                "", {"message": "provider message", "text": "fallback"}, ""),
            "provider message")
        self.assertEqual(
            webhooks.render_message("", {"text": "provider text"}, ""),
            "provider text")
        self.assertEqual(
            webhooks.render_message("", {"event": "push", "count": 2}, ""),
            '{"event":"push","count":2}')
        self.assertEqual(
            webhooks.render_message("", "plain request", "plain request"),
            "plain request")

    def test_invalid_or_missing_placeholders_fail_before_delivery(self):
        cases = (
            ("{{ payload.issue.title }}", {"issue": {}}, "not found"),
            ("{{ environment.secret }}", {}, "unsupported"),
            ("{{ raw.child }}", {}, "cannot have child"),
            ("unmatched {{ payload.message", {}, "unmatched"),
            ("", "", "empty"),
        )
        for template, payload, phrase in cases:
            with self.subTest(template=template), self.assertRaisesRegex(
                    webhooks.WebhookError, phrase):
                webhooks.render_message(template, payload, "", {})

    def test_template_and_rendered_message_limits_are_enforced(self):
        with self.assertRaisesRegex(webhooks.WebhookError, "template exceeds"):
            webhooks.validate_template("x" * (gs.WEBHOOK_TEMPLATE_MAX + 1))
        with self.assertRaisesRegex(webhooks.WebhookError, "message exceeds"):
            webhooks.render_message(
                "{{ payload }}", "x" * (webhooks.MAX_MESSAGE_CHARS + 1), "")


class PublicUrlTests(unittest.TestCase):
    def test_operator_shape_exposes_url_without_a_standalone_token(self):
        hook = {
            "_guid": "hook-guid", "name": "issues",
            "webhook_token": "a" * 43,
        }
        with mock.patch.object(
                webhooks.config, "WEBHOOK_PUBLIC_BASE_URL",
                "https://hooks.example.test"):
            result = webhooks.for_operator(hook)

        self.assertNotIn("webhook_token", result)
        self.assertEqual(
            result["public_url"],
            "https://hooks.example.test/hooks/" + ("a" * 43))
        self.assertEqual(hook["webhook_token"], "a" * 43)


class _DeliveryHarness:
    """Small in-memory MorphDB surface for retry and snapshot contracts."""

    def __init__(self, route_count=2):
        self.hook = {
            "_guid": "hook-guid",
            "name": "issues",
            "kind": gs.WEBHOOK_KIND,
            "role": "",
            "webhook_token": "token",
            "webhook_token_hash": gs.webhook_token_hash("token"),
            "webhook_template": "Issue {{ headers.x-render }}",
        }
        self.edges = []
        self.targets = {}
        for index in range(route_count):
            target_guid = f"target-{index + 1}"
            target_name = f"worker_{index + 1}"
            self.targets[target_guid] = {
                "_guid": target_guid,
                "name": target_name,
                "kind": "agent",
            }
            self.edges.append({
                "_guid": f"edge-{index + 1}",
                "source": self.hook["_guid"],
                "target": target_guid,
                "directed": True,
            })
        self.durable = {}

    def list_objects(self, object_type, **_kwargs):
        if object_type == "edge":
            return {"objects": [dict(row) for row in self.edges]}
        if object_type == "webhook_delivery":
            return {
                "objects": [dict(self.durable)] if self.durable else [],
            }
        raise AssertionError(f"unexpected object type: {object_type}")

    def create_object(self, object_type, body):
        if object_type != "webhook_delivery":
            raise AssertionError(f"unexpected object type: {object_type}")
        self.durable.update(body, _guid="delivery-guid")
        return dict(self.durable)

    def get_object(self, guid):
        if guid == self.hook["_guid"]:
            return dict(self.hook)
        if guid == "delivery-guid":
            return dict(self.durable)
        for edge in self.edges:
            if edge["_guid"] == guid:
                return dict(edge)
        if guid in self.targets:
            return dict(self.targets[guid])
        raise gs.GraphError("404: missing fixture object")

    def patch_object(self, object_type, guid, body):
        if (object_type, guid) == ("agent", self.hook["_guid"]):
            self.hook.update(body)
            return dict(self.hook)
        self.assert_delivery(object_type, guid)
        self.durable.update(body)
        return dict(self.durable)

    @staticmethod
    def assert_delivery(object_type, guid):
        if (object_type, guid) != ("webhook_delivery", "delivery-guid"):
            raise AssertionError(
                f"unexpected delivery patch: {(object_type, guid)!r}")

    def get_agent_by_name(self, name):
        return next((
            dict(row) for row in self.targets.values()
            if row["name"] == name
        ), None)

    @contextlib.contextmanager
    def patched(self, enqueue, invariant_lock=None):
        invariant_lock = (
            invariant_lock
            or (lambda *_args, **_kwargs: contextlib.nullcontext())
        )
        patches = (
            mock.patch.object(
                gs, "get_webhook_by_token",
                side_effect=lambda token: (
                    dict(self.hook)
                    if token == self.hook.get("webhook_token") else None)),
            mock.patch.object(
                gs, "list_objects", side_effect=self.list_objects),
            mock.patch.object(
                gs, "create_object", side_effect=self.create_object),
            mock.patch.object(gs, "get_object", side_effect=self.get_object),
            mock.patch.object(
                gs, "get_agent_by_name",
                side_effect=self.get_agent_by_name),
            mock.patch.object(
                gs, "_patch_object_verified",
                side_effect=self.patch_object),
            mock.patch.object(
                gs, "_invariant_lock",
                side_effect=invariant_lock),
            mock.patch.object(gs, "mark_webhook_called"),
            mock.patch("crew.webhooks.mail.enqueue", side_effect=enqueue),
        )
        with contextlib.ExitStack() as stack:
            entered = [stack.enter_context(patch) for patch in patches]
            yield entered[-1]


class DeliveryRetryTests(unittest.TestCase):
    def test_retry_reuses_rendered_message_and_immutable_route_snapshot(self):
        harness = _DeliveryHarness(route_count=2)
        calls = []
        failed = False

        def enqueue(target, message, **kwargs):
            nonlocal failed
            calls.append((target, message, dict(kwargs)))
            if target == "worker_2" and not failed:
                failed = True
                raise gs.GraphError("transient message storage failure")
            return True, {
                "_guid": "message-" + kwargs["expected_edge_guid"],
                "status": "queued",
            }

        headers = {
            "Idempotency-Key": "provider-delivery-1",
            "X-Render": "original",
        }
        with harness.patched(enqueue):
            with self.assertRaisesRegex(
                    gs.GraphError, "transient message storage failure"):
                webhooks.receive(
                    "token", b'{"event":"opened"}',
                    "application/json", headers)

        self.assertEqual(harness.durable["status"], "processing")
        self.assertEqual(
            harness.durable["rendered_message"], "Issue original")
        self.assertEqual(harness.durable["receipt_version"], 1)
        self.assertEqual(
            [route["edge_guid"] for route in harness.durable["routes"]],
            ["edge-1", "edge-2"])

        harness.hook["webhook_template"] = "Changed {{ headers.x-render }}"
        retry_headers = dict(headers, **{"X-Render": "retry"})
        with harness.patched(enqueue):
            result = webhooks.receive(
                "token", b'{"event":"opened"}',
                "application/json", retry_headers)

        self.assertEqual((result["accepted"], result["rejected"]), (2, 0))
        retry_calls = calls[-2:]
        self.assertEqual(
            [message for _target, message, _kwargs in retry_calls],
            ["Issue original", "Issue original"])
        for route, (target, _message, kwargs) in zip(
                harness.durable["routes"], retry_calls):
            self.assertEqual(target, route["target_name"])
            self.assertEqual(
                kwargs["expected_edge_guid"], route["edge_guid"])
            self.assertEqual(
                kwargs["expected_sender_guid"], route["source_guid"])
            self.assertEqual(
                kwargs["expected_target_guid"], route["target_guid"])
            self.assertTrue(kwargs["raise_graph_errors"])

    def test_retargeted_edge_cannot_redirect_a_processing_invocation(self):
        harness = _DeliveryHarness(route_count=1)

        def fail_once(_target, _message, **_kwargs):
            raise gs.GraphError("transient message storage failure")

        headers = {
            "Idempotency-Key": "provider-delivery-retarget",
            "X-Render": "original",
        }
        with harness.patched(fail_once):
            with self.assertRaises(gs.GraphError):
                webhooks.receive(
                    "token", b'{"event":"opened"}',
                    "application/json", headers)

        harness.targets["replacement-target"] = {
            "_guid": "replacement-target",
            "name": "replacement_worker",
            "kind": "agent",
        }
        harness.edges[0]["target"] = "replacement-target"

        def reject_old_snapshot(target, _message, **kwargs):
            self.assertEqual(target, "worker_1")
            self.assertEqual(kwargs["expected_edge_guid"], "edge-1")
            self.assertEqual(kwargs["expected_target_guid"], "target-1")
            return False, "route identity changed"

        with harness.patched(reject_old_snapshot) as enqueue:
            result = webhooks.receive(
                "token", b'{"event":"opened"}',
                "application/json", headers)

        enqueue.assert_called_once()
        self.assertEqual((result["accepted"], result["rejected"]), (0, 1))
        self.assertEqual(
            result["deliveries"][0]["error"]["code"],
            "route_delivery_failed")
        self.assertEqual(harness.durable["status"], "completed")

    def test_completed_duplicate_returns_before_parse_or_render(self):
        harness = _DeliveryHarness(route_count=1)
        raw = b'{"event":"already-completed"}'
        headers = {"Idempotency-Key": "provider-delivery-completed"}
        harness.durable.update({
            "_guid": "delivery-guid",
            "hook_guid": harness.hook["_guid"],
            "request_id": "delivery-completed",
            "idempotency_key_hash": webhooks._idempotency_hash(headers),
            "payload_hash": webhooks._payload_hash(
                raw, "application/json"),
            "receipt_version": 1,
            "rendered_message": "original",
            "routes": [],
            "status": "completed",
            "results": [{
                "edge_guid": "edge-1",
                "target": "worker_1",
                "accepted": True,
                "message_guid": "message-1",
                "status": "queued",
            }],
        })

        with harness.patched(
                lambda *_args, **_kwargs:
                (_ for _ in ()).throw(
                    AssertionError("completed duplicate must not enqueue"))), \
             mock.patch.object(webhooks, "parse_payload") as parse, \
             mock.patch.object(webhooks, "render_message") as render:
            result = webhooks.receive(
                "token", raw, "application/json", headers)

        self.assertTrue(result["duplicate"])
        self.assertEqual(result["accepted"], 1)
        parse.assert_not_called()
        render.assert_not_called()

    def test_same_body_retry_ignores_content_type_header_change(self):
        cases = (
            (
                b'{"message":"same json"}',
                "application/json",
                "application/problem+json",
            ),
            (b"same text", "", "text/plain"),
        )
        for index, (raw, initial_type, retry_type) in enumerate(cases):
            with self.subTest(
                    initial_type=initial_type, retry_type=retry_type):
                harness = _DeliveryHarness(route_count=1)
                calls = []

                def enqueue(_target, _message, **_kwargs):
                    calls.append(1)
                    return True, {
                        "_guid": f"message-content-type-{index}",
                        "status": "queued",
                    }

                headers = {
                    "Idempotency-Key": f"provider-content-type-{index}",
                    "X-Render": "frozen",
                }
                with harness.patched(enqueue):
                    initial = webhooks.receive(
                        "token", raw, initial_type, headers)
                    duplicate = webhooks.receive(
                        "token", raw, retry_type, headers)

                self.assertFalse(initial["duplicate"])
                self.assertTrue(duplicate["duplicate"])
                self.assertEqual(calls, [1])
                self.assertEqual(
                    initial["deliveries"], duplicate["deliveries"])

    def test_concurrent_completed_finalizer_wins_without_overwrite(self):
        harness = _DeliveryHarness(route_count=1)

        def complete_elsewhere(_target, _message, **_kwargs):
            harness.durable.update({
                "status": "completed",
                "results": [{
                    "edge_guid": "edge-1",
                    "target": "worker_1",
                    "accepted": True,
                    "message_guid": "message-from-concurrent-retry",
                    "status": "queued",
                }],
            })
            return True, {
                "_guid": "message-from-this-retry",
                "status": "queued",
            }

        with harness.patched(complete_elsewhere):
            result = webhooks.receive(
                "token", b'{"event":"opened"}', "application/json", {
                    "Idempotency-Key": "provider-delivery-concurrent",
                    "X-Render": "original",
                })

        self.assertTrue(result["duplicate"])
        self.assertEqual(
            result["deliveries"][0]["message_guid"],
            "message-from-concurrent-retry")
        self.assertEqual(
            harness.durable["results"][0]["message_guid"],
            "message-from-concurrent-retry")

    def test_same_key_retries_serialize_terminal_rejection_and_completion(self):
        harness = _DeliveryHarness(route_count=1)
        locks = {}
        locks_guard = threading.Lock()
        attempt_contenders = 0
        both_attempted = threading.Event()
        first_enqueue_entered = threading.Event()
        release_first = threading.Event()
        enqueue_calls = 0

        @contextlib.contextmanager
        def invariant_lock(scope, *_args, **_kwargs):
            nonlocal attempt_contenders
            with locks_guard:
                lock = locks.setdefault(scope, threading.RLock())
                if str(scope).startswith("webhook-fanout-"):
                    attempt_contenders += 1
                    if attempt_contenders == 2:
                        both_attempted.set()
            with lock:
                yield

        def enqueue(_target, _message, **_kwargs):
            nonlocal enqueue_calls
            with locks_guard:
                enqueue_calls += 1
            first_enqueue_entered.set()
            if not release_first.wait(2):
                raise AssertionError("timed out waiting for concurrent retry")
            return False, "route policy rejected this attempt"

        headers = {
            "Idempotency-Key": "provider-delivery-concurrent-serialized",
            "X-Render": "original",
        }
        with harness.patched(enqueue, invariant_lock=invariant_lock):
            with concurrent.futures.ThreadPoolExecutor(
                    max_workers=2) as executor:
                first = executor.submit(
                    webhooks.receive, "token", b'{"event":"opened"}',
                    "application/json", headers)
                self.assertTrue(first_enqueue_entered.wait(2))
                second = executor.submit(
                    webhooks.receive, "token", b'{"event":"opened"}',
                    "application/json", headers)
                self.assertTrue(both_attempted.wait(2))
                self.assertEqual(enqueue_calls, 1)
                release_first.set()
                responses = [first.result(2), second.result(2)]

        self.assertEqual(enqueue_calls, 1)
        self.assertEqual(
            sorted(response["duplicate"] for response in responses),
            [False, True],
        )
        self.assertEqual(
            {(response["accepted"], response["rejected"])
             for response in responses},
            {(0, 1)},
        )

    def test_rotation_linearizes_after_old_token_receipt_creation(self):
        harness = _DeliveryHarness(route_count=1)
        locks = {}
        locks_guard = threading.Lock()
        render_entered = threading.Event()
        release_render = threading.Event()
        rotation_attempted = threading.Event()
        rotation_done = threading.Event()
        operation_order = []
        rotation_thread = {"ident": None}
        original_render = webhooks.render_message
        original_create_delivery = webhooks._create_delivery

        @contextlib.contextmanager
        def invariant_lock(scope, *_args, **_kwargs):
            with locks_guard:
                lock = locks.setdefault(scope, threading.RLock())
            if (
                    scope == gs._WEBHOOK_ADMISSION_LOCK_SCOPE
                    and threading.get_ident() == rotation_thread["ident"]):
                rotation_attempted.set()
            with lock:
                yield

        def blocked_render(*args, **kwargs):
            render_entered.set()
            if not release_render.wait(2):
                raise AssertionError("timed out waiting to release rendering")
            return original_render(*args, **kwargs)

        def create_delivery(body):
            result = original_create_delivery(body)
            operation_order.append("receipt")
            return result

        def rotate():
            rotation_thread["ident"] = threading.get_ident()
            try:
                rotated = gs.update_webhook(
                    harness.hook["_guid"], rotate=True)
                operation_order.append("rotation")
                return rotated
            finally:
                rotation_done.set()

        def enqueue(_target, _message, **_kwargs):
            return True, {"_guid": "message-race", "status": "queued"}

        with harness.patched(enqueue, invariant_lock=invariant_lock), \
             mock.patch.object(
                 webhooks, "render_message", side_effect=blocked_render), \
             mock.patch.object(
                 webhooks, "_create_delivery", side_effect=create_delivery), \
             mock.patch.object(
                 gs, "_new_webhook_token", return_value="replacement-token"), \
             mock.patch.object(gs.guard, "check"), \
             mock.patch.object(gs.guard, "audit"):
            with concurrent.futures.ThreadPoolExecutor(
                    max_workers=2) as executor:
                receiving = executor.submit(
                    webhooks.receive, "token", b'{"event":"opened"}',
                    "application/json", {
                        "Idempotency-Key": "provider-rotation-race",
                        "X-Render": "original",
                    })
                self.assertTrue(render_entered.wait(2))
                rotating = executor.submit(rotate)
                self.assertTrue(rotation_attempted.wait(2))
                self.assertFalse(rotation_done.is_set())
                release_render.set()
                received = receiving.result(2)
                rotated = rotating.result(2)

            with self.assertRaises(webhooks.WebhookError) as rejected:
                webhooks.receive(
                    "token", b'{"event":"after-rotation"}',
                    "application/json", {
                        "Idempotency-Key": "provider-after-rotation",
                    })

        self.assertEqual(operation_order, ["receipt", "rotation"])
        self.assertEqual(received["accepted"], 1)
        self.assertEqual(rotated["webhook_token"], "replacement-token")
        self.assertEqual(rejected.exception.status, 404)

    def test_legacy_processing_receipt_fails_closed(self):
        harness = _DeliveryHarness(route_count=1)
        raw = b'{"event":"legacy"}'
        headers = {"Idempotency-Key": "provider-delivery-legacy"}
        harness.durable.update({
            "_guid": "delivery-guid",
            "hook_guid": harness.hook["_guid"],
            "request_id": "delivery-legacy",
            "idempotency_key_hash": webhooks._idempotency_hash(headers),
            "payload_hash": webhooks._payload_hash(
                raw, "application/json"),
            "edge_guids": ["edge-1"],
            "status": "processing",
            "results": [],
        })

        with harness.patched(
                lambda *_args, **_kwargs:
                (_ for _ in ()).throw(
                    AssertionError("unsafe legacy receipt must not enqueue"))):
            with self.assertRaises(webhooks.WebhookError) as raised:
                webhooks.receive(
                    "token", raw, "application/json", headers)

        self.assertEqual(raised.exception.status, 503)


class DeliveryResponseTests(unittest.TestCase):
    def test_accepted_delivery_projection_preserves_only_safe_fields(self):
        response = webhooks._completed_response({
            "request_id": "delivery-123",
            "results": [{
                "edge_guid": "edge-123",
                "target": "worker_one",
                "accepted": True,
                "message_guid": "message-123",
                "status": "queued",
                "internal_detail": "/private/operator/path",
                "error": "https://internal.example.test/debug",
            }],
        })

        self.assertEqual(response["delivery_id"], "delivery-123")
        self.assertEqual(response["deliveries"], [{
            "edge_guid": "edge-123",
            "target": "worker_one",
            "accepted": True,
            "message_guid": "message-123",
            "status": "queued",
        }])

    def test_public_identifiers_drop_unsafe_or_unbounded_values(self):
        response = webhooks._completed_response({
            "request_id": "https://internal.example.test/?token=secret",
            "results": [{
                "edge_guid": "/Users/operator/private/edge",
                "target": "x" * 257,
                "accepted": True,
                "message_guid": "message with spaces",
                "status": "private_backend_status",
            }],
        })

        self.assertEqual(response["delivery_id"], "")
        self.assertEqual(response["deliveries"], [{
            "edge_guid": "",
            "target": "",
            "accepted": True,
            "message_guid": "",
            "status": "accepted",
        }])

    def test_enqueue_diagnostics_are_durable_but_never_public(self):
        private_path = "/Users/operator/private/crew-secrets.json"
        private_url = "https://internal.example.test/admin?token=secret"
        hook = {
            "_guid": "hook-guid",
            "name": "issues",
            "kind": gs.WEBHOOK_KIND,
            "webhook_template": "",
        }
        edges = [
            {
                "_guid": "edge-one",
                "source": hook["_guid"],
                "target": "target-one",
                "directed": True,
            },
            {
                "_guid": "edge-two",
                "source": hook["_guid"],
                "target": "target-two",
                "directed": True,
            },
        ]
        targets = {
            "target-one": {
                "_guid": "target-one",
                "name": "worker_one",
                "kind": "agent",
            },
            "target-two": {
                "_guid": "target-two",
                "name": "worker_two",
                "kind": "agent",
            },
        }
        durable = {}

        def list_objects(otype, **_kwargs):
            if otype == "edge":
                return {"objects": edges}
            if otype == "webhook_delivery":
                return {"objects": [dict(durable)] if durable else []}
            raise AssertionError(f"unexpected object type: {otype}")

        def create_object(otype, body):
            self.assertEqual(otype, "webhook_delivery")
            durable.update(body, _guid="delivery-guid")
            return dict(durable)

        def get_object(guid):
            if guid == "delivery-guid":
                return dict(durable)
            for edge in edges:
                if edge["_guid"] == guid:
                    return dict(edge)
            if guid in targets:
                return dict(targets[guid])
            raise AssertionError(f"unexpected guid: {guid}")

        def patch_object(otype, guid, body):
            self.assertEqual((otype, guid), (
                "webhook_delivery", "delivery-guid"))
            durable.update(body)
            return dict(durable)

        def enqueue(target, _message, **_kwargs):
            return False, (
                f"{target} policy detail at {private_path}; see {private_url}")

        headers = {"Idempotency-Key": "private-diagnostic-regression"}
        patches = (
            mock.patch.object(gs, "get_webhook_by_token", return_value=hook),
            mock.patch.object(gs, "list_objects", side_effect=list_objects),
            mock.patch.object(gs, "create_object", side_effect=create_object),
            mock.patch.object(gs, "get_object", side_effect=get_object),
            mock.patch.object(
                gs, "get_agent_by_name",
                side_effect=lambda name: next(
                    (dict(row) for row in targets.values()
                     if row["name"] == name),
                    None)),
            mock.patch.object(
                gs, "_patch_object_verified", side_effect=patch_object),
            mock.patch.object(
                gs, "_invariant_lock",
                side_effect=lambda _name: contextlib.nullcontext()),
            mock.patch.object(gs, "mark_webhook_called"),
            mock.patch("crew.webhooks.mail.enqueue", side_effect=enqueue),
        )
        with contextlib.ExitStack() as stack:
            for patcher in patches:
                stack.enter_context(patcher)
            initial = webhooks.receive(
                "token", b'{"message":"hello"}', "application/json",
                headers)
            retry = webhooks.receive(
                "token", b'{"message":"hello"}', "application/json",
                headers)

        self.assertFalse(initial["duplicate"])
        self.assertTrue(retry["duplicate"])
        self.assertEqual(initial["accepted"], 0)
        self.assertEqual(initial["rejected"], 2)
        self.assertEqual(initial["deliveries"], retry["deliveries"])
        for public in (initial, retry):
            serialized = json.dumps(public, sort_keys=True)
            self.assertNotIn(private_path, serialized)
            self.assertNotIn(private_url, serialized)
            for result in public["deliveries"]:
                self.assertFalse(result["accepted"])
                self.assertEqual(result["status"], "rejected")
                self.assertEqual(result["error"], {
                    "code": "route_delivery_failed",
                    "message": "Webhook route delivery failed.",
                })

        durable_diagnostics = json.dumps(
            durable["results"], sort_keys=True)
        self.assertIn(private_path, durable_diagnostics)
        self.assertIn(private_url, durable_diagnostics)


if __name__ == "__main__":
    unittest.main(verbosity=2)
