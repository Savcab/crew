"""CLI actor resolution must fail closed for an owned crew pane."""
import argparse
import contextlib
import io
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from crew import cli, config, graphstore as gs  # noqa: E402


class ReservedAgentNameTests(unittest.TestCase):
    def test_operator_and_system_actor_names_are_reserved_case_insensitively(self):
        for name in ("human", "Human", "HUMAN", "unknown", "Unknown",
                     "crew", "CREW"):
            with self.subTest(name=name):
                self.assertFalse(config.valid_agent_name(name))

    def test_nearby_ordinary_agent_names_remain_valid(self):
        for name in ("human_reviewer", "crew-builder", "unknowns"):
            with self.subTest(name=name):
                self.assertTrue(config.valid_agent_name(name))


class CliHelpContractTests(unittest.TestCase):
    def test_top_level_help_describes_current_governance_and_identity(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as exit_:
            cli.build_parser().parse_args(["--help"])
        self.assertEqual(exit_.exception.code, 0)
        help_text = out.getvalue()
        self.assertNotIn("a later wave", help_text)
        self.assertNotIn("$CREW_AGENT is pinned", help_text)
        self.assertIn("live managed tmux pane", help_text)
        self.assertIn("bounded foreman", help_text)

    def test_subcommand_help_describes_named_worktree_and_transform_timing(self):
        for argv, expected in (
            (["spawn-agent", "--help"], "persistent named worktree branch"),
            (["connect", "--help"], "runs once before queueing or delivery"),
        ):
            with self.subTest(argv=argv):
                out = io.StringIO()
                with contextlib.redirect_stdout(out), \
                     self.assertRaises(SystemExit) as exit_:
                    cli.build_parser().parse_args(argv)
                self.assertEqual(exit_.exception.code, 0)
                self.assertIn(expected, out.getvalue())

    def test_mail_help_exposes_every_durable_delivery_status(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as exit_:
            cli.build_parser().parse_args(["mail", "--help"])
        self.assertEqual(exit_.exception.code, 0)
        help_text = out.getvalue()
        for status in ("queued", "submitting", "delivered", "runtime_queued",
                       "delivery_uncertain", "failed", "blocked",
                       "ratelimited", "budget", "budget_unavailable",
                       "filtered"):
            with self.subTest(status=status):
                self.assertIn(status, help_text)

    def test_no_prefix_help_does_not_promise_unsafe_verbatim_delivery(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as exit_:
            cli.build_parser().parse_args(["message", "--help"])
        self.assertEqual(exit_.exception.code, 0)
        help_text = out.getvalue()
        self.assertNotIn("deliver verbatim", help_text)
        self.assertIn("delivery safety and queueing still apply", help_text)


class CliMailObservabilityTests(unittest.TestCase):
    def test_status_counts_retryable_failed_and_attention_states_separately(self):
        messages = {
            "queued": [{"target": "alice"}],
            "failed": [{"target": "alice"}],
            "submitting": [{"target": "alice"}],
            "delivery_uncertain": [
                {"target": "alice"}, {"target": "alice"}],
            "runtime_queued": [{"target": "alice"}],
        }

        def list_messages(*, status, limit):
            self.assertEqual(limit, 2000)
            return messages.get(status, [])

        out = io.StringIO()
        agent = {"_guid": "agent-alice", "name": "alice",
                 "runtime": "codex", "session": "alice", "role": "builder"}
        live = {"runtime": "codex", "session_alive": True,
                "runtime_alive": True, "live_status": "idle",
                "migration_required": False}
        inventory = {
            "agent-alice": {"session": "alice", "pane": "%1"},
        }
        with mock.patch.object(cli.gs, "list_agents", return_value=[agent]), \
             mock.patch.object(cli.gs, "list_messages",
                               side_effect=list_messages), \
             mock.patch.object(cli.tmuxio, "live_agent_inventory",
                               return_value=inventory), \
             mock.patch.object(cli.tmuxio, "agent_snapshot_fields",
                               return_value=live), \
             contextlib.redirect_stdout(out):
            self.assertEqual(cli.cmd_status(None), 0)
        lines = out.getvalue().splitlines()
        self.assertIn("attention", lines[0])
        self.assertEqual(lines[1].split()[:7],
                         ["alice", "codex", "up", "idle", "1", "3", "1"])

    def test_mail_prints_long_status_and_attention_detail(self):
        row = {
            "_guid": "message-guid", "sender": "alice", "target": "bob",
            "status": "delivery_uncertain",
            "status_detail": "tmux may have accepted the input",
            "body": "hello", "created_at": 1,
        }
        out = io.StringIO()
        args = argparse.Namespace(agent=None, status=None, n=20)
        with mock.patch.object(
                cli.gs, "list_objects", return_value={"objects": [row]}), \
             contextlib.redirect_stdout(out):
            self.assertEqual(cli.cmd_mail(args), 0)
        text = out.getvalue()
        self.assertIn("delivery_uncertain", text)
        self.assertIn("tmux may have accepted the input", text)


class MalformedAgentRowTests(unittest.TestCase):
    """Operator surfaces quarantine rows that have no usable durable identity."""

    BAD_GUID = "malformed-agent-guid"

    @staticmethod
    def _valid_sparse(**fields):
        row = {
            "_guid": "valid-agent-guid",
            "name": "alice",
            "session": None,
            "home": None,
            "runtime": None,
            "status": None,
            "role": None,
        }
        row.update(fields)
        return row

    @classmethod
    def _malformed(cls, **fields):
        row = {
            "_guid": cls.BAD_GUID,
            "name": None,
            "session": None,
            "home": None,
            "runtime": None,
            "status": None,
            "role": None,
        }
        row.update(fields)
        return row

    def _assert_warned(self, err):
        self.assertIn(self.BAD_GUID, err.getvalue())

    def test_agents_keeps_a_sparse_legacy_row_and_quarantines_bad_identity(self):
        valid = self._valid_sparse()
        malformed = self._malformed()
        out, err = io.StringIO(), io.StringIO()

        with mock.patch.object(
                cli.gs, "list_agents", return_value=[valid, malformed]), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(cli.cmd_agents(None), 0)

        self.assertIn("alice", out.getvalue())
        self.assertIn("[claude]", out.getvalue())
        self.assertNotIn(self.BAD_GUID, out.getvalue())
        self._assert_warned(err)

    def test_status_only_probes_and_renders_identity_valid_rows(self):
        valid = self._valid_sparse()
        malformed = self._malformed()
        out, err = io.StringIO(), io.StringIO()
        live = {
            "runtime": "claude", "session_alive": False,
            "runtime_alive": False, "live_status": "down",
            "migration_required": False,
        }

        def inventory(rows):
            self.assertEqual(rows, [valid])
            return {"valid-agent-guid": {"session": None, "pane": None}}

        with mock.patch.object(
                cli.gs, "list_agents", return_value=[valid, malformed]), \
             mock.patch.object(cli.gs, "list_messages", return_value=[]), \
             mock.patch.object(
                 cli.tmuxio, "live_agent_inventory", side_effect=inventory), \
             mock.patch.object(
                 cli.tmuxio, "agent_snapshot_fields", return_value=live) as snapshot, \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(cli.cmd_status(None), 0)

        snapshot.assert_called_once()
        self.assertEqual(snapshot.call_args.args[0], valid)
        self.assertIn("alice", out.getvalue())
        self.assertNotIn(self.BAD_GUID, out.getvalue())
        self._assert_warned(err)

    def test_edges_resolve_valid_names_and_hide_malformed_endpoint_names(self):
        valid = self._valid_sparse()
        malformed = self._malformed()
        edge = {
            "_guid": "edge-guid", "source": valid["_guid"],
            "target": malformed["_guid"], "directed": True,
        }
        out, err = io.StringIO(), io.StringIO()

        with mock.patch.object(cli.gs, "list_edges", return_value=[edge]), \
             mock.patch.object(
                 cli.gs, "list_agents", return_value=[valid, malformed]), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(cli.cmd_edges(None), 0)

        self.assertIn("alice -> ?", out.getvalue())
        self.assertNotIn("None", out.getvalue())
        self._assert_warned(err)

    def test_peers_hides_a_malformed_neighbor_name(self):
        valid = self._valid_sparse()
        malformed = self._malformed()
        edge = {"condition": "", "directed": True}
        args = argparse.Namespace(name="alice")
        out, err = io.StringIO(), io.StringIO()

        with mock.patch.object(
                cli.gs, "get_agent_by_name", return_value=valid), \
             mock.patch.object(
                 cli.gs, "list_agents", return_value=[valid, malformed]), \
             mock.patch.object(
                 cli.gs, "messageable_targets",
                 return_value=[(malformed["_guid"], edge)]), \
             mock.patch.object(cli.gs, "incoming_edges", return_value=[]), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(cli.cmd_peers(args), 0)

        self.assertIn("→ ?", out.getvalue())
        self.assertNotIn("None", out.getvalue())
        self._assert_warned(err)

    def test_grants_ignores_grants_attached_to_a_malformed_identity(self):
        valid = self._valid_sparse(grants=[{
            "name": "docs", "mode": "ro", "path": "/tmp/alice-docs",
            "created_at": 1,
        }])
        malformed = self._malformed(grants=[{
            "name": "secrets", "mode": "rw", "path": "/tmp/bad-secrets",
            "created_at": 1,
        }])
        args = argparse.Namespace(agent=None)
        out, err = io.StringIO(), io.StringIO()

        with mock.patch.object(
                cli.gs, "list_agents", return_value=[valid, malformed]), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(cli.cmd_grants(args), 0)

        self.assertIn("alice", out.getvalue())
        self.assertIn("/tmp/alice-docs", out.getvalue())
        self.assertNotIn("/tmp/bad-secrets", out.getvalue())
        self._assert_warned(err)

    def test_lifecycle_all_never_targets_a_malformed_identity(self):
        args = argparse.Namespace(all=True, name=None)

        for command in (cli.cmd_up, cli.cmd_down, cli.cmd_restart):
            with self.subTest(command=command.__name__):
                valid = self._valid_sparse()
                malformed = self._malformed()
                out, err = io.StringIO(), io.StringIO()

                def inventory(rows):
                    self.assertEqual(rows, [valid])
                    return {"valid-agent-guid": {
                        "session": None, "pane": None}}

                with mock.patch.object(
                        cli.gs, "list_agents", return_value=[valid, malformed]), \
                     mock.patch.object(
                         cli.tmuxio, "live_agent_inventory",
                         side_effect=inventory), \
                     mock.patch.object(
                         cli.spawn, "validated_session_name",
                         return_value="alice") as validated, \
                     mock.patch.object(
                         cli.spawn, "start_session", return_value=valid) as start, \
                     mock.patch.object(
                         cli.spawn, "stop_session", return_value=False) as stop, \
                     contextlib.redirect_stdout(out), \
                     contextlib.redirect_stderr(err):
                    self.assertEqual(command(args), 0)

                if command in (cli.cmd_up, cli.cmd_restart):
                    start.assert_called_once_with("alice", actor="human")
                else:
                    start.assert_not_called()
                if command in (cli.cmd_down, cli.cmd_restart):
                    expected = (
                        mock.call("alice", actor="human", refuse_legacy=True)
                        if command is cli.cmd_restart
                        else mock.call("alice", actor="human"))
                    self.assertEqual(stop.call_args_list, [expected])
                else:
                    stop.assert_not_called()
                    validated.assert_called_once()
                    self.assertIs(validated.call_args.args[0], valid)
                self.assertIn("alice", out.getvalue())
                self._assert_warned(err)


class CliActorResolutionTests(unittest.TestCase):
    def _managed_actor(self, foreman=False):
        return mock.patch.multiple(
            cli,
            _ACTOR="human",
        ), mock.patch.object(cli.mail, "whoami", return_value="alice"), \
            mock.patch.object(
                cli.gs, "get_agent_by_name",
                return_value={"name": "alice", "can_edit_graph": foreman}), \
            mock.patch.object(cli.guard, "audit")

    def test_invalid_project_flag_fails_before_identity_or_backend(self):
        err = io.StringIO()
        with mock.patch.dict(os.environ, {}, clear=False), \
             mock.patch.object(cli.mail, "whoami", return_value="unknown") as who, \
             mock.patch.object(cli.gs, "list_agents") as listed, \
             contextlib.redirect_stderr(err):
            rc = cli.main(["--project", "../escape", "agents"])
        self.assertEqual(rc, 1)
        self.assertIn("invalid project", err.getvalue())
        who.assert_not_called()
        listed.assert_not_called()

    def test_invalid_project_env_fails_before_identity_or_backend(self):
        err = io.StringIO()
        with mock.patch.dict(os.environ, {"CREW_PROJECT": "has space"}, clear=False), \
             mock.patch.object(cli.mail, "whoami", return_value="unknown") as who, \
             mock.patch.object(cli.gs, "list_agents") as listed, \
             contextlib.redirect_stderr(err):
            rc = cli.main(["agents"])
        self.assertEqual(rc, 1)
        self.assertIn("invalid project", err.getvalue())
        who.assert_not_called()
        listed.assert_not_called()

    def test_resolution_error_does_not_fall_open_to_human(self):
        err = io.StringIO()
        with mock.patch.object(
                cli.mail, "whoami",
                side_effect=gs.GraphError("agent pane belongs to project demo")), \
             mock.patch.object(cli.gs, "list_agents", return_value=[]), \
             contextlib.redirect_stderr(err):
            rc = cli.main(["agents"])
        self.assertEqual(rc, 1)
        self.assertIn("belongs to project demo", err.getvalue())

    def test_unresolved_inherited_agent_marker_cannot_default_to_human(self):
        err = io.StringIO()
        env = {
            "CREW_AGENT": "deleted-or-forged-agent",
            "AGENT_MAIL_NAME": "",
        }
        with mock.patch.dict(os.environ, env, clear=False), \
             mock.patch.object(cli.mail, "whoami", return_value="unknown"), \
             mock.patch.object(cli.gs, "get_agent_by_name", return_value=None), \
             mock.patch.object(cli, "_ensure_morphdb") as ensure, \
             mock.patch.object(cli.schema, "ensure_schema") as schema_write, \
             mock.patch.object(cli.config, "register_project") as register, \
             contextlib.redirect_stderr(err):
            rc = cli.main(["project", "create", "forged-human-project"])

        self.assertEqual(rc, 1)
        self.assertIn("caller identity", err.getvalue().lower())
        ensure.assert_not_called()
        schema_write.assert_not_called()
        register.assert_not_called()

    def test_kickoff_carries_resolved_agent_actor_to_mail_gate(self):
        err = io.StringIO()
        with mock.patch.object(cli.mail, "whoami", return_value="alice"), \
             mock.patch.object(
                 cli.gs, "get_agent_by_name", return_value={"name": "alice"}), \
             mock.patch.object(
                 cli.mail, "say_to_agent",
                 return_value=(False, "operator kickoff is human-only")) as send, \
             contextlib.redirect_stderr(err):
            rc = cli.main(["kickoff", "bob", "do", "the", "thing"])
        self.assertEqual(rc, 1)
        send.assert_called_once_with(
            "bob", "do the thing", actor="alice")
        self.assertIn("human-only", err.getvalue())

    def test_managed_agent_and_foreman_cannot_create_projects(self):
        for foreman in (False, True):
            err = io.StringIO()
            actor, who, registered, audit = self._managed_actor(foreman)
            with self.subTest(foreman=foreman), actor, who, registered, audit, \
                 mock.patch.object(cli, "_ensure_morphdb") as ensure, \
                 mock.patch.object(cli.schema, "ensure_schema") as schema_write, \
                 mock.patch.object(cli.config, "register_project") as register, \
                 contextlib.redirect_stderr(err):
                rc = cli.main(["project", "create", "evil"])
            self.assertEqual(rc, 1)
            self.assertIn("human", err.getvalue().lower())
            ensure.assert_not_called()
            schema_write.assert_not_called()
            register.assert_not_called()

    def test_managed_agent_and_foreman_cannot_initialize_control_plane(self):
        for foreman in (False, True):
            err = io.StringIO()
            actor, who, registered, audit = self._managed_actor(foreman)
            with self.subTest(foreman=foreman), actor, who, registered, audit, \
                 mock.patch.object(cli, "_ensure_morphdb") as ensure, \
                 mock.patch.object(cli.schema, "ensure_schema") as schema_write, \
                 mock.patch.object(
                     cli, "start_dashboard",
                     return_value=("http://127.0.0.1:1", True)) as start, \
                 contextlib.redirect_stdout(io.StringIO()), \
                 contextlib.redirect_stderr(err):
                rc = cli.main(["init"])
            self.assertEqual(rc, 1)
            self.assertIn("human", err.getvalue().lower())
            ensure.assert_not_called()
            schema_write.assert_not_called()
            start.assert_not_called()

    def test_managed_agent_and_foreman_cannot_control_dashboard(self):
        for foreman in (False, True):
            for action in ("start", "stop", "open"):
                err = io.StringIO()
                actor, who, registered, audit = self._managed_actor(foreman)
                with self.subTest(foreman=foreman, action=action), \
                     actor, who, registered, audit, \
                     mock.patch.object(
                         cli, "start_dashboard",
                         return_value=("http://127.0.0.1:1", True)) as start, \
                     mock.patch.object(
                         cli, "stop_dashboard", return_value=True) as stop, \
                     mock.patch("webbrowser.open") as browser_open, \
                     contextlib.redirect_stdout(io.StringIO()), \
                     contextlib.redirect_stderr(err):
                    rc = cli.main(["dashboard", action])
                self.assertEqual(rc, 1)
                self.assertIn("human", err.getvalue().lower())
                start.assert_not_called()
                stop.assert_not_called()
                browser_open.assert_not_called()


if __name__ == "__main__":
    unittest.main()
