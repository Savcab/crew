"""Low-level MorphDB requests keep explicit tenant routing exact."""
import io
import json
import os
import sys
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from crew import graphstore as gs, schema  # noqa: E402


class _Response:
    def __init__(self, body=None):
        self.body = json.dumps(body).encode() if body is not None else b""

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.body


def _schema_drift_error():
    body = {"error": {"message": "Unknown field. Update the schema first"}}
    return urllib.error.HTTPError(
        "http://morph.test/objects/agent", 400, "Bad Request", {},
        io.BytesIO(json.dumps(body).encode()))


def _not_found_error():
    body = {"error": {"message": "Unknown app"}}
    return urllib.error.HTTPError(
        "http://morph.test/objects/agent", 404, "Not Found", {},
        io.BytesIO(json.dumps(body).encode()))


class RequestAppRoutingTests(unittest.TestCase):
    def setUp(self):
        gs._req._healing = False

    def tearDown(self):
        gs._req._healing = False

    def _request_header(self, *, app_marker="omitted"):
        with mock.patch.object(gs.config, "morphdb_base",
                               return_value="http://morph.test"), \
             mock.patch.object(gs.config, "current_app",
                               return_value="crew-current"), \
             mock.patch.object(gs.urllib.request, "urlopen",
                               return_value=_Response({"objects": []})) as opened:
            if app_marker == "omitted":
                gs._req("GET", "/objects/agent")
            else:
                gs._req("GET", "/objects/agent", app=app_marker)
        request = opened.call_args.args[0]
        return request.get_header("X-app-key")

    def test_omitted_app_uses_current_tenant(self):
        self.assertEqual(self._request_header(), "crew-current")

    def test_explicit_app_uses_that_tenant(self):
        self.assertEqual(self._request_header(app_marker="crew-other"),
                         "crew-other")

    def test_explicit_none_omits_tenant_header(self):
        self.assertIsNone(self._request_header(app_marker=None))

    def test_default_list_objects_still_uses_current_tenant(self):
        with mock.patch.object(gs.config, "morphdb_base",
                               return_value="http://morph.test"), \
             mock.patch.object(gs.config, "current_app",
                               return_value="crew-current"), \
             mock.patch.object(gs.urllib.request, "urlopen",
                               return_value=_Response({"objects": []})) as opened:
            gs.list_objects("agent")
        self.assertEqual(opened.call_args.args[0].get_header("X-app-key"),
                         "crew-current")

    def test_explicit_app_schema_drift_repairs_and_retries_same_tenant(self):
        drift = _schema_drift_error()
        opened = mock.Mock(side_effect=[drift, _Response({"ok": True})])
        with mock.patch.object(gs.config, "morphdb_base",
                               return_value="http://morph.test"), \
             mock.patch.object(gs.urllib.request, "urlopen", opened), \
             mock.patch.object(schema, "ensure_schema") as ensure:
            result = gs._req("POST", "/objects/agent", {"name": "x"},
                             app="crew-other")

        self.assertEqual(result, {"ok": True})
        ensure.assert_called_once_with("crew-other")
        self.assertEqual(len(opened.call_args_list), 2)
        for call in opened.call_args_list:
            self.assertEqual(call.args[0].get_header("X-app-key"),
                             "crew-other")
        self.assertTrue(drift.closed)

    def test_http_error_response_is_closed_after_graph_error(self):
        error = _not_found_error()
        with mock.patch.object(gs.config, "morphdb_base",
                               return_value="http://morph.test"), \
             mock.patch.object(gs.urllib.request, "urlopen", side_effect=error):
            with self.assertRaisesRegex(gs.GraphError, "404: Unknown app"):
                gs._req("GET", "/objects/agent", app="missing-app")
        self.assertTrue(error.closed)


class SchemaRegistrationRoutingTests(unittest.TestCase):
    def test_app_registration_explicitly_requests_no_tenant_header(self):
        with mock.patch.object(schema, "_req") as request:
            created = schema.ensure_app("crew-new")
        self.assertTrue(created)
        request.assert_called_once_with(
            "POST", "/app", {"key": "crew-new"}, app=None)


if __name__ == "__main__":
    unittest.main()
