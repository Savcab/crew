"""Fail-closed lifecycle, trust, and durable identity write regressions.

Every filesystem fixture is disposable.  Tmux and MorphDB boundaries are mocked;
these tests must never inspect or mutate a real session or backend row.
"""
import concurrent.futures
import contextlib
import io
import json
import multiprocessing
import os
import pathlib
import stat
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

from crew import cli, graphstore as gs, guard, identity, schema, spawn
from crew.server import tmuxio


def _cross_process_create_agent(
        app, name, actor, mode, max_agents, spawn_rate, barrier, results):
    """Spawn-safe worker for real interprocess commit-time invariant tests."""
    os.environ.update({
        "CREW_APP": app,
        "CREW_PROJECT": "default",
        "MORPHDB_HOST": "127.0.0.1:18787",
    })
    from crew import config as child_config
    from crew import graphstore as child_gs
    from crew import guard as child_guard

    child_config.MORPHDB_HOST = "127.0.0.1:18787"
    child_config.MAX_AGENTS = max_agents
    child_config.SPAWN_RATE = spawn_rate
    original_check = child_guard.check
    local = {"spawn_checks": 0}

    if mode == "foreman":
        try:
            original_check("human", "foreman", name=name, revoke=False)
            barrier.wait(10)
        except Exception as error:
            results.put((name, "error", str(error)))
            return
    else:
        def synchronized_check(check_actor, op, **ctx):
            if op == "spawn":
                local["spawn_checks"] += 1
                original_check(check_actor, op, **ctx)
                if local["spawn_checks"] == 1:
                    barrier.wait(10)
                return
            return original_check(check_actor, op, **ctx)

        child_guard.check = synchronized_check

    try:
        child_gs.create_agent(
            name, home=f"/tmp/{name}", runtime="custom", launch_cmd="true",
            status="not_started", actor=actor,
            can_edit_graph=(mode == "foreman"))
        results.put((name, "ok", ""))
    except Exception as error:
        results.put((name, "error", str(error)))


class SpawnArgumentTests(unittest.TestCase):
    def test_cli_parser_rejects_home_and_repo_as_mutually_exclusive(self):
        with contextlib.redirect_stderr(io.StringIO()), \
             self.assertRaises(SystemExit):
            cli.build_parser().parse_args([
                "spawn-agent", "worker", "--home", "/tmp/worker",
                "--repo", "/tmp/repo",
            ])

    def test_home_and_repo_are_rejected_before_any_side_effect(self):
        with mock.patch.object(spawn.config, "current_project", return_value="demo"), \
             mock.patch.object(spawn.guard, "check") as check, \
             mock.patch.object(spawn.gs, "get_agent_by_name") as get_agent, \
             mock.patch.object(spawn, "_plan_home") as plan_home, \
             mock.patch.object(spawn, "_materialize_home") as materialize, \
             mock.patch.object(spawn, "_tmux") as tmux:
            with self.assertRaisesRegex(gs.GraphError, "--home.*--repo"):
                spawn.spawn_agent(
                    "worker", home="/tmp/worker", repo="/tmp/repo",
                    launch=False)

        check.assert_not_called()
        get_agent.assert_not_called()
        plan_home.assert_not_called()
        materialize.assert_not_called()
        tmux.assert_not_called()


