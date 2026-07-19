"""Cross-process graph invariant tests against a private MorphDB process.

The graphstore invariants are stronger than MorphDB's generic field indexes:
agent names are unique within a Crew app, and an ordered sender->target pair is
authorized by at most one edge.  These tests deliberately widen the interval
between the invariant read and write, then race separate Python processes.

No shared/default MorphDB is used.  The module starts a temporary server on an
ephemeral port backed by a temporary SQLite file and removes the whole fixture
afterward.
"""
import multiprocessing
import os
import pathlib
import re
import socket
import subprocess
import tempfile
import time
import unittest
import urllib.error
import urllib.request
import uuid
from unittest import mock

from crew import config, graphstore as gs, schema, spawn as crew_spawn


class InvariantLockFilesystemSafetyTests(unittest.TestCase):
    def test_production_graph_locks_live_outside_repo_var(self):
        lock_dir = os.path.realpath(gs._INVARIANT_LOCK_DIR)
        repo_var = os.path.realpath(config.VAR)

        self.assertNotEqual(
            os.path.commonpath((lock_dir, repo_var)), repo_var)
        self.assertEqual(
            os.path.commonpath((lock_dir, os.path.realpath("/tmp"))),
            os.path.realpath("/tmp"))

    def test_permission_failure_is_wrapped_as_graph_error(self):
        with tempfile.TemporaryDirectory(prefix="crew-lock-permission-") as root, \
             mock.patch.object(
                 gs, "_INVARIANT_LOCK_DIR", os.path.join(root, "locks")), \
             mock.patch.object(
                 gs.os, "open", side_effect=PermissionError("sandbox denied")):
            with self.assertRaisesRegex(gs.GraphError, "lock|sandbox denied"):
                with gs._invariant_lock("agent", app="permission-test"):
                    pass

    def test_symlink_lock_directory_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix="crew-lock-symlink-") as root:
            target = os.path.join(root, "target")
            os.mkdir(target)
            link = os.path.join(root, "locks")
            os.symlink(target, link)
            with mock.patch.object(gs, "_INVARIANT_LOCK_DIR", link):
                with self.assertRaisesRegex(gs.GraphError, "symlink|lock"):
                    with gs._invariant_lock("agent", app="symlink-test"):
                        pass

    def test_symlink_lock_file_is_rejected_without_touching_target(self):
        with tempfile.TemporaryDirectory(prefix="crew-lock-file-symlink-") as root:
            lock_dir = os.path.join(root, "locks")
            os.mkdir(lock_dir, 0o700)
            victim = os.path.join(root, "victim")
            with open(victim, "w") as stream:
                stream.write("do-not-touch")
            with mock.patch.object(gs, "_INVARIANT_LOCK_DIR", lock_dir):
                lock_path = gs._invariant_lock_path(
                    "agent", app="symlink-file-test")
                os.symlink(victim, lock_path)
                with self.assertRaisesRegex(gs.GraphError, "lock"):
                    with gs._invariant_lock(
                            "agent", app="symlink-file-test"):
                        pass
            with open(victim) as stream:
                self.assertEqual(stream.read(), "do-not-touch")


def _configure_worker(host, app):
    """Point a spawned worker at this module's private MorphDB fixture."""
    config.MORPHDB_HOST = host
    os.environ["CREW_APP"] = app
    os.environ.pop("CREW_PROJECT", None)
    gs._INVARIANT_LOCK_DIR = os.environ["CREW_TEST_GRAPH_LOCK_DIR"]


def _slow_create_worker(host, app, barrier, results, operation, args):
    """Race a create after widening graphstore's check->POST interval."""
    _configure_worker(host, app)
    original = gs.create_object

    def slow_create(object_type, body):
        if object_type == operation:
            time.sleep(0.35)
        return original(object_type, body)

    gs.create_object = slow_create
    try:
        barrier.wait(timeout=10)
        if operation == "agent":
            gs.create_agent(*args)
        else:
            gs.create_edge(*args)
        results.put(("ok", ""))
    except Exception as error:  # serialized result is easier to diagnose
        results.put(("error", f"{type(error).__name__}: {error}"))


