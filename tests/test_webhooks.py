"""Pure contracts for public webhook parsing, templating, and URL exposure."""
import os
import sys
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
        )
        for raw, media_type, phrase in cases:
            with self.subTest(raw=raw), self.assertRaisesRegex(
                    webhooks.WebhookError, phrase):
                webhooks.parse_payload(raw, media_type)


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
