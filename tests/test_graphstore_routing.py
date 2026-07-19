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


class TypedObjectRoutingTests(unittest.TestCase):
    def test_typed_get_uses_item_route_and_preserves_include(self):
        expected = {"_guid": "edge-guid", "_type": "edge"}
        typed_get = getattr(gs, "get_typed_object", None)
        if typed_get is None:
            self.fail(
                "graphstore must expose a typed object getter for mutation "
                "preflights")

        with mock.patch.object(gs, "_req", return_value=expected) as request:
            result = typed_get(
                "edge", "edge-guid", include="source,target")

        self.assertEqual(result, expected)
        request.assert_called_once_with(
            "GET", "/objects/edge/edge-guid?include=source%2Ctarget")

    def test_typed_get_rejects_a_mismatched_response_identity(self):
        with mock.patch.object(gs, "_req", return_value={
                "_guid": "agent-guid", "_type": "agent"}):
            with self.assertRaisesRegex(gs.GraphError, "type.*agent|agent.*edge"):
                gs.get_typed_object("edge", "agent-guid")

        with mock.patch.object(gs, "_req", return_value={
                "_guid": "different-guid", "_type": "edge"}):
            with self.assertRaisesRegex(gs.GraphError, "different-guid"):
                gs.get_typed_object("edge", "edge-guid")

    def test_typed_get_requires_complete_persisted_identity_metadata(self):
        for response in (None, {}, {"_guid": "edge-guid"},
                         {"_type": "edge"}):
            with self.subTest(response=response), \
                 mock.patch.object(gs, "_req", return_value=response), \
                 self.assertRaises(gs.GraphError):
                gs.get_typed_object("edge", "edge-guid")

    def test_public_delete_stops_when_typed_preflight_rejects_guid(self):
        mismatch = gs.GraphError(
            "404: Object 'agent-guid' is of type 'agent', not 'edge'.")

        def request(method, path, *args, **kwargs):
            if method == "GET":
                raise mismatch
            return {"deleted": "agent-guid"}

        error = None
        with mock.patch.object(gs, "_req", side_effect=request) as requested:
            try:
                gs.delete_object("edge", "agent-guid")
            except gs.GraphError as caught:
                error = caught

        with self.subTest("type mismatch propagates"):
            self.assertIs(error, mismatch)
        with self.subTest("DELETE is never sent"):
            self.assertEqual(
                requested.call_args_list,
                [mock.call("GET", "/objects/edge/agent-guid")],
            )

    def test_public_patch_stops_when_typed_preflight_rejects_guid(self):
        mismatch = gs.GraphError(
            "404: Object 'edge-guid' is of type 'edge', not 'agent'.")

        def request(method, path, *args, **kwargs):
            if method == "GET":
                raise mismatch
            return {"_guid": "edge-guid", "role": "must-not-land"}

        error = None
        with mock.patch.object(gs, "_req", side_effect=request) as requested:
            try:
                gs.patch_object(
                    "agent", "edge-guid", {"role": "must-not-land"})
            except gs.GraphError as caught:
                error = caught

        with self.subTest("type mismatch propagates"):
            self.assertIs(error, mismatch)
        with self.subTest("PATCH is never sent"):
            self.assertEqual(
                requested.call_args_list,
                [mock.call("GET", "/objects/agent/edge-guid")],
            )


class SchemaRegistrationRoutingTests(unittest.TestCase):
    def test_app_registration_explicitly_requests_no_tenant_header(self):
        with mock.patch.object(schema, "_req") as request:
            created = schema.ensure_app("crew-new")
        self.assertTrue(created)
        request.assert_called_once_with(
            "POST", "/app", {"key": "crew-new"}, app=None)


if __name__ == "__main__":
    unittest.main()