def _edge_update_or_create_worker(host, app, barrier, results, operation, args):
    """Race an edge PATCH that expands auth against a conflicting edge POST."""
    _configure_worker(host, app)
    original_create = gs.create_object
    original_patch = gs._patch_object_unchecked

    def slow_create(object_type, body):
        if object_type == "edge":
            time.sleep(0.35)
        return original_create(object_type, body)

    def slow_patch(object_type, guid, body):
        if object_type == "edge":
            time.sleep(0.35)
        return original_patch(object_type, guid, body)

    gs.create_object = slow_create
    gs._patch_object_unchecked = slow_patch
    try:
        barrier.wait(timeout=10)
        if operation == "update":
            gs.update_edge(args[0], {"directed": False})
        else:
            gs.create_edge(args[0], args[1], directed=True)
        results.put((operation, "ok", ""))
    except Exception as error:
        results.put((operation, "error", f"{type(error).__name__}: {error}"))


def _agent_update_or_create_worker(host, app, barrier, results, operation, args):
    """Race a rename against creation of the same unique agent name."""
    _configure_worker(host, app)
    original_create = gs.create_object
    original_patch = gs.patch_object

    def slow_create(object_type, body):
        if object_type == "agent":
            time.sleep(0.35)
        return original_create(object_type, body)

    def slow_patch(object_type, guid, body):
        if object_type == "agent":
            time.sleep(0.35)
        return original_patch(object_type, guid, body)

    gs.create_object = slow_create
    gs.patch_object = slow_patch
    try:
        barrier.wait(timeout=10)
        if operation == "update":
            gs.update_agent(args[0], name="contended-name")
        else:
            gs.create_agent("contended-name")
        results.put((operation, "ok", ""))
    except Exception as error:
        results.put((operation, "error", f"{type(error).__name__}: {error}"))


def _edge_update_or_delete_worker(host, app, barrier, results, operation, args):
    """Force DELETE ahead of PATCH unless both use the authorization lock."""
    _configure_worker(host, app)
    original_patch = gs._patch_object_unchecked
    original_delete = gs._delete_object_unchecked

    def slow_patch(object_type, guid, body):
        if object_type == "edge":
            time.sleep(0.35)
        return original_patch(object_type, guid, body)

    def fast_delete(object_type, guid):
        if object_type == "edge":
            time.sleep(0.05)
        return original_delete(object_type, guid)

    gs._patch_object_unchecked = slow_patch
    gs._delete_object_unchecked = fast_delete
    try:
        barrier.wait(timeout=10)
        if operation == "update":
            gs.update_edge(args[0], {"label": "updated"})
        else:
            gs.delete_edge(args[0])
        results.put((operation, "ok", ""))
    except Exception as error:
        results.put((operation, "error", f"{type(error).__name__}: {error}"))


def _agent_update_or_delete_worker(host, app, barrier, results, operation, args):
    """Force agent DELETE ahead of PATCH unless both use a row lock."""
    _configure_worker(host, app)
    original_patch = gs.patch_object
    original_delete = gs._delete_object_unchecked

    def slow_patch(object_type, guid, body):
        if object_type == "agent":
            time.sleep(0.35)
        return original_patch(object_type, guid, body)

    def fast_delete(object_type, guid):
        if object_type == "agent":
            time.sleep(0.05)
        return original_delete(object_type, guid)

    gs.patch_object = slow_patch
    gs._delete_object_unchecked = fast_delete
    try:
        barrier.wait(timeout=10)
        if operation == "update":
            gs.update_agent(args[0], role="updated")
        else:
            gs.delete_agent(args[0])
        results.put((operation, "ok", ""))
    except Exception as error:
        results.put((operation, "error", f"{type(error).__name__}: {error}"))


