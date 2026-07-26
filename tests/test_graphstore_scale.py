"""Whole-graph scans, name ambiguity, and per-request schema healing.

Three QA findings share one shape — a read that answers a semantic question
("all agents", "the agent named X", "is the schema current?") while silently
looking at part of the truth:

  * fixed-limit list helpers treat the FIRST page as the entire graph, so the
    foreman singleton, home-overlap, cascade-delete and spawn-quota decisions
    are made on a partial view;
  * a duplicated node name resolves to whichever row MorphDB returns first
    instead of failing closed;
  * the ``_req`` self-heal used a process-global flag, so a concurrent writer
    saw another thread's heal as its own and leaked a false failure — and the
    heal ran data migrations that take graph locks from inside graph locks.

MorphDB is faked in-process here on purpose: the interesting sizes (1001
agents, 2001 edges) are page-boundary behavior, not storage behavior, and a
live fixture at that size costs minutes per run.  Live write coverage lives in
tests/live_smoke.py; the deletion-vs-migration race runs against a real
private MorphDB in tests/test_graphstore_races.py.

    python3 -m unittest tests.test_graphstore_scale   (from the repo root)
"""
import contextlib
import email.message
import io
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from crew import cli, graphstore as gs, schema, webhooks  # noqa: E402


class FakeMorphDB:
    """An in-process object store with MorphDB's paging contract."""

    def __init__(self):
        self.rows = []

    def add(self, otype, guid=None, **fields):
        row = {"_guid": guid or f"{otype}_{len(self.rows):06d}", "_type": otype}
        row.update(fields)
        self.rows.append(row)
        return row

    def list_objects(self, otype, include=None, sort=None, order=None,
                     limit=None, offset=None, app=None, **filters):
        matched = [
            row for row in self.rows
            if row["_type"] == otype
            and all(row.get(key) == value for key, value in filters.items())
        ]
        if sort:
            matched.sort(
                key=lambda row: (row.get(sort) is None, row.get(sort) or 0),
                reverse=(order == "desc"))
        start = int(offset or 0)
        page = matched[start:start + int(limit)] if limit else matched[start:]
        return {"objects": page, "total": len(matched),
                "limit": limit, "offset": start}


class FakeBackendTestCase(unittest.TestCase):
    def setUp(self):
        self.db = FakeMorphDB()
        patcher = mock.patch.object(gs, "list_objects", self.db.list_objects)
        patcher.start()
        self.addCleanup(patcher.stop)


class WholeGraphScanTests(FakeBackendTestCase):
    def test_list_agents_returns_every_row_past_one_page(self):
        for index in range(1001):
            self.db.add("agent", name=f"a{index}", created_at=1700000000 + index)

        rows = gs.list_agents()

        self.assertEqual(len(rows), 1001)
        self.assertEqual(len({row["_guid"] for row in rows}), 1001)

    def test_list_nodes_keeps_webhooks_out_of_the_agent_view(self):
        for index in range(1001):
            self.db.add("agent", name=f"a{index}", created_at=1700000000 + index)
        self.db.add("agent", name="hook", kind=gs.WEBHOOK_KIND,
                    created_at=1700009999)

        self.assertEqual(len(gs.list_nodes()), 1002)
        self.assertEqual(len(gs.list_agents()), 1001)

    def test_list_edges_returns_every_row_past_two_pages(self):
        for index in range(2001):
            self.db.add("edge", source="agent_a", target="agent_b",
                        created_at=1700000000 + index)

        self.assertEqual(len(gs.list_edges()), 2001)

    def test_edges_touching_returns_every_incident_edge(self):
        for index in range(2001):
            self.db.add("edge", source="doomed", target=f"peer{index}",
                        created_at=1700000000 + index)
            self.db.add("edge", source=f"peer{index}", target="doomed",
                        created_at=1700000000 + index)

        edges = gs.edges_touching("doomed")

        self.assertEqual(len(edges), 4002)
        self.assertEqual(len({edge["_guid"] for edge in edges}), 4002)

    def test_edges_from_to_returns_every_duplicate_pair_edge(self):
        for index in range(51):
            self.db.add("edge", source="agent_a", target="agent_b",
                        created_at=1700000000 + index)

        self.assertEqual(len(gs.edges_from_to("agent_a", "agent_b")), 51)

    def test_neighbor_scan_sees_every_authorizing_edge(self):
        for index in range(2001):
            self.db.add("edge", source="me", target=f"peer{index}",
                        directed=True, created_at=1700000000 + index)

        self.assertEqual(len(gs._neighbors("me", "source", "target")), 2001)

    def test_webhook_fanout_sees_every_route(self):
        for index in range(2001):
            self.db.add("edge", source="hook", target=f"peer{index}",
                        directed=True, created_at=1700000000 + index)

        self.assertEqual(len(webhooks._outgoing_edges("hook")), 2001)

    def test_short_page_against_a_larger_total_is_a_loud_error(self):
        with mock.patch.object(
                gs, "list_objects",
                lambda *args, **kwargs: {"objects": [], "total": 5}):
            with self.assertRaisesRegex(gs.GraphError, "incomplete"):
                gs.list_agents()

    def test_missing_total_is_a_loud_error(self):
        with mock.patch.object(
                gs, "list_objects",
                lambda *args, **kwargs: {"objects": [{"_guid": "agent_x"}]}):
            with self.assertRaisesRegex(gs.GraphError, "invalid"):
                gs.list_agents()