class LifecycleOwnershipTests(unittest.TestCase):
    AGENT = {
        "_guid": "agent-guid",
        "name": "worker",
        "home": "/tmp/crew-test-worker",
        "session": "victim-session",
        "runtime": "custom",
        "launch_cmd": "true",
        "status": "not_started",
    }

    def setUp(self):
        invariant = mock.patch.object(
            gs, "_invariant_lock",
            side_effect=lambda *_a, **_k: contextlib.nullcontext())
        edges = mock.patch.object(gs, "edges_touching", return_value=[])
        invariant.start()
        edges.start()
        self.addCleanup(invariant.stop)
        self.addCleanup(edges.stop)

    def test_start_refuses_corrupt_stored_session_before_tmux_or_files(self):
        with mock.patch.dict(os.environ, {"CREW_PROJECT": "demo"}, clear=False), \
             mock.patch.object(spawn.gs, "get_agent_by_name", return_value=self.AGENT), \
             mock.patch.object(spawn.guard, "check"), \
             mock.patch.object(spawn, "_tmux") as tmux, \
             mock.patch.object(spawn, "rewrite_identity") as rewrite, \
             mock.patch.object(os, "makedirs") as makedirs:
            with self.assertRaisesRegex(gs.GraphError, "stored tmux session"):
                spawn.start_session("worker")

        tmux.assert_not_called()
        rewrite.assert_not_called()
        makedirs.assert_not_called()

    def test_remove_refuses_corrupt_stored_session_before_delete_or_tmux(self):
        with mock.patch.dict(os.environ, {"CREW_PROJECT": "demo"}, clear=False), \
             mock.patch.object(spawn.gs, "get_agent_by_name", return_value=self.AGENT), \
             mock.patch.object(spawn.guard, "check"), \
             mock.patch.object(spawn.gs, "delete_agent") as delete, \
             mock.patch.object(spawn, "_tmux") as tmux:
            with self.assertRaisesRegex(gs.GraphError, "stored tmux session"):
                spawn.remove_agent("worker")

        delete.assert_not_called()
        tmux.assert_not_called()

    def test_down_refuses_corrupt_stored_session_without_targeting_tmux(self):
        with mock.patch.dict(os.environ, {"CREW_PROJECT": "demo"}, clear=False), \
             mock.patch.object(spawn.gs, "get_agent_by_name", return_value=self.AGENT), \
             mock.patch.object(spawn.guard, "check"), \
             mock.patch.object(spawn.guard, "audit"), \
             mock.patch.object(spawn, "_tmux") as tmux:
            with self.assertRaisesRegex(gs.GraphError, "stored tmux session"):
                spawn.stop_session("worker")

        tmux.assert_not_called()

    def test_cli_down_delegates_to_the_validated_spawn_boundary(self):
        corrupt = {"name": "worker", "session": "victim-session"}
        with mock.patch.object(
                 cli.spawn, "stop_session",
                 side_effect=gs.GraphError("invalid stored tmux session")) as stop, \
             mock.patch.object(cli.tmuxio, "tmux") as raw_tmux:
            result = cli._kill_session(corrupt, "human")

        self.assertTrue(result.startswith("error:"), result)
        stop.assert_called_once_with("worker", actor="human")
        raw_tmux.assert_not_called()

    def test_remove_refuses_foreign_live_session_before_deleting_row(self):
        owned_name = "demo__worker"
        agent = dict(self.AGENT, session=owned_name)

        def fake_tmux(*args, **_kwargs):
            if args[0] == "has-session":
                return True, ""
            if args[0] == "show-environment":
                key = args[-1]
                values = {
                    "CREW_PROJECT": "demo",
                    "CREW_AGENT": "someone_else",
                    "CREW_APP": "crew-demo",
                    "MORPHDB_HOST": spawn.config.MORPHDB_HOST,
                }
                return True, f"{key}={values[key]}\n"
            self.fail(f"unexpected tmux call: {args!r}")

        with mock.patch.dict(os.environ, {"CREW_PROJECT": "demo"}, clear=False), \
             mock.patch.object(spawn.gs, "get_agent_by_name", return_value=agent), \
             mock.patch.object(spawn.guard, "check"), \
             mock.patch.object(spawn.gs, "delete_agent") as delete, \
             mock.patch.object(spawn, "_tmux", side_effect=fake_tmux):
            with self.assertRaisesRegex(gs.GraphError, "refusing to adopt"):
                spawn.remove_agent("worker")

        delete.assert_not_called()

    def test_remove_kill_failure_preserves_row_and_edges_for_retry(self):
        agent = dict(self.AGENT, session="demo__worker", pane="%fixture")
        kill_results = iter([
            (False, "fixture kill failure"),
            (True, ""),
        ])

        def fake_tmux(*args, **kwargs):
            endpoint = (kwargs.get("endpoint")
                        or spawn.config.tmux_target_endpoint(*args))
            if args[0] == "has-session":
                return endpoint == spawn.config.TMUX_ENDPOINT_CREW, ""
            if args[0] == "show-environment":
                expected = dict(spawn._session_context("worker", "demo"))
                key = args[-1]
                if key in expected:
                    return True, f"{key}={expected[key]}\n"
                return True, "".join(
                    f"{name}={value}\n" for name, value in expected.items())
            if args[0] == "display-message":
                return True, "demo__worker"
            if args[0] == "list-panes":
                return True, "%fixture"
            if args[0] == "kill-session":
                return next(kill_results)
            self.fail(f"unexpected tmux call: {args!r}")

        with mock.patch.dict(os.environ, {
                 "CREW_PROJECT": "demo", "CREW_APP": "crew-demo",
             }, clear=False), \
             mock.patch.object(
                 spawn.gs, "get_agent_by_name", return_value=agent), \
             mock.patch.object(spawn.guard, "check"), \
             mock.patch.object(spawn.gs, "delete_agent") as delete, \
             mock.patch.object(spawn, "_tmux", side_effect=fake_tmux), \
             mock.patch.object(tmuxio, "tmux", side_effect=fake_tmux):
            with self.assertRaisesRegex(gs.GraphError, "fixture kill failure"):
                spawn.remove_agent("worker")
            delete.assert_not_called()

            removed = spawn.remove_agent("worker")

        self.assertEqual(removed, agent)
        self.assertEqual(delete.call_args.args, ("agent-guid",))
        self.assertEqual(delete.call_args.kwargs["actor"], "human")
        self.assertIs(
            delete.call_args.kwargs["_identity_rewriter"],
            spawn.rewrite_identity)
        self.assertTrue(callable(
            delete.call_args.kwargs["_identity_projector"]))

    def test_remove_revalidates_exact_owner_before_kill_or_graph_delete(self):
        agent = dict(self.AGENT, session="demo__worker", pane="%fixture")
        edge = {
            "_guid": "edge-guid", "source": agent["_guid"],
            "target": "peer-guid",
        }
        durable = {"agent": dict(agent), "edge": dict(edge)}
        initial = spawn.config.tmux_target(
            agent["session"], spawn.config.TMUX_ENDPOINT_CREW)
        initial_locations = {
            "session": agent["session"], "owned": initial,
            "dedicated_exists": True, "legacy_exists": False,
        }
        replacement_locations = {
            "session": agent["session"], "owned": None,
            "dedicated_exists": True, "legacy_exists": False,
        }

        def delete_agent(_guid, **_kwargs):
            durable["agent"] = None
            durable["edge"] = None

        with mock.patch.object(
                 spawn.config, "current_project", return_value="demo"), \
             mock.patch.object(
                 spawn.gs, "get_agent_by_name", return_value=agent), \
             mock.patch.object(spawn.guard, "check"), \
             mock.patch.object(
                 spawn, "_session_locations",
                 side_effect=[initial_locations, replacement_locations]), \
             mock.patch.object(
                 spawn.gs, "delete_agent", side_effect=delete_agent) as delete, \
             mock.patch.object(
                 spawn, "_tmux", return_value=(True, "")) as tmux:
            with self.assertRaisesRegex(gs.GraphError, "ownership changed|retry"):
                spawn._remove_agent_locked("worker")

        tmux.assert_not_called()
        delete.assert_not_called()
        self.assertEqual(durable["agent"]["_guid"], agent["_guid"])
        self.assertEqual(durable["edge"]["_guid"], edge["_guid"])

    def test_start_refuses_to_recreate_a_missing_recorded_worktree(self):
        with tempfile.TemporaryDirectory(prefix="crew-missing-worktree-") as root:
            home = os.path.join(root, "repo-worktrees", "worker")
            agent = {
                "_guid": "agent-guid",
                "name": "worker",
                "home": home,
                "session": "worker",
                "worktree": "worker",
                "runtime": "custom",
                "launch_cmd": "sleep 30",
                "status": "not_started",
            }

            with mock.patch.object(
                     spawn.config, "current_project", return_value="default"), \
                 mock.patch.object(
                     spawn.gs, "get_agent_by_name", return_value=agent), \
                 mock.patch.object(spawn.guard, "check"), \
                 mock.patch.object(
                     spawn, "_tmux", return_value=(False, "")) as tmux, \
                 mock.patch.object(
                     spawn, "_open_session", return_value="%fixture") as opened, \
                 mock.patch.object(spawn, "rewrite_identity") as rewrite, \
                 mock.patch.object(spawn, "_launch_runtime") as launch, \
                 mock.patch.object(
                     spawn.gs, "update_agent_runtime_state") as update:
                with self.assertRaisesRegex(
                        gs.GraphError, "recorded git worktree.*missing"):
                    spawn.start_session("worker")

            self.assertFalse(os.path.lexists(home))
            tmux.assert_not_called()
            opened.assert_not_called()
            rewrite.assert_not_called()
            launch.assert_not_called()
            update.assert_not_called()

    def test_start_refuses_a_plain_directory_for_a_recorded_worktree(self):
        with tempfile.TemporaryDirectory(prefix="crew-plain-worktree-") as root:
            home = os.path.join(root, "repo-worktrees", "worker")
            os.makedirs(home)
            agent = {
                "_guid": "agent-guid", "name": "worker", "home": home,
                "session": "worker", "worktree": "worker",
                "runtime": "custom", "launch_cmd": "sleep 30",
                "status": "not_started",
            }

            with mock.patch.object(
                     spawn.config, "current_project", return_value="default"), \
                 mock.patch.object(
                     spawn.gs, "get_agent_by_name", return_value=agent), \
                 mock.patch.object(spawn.guard, "check"), \
                 mock.patch.object(
                     spawn, "_tmux", return_value=(False, "")) as tmux, \
                 mock.patch.object(
                     spawn, "_open_session", return_value="%fixture"), \
                 mock.patch.object(spawn, "rewrite_identity") as rewrite, \
                 mock.patch.object(spawn, "_launch_runtime") as launch, \
                 mock.patch.object(spawn.gs, "update_agent_runtime_state"):
                with self.assertRaisesRegex(
                        gs.GraphError, "not a registered git worktree"):
                    spawn.start_session("worker")

            tmux.assert_not_called()
            rewrite.assert_not_called()
            launch.assert_not_called()