def _edge_create_or_agent_delete_worker(host, app, barrier, results,
                                        operation, args):
    """Race incident-edge creation against deletion after its edge scan."""
    _configure_worker(host, app)
    original_create = gs.create_object
    original_delete = gs._delete_object_unchecked

    def fast_edge_create(object_type, body):
        if object_type == "edge":
            time.sleep(0.05)
        return original_create(object_type, body)

    def slow_agent_delete(object_type, guid):
        if object_type == "agent":
            time.sleep(0.35)
        return original_delete(object_type, guid)

    gs.create_object = fast_edge_create
    gs._delete_object_unchecked = slow_agent_delete
    try:
        barrier.wait(timeout=10)
        if operation == "create":
            gs.create_edge(args[0], args[1])
        else:
            gs.delete_agent(args[0])
        results.put((operation, "ok", ""))
    except Exception as error:
        results.put((operation, "error", f"{type(error).__name__}: {error}"))


def _lock_probe_worker(host, app, results):
    _configure_worker(host, app)
    try:
        with gs._invariant_lock("agent", app=app):
            results.put(("ok", ""))
    except Exception as error:
        results.put(("error", f"{type(error).__name__}: {error}"))


def _cross_app_home_spawn_worker(host, _app, barrier, results,
                                 project, args):
    """Race two named projects for one physical home without touching tmux."""
    known_projects, home, name = args
    config.MORPHDB_HOST = host
    os.environ.pop("CREW_APP", None)
    os.environ["CREW_PROJECT"] = project
    gs._INVARIANT_LOCK_DIR = os.environ["CREW_TEST_GRAPH_LOCK_DIR"]
    config.list_known_projects = lambda: list(known_projects)

    original_materialize = crew_spawn._materialize_home

    def slow_materialize(home_path, plan):
        # Widen the interval after home_conflict_across_apps has returned but
        # before either durable row exists. Without one global claim lock both
        # projects pass the read and create records for the same workspace.
        time.sleep(0.5)
        return original_materialize(home_path, plan)

    def fake_tmux(*tmux_args, **_kwargs):
        return (False, "") if tmux_args[0] == "has-session" else (True, "")

    crew_spawn._materialize_home = slow_materialize
    crew_spawn._tmux = fake_tmux
    crew_spawn._open_session = lambda *_args, **_kwargs: "%private"
    crew_spawn.rewrite_identity = lambda *_args, **_kwargs: None
    try:
        barrier.wait(timeout=10)
        crew_spawn.spawn_agent(name, home=home, launch=False)
        results.put((project, "ok", ""))
    except Exception as error:
        results.put((project, "error", f"{type(error).__name__}: {error}"))


