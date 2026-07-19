"""Area A tests: crew.usage (transcript-based hourly spend metering) and
crew.notify (fire-and-forget webhook notifications).

Neither module talks to MorphDB or tmux, so no throwaway app / live server is
needed here — transcripts are fake .jsonl files under a tmp dir (usage.PROJECTS_DIR
is monkeypatched to point at it) and the webhook POST is a fake urllib.request.urlopen.

    python3 -m unittest tests.test_usage_notify   (from the repo root)
"""
import calendar
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from crew import config, notify as notify_mod, usage  # noqa: E402


def _write_jsonl(path, objs):
    with open(path, "w", encoding="utf-8") as fh:
        for o in objs:
            fh.write(json.dumps(o) + "\n")


def _iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(ts))


def _utc_ts(stamp):
    return calendar.timegm(time.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ"))


def _assistant_line(ts, model, inp=0, out=0, cache_write=0, cache_read=0, request_id=None):
    return {
        "type": "assistant",
        "timestamp": _iso(ts),
        "requestId": request_id,
        "message": {
            "model": model,
            "usage": {
                "input_tokens": inp,
                "output_tokens": out,
                "cache_creation_input_tokens": cache_write,
                "cache_read_input_tokens": cache_read,
            },
        },
    }


# --------------------------------------------------------------------------- #
# crew.usage — hourly_spend()
# --------------------------------------------------------------------------- #
class UsageFixture(unittest.TestCase):
    def setUp(self):
        self.projects_dir = tempfile.mkdtemp(prefix="crew_usage_projects_")
        self.addCleanup(shutil.rmtree, self.projects_dir, ignore_errors=True)
        p = mock.patch.object(usage, "PROJECTS_DIR", self.projects_dir)
        p.start()
        self.addCleanup(p.stop)
        self.home = tempfile.mkdtemp(prefix="crew_usage_home_")
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.pdir = os.path.join(self.projects_dir, usage._slug(self.home))
        os.makedirs(self.pdir, exist_ok=True)

    def _transcript(self, name, lines):
        path = os.path.join(self.pdir, name)
        _write_jsonl(path, lines)
        return path


class HourlySpendTests(UsageFixture):
    def test_empty_home_string_returns_zero_without_touching_disk(self):
        self.assertEqual(usage.hourly_spend("", time.time() - 3600), {"tokens": 0, "cost": 0.0})

    def test_missing_transcript_dir_fails_open(self):
        shutil.rmtree(self.pdir)
        r = usage.hourly_spend(self.home, time.time() - 3600)
        self.assertEqual(r, {"tokens": 0, "cost": 0.0})

    def test_sums_tokens_and_prices_a_known_model(self):
        now = _utc_ts("2026-07-19T12:00:00Z")
        self._transcript("a.jsonl", [
            _assistant_line(now - 60, "claude-sonnet-5-20260101",
                            inp=1000, out=500, request_id="r1"),
        ])
        r = usage.hourly_spend(self.home, 0)
        self.assertEqual(r["tokens"], 1500)
        expected_cost = (2.0 * 1000 + 10.0 * 500) / 1e6   # claude-sonnet-5 price row
        self.assertAlmostEqual(r["cost"], expected_cost, places=6)

    def test_excludes_lines_before_the_window(self):
        now = time.time()
        self._transcript("b.jsonl", [
            _assistant_line(now - 7200, "claude-sonnet-5", inp=100, out=100, request_id="old"),
        ])
        r = usage.hourly_spend(self.home, now - 3600)
        self.assertEqual(r, {"tokens": 0, "cost": 0.0})

    def test_dedupes_multiple_lines_sharing_a_request_id(self):
        now = time.time()
        self._transcript("c.jsonl", [
            _assistant_line(now - 30, "claude-sonnet-5", inp=100, out=50, request_id="dup"),
            _assistant_line(now - 20, "claude-sonnet-5", inp=100, out=50, request_id="dup"),
        ])
        r = usage.hourly_spend(self.home, now - 3600)
        self.assertEqual(r["tokens"], 150)   # counted once, not twice

    def test_unknown_model_counts_tokens_but_skips_cost(self):
        now = time.time()
        self._transcript("d.jsonl", [
            _assistant_line(now - 30, "some-unreleased-model-9000",
                            inp=100, out=50, request_id="u1"),
        ])
        r = usage.hourly_spend(self.home, now - 3600)
        self.assertEqual(r["tokens"], 150)
        self.assertEqual(r["cost"], 0.0)

    def test_malformed_json_line_fails_open(self):
        path = os.path.join(self.pdir, "e.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write('{"type": "assistant", this is not valid json\n')
        now = time.time()
        os.utime(path, (now, now))
        r = usage.hourly_spend(self.home, now - 3600)
        self.assertEqual(r, {"tokens": 0, "cost": 0.0})

    def test_non_assistant_lines_ignored(self):
        now = time.time()
        self._transcript("f.jsonl", [
            {"type": "user", "timestamp": _iso(now - 10), "message": {}},
        ])
        r = usage.hourly_spend(self.home, now - 3600)
        self.assertEqual(r, {"tokens": 0, "cost": 0.0})

    def test_cold_file_skipped_by_mtime_gate(self):
        # a transcript untouched since before the window can't hold an in-window
        # line, so hourly_spend must skip it WITHOUT even reading it — write lines
        # that WOULD count, then backdate the file's mtime past the window.
        now = time.time()
        path = self._transcript("g.jsonl", [
            _assistant_line(now - 10, "claude-sonnet-5", inp=999, out=999, request_id="cold"),
        ])
        old = now - 7200
        os.utime(path, (old, old))
        r = usage.hourly_spend(self.home, now - 3600)
        self.assertEqual(r, {"tokens": 0, "cost": 0.0})

    def test_non_jsonl_extension_ignored(self):
        now = time.time()
        path = os.path.join(self.pdir, "notes.txt")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(_assistant_line(now - 5, "claude-sonnet-5",
                                                inp=10, out=10, request_id="x")) + "\n")
        os.utime(path, (now, now))
        r = usage.hourly_spend(self.home, now - 3600)
        self.assertEqual(r, {"tokens": 0, "cost": 0.0})

    def test_price_matched_by_prefix_on_a_dated_model_id(self):
        now = time.time()
        self._transcript("h.jsonl", [
            _assistant_line(now - 5, "claude-opus-4-1-20250805", inp=10, out=10, request_id="pfx"),
        ])
        r = usage.hourly_spend(self.home, now - 3600)
        expected = (15.0 * 10 + 75.0 * 10) / 1e6   # claude-opus-4-1 price row
        self.assertAlmostEqual(r["cost"], expected, places=6)

    def test_cache_tokens_counted_and_priced_per_module_formula(self):
        now = _utc_ts("2026-07-19T12:00:00Z")
        self._transcript("i.jsonl", [
            _assistant_line(now - 5, "claude-sonnet-5", inp=0, out=0,
                            cache_write=1000, cache_read=1000, request_id="cache1"),
        ])
        r = usage.hourly_spend(self.home, 0)
        self.assertEqual(r["tokens"], 2000)
        # PRICES["claude-sonnet-5"] = (2.0, 10.0); formula: p[0]*(inp+1.25*cw+0.1*cr)+p[1]*out
        expected = (2.0 * (0 + 1.25 * 1000 + 0.1 * 1000) + 10.0 * 0) / 1e6
        self.assertAlmostEqual(r["cost"], expected, places=6)


class HourlyUsageAvailabilityTests(UsageFixture):
    """The policy-facing reader distinguishes measured zero from no meter."""

    def test_readable_empty_source_is_available_zero(self):
        reading = usage.hourly_usage(self.home, time.time() - 3600)
        self.assertEqual(reading["runtime"], "claude")
        self.assertEqual(reading["tokens"]["value"], 0)
        self.assertTrue(reading["tokens"]["available"])
        self.assertEqual(reading["cost"]["value"], 0.0)
        self.assertTrue(reading["cost"]["available"])

    def test_missing_source_is_unavailable_while_legacy_wrapper_stays_zero(self):
        shutil.rmtree(self.pdir)
        reading = usage.hourly_usage(self.home, time.time() - 3600)
        self.assertFalse(reading["tokens"]["available"])
        self.assertIsNone(reading["tokens"]["value"])
        self.assertIn("transcript", reading["tokens"]["reason"].lower())
        self.assertFalse(reading["cost"]["available"])
        self.assertEqual(
            usage.hourly_spend(self.home, time.time() - 3600),
            {"tokens": 0, "cost": 0.0},
        )

    def test_unreadable_source_is_unavailable(self):
        self._transcript("blocked.jsonl", [])
        with mock.patch("builtins.open", side_effect=OSError("permission denied")):
            reading = usage.hourly_usage(self.home, time.time() - 3600)
        self.assertFalse(reading["tokens"]["available"])
        self.assertIn("unreadable", reading["tokens"]["reason"].lower())
        self.assertFalse(reading["cost"]["available"])

    def test_fully_malformed_source_is_unavailable(self):
        path = os.path.join(self.pdir, "broken.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write('{"type": "assistant", not-json\n')
        now = time.time()
        os.utime(path, (now, now))
        reading = usage.hourly_usage(self.home, now - 3600)
        self.assertFalse(reading["tokens"]["available"])
        self.assertIn("valid", reading["tokens"]["reason"].lower())
        self.assertFalse(reading["cost"]["available"])

    def test_malformed_json_poison_mixed_valid_usage(self):
        now = time.time()
        path = self._transcript("mixed-malformed.jsonl", [
            _assistant_line(
                now - 10, "claude-sonnet-5", inp=100, out=50,
                request_id="valid-before-malformed"),
        ])
        with open(path, "a", encoding="utf-8") as fh:
            fh.write('{"type": "assistant", not-json\n')

        reading = usage.hourly_usage(self.home, now - 3600)

        self.assertFalse(reading["tokens"]["available"])
        self.assertFalse(reading["cost"]["available"])
        self.assertIn("malformed json", reading["tokens"]["reason"].lower())

    def test_invalid_assistant_timestamp_poison_mixed_valid_usage(self):
        now = time.time()
        invalid_timestamp = _assistant_line(
            now - 5, "claude-sonnet-5", inp=25, out=10,
            request_id="invalid-timestamp")
        invalid_timestamp["timestamp"] = "not-a-timestamp"
        self._transcript("mixed-timestamp.jsonl", [
            _assistant_line(
                now - 10, "claude-sonnet-5", inp=100, out=50,
                request_id="valid-before-invalid-timestamp"),
            invalid_timestamp,
        ])

        reading = usage.hourly_usage(self.home, now - 3600)

        self.assertFalse(reading["tokens"]["available"])
        self.assertFalse(reading["cost"]["available"])
        self.assertIn("timestamp", reading["tokens"]["reason"].lower())

    def test_assistant_record_missing_usage_block_is_unavailable(self):
        now = time.time()
        record = _assistant_line(
            now - 5, "claude-sonnet-5", inp=10, out=5, request_id="missing")
        del record["message"]["usage"]
        self._transcript("missing-usage.jsonl", [record])
        reading = usage.hourly_usage(self.home, now - 3600)
        self.assertFalse(reading["tokens"]["available"])
        self.assertFalse(reading["cost"]["available"])
        self.assertIn("usage", reading["tokens"]["reason"].lower())

    def test_assistant_record_empty_usage_block_is_unavailable(self):
        now = time.time()
        record = _assistant_line(
            now - 5, "claude-sonnet-5", request_id="empty")
        record["message"]["usage"] = {}
        self._transcript("empty-usage.jsonl", [record])
        reading = usage.hourly_usage(self.home, now - 3600)
        self.assertFalse(reading["tokens"]["available"])
        self.assertFalse(reading["cost"]["available"])

    def test_partial_usage_schema_is_unavailable_not_a_partial_total(self):
        now = time.time()
        record = _assistant_line(
            now - 5, "claude-sonnet-5", inp=10, out=5, request_id="partial")
        del record["message"]["usage"]["cache_read_input_tokens"]
        self._transcript("partial-usage.jsonl", [record])
        reading = usage.hourly_usage(self.home, now - 3600)
        self.assertFalse(reading["tokens"]["available"])
        self.assertFalse(reading["cost"]["available"])
        self.assertIn("cache_read_input_tokens", reading["tokens"]["reason"])

    def test_mixed_valid_and_schema_drifted_records_are_unavailable(self):
        now = time.time()
        valid = _assistant_line(
            now - 10, "claude-sonnet-5", inp=100, out=50,
            request_id="mixed-valid")
        drifted = _assistant_line(
            now - 5, "claude-sonnet-5", inp=25, out=10,
            request_id="mixed-drift")
        drifted["message"]["usage"]["output_tokens"] = None
        self._transcript("mixed-usage.jsonl", [valid, drifted])
        reading = usage.hourly_usage(self.home, now - 3600)
        self.assertFalse(reading["tokens"]["available"])
        self.assertIsNone(reading["tokens"]["value"])
        self.assertFalse(reading["cost"]["available"])

    def test_complete_all_zero_usage_record_is_available_zero(self):
        now = time.time()
        self._transcript("zero-usage.jsonl", [
            _assistant_line(
                now - 5, "claude-sonnet-5", request_id="measured-zero"),
        ])
        reading = usage.hourly_usage(self.home, now - 3600)
        self.assertEqual(reading["tokens"], {
            "available": True, "value": 0, "reason": ""})
        self.assertEqual(reading["cost"], {
            "available": True, "value": 0.0, "reason": ""})

    def test_schema_drift_before_the_window_does_not_poison_current_usage(self):
        now = time.time()
        old = _assistant_line(
            now - 7200, "claude-sonnet-5", inp=10, out=5,
            request_id="old-drift")
        old["message"]["usage"] = {}
        current = _assistant_line(
            now - 5, "claude-sonnet-5", inp=20, out=5,
            request_id="current-valid")
        self._transcript("old-drift.jsonl", [old, current])
        reading = usage.hourly_usage(self.home, now - 3600)
        self.assertEqual(reading["tokens"], {
            "available": True, "value": 25, "reason": ""})
        self.assertTrue(reading["cost"]["available"])

    def test_known_model_has_available_tokens_and_cost(self):
        now = time.time()
        self._transcript("known.jsonl", [
            _assistant_line(now - 5, "claude-sonnet-5", inp=100, out=50,
                            request_id="known"),
        ])
        reading = usage.hourly_usage(self.home, now - 3600)
        self.assertEqual(reading["tokens"], {
            "available": True, "value": 150, "reason": ""})
        self.assertTrue(reading["cost"]["available"])
        self.assertGreater(reading["cost"]["value"], 0)

    def test_sonnet5_cost_uses_each_records_utc_pricing_period(self):
        intro = _utc_ts("2026-08-31T23:59:59Z")
        standard = _utc_ts("2026-09-01T00:00:00Z")
        self._transcript("sonnet5-price-boundary.jsonl", [
            _assistant_line(
                intro, "claude-sonnet-5", inp=1_000_000, out=1_000_000,
                request_id="sonnet5-intro-last-second"),
            _assistant_line(
                standard, "claude-sonnet-5", inp=1_000_000, out=1_000_000,
                request_id="sonnet5-standard-first-second"),
        ])

        reading = usage.hourly_usage(self.home, 0)

        self.assertTrue(reading["cost"]["available"])
        self.assertEqual(reading["tokens"]["value"], 4_000_000)
        # Intro record: $2 input + $10 output. Standard record: $3 + $15.
        self.assertAlmostEqual(reading["cost"]["value"], 30.0, places=9)

    def test_unknown_model_has_tokens_but_cost_is_unavailable(self):
        now = time.time()
        self._transcript("unknown.jsonl", [
            _assistant_line(now - 5, "claude-future-unknown", inp=100, out=50,
                            request_id="unknown"),
        ])
        reading = usage.hourly_usage(self.home, now - 3600)
        self.assertEqual(reading["tokens"], {
            "available": True, "value": 150, "reason": ""})
        self.assertFalse(reading["cost"]["available"])
        self.assertIsNone(reading["cost"]["value"])
        self.assertIn("unknown", reading["cost"]["reason"].lower())

    def test_codex_and_custom_runtime_usage_are_unavailable(self):
        for runtime_key in ("codex", "custom"):
            with self.subTest(runtime=runtime_key):
                reading = usage.hourly_usage(
                    self.home, time.time() - 3600, runtime_key=runtime_key)
                self.assertFalse(reading["tokens"]["available"])
                self.assertFalse(reading["cost"]["available"])
                self.assertIn(runtime_key, reading["tokens"]["reason"])


class SlugTests(unittest.TestCase):
    def test_replaces_non_alnum_characters(self):
        s = usage._slug("/tmp/some path/with.dots")
        self.assertNotIn("/", s)
        self.assertNotIn(".", s)
        self.assertNotIn(" ", s)

    def test_same_home_always_slugs_the_same(self):
        self.assertEqual(usage._slug("/tmp/x/y"), usage._slug("/tmp/x/y"))


class UsageValueValidationTests(unittest.TestCase):
    def test_usage_counters_require_json_integers(self):
        for value in (True, False, 1.5, 1.0, "7"):
            with self.subTest(value=value):
                values, reason = usage._parse_usage_block({
                    "input_tokens": value,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                    "output_tokens": 0,
                })
                self.assertIsNone(values)
                self.assertIn("integer", reason)

    def test_complete_integer_zero_usage_remains_valid(self):
        values, reason = usage._parse_usage_block({
            "input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "output_tokens": 0,
        })
        self.assertEqual(values, (0, 0, 0, 0))
        self.assertEqual(reason, "")


class PriceFamilyTests(unittest.TestCase):
    def test_exact_and_hyphen_delimited_model_family_match(self):
        intro = _utc_ts("2026-07-19T12:00:00Z")
        self.assertEqual(usage._price("claude-sonnet-5", intro), (2.0, 10.0))
        self.assertEqual(
            usage._price("claude-sonnet-5-20260101", intro),
            (2.0, 10.0),
        )

    def test_lookalike_model_prefix_does_not_inherit_price(self):
        self.assertIsNone(usage._price(
            "claude-sonnet-50-future", _utc_ts("2026-07-19T12:00:00Z")))

    def test_sonnet5_price_changes_at_september_utc_boundary(self):
        self.assertEqual(
            usage._price(
                "claude-sonnet-5", _utc_ts("2026-08-31T23:59:59Z")),
            (2.0, 10.0),
        )
        self.assertEqual(
            usage._price(
                "claude-sonnet-5", _utc_ts("2026-09-01T00:00:00Z")),
            (3.0, 15.0),
        )


class TsTests(unittest.TestCase):
    def test_parses_iso_z_timestamp(self):
        ts = usage._ts("2026-07-17T12:00:00.123Z")
        self.assertGreater(ts, 0)

    def test_malformed_timestamp_fails_open_to_zero(self):
        self.assertEqual(usage._ts("not-a-timestamp"), 0)
        self.assertEqual(usage._ts(None), 0)


# --------------------------------------------------------------------------- #
# crew.notify — notify()
# --------------------------------------------------------------------------- #
class NotifyTests(unittest.TestCase):
    def setUp(self):
        self._orig_urlopen = notify_mod.urllib.request.urlopen
        self.addCleanup(setattr, notify_mod.urllib.request, "urlopen", self._orig_urlopen)
        self.calls = []

        class _Resp:
            def close(self_inner):
                pass

        def fake_urlopen(req, timeout=None):
            self.calls.append(req)
            return _Resp()

        notify_mod.urllib.request.urlopen = fake_urlopen

        # Isolate from whatever the real environment/config happens to carry.
        env_patch = mock.patch.dict(os.environ, {})
        env_patch.start()
        self.addCleanup(env_patch.stop)
        os.environ.pop("CREW_WEBHOOK_URL", None)
        os.environ.pop("CREW_WEBHOOK_FORMAT", None)

        self._orig_webhook_url = config.WEBHOOK_URL
        config.WEBHOOK_URL = ""
        self.addCleanup(setattr, config, "WEBHOOK_URL", self._orig_webhook_url)

    def test_no_url_configured_is_a_noop(self):
        notify_mod.notify("agent_down", "builder", "x")
        self.assertEqual(self.calls, [])

    def test_ntfy_host_gets_plain_text_body_and_title_header(self):
        os.environ["CREW_WEBHOOK_URL"] = "https://ntfy.sh/demo-topic"
        notify_mod.notify("agent_down", "builder", "session died")
        self.assertEqual(len(self.calls), 1)
        req = self.calls[0]
        self.assertEqual(req.data, b"builder: session died")
        self.assertEqual(req.get_header("Title"), "crew: agent_down")

    def test_generic_host_gets_json_body(self):
        os.environ["CREW_WEBHOOK_URL"] = "https://hooks.example.com/w"
        notify_mod.notify("needs_input", "leads", "permission prompt")
        req = self.calls[0]
        body = json.loads(req.data.decode())
        self.assertEqual(body["event"], "needs_input")
        self.assertEqual(body["agent"], "leads")
        self.assertEqual(body["detail"], "permission prompt")
        self.assertIsInstance(body["ts"], int)
        self.assertEqual(req.get_header("Content-type"), "application/json")

    def test_webhook_format_env_forces_ntfy_shape_on_a_non_ntfy_host(self):
        os.environ["CREW_WEBHOOK_URL"] = "https://hooks.example.com/w"
        os.environ["CREW_WEBHOOK_FORMAT"] = "ntfy"
        notify_mod.notify("agent_down", "sales", "down")
        req = self.calls[0]
        self.assertEqual(req.data, b"sales: down")
        self.assertEqual(req.get_header("Title"), "crew: agent_down")

    def test_env_url_takes_priority_over_config_url(self):
        config.WEBHOOK_URL = "https://hooks.example.com/from-config"
        os.environ["CREW_WEBHOOK_URL"] = "https://hooks.example.com/from-env"
        notify_mod.notify("agent_down", "sales", "down")
        req = self.calls[0]
        self.assertEqual(req.full_url, "https://hooks.example.com/from-env")

    def test_falls_back_to_config_url_when_env_unset(self):
        config.WEBHOOK_URL = "https://hooks.example.com/from-config"
        notify_mod.notify("agent_down", "sales", "down")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0].full_url, "https://hooks.example.com/from-config")

    def test_urlopen_failure_is_swallowed_not_raised(self):
        def boom(req, timeout=None):
            raise OSError("connection refused")
        notify_mod.urllib.request.urlopen = boom
        os.environ["CREW_WEBHOOK_URL"] = "https://hooks.example.com/w"
        try:
            notify_mod.notify("agent_down", "sales", "down")
        except Exception as e:   # pragma: no cover - failure path
            self.fail(f"notify() must never raise, but raised: {e}")

    def test_http_error_response_is_closed_when_webhook_fails(self):
        error = urllib.error.HTTPError(
            "https://hooks.example.com/w",
            500,
            "server error",
            {},
            io.BytesIO(b'{"error":"broken"}'),
        )
        self.addCleanup(error.close)
        notify_mod.urllib.request.urlopen = mock.Mock(side_effect=error)
        os.environ["CREW_WEBHOOK_URL"] = "https://hooks.example.com/w"

        notify_mod.notify("agent_down", "sales", "down")

        self.assertTrue(error.closed)


if __name__ == "__main__":
    unittest.main(verbosity=2)