class AgentRemovalIdentityTransactionTests(unittest.TestCase):
    TARGET = {
        "_guid": "target-guid", "name": "target", "home": "/tmp/target",
        "session": "target", "runtime": "custom", "launch_cmd": "true",
    }
    SURVIVOR = {
        "_guid": "survivor-guid", "name": "survivor",
        "home": "/tmp/survivor", "session": "survivor",
        "runtime": "custom", "launch_cmd": "true",
    }
    EDGE = {
        "_guid": "edge-guid", "source": "survivor-guid",
        "target": "target-guid", "directed": True,
        "conditions": ["handoff"],
    }

    def test_delete_agent_refreshes_each_surviving_neighbor(self):
        projector = mock.Mock()
        rewriter = mock.Mock()
        deleted_agent = {"deleted": "target-guid"}
        with mock.patch.object(gs.guard, "check"), \
             mock.patch.object(gs.guard, "audit") as audit, \
             mock.patch.object(
                 gs, "_invariant_lock",
                 side_effect=lambda *_a, **_k: contextlib.nullcontext()), \
             mock.patch.object(gs, "get_object", return_value=self.TARGET), \
             mock.patch.object(gs, "edges_touching", return_value=[self.EDGE]), \
             mock.patch.object(
                 gs, "_delete_object_verified",
                 side_effect=[{"deleted": "edge-guid"}, deleted_agent]) as delete, \
             mock.patch.object(
                 gs, "_rewrite_agent_identities", create=True) as rewrite:
            removed = gs.delete_agent(
                "target-guid", _identity_projector=projector,
                _identity_rewriter=rewriter)

        self.assertEqual(removed, deleted_agent)
        self.assertEqual(rewrite.call_args_list, [
            mock.call(("survivor-guid",), projector, notify=False),
        ])
        self.assertEqual(delete.call_args_list, [
            mock.call("edge", "edge-guid"),
            mock.call("agent", "target-guid"),
        ])
        audit.assert_called_once_with(
            "human", "remove", {"guid": "target-guid"}, "applied")

    def test_projected_identity_failure_leaves_graph_untouched(self):
        projector = mock.Mock()
        rewriter = mock.Mock()
        with mock.patch.object(gs.guard, "check"), \
             mock.patch.object(gs.guard, "audit") as audit, \
             mock.patch.object(
                 gs, "_invariant_lock",
                 side_effect=lambda *_a, **_k: contextlib.nullcontext()), \
             mock.patch.object(gs, "get_object", return_value=self.TARGET), \
             mock.patch.object(gs, "edges_touching", return_value=[self.EDGE]), \
             mock.patch.object(gs, "_delete_object_verified") as delete, \
             mock.patch.object(
                 gs, "_rewrite_agent_identities", create=True,
                 side_effect=[gs.GraphError("identity destination failed"), None]
             ) as rewrite, \
             mock.patch.object(
                 gs, "_restore_object_snapshot", create=True) as restore:
            with self.assertRaisesRegex(
                    gs.GraphError, "remove agent.*identity destination failed"):
                gs.delete_agent(
                    "target-guid", _identity_projector=projector,
                    _identity_rewriter=rewriter)

        delete.assert_not_called()
        restore.assert_not_called()
        self.assertEqual(rewrite.call_args_list, [
            mock.call(("survivor-guid",), projector, notify=False),
            mock.call(("survivor-guid",), rewriter, notify=False),
        ])
        self.assertEqual(audit.call_args.args[:4], (
            "human", "remove", {"guid": "target-guid"}, "failed"))

    def test_delete_failure_restores_only_edges_while_target_still_exists(self):
        projector = mock.Mock()
        rewriter = mock.Mock()
        with mock.patch.object(gs.guard, "check"), \
             mock.patch.object(gs.guard, "audit") as audit, \
             mock.patch.object(
                 gs, "_invariant_lock",
                 side_effect=lambda *_a, **_k: contextlib.nullcontext()), \
             mock.patch.object(gs, "get_object", return_value=self.TARGET), \
             mock.patch.object(gs, "edges_touching", return_value=[self.EDGE]), \
             mock.patch.object(
                 gs, "_delete_object_verified",
                 side_effect=[{"deleted": "edge-guid"},
                              gs.GraphError("target delete failed")]), \
             mock.patch.object(
                 gs, "_rewrite_agent_identities", create=True) as rewrite, \
             mock.patch.object(
                 gs, "_restore_object_snapshot", create=True) as restore:
            with self.assertRaisesRegex(
                    gs.GraphError, "remove agent.*target delete failed"):
                gs.delete_agent(
                    "target-guid", _identity_projector=projector,
                    _identity_rewriter=rewriter)

        restore.assert_called_once_with("edge", self.EDGE)
        self.assertEqual(rewrite.call_args_list, [
            mock.call(("survivor-guid",), projector, notify=False),
            mock.call(("survivor-guid",), rewriter, notify=False),
        ])
        self.assertEqual(audit.call_args.args[:4], (
            "human", "remove", {"guid": "target-guid"}, "failed"))

    def test_projected_identity_excludes_the_agent_being_removed(self):
        other = dict(self.SURVIVOR, _guid="other-guid", name="other")
        neighbors = [(self.TARGET, self.EDGE), (other, self.EDGE)]
        incoming = [(self.TARGET, self.EDGE), (other, self.EDGE)]
        with mock.patch.object(
                 spawn, "_validated_agent_home", return_value="/tmp/survivor"), \
             mock.patch.object(
                 spawn, "_resolve_neighbors", return_value=neighbors), \
             mock.patch.object(
                 spawn, "_resolve_incoming", return_value=incoming), \
             mock.patch.object(
                 spawn.identity, "render_identity_md", return_value="body") as render, \
             mock.patch.object(
                 spawn.identity, "write_identity", return_value="/tmp/identity"), \
             mock.patch.object(
                 spawn.identity, "render_native_md", return_value=None):
            spawn.rewrite_identity(
                self.SURVIVOR, exclude_agent_guids={"target-guid"})

        rendered_neighbors = render.call_args.args[1]
        rendered_incoming = render.call_args.args[2]
        self.assertEqual([a["_guid"] for a, _ in rendered_neighbors], ["other-guid"])
        self.assertEqual([a["_guid"] for a, _ in rendered_incoming], ["other-guid"])

    def test_removal_projector_renders_post_delete_foreman_quota(self):
        target = dict(
            self.TARGET, created_by="foreman", created_at=900,
        )
        survivor = dict(self.SURVIVOR, can_edit_graph=True)
        live_quota = {
            "agents_used": 12, "max_agents": 12,
            "spawns_this_hour": 4, "spawn_rate": 4,
            "max_turns_ceiling": 30,
            "token_cap_ceiling": 500000,
            "cost_cap_ceiling": 25,
        }

        def project_during_delete(_guid, **kwargs):
            kwargs["_identity_projector"](survivor, notify=False)
            return {"deleted": "target-guid"}

        with mock.patch.object(
                 spawn.config, "current_project", return_value="default"), \
             mock.patch.object(
                 spawn.gs, "_invariant_lock",
                 side_effect=lambda *_a, **_k: contextlib.nullcontext()), \
             mock.patch.object(
                 spawn.gs, "get_agent_by_name", return_value=target), \
             mock.patch.object(spawn.gs, "edges_touching", return_value=[]), \
             mock.patch.object(spawn.guard, "check"), \
             mock.patch.object(spawn.guard, "quota_state", return_value=live_quota), \
             mock.patch.object(spawn.time, "time", return_value=1000), \
             mock.patch.object(spawn, "_tmux", return_value=(False, "")), \
             mock.patch.object(spawn, "rewrite_identity") as rewrite, \
             mock.patch.object(
                 spawn.gs, "delete_agent", side_effect=project_during_delete):
            spawn.remove_agent("target")

        rewrite.assert_called_once_with(
            survivor, notify=False, exclude_agent_guids={"target-guid"},
            quota_override={
                **live_quota,
                "agents_used": 11,
                "spawns_this_hour": 3,
            })

    def test_spawn_remove_supplies_the_durable_identity_rewriter(self):
        agent = dict(self.TARGET)
        with mock.patch.object(
                 spawn.config, "current_project", return_value="default"), \
             mock.patch.object(
                 spawn.gs, "get_agent_by_name", return_value=agent), \
             mock.patch.object(spawn.guard, "check"), \
             mock.patch.object(
                 spawn, "_tmux", return_value=(False, "")), \
             mock.patch.object(spawn.gs, "edges_touching", return_value=[]), \
             mock.patch.object(spawn.gs, "delete_agent") as delete:
            spawn.remove_agent("target")

        kwargs = delete.call_args.kwargs
        self.assertEqual(delete.call_args.args, ("target-guid",))
        self.assertEqual(kwargs["actor"], "human")
        self.assertIs(kwargs["_identity_rewriter"], spawn.rewrite_identity)
        self.assertTrue(callable(kwargs["_identity_projector"]))