class GraphstoreProcessRaces(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._old_host = config.MORPHDB_HOST
        cls._old_app = os.environ.get("CREW_APP")
        cls._old_project = os.environ.get("CREW_PROJECT")
        cls._old_lock_dir = gs._INVARIANT_LOCK_DIR
        cls._old_lock_env = os.environ.get("CREW_TEST_GRAPH_LOCK_DIR")
        cls._tempdir = tempfile.TemporaryDirectory(prefix="crew-morph-races-")
        cls._lock_dir = os.path.join(cls._tempdir.name, "graph-locks")
        gs._INVARIANT_LOCK_DIR = cls._lock_dir
        os.environ["CREW_TEST_GRAPH_LOCK_DIR"] = cls._lock_dir
        cls._app = f"crew-races-{os.getpid()}-{uuid.uuid4().hex[:8]}"

        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            cls._port = listener.getsockname()[1]
        cls._host = f"127.0.0.1:{cls._port}"
        db_path = os.path.join(cls._tempdir.name, "morphdb.sqlite3")
        env = dict(os.environ)
        env["MORPHDB_QUIET"] = "1"
        cls._server = subprocess.Popen(
            ["morphdb", "run", "--host", "127.0.0.1", "--port",
             str(cls._port), "--db", db_path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
        )
        deadline = time.monotonic() + 10
        health = f"http://{cls._host}/health"
        while time.monotonic() < deadline:
            if cls._server.poll() is not None:
                raise RuntimeError("private MorphDB exited during startup")
            try:
                with urllib.request.urlopen(health, timeout=0.25) as response:
                    if response.status == 200:
                        break
            except urllib.error.HTTPError as error:
                error.close()
                time.sleep(0.05)
            except OSError:
                time.sleep(0.05)
        else:
            raise RuntimeError("private MorphDB did not become healthy")

        config.MORPHDB_HOST = cls._host
        os.environ["CREW_APP"] = cls._app
        os.environ.pop("CREW_PROJECT", None)

    @classmethod
    def tearDownClass(cls):
        cls._server.terminate()
        try:
            cls._server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            cls._server.kill()
            cls._server.wait(timeout=5)
        gs._INVARIANT_LOCK_DIR = cls._old_lock_dir
        if cls._old_lock_env is None:
            os.environ.pop("CREW_TEST_GRAPH_LOCK_DIR", None)
        else:
            os.environ["CREW_TEST_GRAPH_LOCK_DIR"] = cls._old_lock_env
        config.MORPHDB_HOST = cls._old_host
        if cls._old_app is None:
            os.environ.pop("CREW_APP", None)
        else:
            os.environ["CREW_APP"] = cls._old_app
        if cls._old_project is None:
            os.environ.pop("CREW_PROJECT", None)
        else:
            os.environ["CREW_PROJECT"] = cls._old_project
        # Every worker is joined and the private server is stopped before its
        # lock files are removed, so no process can hold an unlinked lock inode.
        cls._tempdir.cleanup()

    def setUp(self):
        config.MORPHDB_HOST = self._host
        os.environ["CREW_APP"] = self._app
        os.environ.pop("CREW_PROJECT", None)
        try:
            gs._req("DELETE", f"/app/{self._app}", app=None)
        except gs.GraphError:
            pass
        schema.ensure_schema(self._app)

    def _race(self, target, calls):
        ctx = multiprocessing.get_context("spawn")
        barrier = ctx.Barrier(len(calls))
        results = ctx.Queue()
        processes = [
            ctx.Process(target=target, args=(self._host, self._app, barrier,
                                             results, *call))
            for call in calls
        ]
        try:
            for process in processes:
                process.start()
            for process in processes:
                process.join(timeout=15)
            self.assertTrue(all(not process.is_alive() for process in processes),
                            "race worker hung")
            self.assertTrue(all(process.exitcode == 0 for process in processes),
                            [process.exitcode for process in processes])
            return [results.get(timeout=3) for _ in calls]
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=3)
            results.close()

    def test_concurrent_same_name_creates_leave_one_agent(self):
        outcomes = self._race(
            _slow_create_worker,
            [("agent", ("same-name",)), ("agent", ("same-name",))],
        )
        self.assertEqual([status for status, _ in outcomes].count("ok"), 1,
                         outcomes)
        self.assertEqual([status for status, _ in outcomes].count("error"), 1,
                         outcomes)
        rows = gs.list_objects("agent", name="same-name", limit=10)["objects"]
        self.assertEqual(len(rows), 1, rows)

    def test_named_projects_cannot_concurrently_claim_one_home(self):
        project_a = "racehomea"
        project_b = "racehomeb"
        app_a = config.project_app(project_a)
        app_b = config.project_app(project_b)
        known = (config.DEFAULT_PROJECT, project_a, project_b)
        home = os.path.join(self._tempdir.name, "one-physical-home")
        for app in (app_a, app_b):
            try:
                gs._req("DELETE", f"/app/{app}", app=None)
            except gs.GraphError:
                pass
            schema.ensure_schema(app)
        try:
            outcomes = self._race(
                _cross_app_home_spawn_worker,
                [(project_a, (known, home, "agent-a")),
                 (project_b, (known, home, "agent-b"))],
            )
            self.assertEqual(
                [status for _, status, _ in outcomes].count("ok"), 1,
                outcomes)
            self.assertEqual(
                [status for _, status, _ in outcomes].count("error"), 1,
                outcomes)
            rows = (gs.list_agents(app=app_a) + gs.list_agents(app=app_b))
            self.assertEqual(len(rows), 1, (outcomes, rows))
            self.assertEqual(gs.normalize_home(rows[0]["home"]),
                             gs.normalize_home(home))
        finally:
            for app in (app_a, app_b):
                try:
                    gs._req("DELETE", f"/app/{app}", app=None)
                except gs.GraphError:
                    pass

    def test_rename_and_create_cannot_claim_the_same_agent_name(self):
        existing = gs.create_agent("rename-source")
        outcomes = self._race(
            _agent_update_or_create_worker,
            [("update", (existing["_guid"],)), ("create", ())],
        )
        self.assertEqual([status for _, status, _ in outcomes].count("ok"), 1,
                         outcomes)
        self.assertEqual([status for _, status, _ in outcomes].count("error"), 1,
                         outcomes)
        rows = gs.list_objects("agent", name="contended-name", limit=10)["objects"]
        self.assertEqual(len(rows), 1, rows)

    def test_rename_rejects_an_existing_or_invalid_agent_name(self):
        first = gs.create_agent("rename-first")
        second = gs.create_agent("rename-second")
        with self.assertRaisesRegex(gs.GraphError, "already exists"):
            gs.update_agent(second["_guid"], name=first["name"])
        with self.assertRaisesRegex(gs.GraphError, "invalid agent name"):
            gs.update_agent(second["_guid"], name="bad/name")

    def test_agent_update_cannot_resurrect_a_concurrently_deleted_agent(self):
        agent = gs.create_agent("delete-update-agent")
        outcomes = self._race(
            _agent_update_or_delete_worker,
            [("update", (agent["_guid"],)), ("delete", (agent["_guid"],))],
        )
        delete_outcome = next(row for row in outcomes if row[0] == "delete")
        self.assertEqual(delete_outcome[1], "ok", outcomes)
        remaining = [row for row in gs.list_agents()
                     if row.get("_guid") == agent["_guid"]]
        self.assertEqual(remaining, [], (outcomes, remaining))

    def test_agent_delete_and_edge_create_cannot_leave_an_orphan_edge(self):
        doomed = gs.create_agent("doomed")
        peer = gs.create_agent("peer")
        outcomes = self._race(
            _edge_create_or_agent_delete_worker,
            [("delete", (doomed["_guid"],)),
             ("create", (doomed["_guid"], peer["_guid"]))],
        )
        delete_outcome = next(row for row in outcomes if row[0] == "delete")
        self.assertEqual(delete_outcome[1], "ok", outcomes)
        self.assertEqual(gs.list_edges(), [], outcomes)

    def test_concurrent_overlapping_edge_creates_leave_one_edge(self):
        source = gs.create_agent("source")
        target = gs.create_agent("target")
        args = (source["_guid"], target["_guid"])
        outcomes = self._race(
            _slow_create_worker,
            [("edge", args + ("directed", "", None, "", False, None, "", False,
                               0, 0, 0, True)),
             ("edge", args + ("two-way", "", None, "", False, None, "", False,
                               0, 0, 0, False))],
        )
        self.assertEqual([status for status, _ in outcomes].count("ok"), 1,
                         outcomes)
        self.assertEqual([status for status, _ in outcomes].count("error"), 1,
                         outcomes)
        self.assertEqual(len(gs.list_edges()), 1, gs.list_edges())

    def test_edge_update_and_create_share_the_authorization_lock(self):
        source = gs.create_agent("source")
        target = gs.create_agent("target")
        first = gs.create_edge(source["_guid"], target["_guid"], directed=True)
        outcomes = self._race(
            _edge_update_or_create_worker,
            [("update", (first["_guid"],)),
             ("create", (target["_guid"], source["_guid"]))],
        )
        self.assertEqual([status for _, status, _ in outcomes].count("ok"), 1,
                         outcomes)
        self.assertEqual([status for _, status, _ in outcomes].count("error"), 1,
                         outcomes)
        # Whichever operation wins, target->source has exactly one authorizer.
        self.assertEqual(len(gs.authorizing_edges("target", "source")), 1)

    def test_edge_update_cannot_resurrect_a_concurrently_deleted_edge(self):
        source = gs.create_agent("source")
        target = gs.create_agent("target")
        edge = gs.create_edge(source["_guid"], target["_guid"])
        outcomes = self._race(
            _edge_update_or_delete_worker,
            [("update", (edge["_guid"],)), ("delete", (edge["_guid"],))],
        )
        delete_outcome = next(row for row in outcomes if row[0] == "delete")
        self.assertEqual(delete_outcome[1], "ok", outcomes)
        remaining = [row for row in gs.list_edges()
                     if row.get("_guid") == edge["_guid"]]
        self.assertEqual(remaining, [], (outcomes, remaining))

    def test_lock_filename_is_hashed_and_scoped(self):
        first = gs._invariant_lock_path("agent", app="app/unsafe name")
        same = gs._invariant_lock_path("agent", app="app/unsafe name")
        other_app = gs._invariant_lock_path("agent", app="another-app")
        other_scope = gs._invariant_lock_path("edge-authorization",
                                              app="app/unsafe name")
        self.assertEqual(first, same)
        self.assertNotEqual(first, other_app)
        self.assertNotEqual(first, other_scope)
        self.assertRegex(os.path.basename(first), re.compile(r"^[0-9a-f]{64}\.lock$"))
        self.assertNotIn("unsafe", first)
        self.assertEqual(os.path.commonpath((first, self._lock_dir)),
                         self._lock_dir)
        # Production identity transactions use one fixed file per app, not one
        # per historical GUID. Exercise far more GUIDs than this test graph can
        # contain and prove the lock namespace grows by at most that one scope.
        before_identity = set(pathlib.Path(self._lock_dir).glob("*.lock"))
        for index in range(250):
            with gs._identity_transaction_locks((f"synthetic-guid-{index}",)):
                pass
        after_identity = set(pathlib.Path(self._lock_dir).glob("*.lock"))
        self.assertLessEqual(len(after_identity - before_identity), 1)

        # Agent/edge/identity/migration/lifecycle are fixed app scopes, plus one
        # backend-global home claim. Other apps exercised by this module get
        # their own bounded set, so the total is no longer exactly three.
        with gs._invariant_lock("agent"), \
             gs._invariant_lock("edge-authorization"):
            pass
        with gs._home_claim_lock():
            pass
        files = list(pathlib.Path(self._lock_dir).glob("*.lock"))
        self.assertLessEqual(len(files), 24, files)

    def test_exception_releases_lock_for_another_process(self):
        with self.assertRaisesRegex(RuntimeError, "boom"):
            with gs._invariant_lock("agent", app=self._app):
                raise RuntimeError("boom")

        ctx = multiprocessing.get_context("spawn")
        results = ctx.Queue()
        process = ctx.Process(target=_lock_probe_worker,
                              args=(self._host, self._app, results))
        process.start()
        process.join(timeout=5)
        try:
            self.assertFalse(process.is_alive(), "released lock stayed blocked")
            self.assertEqual(process.exitcode, 0)
            self.assertEqual(results.get(timeout=2), ("ok", ""))
        finally:
            if process.is_alive():
                process.terminate()
                process.join(timeout=3)
            results.close()


if __name__ == "__main__":
    unittest.main()