class DuplicateNameTests(FakeBackendTestCase):
    def test_duplicate_node_name_fails_closed_everywhere(self):
        self.db.add("agent", name="duplicate", created_at=1700000000)
        self.db.add("agent", name="duplicate", created_at=1700000001)

        for lookup in (gs.get_node_by_name, gs.get_agent_by_name,
                       gs.get_webhook_by_name):
            with self.assertRaisesRegex(gs.GraphError, "ambiguous"):
                lookup("duplicate")

    def test_unique_name_still_resolves(self):
        self.db.add("agent", name="solo", created_at=1700000000)
        self.db.add("agent", name="other", created_at=1700000001)

        self.assertEqual(gs.get_agent_by_name("solo")["name"], "solo")
        self.assertIsNone(gs.get_webhook_by_name("solo"))

    def test_absent_and_blank_names_stay_none(self):
        self.assertIsNone(gs.get_node_by_name("nobody"))
        self.assertIsNone(gs.get_node_by_name(""))
        self.assertIsNone(gs.get_node_by_name(None))


class PendingPrefixTests(FakeBackendTestCase):
    def _fill(self, count=1001):
        for index in range(count):
            self.db.add("graph_edit", result="pending", created_order=index,
                        actor="human", op="connect")

    def test_resolves_a_pending_row_past_the_first_page(self):
        self._fill()
        oldest = self.db.rows[0]

        row = cli._resolve_pending(oldest["_guid"])

        self.assertEqual(row["_guid"], oldest["_guid"])

    def test_ambiguous_prefix_is_detected_across_pages(self):
        self.db.add("graph_edit", guid="pendingdupe-a", result="pending",
                    created_order=10_000)
        self._fill()
        self.db.add("graph_edit", guid="pendingdupe-b", result="pending",
                    created_order=-1)

        with self.assertRaisesRegex(gs.GraphError, "ambiguous"):
            cli._resolve_pending("pendingdupe-")


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class SchemaHealTests(unittest.TestCase):
    """`_req` heals schema drift; healing is per-thread and lock-free.

    The fake MorphDB refuses object writes with MorphDB's real drift message
    until a schema push lands, and always accepts schema/app calls — so the
    genuine `crew.schema` push path runs, and a lock it should not take would
    hang the test rather than pass it.
    """

    def setUp(self):
        self.healed = False
        self.migrations = []
        self.lock = threading.Lock()
        self.barrier = None
        lock_dir = tempfile.mkdtemp(prefix="crew-heal-locks-")
        self.addCleanup(shutil.rmtree, lock_dir, ignore_errors=True)
        patcher = mock.patch.object(gs, "_INVARIANT_LOCK_DIR", lock_dir)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _urlopen(self, request, timeout=None):
        path = urllib.parse.urlsplit(request.full_url).path
        if path.startswith("/schema/") or path == "/app":
            with self.lock:
                self.healed = True
            return _FakeResponse(b'{"ok": true}')
        with self.lock:
            healed = self.healed
        if healed:
            return _FakeResponse(b'{"ok": true}')
        if self.barrier is not None:
            self.barrier.wait(timeout=5)
        raise urllib.error.HTTPError(
            request.full_url, 400, "Bad Request", email.message.Message(),
            io.BytesIO(b'{"error":{"message":"Update the schema first"}}'))

    @contextlib.contextmanager
    def _drifting_backend(self, threads=1, record_migrations=False):
        self.barrier = threading.Barrier(threads) if threads > 1 else None
        with contextlib.ExitStack() as stack:
            stack.enter_context(
                mock.patch.object(gs.urllib.request, "urlopen", self._urlopen))
            if record_migrations:
                stack.enter_context(mock.patch.object(
                    schema, "ensure_schema", self.migrations.append))
            yield

    def test_concurrent_writers_each_heal_instead_of_failing(self):
        results = {}

        def write(index):
            try:
                results[index] = gs._req(
                    "PATCH", "/objects/agent/agent_x", {"notes": "n"},
                    app="heal-test")
            except BaseException as error:  # reported, not raised, per thread
                results[index] = error

        with self._drifting_backend(threads=2):
            workers = [threading.Thread(target=write, args=(index,),
                                        daemon=True)
                       for index in range(2)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(timeout=10)

        self.assertEqual([worker.is_alive() for worker in workers],
                         [False, False])
        self.assertEqual(results, {0: {"ok": True}, 1: {"ok": True}})

    def test_healing_pushes_schema_without_running_data_migrations(self):
        with self._drifting_backend(record_migrations=True):
            self.assertEqual(
                gs._req("POST", "/objects/agent", {"name": "n"},
                        app="heal-test"),
                {"ok": True})

        self.assertEqual(self.migrations, [])

    def test_healing_inside_a_graph_lock_does_not_deadlock(self):
        # create_agent/update_agent call _req while holding the agent lock, so
        # a heal that takes any graph lock of its own self-deadlocks: flock is
        # per open file description, not reentrant within a process.
        outcome = {}

        def write():
            try:
                with gs._invariant_lock("agent", app="heal-deadlock-test"):
                    outcome["value"] = gs._req(
                        "POST", "/objects/agent", {"name": "n"},
                        app="heal-deadlock-test")
            except BaseException as error:
                outcome["error"] = error

        with self._drifting_backend():
            worker = threading.Thread(target=write, daemon=True)
            worker.start()
            worker.join(timeout=10)

        self.assertFalse(worker.is_alive(),
                         "schema heal deadlocked against a held graph lock")
        self.assertEqual(outcome.get("value"), {"ok": True}, outcome)


class SchemaMigrationLockTests(unittest.TestCase):
    def test_push_schema_takes_no_graph_lock(self):
        def refuse(scope, app=None):
            raise AssertionError(
                f"push_schema must not take the {scope!r} graph lock")

        with mock.patch.object(schema, "_req", lambda *a, **k: {}), \
                mock.patch.object(schema, "_invariant_lock", refuse):
            self.assertEqual(schema.push_schema("scale-test-app"),
                             "scale-test-app")

    def test_ownership_backfill_holds_the_canonical_mutation_locks(self):
        scopes = []

        @contextlib.contextmanager
        def record(scope, app=None):
            scopes.append(scope)
            yield

        with mock.patch.object(schema, "_invariant_lock", record), \
                mock.patch.object(schema, "_all_objects",
                                  lambda app, otype: []):
            schema._backfill_legacy_creator_guids("scale-test-app")

        self.assertEqual(scopes, ["agent", "edge-authorization"])


if __name__ == "__main__":
    unittest.main()