class LifecycleConcurrencyTests(unittest.TestCase):
    def test_agent_names_do_not_create_unbounded_lifecycle_lock_files(self):
        with tempfile.TemporaryDirectory(prefix="crew-lifecycle-locks-") as root, \
             mock.patch.object(gs, "_INVARIANT_LOCK_DIR", root), \
             mock.patch.object(spawn.config, "current_project", return_value="test"), \
             mock.patch.object(gs.config, "current_app", return_value="crew_test"), \
             mock.patch.object(
                 spawn, "_spawn_agent_locked",
                 side_effect=lambda name, **_kwargs: {"name": name}):
            for index in range(250):
                result = spawn.spawn_agent(f"historical-{index}", launch=False)
                self.assertEqual(result["name"], f"historical-{index}")

            files = list(pathlib.Path(root).glob("*.lock"))

        self.assertEqual(len(files), 1, files)

    def test_create_agent_recovers_a_committed_row_after_post_response_loss(self):
        committed = {
            "_guid": "committed-guid", "name": "worker", "role": "",
            "identity": "", "home": "/tmp/worker", "session": "worker",
            "pane": "", "worktree": "", "status": "not_started",
            "runtime": "custom", "launch_cmd": "true", "created_at": 1000,
            "kind": "agent", "can_edit_graph": False,
            "created_by": "human", "created_by_guid": "",
            "blessed": True, "notes": "",
        }
        with mock.patch.object(gs.guard, "check"), \
             mock.patch.object(gs.guard, "audit") as audit, \
             mock.patch.object(gs.time, "time", return_value=1000), \
             mock.patch.object(
                 gs, "_invariant_lock",
                 side_effect=lambda *_a, **_k: contextlib.nullcontext()), \
             mock.patch.object(
                 gs, "get_agent_by_name",
                 side_effect=[None, committed]) as lookup, \
             mock.patch.object(
                 gs, "create_object",
                 side_effect=gs.GraphError("response lost after commit")):
            result = gs.create_agent(
                "worker", home="/tmp/worker", runtime="custom",
                launch_cmd="true", status="not_started")

        self.assertEqual(result, committed)
        self.assertEqual(lookup.call_count, 2)
        audit.assert_called_once_with(
            "human", "spawn", {"name": "worker"}, "applied")

    def test_hourly_spawn_count_uses_durable_rows_not_best_effort_audits(self):
        agents = [
            {"created_by": "foreman", "created_at": 200},
            {"created_by": "human", "created_at": 250},
            {"created_by": "foreman", "created_at": 50},
        ]
        with mock.patch.object(gs, "list_agents", return_value=agents), \
             mock.patch.object(
                 gs, "list_objects", return_value={"objects": []}) as audits:
            count = guard._agent_spawn_count_since(100)

        self.assertEqual(count, 1)
        audits.assert_not_called()

    def test_start_and_remove_are_serialized_without_orphaning_a_session(self):
        with tempfile.TemporaryDirectory(prefix="crew-lifecycle-race-") as root:
            home = os.path.join(root, "worker")
            os.mkdir(home)
            row = {
                "_guid": "worker-guid", "name": "worker", "home": home,
                "session": "worker", "runtime": "custom",
                "launch_cmd": "sleep 30", "status": "not_started",
            }
            state = {"row": row}
            state_guard = threading.Lock()
            open_entered = threading.Event()
            allow_open = threading.Event()
            deleted = threading.Event()
            errors = []

            def get_agent(name):
                with state_guard:
                    value = state["row"]
                    return dict(value) if value and value["name"] == name else None

            def update_state(guid, **fields):
                with state_guard:
                    if state["row"] is None:
                        raise gs.GraphError("row was concurrently deleted")
                    state["row"].update(fields)
                    return dict(state["row"])

            def delete_agent(guid, **_kwargs):
                with state_guard:
                    if state["row"] is None or state["row"]["_guid"] != guid:
                        raise gs.GraphError("row already deleted")
                    state["row"] = None
                deleted.set()
                return {"deleted": guid}

            def open_session(*_args, **_kwargs):
                open_entered.set()
                if not allow_open.wait(5):
                    raise AssertionError("test did not release session creation")
                return "%fixture"

            def run(call):
                try:
                    call()
                except Exception as error:
                    errors.append(error)

            old_lock_dir = gs._INVARIANT_LOCK_DIR
            gs._INVARIANT_LOCK_DIR = os.path.join(root, "locks")
            try:
                with mock.patch.object(
                         spawn.config, "current_project", return_value="default"), \
                     mock.patch.object(
                         spawn.gs, "get_agent_by_name", side_effect=get_agent), \
                     mock.patch.object(
                         spawn.gs, "update_agent_runtime_state",
                         side_effect=update_state), \
                     mock.patch.object(
                         spawn.gs, "delete_agent", side_effect=delete_agent), \
                     mock.patch.object(spawn.guard, "check"), \
                     mock.patch.object(spawn.guard, "audit"), \
                     mock.patch.object(
                         spawn, "_tmux", return_value=(False, "")), \
                     mock.patch.object(
                         spawn, "_open_session", side_effect=open_session), \
                     mock.patch.object(spawn, "rewrite_identity"), \
                     mock.patch.object(spawn, "_launch_runtime"):
                    starter = threading.Thread(
                        target=run, args=(lambda: spawn.start_session("worker"),))
                    remover = threading.Thread(
                        target=run, args=(lambda: spawn.remove_agent("worker"),))
                    starter.start()
                    self.assertTrue(open_entered.wait(2))
                    remover.start()
                    removed_while_starting = deleted.wait(0.2)
                    allow_open.set()
                    starter.join(5)
                    remover.join(5)
            finally:
                allow_open.set()
                gs._INVARIANT_LOCK_DIR = old_lock_dir

            self.assertFalse(removed_while_starting)
            self.assertFalse(starter.is_alive())
            self.assertFalse(remover.is_alive())
            self.assertEqual(errors, [])
            self.assertTrue(deleted.is_set())

    def test_grant_and_remove_serialize_through_identity_publication(self):
        with tempfile.TemporaryDirectory(prefix="crew-grant-remove-race-") as root:
            home = os.path.join(root, "worker")
            target = os.path.join(root, "shared")
            os.mkdir(home)
            os.mkdir(target)
            row = {
                "_guid": "worker-guid", "name": "worker", "home": home,
                "session": "worker", "runtime": "custom",
                "launch_cmd": "true", "status": "not_started", "grants": [],
            }
            state = {"row": row}
            state_guard = threading.Lock()
            grant_in_publish = threading.Event()
            allow_grant_publish = threading.Event()
            remove_attempted = threading.Event()
            remove_finished = threading.Event()
            order = []
            errors = []

            def current_row(_name=None):
                with state_guard:
                    value = state["row"]
                    return dict(value) if value else None

            def get_object(_guid):
                value = current_row()
                if not value:
                    raise gs.GraphError("agent row was deleted")
                return value

            def update_grants(_guid, grants):
                with state_guard:
                    if state["row"] is None:
                        raise gs.GraphError("grant raced after remove")
                    state["row"]["grants"] = list(grants)
                    order.append("grant-row")
                    return dict(state["row"])

            def rewrite(snapshot, notify=False, **_kwargs):
                if threading.current_thread().name == "grant-thread":
                    grant_in_publish.set()
                    if not allow_grant_publish.wait(5):
                        raise AssertionError("test did not release grant publish")
                    order.append("grant-identity")
                return snapshot

            def delete_agent(_guid, **_kwargs):
                with state_guard:
                    if state["row"] is None:
                        raise gs.GraphError("agent already removed")
                    order.append("remove-row")
                    state["row"] = None
                remove_finished.set()
                return {"deleted": _guid}

            def run(call):
                try:
                    call()
                except Exception as error:
                    errors.append(error)

            old_lock_dir = gs._INVARIANT_LOCK_DIR
            gs._INVARIANT_LOCK_DIR = os.path.join(root, "locks")
            try:
                with mock.patch.object(
                         spawn.config, "current_project", return_value="default"), \
                     mock.patch.object(
                         spawn.gs, "get_agent_by_name", side_effect=current_row), \
                     mock.patch.object(
                         spawn.gs, "get_object", side_effect=get_object), \
                     mock.patch.object(
                         spawn.gs, "update_agent_grants",
                         side_effect=update_grants), \
                     mock.patch.object(
                         spawn.gs, "delete_agent", side_effect=delete_agent), \
                     mock.patch.object(spawn.gs, "edges_touching", return_value=[]), \
                     mock.patch.object(spawn.guard, "check"), \
                     mock.patch.object(spawn.guard, "audit"), \
                     mock.patch.object(spawn, "rewrite_identity", side_effect=rewrite):
                    grant_thread = threading.Thread(
                        target=run,
                        args=(lambda: spawn.grant_path(
                            "worker", target, actor="human"),),
                        name="grant-thread")

                    def remove():
                        if not grant_in_publish.wait(5):
                            raise AssertionError("grant never reached publication")
                        remove_attempted.set()
                        spawn.remove_agent(
                            "worker", kill_session=False, actor="human")

                    remove_thread = threading.Thread(
                        target=run, args=(remove,), name="remove-thread")
                    grant_thread.start()
                    remove_thread.start()
                    self.assertTrue(grant_in_publish.wait(2))
                    self.assertTrue(remove_attempted.wait(2))
                    removed_during_grant = remove_finished.wait(0.2)
                    allow_grant_publish.set()
                    grant_thread.join(5)
                    remove_thread.join(5)
            finally:
                allow_grant_publish.set()
                gs._INVARIANT_LOCK_DIR = old_lock_dir

            self.assertFalse(removed_during_grant)
            self.assertFalse(grant_thread.is_alive())
            self.assertFalse(remove_thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(order, ["grant-row", "grant-identity", "remove-row"])
            self.assertIsNone(state["row"])


class ClaudeTrustPreservationTests(unittest.TestCase):
    def test_claude_config_lock_uses_private_runtime_state(self):
        with tempfile.TemporaryDirectory(prefix="crew-claude-lock-") as root:
            lock_dir = os.path.join(root, "runtime-locks")
            os.mkdir(lock_dir, 0o700)
            legacy_var = os.path.join(root, "repo-var")
            with mock.patch.object(spawn.config, "VAR", legacy_var), \
                 mock.patch.object(
                     spawn.config, "runtime_state_dir",
                     return_value=lock_dir) as runtime_dir:
                with spawn._claude_config_lock():
                    pass

            runtime_dir.assert_called_once_with("claude-config-locks")
            self.assertFalse(os.path.exists(legacy_var))
            path = os.path.join(lock_dir, "claude-config.lock")
            self.assertTrue(os.path.isfile(path))
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    def test_claude_config_lock_wraps_unsafe_runtime_directory(self):
        with mock.patch.object(
                spawn.config, "runtime_state_dir",
                side_effect=PermissionError("sandbox denied")):
            with self.assertRaisesRegex(
                    gs.GraphError, "Claude config lock.*sandbox denied"):
                with spawn._claude_config_lock():
                    pass

    def test_pretrust_remains_best_effort_when_lock_is_unavailable(self):
        with mock.patch.object(
                spawn, "_claude_config_lock",
                side_effect=gs.GraphError("lock unavailable")):
            spawn._pretrust_home("/agent/home")

    def _round_trip(self, entry):
        temp = tempfile.TemporaryDirectory(prefix="crew-trust-test-")
        self.addCleanup(temp.cleanup)
        cfg_path = os.path.join(temp.name, ".claude.json")
        payload = {"projects": {"/agent/home": entry}, "other": {"keep": True}}
        with open(cfg_path, "w") as stream:
            json.dump(payload, stream)
        home_env = mock.patch.dict(os.environ, {"HOME": temp.name}, clear=False)
        home_env.start()
        self.addCleanup(home_env.stop)
        var_patch = mock.patch.object(
            spawn.config, "VAR", os.path.join(temp.name, "var"))
        var_patch.start()
        self.addCleanup(var_patch.stop)
        spawn._pretrust_home("/agent/home")
        spawn._untrust_home("/agent/home")
        with open(cfg_path) as stream:
            return json.load(stream)

    def test_preexisting_true_trust_and_project_metadata_survive_remove(self):
        result = self._round_trip({
            "hasTrustDialogAccepted": True,
            "userSetting": "preserve-me",
        })
        self.assertEqual(result["projects"]["/agent/home"], {
            "hasTrustDialogAccepted": True,
            "userSetting": "preserve-me",
        })
        self.assertEqual(result["other"], {"keep": True})

    def test_crew_added_trust_is_removed_without_deleting_other_metadata(self):
        result = self._round_trip({"userSetting": "preserve-me"})
        self.assertEqual(
            result["projects"]["/agent/home"],
            {"userSetting": "preserve-me"})

    def test_preexisting_false_trust_is_restored_exactly(self):
        result = self._round_trip({
            "hasTrustDialogAccepted": False,
            "userSetting": "preserve-me",
        })
        self.assertIs(
            result["projects"]["/agent/home"]["hasTrustDialogAccepted"],
            False)


class IdentityWriteSafetyTests(unittest.TestCase):
    def test_spawn_boundary_reports_identity_refusal_as_graph_error(self):
        agent = {
            "_guid": "agent-guid", "name": "worker",
            "home": "/tmp/crew-test-worker", "runtime": "claude",
        }
        with mock.patch.object(
                 spawn, "_validated_agent_home",
                 return_value="/tmp/crew-test-worker"), \
             mock.patch.object(spawn, "_resolve_neighbors", return_value=[]), \
             mock.patch.object(spawn, "_resolve_incoming", return_value=[]), \
             mock.patch.object(
                 spawn.identity, "write_identity_bundle",
                 side_effect=identity.IdentityWriteError(
                     "destination is a symlink")):
            with self.assertRaisesRegex(gs.GraphError, "symlink"):
                spawn.rewrite_identity(agent)

    def test_identity_write_refuses_symlink_and_preserves_target(self):
        with tempfile.TemporaryDirectory(prefix="crew-identity-test-") as root:
            home = os.path.join(root, "home")
            os.mkdir(home)
            target = os.path.join(root, "outside.md")
            with open(target, "w") as stream:
                stream.write("outside must remain")
            os.symlink(target, os.path.join(home, "identity.md"))

            with self.assertRaisesRegex(identity.IdentityWriteError, "symlink"):
                identity.write_identity(home, "crew data")

            with open(target) as stream:
                self.assertEqual(stream.read(), "outside must remain")

    def test_identity_write_refuses_a_symlink_home(self):
        with tempfile.TemporaryDirectory(prefix="crew-home-link-test-") as root:
            outside = os.path.join(root, "outside-home")
            os.mkdir(outside)
            linked_home = os.path.join(root, "agent-home")
            os.symlink(outside, linked_home)

            with self.assertRaisesRegex(identity.IdentityWriteError, "home.*symlink"):
                identity.write_identity(linked_home, "crew data")

            self.assertEqual(os.listdir(outside), [])

    def test_native_write_refuses_symlink_and_preserves_target(self):
        with tempfile.TemporaryDirectory(prefix="crew-native-test-") as root:
            home = os.path.join(root, "home")
            os.mkdir(home)
            target = os.path.join(root, "outside.md")
            with open(target, "w") as stream:
                stream.write("outside must remain")
            os.symlink(target, os.path.join(home, "CLAUDE.md"))

            with self.assertRaisesRegex(identity.IdentityWriteError, "symlink"):
                identity.write_claude_md(home, "crew data")

            with open(target) as stream:
                self.assertEqual(stream.read(), "outside must remain")

    def test_codex_override_symlink_refuses_before_agents_file_changes(self):
        with tempfile.TemporaryDirectory(prefix="crew-override-test-") as root:
            home = os.path.join(root, "home")
            os.mkdir(home)
            target = os.path.join(root, "outside.md")
            with open(target, "w") as stream:
                stream.write("active outside override")
            os.symlink(target, os.path.join(home, "AGENTS.override.md"))

            with self.assertRaisesRegex(identity.IdentityWriteError, "symlink"):
                identity.write_agents_md(home, "crew data")

            self.assertFalse(os.path.lexists(os.path.join(home, "AGENTS.md")))
            with open(target) as stream:
                self.assertEqual(stream.read(), "active outside override")

    def test_identity_replacement_is_atomic(self):
        with tempfile.TemporaryDirectory(prefix="crew-atomic-test-") as home, \
             mock.patch.object(
                 identity.os, "replace", wraps=os.replace) as replace:
            path = identity.write_identity(home, "complete identity\n")
            replace.assert_called_once()
            with open(path) as stream:
                self.assertEqual(stream.read(), "complete identity\n")

    def test_concurrent_native_writers_never_share_a_temp_or_publish_partial(self):
        with tempfile.TemporaryDirectory(prefix="crew-concurrent-test-") as home:
            workers = 16
            barrier = threading.Barrier(workers)

            def write(index):
                barrier.wait()
                block = f"writer-{index}\n" + (str(index % 10) * 100_000)
                identity.write_claude_md(home, block)
                return block

            with concurrent.futures.ThreadPoolExecutor(
                    max_workers=workers) as pool:
                blocks = list(pool.map(write, range(workers)))

            with open(os.path.join(home, "CLAUDE.md")) as stream:
                published = stream.read()
            self.assertEqual(published.count(identity.CREW_BLOCK_BEGIN), 1)
            self.assertEqual(published.count(identity.CREW_BLOCK_END), 1)
            self.assertTrue(any(block.rstrip() in published for block in blocks))


class WorktreeAndRollbackTests(unittest.TestCase):
    @staticmethod
    def _git(repo, *args):
        return subprocess.run(
            ["git", *args], cwd=repo, check=True, capture_output=True,
            text=True).stdout.strip()

    def test_remote_default_branch_with_slash_is_not_truncated(self):
        with tempfile.TemporaryDirectory(prefix="crew-branch-test-") as root:
            repo = os.path.join(root, "source")
            os.mkdir(repo)
            self._git(repo, "init")
            self._git(repo, "config", "user.name", "Crew Test")
            self._git(repo, "config", "user.email", "crew@example.invalid")
            with open(os.path.join(repo, "README.md"), "w") as stream:
                stream.write("fixture\n")
            self._git(repo, "add", "README.md")
            self._git(repo, "commit", "-m", "fixture")
            self._git(repo, "branch", "release/v2")
            self._git(
                repo, "symbolic-ref", "refs/remotes/origin/HEAD",
                "refs/remotes/origin/release/v2")

            worktree, default = spawn._worktree_paths(repo, "worker")
            self.assertEqual(default, "release/v2")
            spawn._create_worktree(
                repo, worktree, default, "crew/default/worker")
            self.assertEqual(
                self._git(worktree, "branch", "--show-current"),
                "crew/default/worker")

    def test_failed_row_create_removes_only_a_new_empty_home(self):
        with tempfile.TemporaryDirectory(prefix="crew-rollback-test-") as root:
            home = os.path.join(root, "new-home")
            tmux_calls = []

            def fake_tmux(*args, **_kwargs):
                tmux_calls.append(args)
                if args[0] == "has-session":
                    return False, ""
                return True, ""

            with mock.patch.object(spawn.config, "current_project", return_value="default"), \
                 mock.patch.object(spawn.guard, "check"), \
                 mock.patch.object(spawn.gs, "get_agent_by_name", return_value=None), \
                 mock.patch.object(spawn.gs, "unsafe_home_reason", return_value=None), \
                 mock.patch.object(spawn.gs, "home_conflict_across_apps", return_value=None), \
                 mock.patch.object(spawn.gs, "_home_claim_lock", return_value=contextlib.nullcontext()), \
                 mock.patch.object(
                     spawn, "_plan_home", return_value=(home, None, ("mkdir",))), \
                 mock.patch.object(spawn, "_open_session", return_value="%fixture"), \
                 mock.patch.object(spawn, "_tmux", side_effect=fake_tmux), \
                 mock.patch.object(
                     spawn.gs, "create_agent",
                     side_effect=gs.GraphError("backend create failed")):
                with self.assertRaisesRegex(gs.GraphError, "backend create failed"):
                    spawn.spawn_agent("worker", launch=False)

            self.assertFalse(os.path.lexists(home))
            self.assertIn(
                ("kill-session", "-t", "=worker"),
                tmux_calls)

    def test_failed_row_create_preserves_a_preexisting_home(self):
        with tempfile.TemporaryDirectory(prefix="crew-rollback-existing-") as root:
            home = os.path.join(root, "existing-home")
            os.mkdir(home)
            marker = os.path.join(home, "user-file")
            with open(marker, "w") as stream:
                stream.write("preserve")

            def fake_tmux(*args, **_kwargs):
                return (False, "") if args[0] == "has-session" else (True, "")

            with mock.patch.object(spawn.config, "current_project", return_value="default"), \
                 mock.patch.object(spawn.guard, "check"), \
                 mock.patch.object(spawn.gs, "get_agent_by_name", return_value=None), \
                 mock.patch.object(spawn.gs, "unsafe_home_reason", return_value=None), \
                 mock.patch.object(spawn.gs, "home_conflict_across_apps", return_value=None), \
                 mock.patch.object(spawn.gs, "_home_claim_lock", return_value=contextlib.nullcontext()), \
                 mock.patch.object(
                     spawn, "_plan_home", return_value=(home, None, ("mkdir",))), \
                 mock.patch.object(spawn, "_open_session", return_value="%fixture"), \
                 mock.patch.object(spawn, "_tmux", side_effect=fake_tmux), \
                 mock.patch.object(
                     spawn.gs, "create_agent",
                     side_effect=gs.GraphError("backend create failed")):
                with self.assertRaisesRegex(gs.GraphError, "backend create failed"):
                    spawn.spawn_agent("worker", launch=False)

            with open(marker) as stream:
                self.assertEqual(stream.read(), "preserve")

    def test_identity_refusal_after_row_create_marks_runtime_not_started(self):
        agent = {
            "_guid": "created-guid", "name": "worker",
            "home": "/tmp/worker", "session": "worker",
            "runtime": "custom", "launch_cmd": "true", "status": "idle",
        }
        with mock.patch.object(spawn.config, "current_project", return_value="default"), \
             mock.patch.object(spawn.guard, "check"), \
             mock.patch.object(
                 spawn.gs, "get_agent_by_name",
                 side_effect=[None, None, agent]), \
             mock.patch.object(spawn.gs, "unsafe_home_reason", return_value=None), \
             mock.patch.object(spawn.gs, "home_conflict_across_apps", return_value=None), \
             mock.patch.object(spawn.gs, "_home_claim_lock", return_value=contextlib.nullcontext()), \
             mock.patch.object(
                 spawn, "_plan_home",
                 return_value=("/tmp/worker", None, ("mkdir",))), \
             mock.patch.object(spawn, "_materialize_home"), \
             mock.patch.object(spawn, "_open_session", return_value="%fixture"), \
             mock.patch.object(spawn, "_tmux", return_value=(False, "")), \
             mock.patch.object(spawn.gs, "create_agent", return_value=agent), \
             mock.patch.object(
                 spawn, "rewrite_identity",
                 side_effect=gs.GraphError("identity destination is a symlink")), \
             mock.patch.object(
                 spawn.gs, "update_agent_runtime_state") as update_state, \
             mock.patch.object(spawn, "_launch_runtime") as launch:
            with self.assertRaisesRegex(gs.GraphError, "symlink"):
                spawn.spawn_agent(
                    "worker", runtime="custom", launch_cmd="true",
                    launch=True)

        update_state.assert_called_once_with(
            "created-guid", status="not_started")
        launch.assert_not_called()

    def test_ambiguous_create_failure_preserves_resources_if_row_exists(self):
        with tempfile.TemporaryDirectory(prefix="crew-rollback-ambiguous-") as root:
            home = os.path.join(root, "new-home")
            durable = {
                "_guid": "inserted-guid", "name": "worker", "home": home,
                "session": "worker", "runtime": "claude",
            }
            tmux_calls = []

            def fake_tmux(*args, **_kwargs):
                tmux_calls.append(args)
                return (False, "") if args[0] == "has-session" else (True, "")

            with mock.patch.object(spawn.config, "current_project", return_value="default"), \
                 mock.patch.object(spawn.guard, "check"), \
                 mock.patch.object(
                     spawn.gs, "get_agent_by_name",
                     side_effect=[None, None, durable]), \
                 mock.patch.object(spawn.gs, "unsafe_home_reason", return_value=None), \
                 mock.patch.object(spawn.gs, "home_conflict_across_apps", return_value=None), \
                 mock.patch.object(spawn.gs, "_home_claim_lock", return_value=contextlib.nullcontext()), \
                 mock.patch.object(
                     spawn, "_plan_home", return_value=(home, None, ("mkdir",))), \
                 mock.patch.object(spawn, "_open_session", return_value="%fixture"), \
                 mock.patch.object(spawn, "_tmux", side_effect=fake_tmux), \
                 mock.patch.object(
                     spawn.gs, "create_agent",
                     side_effect=gs.GraphError("response lost after insert")):
                with self.assertRaisesRegex(gs.GraphError, "response lost"):
                    spawn.spawn_agent("worker", launch=False)

            self.assertTrue(os.path.isdir(home))
            self.assertNotIn(("kill-session", "-t", "=worker"), tmux_calls)


@unittest.skipUnless(
    os.environ.get("CREW_LIVE_TESTS") == "1",
    "set CREW_LIVE_TESTS=1 for isolated MorphDB + private tmux coverage")
class IsolatedLiveLifecycleTests(unittest.TestCase):
    """Real write/session coverage on a private tmux socket and throwaway app."""

    def setUp(self):
        run = f"{os.getpid()}-{int(time.time() * 1_000_000)}"
        self.app = f"crewtest-spawnid-{run}"
        self.tmp = tempfile.TemporaryDirectory(prefix="crew-spawnid-live-")
        self.addCleanup(self.tmp.cleanup)
        # Exercise Crew's real endpoint shape.  A name-based ``tmux -L`` socket
        # is relative to TMUX_TMPDIR, so the same name resolves somewhere else
        # after the managed pane imports its pinned context.  An explicit short
        # ``-S`` path is stable both outside and inside that pane.
        self.tmux_root = tempfile.TemporaryDirectory(
            prefix="crew-spawnid-tmux-", dir="/tmp")
        self.addCleanup(self.tmux_root.cleanup)
        self.tmux_endpoint = mock.patch.object(
            spawn.config, "_TMUX_TMPDIR_TEST_OVERRIDE", self.tmux_root.name)
        self.tmux_endpoint.start()
        self.addCleanup(self.tmux_endpoint.stop)
        self.env = mock.patch.dict(os.environ, {
            "CREW_APP": self.app,
            "CREW_PROJECT": "default",
            "CREW_ROOT": self.tmp.name,
        }, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.host = mock.patch.object(
            spawn.config, "MORPHDB_HOST", "127.0.0.1:18787")
        self.host.start()
        self.addCleanup(self.host.stop)
        self.addCleanup(self._cleanup_backend_and_tmux)
        schema.ensure_schema(self.app)

    def _private_tmux(self, *args, timeout=10):
        try:
            result = subprocess.run(
                spawn.config.tmux_command(*args),
                env=spawn.config.tmux_environment(), capture_output=True,
                text=True, timeout=timeout)
        except (OSError, subprocess.SubprocessError) as error:
            return False, str(error)
        text = result.stdout.strip() if result.returncode == 0 \
            else result.stderr.strip()
        return result.returncode == 0, text

    def _cleanup_backend_and_tmux(self):
        subprocess.run(
            spawn.config.tmux_command("kill-server"),
            env=spawn.config.tmux_environment(), capture_output=True,
            text=True, timeout=5)
        try:
            gs._req("DELETE", f"/app/{self.app}", app=None)
        except gs.GraphError:
            pass

    def test_long_path_fits_fresh_interactive_shell_context_handshake(self):
        """The startup command itself stays below the tty canonical limit.

        A prior completion check embedded the full expected PATH in the bytes
        sent to a brand-new shell.  With a realistic developer PATH that made
        the command longer than 1024 bytes; the tty truncated it before zsh
        enabled line editing, so wait-for timed out even though every session
        value was correct.
        """
        name = "long_path_worker"
        home = os.path.join(self.tmp.name, name)
        os.mkdir(home)
        long_parent_path = os.pathsep.join(
            [f"/tmp/crew-path-segment-{index:03d}" for index in range(100)]
            + [os.environ.get("PATH", "")])
        with mock.patch.dict(os.environ, {"PATH": long_parent_path}, clear=False):
            expected_path = spawn.runtimes.agent_path()
            self.assertGreater(len(expected_path.encode()), 2048)
            pane = spawn._open_session(
                name, home, name, "default", runtime_key="custom")

        self.assertTrue(pane)
        ok, raw = self._private_tmux(
            "show-environment", "-t", f"={name}", "PATH")
        self.assertTrue(ok, raw)
        self.assertEqual(raw.split("=", 1)[1], expected_path)

    def test_failed_builtin_launch_clears_shell_continuation_before_retry(self):
        name = "launch_retry_worker"
        home = os.path.join(self.tmp.name, name)
        marker = os.path.join(self.tmp.name, "launch-retry-succeeded")
        os.mkdir(home)
        pane = spawn._open_session(
            name, home, name, "default", runtime_key="custom")

        # This is executable and shell-valid, but leaves zsh waiting for a
        # loop body/`done`.  It models a launch line truncated after an opening
        # construct: typing the retry into that continuation must never be the
        # recovery mechanism.
        with mock.patch.object(
                 spawn, "_RUNTIME_READY_TIMEOUT_SECONDS", 0.0):
            with self.assertRaisesRegex(gs.GraphError, "did not stay running"):
                spawn._launch_runtime(
                    name, home, "/usr/bin/true ; while true; do", "codex",
                    pane=pane)

        spawn._launch_runtime(
            name, home, f"/usr/bin/touch {marker}", "custom", pane=pane)
        deadline = time.time() + 3
        while time.time() < deadline and not os.path.exists(marker):
            time.sleep(0.05)
        self.assertTrue(
            os.path.exists(marker),
            "retry command remained trapped in the failed launch continuation")

    def test_real_spawn_down_up_remove_round_trip(self):
        name = "live_worker"
        home = os.path.join(self.tmp.name, name)
        agent = spawn.spawn_agent(
            name, home=home, runtime="custom", launch_cmd="true",
            launch=False)
        self.assertEqual(agent["session"], name)
        self.assertTrue(self._private_tmux("has-session", "-t", f"={name}")[0])
        self.assertTrue(os.path.isfile(os.path.join(home, "identity.md")))

        self.assertTrue(spawn.stop_session(name))
        self.assertFalse(self._private_tmux("has-session", "-t", f"={name}")[0])

        revived = spawn.start_session(name)
        self.assertEqual(revived["status"], "idle")
        self.assertTrue(self._private_tmux("has-session", "-t", f"={name}")[0])

        removed = spawn.remove_agent(name)
        self.assertEqual(removed["name"], name)
        self.assertIsNone(gs.get_agent_by_name(name))
        self.assertFalse(self._private_tmux("has-session", "-t", f"={name}")[0])

    def test_corrupt_row_cannot_kill_private_foreign_session(self):
        foreign = "foreign_session"
        ok, error = self._private_tmux(
            "new-session", "-d", "-s", foreign, "-n", "shell")
        self.assertTrue(ok, error)
        name = "corrupt_worker"
        home = os.path.join(self.tmp.name, name)
        os.mkdir(home)
        agent = gs.create_agent(
            name, home=home, session=foreign, pane="", runtime="custom",
            launch_cmd="true", status="not_started")
        with self.assertRaisesRegex(gs.GraphError, "stored tmux session"):
            spawn.stop_session(name)
        self.assertTrue(
            self._private_tmux("has-session", "-t", f"={foreign}")[0])
        gs.delete_agent(agent["_guid"])

    def test_missing_recorded_worktree_preserves_live_row_without_session(self):
        name = "live_missing_worktree"
        home = os.path.join(self.tmp.name, "repo-worktrees", name)
        agent = gs.create_agent(
            name, home=home, session=name, pane="", worktree=name,
            runtime="custom", launch_cmd="sleep 30", status="not_started")
        try:
            with self.assertRaisesRegex(
                    gs.GraphError, "recorded git worktree.*missing"):
                spawn.start_session(name)

            persisted = gs.get_agent_by_name(name)
            self.assertEqual(persisted["_guid"], agent["_guid"])
            self.assertFalse(os.path.lexists(home))
            self.assertFalse(
                self._private_tmux("has-session", "-t", f"={name}")[0])
        finally:
            if gs.get_agent_by_name(name):
                spawn.remove_agent(name)

    def test_builtin_launch_failures_stay_durable_and_retriable(self):
        cases = (
            ("live_missing_exec", "crew-no-such-codex-executable",
             "launch executable.*not available"),
            ("live_immediate_exit", "/usr/bin/true",
             "Codex CLI.*did not stay running"),
        )
        for name, command, error in cases:
            with self.subTest(command=command):
                spawn.spawn_agent(
                    name, home=os.path.join(self.tmp.name, name),
                    runtime="codex", launch_cmd=command, launch=False)
                try:
                    self.assertTrue(spawn.stop_session(name))
                    with self.assertRaisesRegex(gs.GraphError, error):
                        spawn.start_session(name)

                    persisted = gs.get_agent_by_name(name)
                    self.assertIsNotNone(persisted)
                    self.assertEqual(persisted["status"], "not_started")
                    self.assertTrue(self._private_tmux(
                        "has-session", "-t", f"={name}")[0])
                finally:
                    if gs.get_agent_by_name(name):
                        spawn.remove_agent(name)
                self.assertIsNone(gs.get_agent_by_name(name))

    def test_kill_failure_preserves_live_row_and_incident_edge_for_retry(self):
        left_name = "live_remove_left"
        right_name = "live_remove_right"
        left = spawn.spawn_agent(
            left_name, home=os.path.join(self.tmp.name, left_name),
            runtime="custom", launch_cmd="true", launch=False)
        right = spawn.spawn_agent(
            right_name, home=os.path.join(self.tmp.name, right_name),
            runtime="custom", launch_cmd="true", launch=False)
        edge = gs.create_edge(
            left["_guid"], right["_guid"], conditions=["handoff"])
        spawn.rewrite_identity(right)
        right_identity = os.path.join(right["home"], "identity.md")
        with open(right_identity) as stream:
            self.assertIn(left_name, stream.read())

        def refuse_left_kill(*args, **kwargs):
            if (args[:3] == ("kill-session", "-t", f"={left_name}")):
                return False, "injected private tmux kill failure"
            return self._private_tmux(*args, **kwargs)

        try:
            with mock.patch.object(
                    spawn, "_tmux", side_effect=refuse_left_kill):
                with self.assertRaisesRegex(
                        gs.GraphError, "injected private tmux kill failure"):
                    spawn.remove_agent(left_name)

            self.assertEqual(
                gs.get_agent_by_name(left_name)["_guid"], left["_guid"])
            self.assertEqual(gs.get_object(edge["_guid"])["_guid"], edge["_guid"])
            self.assertTrue(self._private_tmux(
                "has-session", "-t", f"={left_name}")[0])
            with open(right_identity) as stream:
                self.assertIn(left_name, stream.read())

            real_rewrite = spawn.rewrite_identity

            def fail_forward_identity(
                    agent, notify=False, exclude_agent_guids=None):
                if exclude_agent_guids:
                    raise gs.GraphError("injected survivor identity failure")
                return real_rewrite(
                    agent, notify=notify,
                    exclude_agent_guids=exclude_agent_guids)

            with mock.patch.object(
                    spawn, "rewrite_identity",
                    side_effect=fail_forward_identity):
                with self.assertRaisesRegex(
                        gs.GraphError, "injected survivor identity failure") as caught:
                    spawn.remove_agent(left_name)
            self.assertNotIn("rollback incomplete", str(caught.exception))

            self.assertEqual(
                gs.get_agent_by_name(left_name)["_guid"], left["_guid"])
            self.assertEqual(gs.get_object(edge["_guid"])["_guid"], edge["_guid"])
            self.assertFalse(self._private_tmux(
                "has-session", "-t", f"={left_name}")[0])
            with open(right_identity) as stream:
                self.assertIn(left_name, stream.read())

            spawn.remove_agent(left_name)
            self.assertIsNone(gs.get_agent_by_name(left_name))
            with self.assertRaises(gs.GraphError):
                gs.get_object(edge["_guid"])
            with open(right_identity) as stream:
                self.assertNotIn(left_name, stream.read())
        finally:
            for name in (left_name, right_name):
                if gs.get_agent_by_name(name):
                    spawn.remove_agent(name)

    def test_ownership_flip_preserves_live_row_edge_and_replacement_session(self):
        left_name = "live_owner_flip_left"
        right_name = "live_owner_flip_right"
        left_home = os.path.join(self.tmp.name, left_name)
        right_home = os.path.join(self.tmp.name, right_name)
        os.makedirs(left_home)
        os.makedirs(right_home)
        ok, pane = self._private_tmux(
            "new-session", "-d", "-P", "-F", "#{pane_id}",
            "-s", left_name, "-n", "shell")
        self.assertTrue(ok, pane)
        left = gs.create_agent(
            left_name, home=left_home, session=left_name, pane=pane,
            runtime="custom", launch_cmd="true", status="not_started")
        right = gs.create_agent(
            right_name, home=right_home, session=right_name, pane="",
            runtime="custom", launch_cmd="true", status="not_started")
        edge = gs.create_edge(
            left["_guid"], right["_guid"], conditions=["handoff"])
        initial = spawn.config.tmux_target(
            left["session"], spawn.config.TMUX_ENDPOINT_CREW)
        initial_locations = {
            "session": left["session"], "owned": initial,
            "dedicated_exists": True, "legacy_exists": False,
        }
        replacement_locations = {
            "session": left["session"], "owned": None,
            "dedicated_exists": True, "legacy_exists": False,
        }

        with mock.patch.object(
                spawn, "_session_locations",
                side_effect=[initial_locations, replacement_locations]):
            with self.assertRaisesRegex(
                    gs.GraphError, "ownership changed|retry"):
                spawn.remove_agent(left_name)

        self.assertEqual(
            gs.get_agent_by_name(left_name)["_guid"], left["_guid"])
        self.assertEqual(gs.get_object(edge["_guid"])["_guid"], edge["_guid"])
        self.assertTrue(self._private_tmux(
            "has-session", "-t", f"={left['session']}")[0])

    def test_cross_process_agent_commit_rechecks_all_spawn_quotas(self):
        cases = (
            ("max", 2, 100, True),
            ("rate", 100, 1, True),
            ("foreman", 100, 100, False),
        )
        ctx = multiprocessing.get_context("spawn")
        for mode, max_agents, spawn_rate, needs_actor in cases:
            with self.subTest(mode=mode):
                app = f"{self.app}-{mode}"
                with mock.patch.dict(os.environ, {"CREW_APP": app}, clear=False):
                    schema.ensure_schema(app)
                    if needs_actor:
                        gs.create_agent(
                            "race_foreman", home=f"/tmp/{app}-foreman",
                            runtime="custom", launch_cmd="true",
                            status="not_started", can_edit_graph=True)
                barrier = ctx.Barrier(2)
                results = ctx.Queue()
                actor = "race_foreman" if needs_actor else "human"
                processes = [
                    ctx.Process(
                        target=_cross_process_create_agent,
                        args=(app, f"race_{mode}_{index}", actor, mode,
                              max_agents, spawn_rate, barrier, results))
                    for index in range(2)
                ]
                try:
                    for process in processes:
                        process.start()
                    for process in processes:
                        process.join(15)
                    outcomes = [results.get(timeout=2) for _ in processes]
                    self.assertEqual(
                        sum(status == "ok" for _, status, _ in outcomes), 1,
                        outcomes)
                    self.assertEqual(
                        sum(status == "error" for _, status, _ in outcomes), 1,
                        outcomes)
                    self.assertTrue(all(
                        process.exitcode == 0 for process in processes),
                        [(process.pid, process.exitcode) for process in processes])
                finally:
                    for process in processes:
                        if process.is_alive():
                            process.terminate()
                            process.join(5)
                    try:
                        gs._req("DELETE", f"/app/{app}", app=None)
                    except gs.GraphError:
                        pass


if __name__ == "__main__":
    unittest.main()
