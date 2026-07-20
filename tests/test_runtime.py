"""Runtime-adapter foundation tests.

These are deliberately split between pure adapter/identity behavior and mocked
boundary tests.  The real no-launch tmux path lives in test_cli_live.py so the
schema write, session environment, and CLI identity are also exercised without
ever starting Claude or Codex.
"""
import contextlib
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from crew import cli, config, graphstore as gs, guard, identity, mail, schema, spawn
from crew.server import app as dashboard_app
from crew.server import tmuxio


_RUNTIME_LOCK_TMP = None
_OLD_INVARIANT_LOCK_DIR = None


def setUpModule():
    """Keep mocked spawn tests from leaving production lock artifacts in var/."""
    global _RUNTIME_LOCK_TMP, _OLD_INVARIANT_LOCK_DIR
    _RUNTIME_LOCK_TMP = tempfile.TemporaryDirectory(
        prefix="crew-runtime-test-locks-")
    _OLD_INVARIANT_LOCK_DIR = gs._INVARIANT_LOCK_DIR
    gs._INVARIANT_LOCK_DIR = os.path.join(
        _RUNTIME_LOCK_TMP.name, "graph-locks")


def tearDownModule():
    gs._INVARIANT_LOCK_DIR = _OLD_INVARIANT_LOCK_DIR
    _RUNTIME_LOCK_TMP.cleanup()


class RuntimeRegistryTests(unittest.TestCase):
    def test_runtime_registry_defaults_and_legacy_inference(self):
        from crew import runtime

        self.assertEqual(runtime.resolve_runtime(), config.DEFAULT_RUNTIME)
        self.assertEqual(runtime.resolve_runtime(None, "claude --print"), "claude")
        self.assertEqual(runtime.resolve_runtime(None, "/opt/homebrew/bin/codex -q"), "codex")
        self.assertEqual(runtime.resolve_runtime(None, "python worker.py"), "custom")
        self.assertEqual(runtime.resolve_runtime("codex", "claude --print"), "codex")
        self.assertEqual(runtime.resolve_agent_runtime({"launch_cmd": ""}), "claude")
        self.assertEqual(runtime.resolve_agent_runtime({"launch_cmd": "codex"}), "codex")

    def test_invalid_runtime_and_commandless_custom_are_rejected(self):
        from crew import runtime

        with self.assertRaisesRegex(ValueError, "runtime"):
            runtime.resolve_runtime("other")
        with self.assertRaisesRegex(ValueError, "launch command"):
            runtime.launch_command("custom", "/tmp/home")

    def test_codex_default_is_scoped_to_home_and_exposes_crew_on_path(self):
        from crew import runtime

        long_path = os.pathsep.join(
            f"/tmp/codex-path-segment-{index:03d}" for index in range(100))
        context = {
            "MORPHDB_HOST": '127.0.0.1:18787/query?value="a b"&slash=\\',
            "CREW_APP": "crew special app",
            "CREW_PROJECT": "demo-project",
            "CREW_AGENT": "worker-special",
            "AGENT_MAIL_NAME": "worker-special",
            "CREW_ROOT": "/tmp/crew root with spaces",
            "CREW_RUNTIME": "codex",
            "PATH": long_path,
        }
        with mock.patch.object(
                 config, "CODEX_LAUNCH_CMD",
                 "codex --dangerously-bypass-approvals-and-sandbox"), \
             mock.patch.dict(os.environ, {"PATH": long_path}, clear=False):
            cmd = runtime.launch_command(
                "codex", "/tmp/home with space", environment=context)
        self.assertIn('projects."/tmp/home with space".trust_level="trusted"', cmd)
        self.assertIn('shell_environment_policy.inherit="all"', cmd)
        for key in runtime.CODEX_CRITICAL_ENV:
            expected = (
                f"shell_environment_policy.set.{key}="
                + json.dumps(context[key], ensure_ascii=False))
            self.assertIn(expected, shlex.split(cmd))
        self.assertNotIn("shell_environment_policy.set.PATH", cmd)
        self.assertNotIn(long_path, cmd)
        self.assertLess(len(cmd.encode()), 1024)

    @unittest.skipUnless(shutil.which("codex"), "Codex CLI is not installed")
    def test_codex_inherit_policy_exposes_crew_inside_a_real_tool_shell(self):
        from crew import runtime

        context = {
            "MORPHDB_HOST": "127.0.0.1:18787",
            "CREW_APP": "crewtest-codex-tool-shell",
            "CREW_PROJECT": "default",
            "CREW_AGENT": "codex-tool-shell",
            "AGENT_MAIL_NAME": "codex-tool-shell",
            "CREW_ROOT": "/tmp/crew-codex-tool-root",
            "CREW_RUNTIME": "codex",
        }
        command = runtime.launch_command(
            "codex", "/tmp/crew-codex-policy-test", environment=context)
        tokens = shlex.split(command)
        configs = [
            tokens[index + 1] for index, token in enumerate(tokens[:-1])
            if token in ("-c", "--config")
        ]
        config_args = [part for value in configs for part in ("-c", value)]
        env = dict(
            os.environ, PATH=runtime.agent_path(),
            MORPHDB_HOST="https://remote-should-not-win.invalid")
        result = subprocess.run(
            [shutil.which("codex"), *config_args, "sandbox", "/bin/sh", "-c",
             'printf "%s\\n%s" "$(command -v crew)" "$MORPHDB_HOST"'],
            cwd=config.ROOT, env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip().splitlines(), [
            os.path.join(config.ROOT, "bin", "crew"),
            "127.0.0.1:18787",
        ])

    def test_legacy_generated_codex_path_override_normalizes_on_revive(self):
        from crew import runtime

        home = "/tmp/legacy-codex-home"
        trust = f'projects."{home}".trust_level="trusted"'
        legacy_path = 'shell_environment_policy.set.PATH="/old/very/long/path"'
        legacy = (
            f"{config.CODEX_LAUNCH_CMD} -c {shlex.quote(trust)} "
            f"-c {shlex.quote(legacy_path)}")
        environment = {
            "MORPHDB_HOST": "127.0.0.1:18787",
            "CREW_APP": "crewtest-legacy",
            "CREW_PROJECT": "default",
            "CREW_AGENT": "legacy",
            "AGENT_MAIL_NAME": "legacy",
            "CREW_ROOT": "/tmp/legacy-root",
            "CREW_RUNTIME": "codex",
        }
        normalized = runtime.revive_launch_command(
            "codex", home, legacy, environment=environment)

        self.assertIn('shell_environment_policy.inherit="all"', normalized)
        self.assertIn(
            'shell_environment_policy.set.MORPHDB_HOST="127.0.0.1:18787"',
            shlex.split(normalized))
        self.assertNotIn("shell_environment_policy.set.PATH", normalized)
        custom = legacy + " --model custom-model"
        self.assertEqual(
            runtime.revive_launch_command(
                "codex", home, custom, environment=environment), custom,
            "a non-generated custom command must not be migrated")

    def test_prior_generated_codex_inherit_only_command_gains_explicit_context(self):
        from crew import runtime

        home = "/tmp/prior-inherit-only-codex-home"
        trust = runtime._codex_trust_config(home)
        prior = (
            f"{config.CODEX_LAUNCH_CMD} -c {shlex.quote(trust)} -c "
            + shlex.quote('shell_environment_policy.inherit="all"'))
        environment = {
            key: f"current-{key.lower()}" for key in runtime.CODEX_CRITICAL_ENV
        }

        refreshed = runtime.revive_launch_command(
            "codex", home, prior, environment=environment)

        for key in runtime.CODEX_CRITICAL_ENV:
            self.assertIn(
                f"shell_environment_policy.set.{key}="
                + json.dumps(environment[key]),
                shlex.split(refreshed))
        custom = prior + " -c model=\"custom\""
        self.assertEqual(
            runtime.revive_launch_command(
                "codex", home, custom, environment=environment),
            custom,
            "an inherit command with an unrelated config is user-authored")

    def test_current_generated_codex_command_refreshes_stale_context_on_revive(self):
        from crew import runtime

        home = "/tmp/current-generated-codex-home"
        stale = {
            key: f"stale-{key.lower()}" for key in runtime.CODEX_CRITICAL_ENV
        }
        current = {
            key: f"current-{key.lower()}" for key in runtime.CODEX_CRITICAL_ENV
        }
        stored = runtime.launch_command("codex", home, environment=stale)

        refreshed = runtime.revive_launch_command(
            "codex", home, stored, environment=current)

        tokens = shlex.split(refreshed)
        for key in runtime.CODEX_CRITICAL_ENV:
            self.assertIn(
                f"shell_environment_policy.set.{key}="
                + json.dumps(current[key]),
                tokens)
            self.assertNotIn(
                f"shell_environment_policy.set.{key}="
                + json.dumps(stale[key]),
                tokens)
        custom = stored + " --model user-selected"
        self.assertEqual(
            runtime.revive_launch_command(
                "codex", home, custom, environment=current),
            custom,
            "extra arguments make an otherwise generated command custom")

    def test_process_matching_uses_executable_not_substrings(self):
        from crew import runtime

        self.assertTrue(runtime.process_matches("claude", "claude", "claude --resume"))
        self.assertTrue(runtime.process_matches("codex", "codex", "/usr/local/bin/codex"))
        self.assertFalse(runtime.process_matches(
            "claude", "python", "python worker.py --note not-a-claude-process"))
        self.assertFalse(runtime.process_matches(
            "codex", "zsh", "echo codex"))
        self.assertTrue(runtime.process_matches(
            "custom", "sleep", "sleep 30", launch_cmd="sleep 30"))

    def test_schema_and_guard_make_runtime_first_class_and_protected(self):
        self.assertIn("runtime", schema.AGENT_FIELDS)
        self.assertIn("runtime", guard.PROTECTED_AGENT_FIELDS)

    def test_graphstore_create_persists_runtime(self):
        with mock.patch.object(gs, "get_agent_by_name", return_value=None), \
             mock.patch.object(gs.guard, "check"), \
             mock.patch.object(gs.guard, "audit"), \
             mock.patch.object(gs, "create_object", side_effect=lambda _t, body: body):
            agent = gs.create_agent(
                "stored_codex", home="/tmp/stored_codex", runtime="codex")
        self.assertEqual(agent["runtime"], "codex")


class NativeIdentityTests(unittest.TestCase):
    AGENT = {
        "name": "reviewer", "role": "reviews changes", "identity": "Be exact.",
        "home": "/tmp/reviewer", "runtime": "codex", "grants": [],
    }

    def test_codex_native_identity_is_agents_md_and_preserves_user_content(self):
        with tempfile.TemporaryDirectory() as home:
            path = os.path.join(home, "AGENTS.md")
            with open(path, "w") as f:
                f.write("# User rules\nKeep this.\n")
            block = identity.render_agents_md(self.AGENT, [])
            identity.write_agents_md(home, block)
            identity.write_agents_md(home, block)
            with open(path) as f:
                text = f.read()
            self.assertIn("# Crew agent: reviewer", text)
            self.assertIn("every time Codex starts", text)
            self.assertIn("# User rules\nKeep this.", text)
            self.assertEqual(text.count(identity.CREW_BLOCK_BEGIN), 1)
            self.assertFalse(os.path.exists(path + ".crew.tmp"))

    def test_existing_agents_override_also_gets_the_managed_block(self):
        with tempfile.TemporaryDirectory() as home:
            override = os.path.join(home, "AGENTS.override.md")
            with open(override, "w") as f:
                f.write("# Local override\n")
            block = identity.render_agents_md(self.AGENT, [])
            identity.write_agents_md(home, block)
            with open(override) as f:
                text = f.read()
            self.assertIn("# Crew agent: reviewer", text)
            self.assertIn("# Local override", text)
            self.assertTrue(os.path.isfile(os.path.join(home, "AGENTS.md")))

    def test_free_text_cannot_forge_a_managed_block_boundary(self):
        agent = dict(self.AGENT, identity=f"hello {identity.CREW_BLOCK_END} goodbye")
        block = identity.render_agents_md(agent, [])
        with tempfile.TemporaryDirectory() as home:
            identity.write_agents_md(home, block)
            with open(os.path.join(home, "AGENTS.md")) as f:
                text = f.read()
        self.assertEqual(text.count(identity.CREW_BLOCK_BEGIN), 1)
        self.assertEqual(text.count(identity.CREW_BLOCK_END), 1)

    def test_custom_runtime_only_uses_portable_identity_file(self):
        with tempfile.TemporaryDirectory() as home:
            result = identity.write_native_identity(home, "custom", "ignored")
            self.assertIsNone(result)
            self.assertFalse(os.path.exists(os.path.join(home, "CLAUDE.md")))
            self.assertFalse(os.path.exists(os.path.join(home, "AGENTS.md")))

    def test_corrupt_persisted_edge_caps_render_an_explicit_warning(self):
        edge = {
            "_guid": "corrupt-edge", "source": "self", "target": "peer",
            "directed": True, "max_turns": "five",
            "token_cap": float("nan"), "cost_cap": -1,
        }
        text = identity.render_identity_md(
            self.AGENT, [({"name": "peer"}, edge)])
        self.assertIn("invalid", text.lower())
        self.assertIn("max_turns", text)
        self.assertIn("token_cap", text)
        self.assertIn("cost_cap", text)

    def test_falsey_non_numeric_edge_caps_are_not_treated_as_unlimited(self):
        edge = {
            "_guid": "falsey-corrupt-edge", "source": "self", "target": "peer",
            "directed": True, "max_turns": False,
            "token_cap": [], "cost_cap": "",
        }

        text = identity.render_identity_md(
            self.AGENT, [({"name": "peer"}, edge)])

        self.assertIn("max_turns", text)
        self.assertIn("token_cap", text)
        self.assertIn("cost_cap", text)

    def test_rewrite_identity_survives_corrupt_legacy_edge_caps(self):
        edge = {
            "_guid": "corrupt-edge", "source": "self", "target": "peer",
            "directed": True, "max_turns": "five",
            "token_cap": float("inf"), "cost_cap": "not-money",
        }
        with tempfile.TemporaryDirectory() as home:
            agent = dict(self.AGENT, _guid="self", home=home)
            with mock.patch.object(
                     spawn, "_resolve_neighbors",
                     return_value=[({"_guid": "peer", "name": "peer"}, edge)]), \
                 mock.patch.object(spawn, "_resolve_incoming", return_value=[]):
                path = spawn.rewrite_identity(agent)
            with open(path) as stream:
                text = stream.read()
        self.assertIn("invalid", text.lower())
        self.assertIn("max_turns", text)

    def test_rewrite_identity_publishes_portable_and_native_files_as_one_bundle(self):
        with tempfile.TemporaryDirectory() as home:
            agent = dict(self.AGENT, _guid="bundle-guid", home=home)
            expected_path = os.path.join(home, config.IDENTITY_FILE)
            with mock.patch.object(
                     spawn, "_resolve_neighbors", return_value=[]), \
                 mock.patch.object(spawn, "_resolve_incoming", return_value=[]), \
                 mock.patch.object(
                     identity, "write_identity_bundle", create=True,
                     return_value=expected_path) as bundle, \
                 mock.patch.object(
                     identity, "write_identity",
                     side_effect=AssertionError("portable write escaped bundle")), \
                 mock.patch.object(
                     identity, "write_native_identity",
                     side_effect=AssertionError("native write escaped bundle")):
                self.assertEqual(spawn.rewrite_identity(agent), expected_path)

        bundle.assert_called_once()
        args = bundle.call_args.args
        self.assertEqual(args[0], os.path.realpath(home))
        self.assertIn("reviewer", args[1])
        self.assertEqual(args[2], "codex")
        self.assertIn("# Crew agent: reviewer", args[3])

    def test_rewrite_identity_converts_raw_disk_errors_to_graph_error(self):
        with tempfile.TemporaryDirectory() as home:
            agent = dict(self.AGENT, _guid="disk-error-guid", home=home)
            with mock.patch.object(
                     spawn, "_resolve_neighbors", return_value=[]), \
                 mock.patch.object(spawn, "_resolve_incoming", return_value=[]), \
                 mock.patch.object(
                     identity, "write_identity_bundle",
                     side_effect=OSError("disk full")):
                with self.assertRaisesRegex(gs.GraphError, "disk full"):
                    spawn.rewrite_identity(agent)

    def test_neighbor_resolution_propagates_uncertain_backend_reads(self):
        edge = {"_guid": "edge", "source": "self", "target": "peer",
                "directed": True}
        with mock.patch.object(
                 spawn.gs, "messageable_targets",
                 return_value=[("peer", edge)]), \
             mock.patch.object(
                 spawn.gs, "get_object",
                 side_effect=gs.GraphError("503: unavailable")):
            with self.assertRaisesRegex(gs.GraphError, "503: unavailable"):
                spawn._resolve_neighbors("self")

        with mock.patch.object(
                 spawn.gs, "incoming_edges",
                 return_value=[("peer", edge)]), \
             mock.patch.object(
                 spawn.gs, "get_object",
                 side_effect=gs.GraphError("timeout reading MorphDB")):
            with self.assertRaisesRegex(gs.GraphError, "timeout"):
                spawn._resolve_incoming("self")

    def test_neighbor_resolution_skips_only_confirmed_dangling_404(self):
        edge = {"_guid": "edge", "source": "self", "target": "peer",
                "directed": True}
        with mock.patch.object(
                 spawn.gs, "messageable_targets",
                 return_value=[("peer", edge)]), \
             mock.patch.object(
                 spawn.gs, "get_object",
                 side_effect=gs.GraphError("404: peer deleted")):
            self.assertEqual(spawn._resolve_neighbors("self"), [])

    def test_rewrite_identity_refuses_a_sparse_agent_without_writing(self):
        sparse = {"_guid": "legacy-guid", "name": "legacy"}
        with mock.patch.object(spawn, "_resolve_neighbors", return_value=[]), \
             mock.patch.object(spawn, "_resolve_incoming", return_value=[]), \
             mock.patch.object(identity, "write_identity") as portable, \
             mock.patch.object(identity, "write_native_identity") as native:
            with self.assertRaisesRegex(gs.GraphError, "valid absolute home"):
                spawn.rewrite_identity(sparse)
        portable.assert_not_called()
        native.assert_not_called()

    def test_dashboard_edge_identity_refresh_surfaces_sparse_legacy_row(self):
        with mock.patch.object(
                 dashboard_app.gs, "get_object",
                 return_value={"_guid": "legacy-guid", "name": "legacy"}), \
             mock.patch.object(
                 dashboard_app.spawn, "rewrite_identity",
                 side_effect=gs.GraphError("agent has no valid absolute home")) as rewrite:
            with self.assertRaisesRegex(gs.GraphError, "valid absolute home"):
                dashboard_app._rewrite_endpoint_identities("legacy-guid")
        rewrite.assert_called_once()


class TmuxRuntimeTests(unittest.TestCase):
    def test_runtime_pane_selection_respects_each_agents_configured_runtime(self):
        panes = [
            {"session": "claude_agent", "pane_id": "%1", "tty": "ttys001"},
            {"session": "codex_agent", "pane_id": "%2", "tty": "ttys002"},
            {"session": "codex_agent", "pane_id": "%3", "tty": "ttys003"},
        ]
        processes = {
            "ttys001": [{"comm": "claude", "command": "claude --resume"}],
            "ttys002": [{"comm": "claude", "command": "claude --resume"}],
            "ttys003": [{"comm": "codex", "command": "codex"}],
        }
        agents = [
            {"name": "c", "session": "claude_agent", "runtime": "claude"},
            {"name": "x", "session": "codex_agent", "runtime": "codex"},
        ]
        self.assertEqual(
            tmuxio._match_runtime_panes(panes, processes, agents),
            {"claude_agent": "%1", "codex_agent": "%3"})

    def test_snapshot_fields_distinguish_session_and_runtime_liveness(self):
        bare = {"name": "bare", "session": "bare", "runtime": "codex",
                "status": "not_started"}
        fields = tmuxio.agent_snapshot_fields(bare, {"bare"}, {}, capture=lambda _: "")
        self.assertTrue(fields["session_alive"])
        self.assertFalse(fields["runtime_alive"])
        self.assertFalse(fields["alive"])
        self.assertEqual(fields["live_status"], "not_started")

        crashed = dict(bare, status="idle")
        fields = tmuxio.agent_snapshot_fields(crashed, {"bare"}, {}, capture=lambda _: "")
        self.assertEqual(fields["live_status"], "down")

        fields = tmuxio.agent_snapshot_fields(
            crashed, {"bare"}, {"bare": "%9"}, capture=lambda _: "esc to interrupt")
        self.assertTrue(fields["runtime_alive"])
        self.assertEqual(fields["live_status"], "working")

        one_shot = dict(bare, runtime="custom", status="idle",
                        launch_cmd="true")
        fields = tmuxio.agent_snapshot_fields(
            one_shot, {"bare"}, {}, capture=lambda _: "")
        self.assertTrue(fields["session_alive"])
        self.assertEqual(fields["live_status"], "unknown")

    def test_open_session_prepends_repo_bin_to_path(self):
        calls = []

        def fake_tmux(*args, **kwargs):
            calls.append(args)
            if args[0] == "list-panes":
                return True, "%4"
            return True, ""

        with mock.patch.object(spawn, "_tmux", side_effect=fake_tmux):
            pane = spawn._open_session("sess", "/tmp/home", "agent", "default", "codex")
        self.assertEqual(pane, "%4")
        new = calls[0]
        context_commands = [
            call for call in calls
            if call[:4] == ("send-keys", "-t", "%4", "-l")
            and "show-environment -s" in call[4]
        ]
        self.assertEqual(len(context_commands), 1, calls)
        context_command = context_commands[0][4]
        self.assertIn("CREW_AGENT", context_command)
        self.assertIn("MORPHDB_HOST", context_command)
        self.assertIn("-t '=sess'", context_command)
        self.assertIn("_crew_context_assignment=", context_command)
        self.assertIn("|| { _crew_context_ok=1; break; }", context_command)
        # tmux may enqueue this before the interactive shell has switched its
        # tty out of canonical startup mode.  Keep the entire handshake below
        # the portable 1024-byte canonical input ceiling; embedding the full
        # PATH in completion comparisons previously truncated the command and
        # made every real spawn wait 15 seconds before failing.
        self.assertLess(len(context_command.encode()), 1024)
        self.assertIn(
            ("send-keys", "-t", "%4", "Enter"), calls,
            "the in-shell context export was never submitted")
        wait_calls = [call for call in calls if call[:1] == ("wait-for",)]
        self.assertEqual(len(wait_calls), 1, calls)
        self.assertGreater(
            calls.index(wait_calls[0]),
            calls.index(("send-keys", "-t", "%4", "Enter")))
        path_arg = next(x for x in new if x.startswith("PATH="))
        self.assertEqual(path_arg.split(os.pathsep)[0], "PATH=" + os.path.join(config.ROOT, "bin"))
        runtime_arg = next(x for x in new if x.startswith("CREW_RUNTIME="))
        self.assertEqual(runtime_arg, "CREW_RUNTIME=codex")

    def test_open_session_pins_the_full_crew_context(self):
        calls = []

        def fake_tmux(*args, **kwargs):
            calls.append(args)
            if args[0] == "list-panes":
                return True, "%4"
            return True, ""

        env = {
            "CREW_APP": "crewtest-session-context",
            "CREW_PROJECT": "demo",
            "CREW_ROOT": "/tmp/crew context root",
        }
        with mock.patch.dict(os.environ, env, clear=False), \
             mock.patch.object(config, "MORPHDB_HOST", "http://127.0.0.1:18787"), \
             mock.patch.object(config, "DEFAULT_RUNTIME", "codex"), \
             mock.patch.object(spawn, "_tmux", side_effect=fake_tmux):
            pane = spawn._open_session(
                "demo__agent", "/tmp/home", "agent", "demo", "codex")

        self.assertEqual(pane, "%4")
        new = calls[0]
        pinned = {
            value.split("=", 1)[0]: value.split("=", 1)[1]
            for index, value in enumerate(new)
            if index and new[index - 1] == "-e"
        }
        self.assertEqual(pinned["CREW_PROJECT"], "demo")
        self.assertEqual(pinned["CREW_APP"], "crewtest-session-context")
        self.assertEqual(pinned["MORPHDB_HOST"], "http://127.0.0.1:18787")
        self.assertEqual(pinned["CREW_ROOT"], "/tmp/crew context root")
        self.assertEqual(pinned["CREW_RUNTIME"], "codex")
        self.assertEqual(pinned["CREW_AGENT"], "agent")
        self.assertEqual(pinned["AGENT_MAIL_NAME"], "agent")

    def test_start_session_recreation_pins_the_full_crew_context(self):
        agent = {"_guid": "g", "name": "sleeper", "session": "demo__sleeper",
                 "home": "/tmp/sleeper", "runtime": "custom",
                 "launch_cmd": "sleep 30", "status": "not_started"}
        calls = []

        def fake_tmux(*args, **kwargs):
            calls.append(args)
            if args[0] == "has-session":
                return False, ""
            if args[0] == "list-panes":
                return True, "%7"
            return True, ""

        updated = dict(agent, status="idle", pane="%7")
        env = {
            "CREW_APP": "crewtest-revive-context",
            "CREW_PROJECT": "demo",
            "CREW_ROOT": "/tmp/revive root",
        }
        with mock.patch.dict(os.environ, env, clear=False), \
             mock.patch.object(config, "MORPHDB_HOST", "127.0.0.1:18787"), \
             mock.patch.object(config, "DEFAULT_RUNTIME", "claude"), \
             mock.patch.object(spawn.gs, "get_agent_by_name", return_value=agent), \
             mock.patch.object(
                 spawn.gs, "update_agent_runtime_state", return_value=updated), \
             mock.patch.object(spawn.guard, "check"), \
             mock.patch.object(spawn.guard, "audit"), \
             mock.patch.object(spawn, "rewrite_identity"), \
             mock.patch.object(spawn, "_tmux", side_effect=fake_tmux):
            result = spawn.start_session("sleeper")

        self.assertEqual(result["status"], "idle")
        new = next(call for call in calls if call[0] == "new-session")
        pinned = {
            value.split("=", 1)[0]: value.split("=", 1)[1]
            for index, value in enumerate(new)
            if index and new[index - 1] == "-e"
        }
        self.assertEqual(pinned["CREW_APP"], "crewtest-revive-context")
        self.assertEqual(pinned["MORPHDB_HOST"], "127.0.0.1:18787")
        self.assertEqual(pinned["CREW_ROOT"], "/tmp/revive root")
        self.assertEqual(pinned["CREW_PROJECT"], "demo")
        self.assertEqual(pinned["CREW_RUNTIME"], "custom")


class RuntimeDeliveryTests(unittest.TestCase):
    def _run_delivery(self, runtime_key, state, ready=False):
        callback = mock.Mock()
        with mock.patch.object(mail, "_acquire_lock", return_value="fake.lock"), \
             mock.patch.object(mail, "_release_lock"), \
             mock.patch.object(mail.tmuxio, "capture_frame", return_value="frame"), \
             mock.patch.object(mail.tmuxio, "detect_status", return_value=state), \
             mock.patch.object(mail.tmuxio, "pane_ready", return_value=ready), \
             mock.patch.object(mail, "_type_into_pane", return_value=True) as typed:
            result = mail._deliver_when_ready(
                "%9", "handoff", 0, "target", on_typed=callback,
                runtime_key=runtime_key)
        return result, typed, callback

    def test_busy_codex_uses_next_turn_tab_queue(self):
        result, typed, callback = self._run_delivery("codex", "working")
        self.assertEqual(result, "runtime_queued")
        typed.assert_called_once_with(
            "%9", "handoff", runtime_key="codex", submit_key="Tab")
        callback.assert_called_once_with()

    def test_idle_codex_submits_with_enter(self):
        result, typed, callback = self._run_delivery("codex", "idle", ready=True)
        self.assertEqual(result, "delivered")
        typed.assert_called_once_with(
            "%9", "handoff", runtime_key="codex", submit_key="Enter")
        callback.assert_called_once_with()

    def test_unknown_custom_runtime_is_left_in_durable_queue(self):
        result, typed, callback = self._run_delivery("custom", "unknown")
        self.assertFalse(result)
        typed.assert_not_called()
        callback.assert_not_called()


class SpawnRuntimeTests(unittest.TestCase):
    def test_spawn_refetches_under_guid_lock_before_initial_identity_publish(self):
        initial = {
            "_guid": "g", "name": "worker", "home": "/tmp/worker",
            "session": "worker", "runtime": "custom",
            "launch_cmd": "true", "status": "not_started", "grants": [],
        }
        refreshed = {
            **initial,
            "grants": [{"name": "docs", "path": "/tmp/docs", "mode": "ro"}],
        }
        state = {"created": False, "identity_locked": False}

        def get_agent(_name):
            if not state["created"]:
                return None
            self.assertTrue(
                state["identity_locked"],
                "spawn refetched the committed row outside its GUID lock")
            return refreshed

        def create_agent(*_args, **_kwargs):
            state["created"] = True
            return initial

        @contextlib.contextmanager
        def identity_lock(guids):
            self.assertEqual(tuple(guids), ("g",))
            state["identity_locked"] = True
            try:
                yield
            finally:
                state["identity_locked"] = False

        with mock.patch.object(spawn.guard, "check"), \
             mock.patch.object(
                 spawn.gs, "get_agent_by_name", side_effect=get_agent), \
             mock.patch.object(spawn.gs, "unsafe_home_reason", return_value=None), \
             mock.patch.object(
                 spawn.gs, "home_conflict_across_apps", return_value=None), \
             mock.patch.object(
                 spawn.gs, "_identity_transaction_locks",
                 side_effect=identity_lock) as lock, \
             mock.patch.object(
                 spawn, "_plan_home",
                 return_value=("/tmp/worker", None, ("mkdir",))), \
             mock.patch.object(spawn, "_materialize_home"), \
             mock.patch.object(spawn, "_tmux", return_value=(False, "")), \
             mock.patch.object(spawn, "_open_session", return_value="%1"), \
             mock.patch.object(
                 spawn.gs, "create_agent", side_effect=create_agent), \
             mock.patch.object(spawn, "rewrite_identity") as rewrite:
            result = spawn.spawn_agent(
                "worker", runtime="custom", launch_cmd="true", launch=False)

        self.assertEqual(result, refreshed)
        lock.assert_called_once_with(("g",))
        rewrite.assert_called_once_with(refreshed)

    def test_tmux_collision_is_checked_before_any_home_materialization(self):
        plans = (
            ("mkdir",),
            ("worktree", "/tmp/repo", "main", "crew/demo/worker"),
        )
        for plan in plans:
            with self.subTest(plan=plan), \
                 mock.patch.dict(os.environ, {"CREW_PROJECT": "demo"}, clear=False), \
                 mock.patch.object(spawn.guard, "check"), \
                 mock.patch.object(spawn.gs, "get_agent_by_name", return_value=None), \
                 mock.patch.object(spawn.gs, "unsafe_home_reason", return_value=None), \
                 mock.patch.object(
                     spawn.gs, "home_conflict_across_apps", return_value=None), \
                 mock.patch.object(
                     spawn, "_plan_home",
                     return_value=("/tmp/planned-worker", None, plan)), \
                 mock.patch.object(spawn, "_materialize_home") as materialize, \
                 mock.patch.object(
                     spawn, "_tmux", return_value=(True, "already exists")):
                with self.assertRaisesRegex(gs.GraphError, "already exists"):
                    spawn.spawn_agent("worker", launch=False)
            materialize.assert_not_called()

    def test_launch_runtime_refuses_when_command_cannot_be_typed(self):
        with mock.patch.object(
                 spawn, "_tmux", return_value=(False, "missing pane")) as tmux:
            with self.assertRaisesRegex(gs.GraphError, "type runtime launch command"):
                spawn._launch_runtime(
                    "session", "/tmp/home", "true", "custom", pane="%404")
        self.assertEqual(tmux.call_count, 1)

    def test_launch_runtime_refuses_when_enter_cannot_be_submitted(self):
        with mock.patch.object(
                 spawn, "_tmux",
                 side_effect=[
                     (True, ""), (False, "missing pane"),
                     (False, "missing pane")]) as tmux:
            with self.assertRaisesRegex(gs.GraphError, "submit runtime launch command"):
                spawn._launch_runtime(
                    "session", "/tmp/home", "true", "custom", pane="%404")
        self.assertEqual(tmux.call_count, 3)
        self.assertEqual(
            tmux.call_args_list[-1].args,
            ("send-keys", "-t", "%404", "C-c"))

    def test_builtin_launch_refuses_a_missing_executable_before_typing(self):
        missing = "crew-definitely-missing-runtime-executable"
        with mock.patch.object(spawn, "_pretrust_home"), \
             mock.patch.object(spawn, "_tmux", return_value=(True, "")) as tmux:
            with self.assertRaisesRegex(
                    gs.GraphError, "launch executable.*not available"):
                spawn._launch_runtime(
                    "session", "/tmp/home", missing, "claude", pane="%404")
        tmux.assert_not_called()

    def test_builtin_launch_refuses_when_runtime_never_stays_alive(self):
        calls = []

        def fake_tmux(*args, **_kwargs):
            calls.append(args)
            if args[0] == "list-panes":
                return True, "/dev/ttys999"
            return True, ""

        with mock.patch.object(spawn, "_pretrust_home"), \
             mock.patch.object(spawn, "_tmux", side_effect=fake_tmux), \
             mock.patch.object(
                 spawn, "_run", return_value=(True, "", "")), \
             mock.patch.object(
                 spawn, "_RUNTIME_READY_TIMEOUT_SECONDS", 0.0, create=True), \
             mock.patch.object(
                 spawn, "_RUNTIME_READY_STABILITY_SECONDS", 0.0, create=True):
            with self.assertRaisesRegex(
                    gs.GraphError, "Claude Code.*did not stay running"):
                spawn._launch_runtime(
                    "session", "/tmp/home", "/usr/bin/true", "claude",
                    pane="%404")

        self.assertIn(("send-keys", "-t", "%404", "Enter"), calls)
        self.assertIn(
            ("list-panes", "-t", "%404", "-F", "#{pane_tty}"), calls)
        self.assertIn(
            ("send-keys", "-t", "%404", "C-c"), calls,
            "a failed built-in launch left the pane unsafe to retry")

    def test_builtin_launch_accepts_a_stably_running_runtime(self):
        def fake_tmux(*args, **_kwargs):
            if args[0] == "list-panes":
                return True, "/dev/ttys999"
            return True, ""

        process_row = "ttys999 claude claude --dangerously-skip-permissions"
        with mock.patch.object(spawn, "_pretrust_home"), \
             mock.patch.object(spawn, "_tmux", side_effect=fake_tmux), \
             mock.patch.object(
                 spawn, "_run", return_value=(True, process_row, "")), \
             mock.patch.object(
                 spawn, "_RUNTIME_READY_TIMEOUT_SECONDS", 0.0, create=True), \
             mock.patch.object(
                 spawn, "_RUNTIME_READY_STABILITY_SECONDS", 0.0, create=True):
            spawn._launch_runtime(
                "session", "/tmp/home", "/usr/bin/true", "claude",
                pane="%404")

    def test_runtime_readiness_uses_exact_foreground_when_ps_is_hidden(self):
        def fake_tmux(*args, **_kwargs):
            if args[0] == "list-panes":
                return True, "/dev/ttys999"
            return True, ""

        foreground = (True, "worker\tworker\t%404\tcodex\n")
        with mock.patch.object(spawn, "_tmux", side_effect=fake_tmux), \
             mock.patch.object(spawn, "_run", return_value=(True, "", "")), \
             mock.patch.object(tmuxio, "tmux", return_value=foreground):
            self.assertTrue(spawn._runtime_process_present(
                "%404", "codex", "codex"))

    def test_failed_initial_launch_downgrades_agent_to_not_started(self):
        agent = {"_guid": "g", "name": "worker", "home": "/tmp/worker",
                 "session": "worker", "runtime": "custom", "status": "idle"}
        with mock.patch.object(spawn.guard, "check"), \
             mock.patch.object(
                 spawn.gs, "get_agent_by_name",
                 side_effect=[None, None, agent]), \
             mock.patch.object(spawn.gs, "unsafe_home_reason", return_value=None), \
             mock.patch.object(
                 spawn.gs, "home_conflict_across_apps", return_value=None), \
             mock.patch.object(
                 spawn, "_plan_home",
                 return_value=("/tmp/worker", None, ("mkdir",))), \
             mock.patch.object(spawn, "_materialize_home"), \
             mock.patch.object(spawn, "_tmux", return_value=(False, "")), \
             mock.patch.object(spawn, "_open_session", return_value="%1"), \
             mock.patch.object(spawn.gs, "create_agent", return_value=agent), \
             mock.patch.object(
                 spawn.gs, "update_agent_runtime_state") as update, \
             mock.patch.object(spawn, "rewrite_identity"), \
             mock.patch.object(
                 spawn, "_launch_runtime",
                 side_effect=gs.GraphError("type runtime launch command failed")):
            with self.assertRaisesRegex(gs.GraphError, "launch command"):
                spawn.spawn_agent(
                    "worker", runtime="custom", launch_cmd="true", launch=True)
        update.assert_called_once_with("g", status="not_started")

    def test_failed_revive_launch_downgrades_agent_to_not_started(self):
        with tempfile.TemporaryDirectory(prefix="crew-failed-revive-") as home:
            agent = {
                "_guid": "g", "name": "worker", "home": home,
                "session": "worker", "runtime": "claude",
                "launch_cmd": "/usr/bin/true", "status": "idle",
            }
            with mock.patch.object(
                     spawn.config, "current_project", return_value="default"), \
                 mock.patch.object(
                     spawn.gs, "get_agent_by_name", return_value=agent), \
                 mock.patch.object(spawn.guard, "check"), \
                 mock.patch.object(
                     spawn, "_tmux", return_value=(False, "")), \
                 mock.patch.object(
                     spawn, "_open_session", return_value="%fixture"), \
                 mock.patch.object(spawn, "rewrite_identity"), \
                 mock.patch.object(
                     spawn, "_launch_runtime",
                     side_effect=gs.GraphError(
                         "Claude Code runtime did not stay running")), \
                 mock.patch.object(
                     spawn.gs, "update_agent_runtime_state") as update:
                with self.assertRaisesRegex(
                        gs.GraphError, "did not stay running"):
                    spawn.start_session("worker")

        update.assert_called_once_with("g", status="not_started")

    def test_revive_launches_a_short_command_for_a_legacy_codex_default(self):
        with tempfile.TemporaryDirectory(prefix="crew-legacy-codex-") as home:
            trust = (
                f'projects."{os.path.realpath(home)}".trust_level="trusted"')
            legacy = (
                f"{config.CODEX_LAUNCH_CMD} -c {shlex.quote(trust)} -c "
                + shlex.quote(
                    'shell_environment_policy.set.PATH="/old/'
                    + ("path:" * 400) + 'bin"'))
            agent = {
                "_guid": "legacy-codex-guid", "name": "legacy-codex",
                "home": home, "session": "legacy-codex", "runtime": "codex",
                "launch_cmd": legacy, "status": "not_started",
            }
            updated = dict(agent, pane="%fixture", status="idle")
            with mock.patch.object(
                     spawn.config, "current_project", return_value="default"), \
                 mock.patch.object(
                     spawn.gs, "get_agent_by_name", return_value=agent), \
                 mock.patch.object(spawn.guard, "check"), \
                 mock.patch.object(
                     spawn, "_tmux", return_value=(False, "")), \
                 mock.patch.object(
                     spawn, "_open_session", return_value="%fixture"), \
                 mock.patch.object(spawn, "rewrite_identity"), \
                 mock.patch.object(spawn, "_launch_runtime") as launch, \
                 mock.patch.object(
                     spawn.gs, "update_agent_runtime_state",
                     return_value=updated):
                result = spawn._start_session_locked(
                    "legacy-codex", _expected_guid="legacy-codex-guid")

        self.assertEqual(result, updated)
        launched = launch.call_args.args[2]
        self.assertIn('shell_environment_policy.inherit="all"', launched)
        self.assertNotIn("shell_environment_policy.set.PATH", launched)
        self.assertLess(len(launched.encode()), 1024)

    def test_unowned_existing_session_is_never_repaired_or_typed_into(self):
        agent = {"_guid": "g", "name": "sleeper", "session": "demo__sleeper",
                 "home": "/tmp/sleeper", "runtime": "custom",
                 "launch_cmd": "touch /tmp/must-not-run", "status": "not_started"}
        calls = []

        def fake_tmux(*args, **kwargs):
            calls.append(args)
            if args[0] == "has-session":
                return True, ""
            if args[0] == "show-environment":
                key = args[-1]
                values = {
                    "CREW_AGENT": "someone_else",
                    "CREW_PROJECT": "demo",
                    "CREW_APP": "crew-demo",
                    "MORPHDB_HOST": "127.0.0.1:8787",
                }
                return True, f"{key}={values[key]}\n"
            return True, ""

        with mock.patch.dict(os.environ, {
                 "CREW_PROJECT": "demo", "CREW_ROOT": "/tmp/crew",
             }, clear=False), \
             mock.patch.object(config, "MORPHDB_HOST", "127.0.0.1:8787"), \
             mock.patch.object(spawn.gs, "get_agent_by_name", return_value=agent), \
             mock.patch.object(spawn.guard, "check"), \
             mock.patch.object(spawn, "_tmux", side_effect=fake_tmux), \
             mock.patch.object(spawn, "rewrite_identity") as rewrite, \
             mock.patch.object(spawn, "_launch_runtime") as launch:
            with self.assertRaisesRegex(gs.GraphError, "refusing to adopt"):
                spawn.start_session("sleeper")
        self.assertFalse(any(call[0] == "set-environment" for call in calls))
        self.assertFalse(any(call[0] == "send-keys" for call in calls))
        rewrite.assert_not_called()
        launch.assert_not_called()

    def test_legacy_empty_session_uses_project_scoped_name(self):
        agent = {"_guid": "g", "name": "legacy", "session": "",
                 "home": "/tmp/legacy", "runtime": "custom",
                 "launch_cmd": "true", "status": "not_started"}
        updated = dict(agent, session="", pane="%8", status="idle")
        with mock.patch.dict(os.environ, {"CREW_PROJECT": "demo"}, clear=False), \
             mock.patch.object(spawn.gs, "get_agent_by_name", return_value=agent), \
             mock.patch.object(
                 spawn.gs, "update_agent_runtime_state", return_value=updated), \
             mock.patch.object(spawn.guard, "check"), \
             mock.patch.object(spawn.guard, "audit"), \
             mock.patch.object(spawn, "_tmux", return_value=(False, "")), \
             mock.patch.object(spawn, "_open_session", return_value="%8") as opened, \
             mock.patch.object(spawn, "rewrite_identity"), \
             mock.patch.object(spawn, "_launch_runtime"):
            spawn.start_session("legacy")
        opened.assert_called_once_with(
            "demo__legacy", os.path.realpath("/tmp/legacy"),
            "legacy", "demo", "custom")

    def test_invalid_project_is_rejected_before_spawn_side_effects(self):
        def fake_tmux(*args, **kwargs):
            if args[0] == "has-session":
                return False, ""
            if args[0] == "list-panes":
                return True, "%1"
            return True, ""

        agent = {"_guid": "g", "name": "worker", "home": "/tmp/worker",
                 "session": "worker", "runtime": "claude"}
        with mock.patch.dict(os.environ, {"CREW_PROJECT": "../../escape"}, clear=False), \
             mock.patch.object(spawn.guard, "check") as guard_check, \
             mock.patch.object(spawn.gs, "get_agent_by_name", return_value=None) as get, \
             mock.patch.object(spawn.gs, "unsafe_home_reason", return_value=None), \
             mock.patch.object(spawn.gs, "home_conflict_across_apps", return_value=None), \
             mock.patch.object(spawn, "_materialize_home") as materialize, \
             mock.patch.object(spawn, "_tmux", side_effect=fake_tmux) as tmux, \
             mock.patch.object(spawn.gs, "create_agent", return_value=agent), \
             mock.patch.object(spawn, "rewrite_identity"):
            with self.assertRaisesRegex(gs.GraphError, "invalid project"):
                spawn.spawn_agent("worker", launch=False)
        guard_check.assert_not_called()
        get.assert_not_called()
        materialize.assert_not_called()
        tmux.assert_not_called()

    def test_sparse_agent_revive_fails_before_filesystem_or_tmux(self):
        sparse = {"_guid": "legacy-guid", "name": "legacy",
                  "runtime": "claude", "launch_cmd": "true"}
        with mock.patch.object(spawn.gs, "get_agent_by_name", return_value=sparse), \
             mock.patch.object(
                 spawn.gs, "_invariant_lock",
                 side_effect=lambda *_a, **_k: contextlib.nullcontext()), \
             mock.patch.object(spawn.guard, "check"), \
             mock.patch.object(spawn, "_tmux", return_value=(False, "")) as tmux, \
             mock.patch.object(os, "makedirs") as makedirs, \
             mock.patch.object(spawn, "rewrite_identity") as rewrite, \
             mock.patch.object(spawn, "_launch_runtime") as launch:
            with self.assertRaisesRegex(gs.GraphError, "valid absolute home"):
                spawn.start_session("legacy")
        tmux.assert_not_called()
        makedirs.assert_not_called()
        rewrite.assert_not_called()
        launch.assert_not_called()

    def test_agent_actor_cannot_override_runtime(self):
        foreman = {
            "_guid": "foreman-guid", "name": "foreman",
            "can_edit_graph": True,
        }
        with mock.patch.object(spawn.guard, "check"), \
             mock.patch.object(spawn.guard, "audit"), \
             mock.patch.object(
                 spawn.gs, "get_agent_by_name",
                 side_effect=lambda name: foreman if name == "foreman" else None):
            with self.assertRaisesRegex(gs.GraphError, "--runtime"):
                spawn.spawn_agent("child", runtime="codex", actor="foreman")

    @mock.patch.object(spawn, "rewrite_identity")
    @mock.patch.object(spawn, "_open_session", return_value="%1")
    @mock.patch.object(spawn, "_materialize_home")
    @mock.patch.object(spawn, "_plan_home", return_value=("/tmp/runtime-home", None, ("mkdir",)))
    @mock.patch.object(spawn.guard, "check")
    @mock.patch.object(spawn.gs, "home_conflict_across_apps", return_value=None)
    @mock.patch.object(spawn.gs, "unsafe_home_reason", return_value=None)
    @mock.patch.object(spawn.gs, "get_agent_by_name", return_value=None)
    @mock.patch.object(spawn, "_tmux", return_value=(False, ""))
    def test_spawn_persists_explicit_runtime_and_not_started_status(
            self, _tmux, _get, _unsafe, _conflict, _guard, _plan, _materialize,
            _open, rewrite):
        seen = {}
        committed = {}

        def create(name, **kwargs):
            seen.update(kwargs)
            committed.update({"_guid": "g", "name": name, **kwargs})
            return dict(committed)

        _get.side_effect = lambda _name: dict(committed) if committed else None
        with mock.patch.object(spawn.gs, "create_agent", side_effect=create), \
             mock.patch.object(spawn, "_launch_runtime") as launch:
            agent = spawn.spawn_agent(
                "codexer", home="/tmp/runtime-home", runtime="codex",
                launch_cmd="true", launch=False)
        self.assertEqual(agent["runtime"], "codex")
        self.assertEqual(seen["runtime"], "codex")
        self.assertEqual(seen["status"], "not_started")
        launch.assert_not_called()
        rewrite.assert_called_once()

    def test_start_session_launches_inside_an_existing_bare_session(self):
        agent = {"_guid": "g", "name": "sleeper",
                 "session": "demo__sleeper",
                 "pane": "%7",
                 "home": "/tmp/sleeper", "runtime": "custom",
                 "launch_cmd": "sleep 30", "status": "not_started"}
        calls = []

        def fake_tmux(*args, **kwargs):
            calls.append(args)
            endpoint = kwargs.get("endpoint") or config.tmux_target_endpoint(*args)
            if args[0] == "has-session":
                return endpoint == config.TMUX_ENDPOINT_CREW, ""
            if args[0] == "show-environment":
                values = {
                    "CREW_PROJECT": "demo",
                    "CREW_AGENT": "sleeper",
                    "CREW_APP": "crewtest-existing-context",
                    "MORPHDB_HOST": "127.0.0.1:18787",
                }
                key = args[-1]
                if key in values:
                    return True, f"{key}={values[key]}\n"
                return True, "".join(
                    f"{name}={value}\n" for name, value in values.items())
            if args[0] == "display-message":
                return True, "demo__sleeper"
            if args[0] == "list-panes":
                return True, "%7"
            return True, ""

        updated = dict(agent, status="idle", pane="%7")
        with mock.patch.dict(os.environ, {
                 "CREW_APP": "crewtest-existing-context",
                 "CREW_PROJECT": "demo",
                 "CREW_ROOT": "/tmp/existing root",
             }, clear=False), \
             mock.patch.object(config, "MORPHDB_HOST", "127.0.0.1:18787"), \
             mock.patch.object(config, "DEFAULT_RUNTIME", "claude"), \
             mock.patch.object(spawn.gs, "get_agent_by_name", return_value=agent), \
             mock.patch.object(
                 spawn.gs, "update_agent_runtime_state", return_value=updated), \
             mock.patch.object(spawn.guard, "check"), \
             mock.patch.object(spawn.guard, "audit"), \
             mock.patch.object(spawn, "rewrite_identity"), \
             mock.patch.object(spawn, "_tmux", side_effect=fake_tmux), \
             mock.patch.object(tmuxio, "tmux", side_effect=fake_tmux), \
             mock.patch.object(tmuxio, "runtime_pane", return_value=None):
            result = spawn.start_session("sleeper")
        self.assertEqual(result["status"], "idle")
        self.assertIn(("set-environment", "-t", "=demo__sleeper", "CREW_APP",
                       "crewtest-existing-context"), calls)
        self.assertIn(("set-environment", "-t", "=demo__sleeper", "MORPHDB_HOST",
                       "127.0.0.1:18787"), calls)
        self.assertIn(("set-environment", "-t", "=demo__sleeper", "CREW_ROOT",
                       "/tmp/existing root"), calls)
        self.assertIn(("set-environment", "-t", "=demo__sleeper", "CREW_PROJECT",
                       "demo"), calls)
        self.assertIn(("send-keys", "-t", "%7", "-l", "sleep 30"), calls)
        self.assertIn(("send-keys", "-t", "%7", "Enter"), calls)
        context_call = next(
            call for call in calls
            if call[:4] == ("send-keys", "-t", "%7", "-l")
            and "show-environment -s" in call[4])
        launch_call = ("send-keys", "-t", "%7", "-l", "sleep 30")
        self.assertIn("CREW_AGENT", context_call[4])
        self.assertIn("MORPHDB_HOST", context_call[4])
        self.assertIn("-t '=demo__sleeper'", context_call[4])
        self.assertLess(calls.index(context_call), calls.index(launch_call))

    def test_start_session_does_not_relaunch_a_ps_hidden_owned_runtime(self):
        with tempfile.TemporaryDirectory(prefix="crew-hidden-runtime-") as home:
            agent = {
                "_guid": "hidden-guid", "name": "hidden", "session": "hidden",
                "pane": "%8", "home": home, "runtime": "codex",
                "launch_cmd": "codex", "status": "idle",
            }
            session = config.tmux_target(
                "hidden", config.TMUX_ENDPOINT_CREW)
            with mock.patch.object(
                     spawn.config, "current_project", return_value="default"), \
                 mock.patch.object(
                     spawn.gs, "get_agent_by_name", return_value=agent), \
                 mock.patch.object(spawn.guard, "check"), \
                 mock.patch.object(spawn.guard, "audit"), \
                 mock.patch.object(
                     spawn, "_session_locations", return_value={
                         "owned": session, "dedicated_exists": True,
                         "legacy_exists": False,
                     }), \
                 mock.patch.object(spawn, "_pin_existing_session_context"), \
                 mock.patch.object(
                     tmuxio, "exact_runtime_pane", return_value=config.tmux_target(
                         "%8", config.TMUX_ENDPOINT_CREW)), \
                 mock.patch.object(spawn, "_launch_runtime") as launch, \
                 mock.patch.object(spawn, "rewrite_identity") as rewrite:
                result = spawn._start_session_locked("hidden")

        self.assertEqual(result, agent)
        launch.assert_not_called()
        rewrite.assert_not_called()


class CliAndApiRuntimeTests(unittest.TestCase):
    def test_cli_parser_accepts_runtime(self):
        args = cli.build_parser().parse_args(
            ["spawn-agent", "writer", "--runtime", "codex", "--no-launch"])
        self.assertEqual(args.runtime, "codex")

    def test_spawn_output_is_runtime_aware_and_not_claude_hardcoded(self):
        args = SimpleNamespace(
            name="writer", role="", identity="", home=None, repo=None,
            no_launch=False, launch_cmd=None, runtime="codex", foreman=False)
        agent = {"name": "writer", "session": "writer", "home": "/tmp/writer",
                 "runtime": "codex"}
        out = io.StringIO()
        with mock.patch.object(cli.schema, "ensure_schema"), \
             mock.patch.object(cli.spawn, "spawn_agent", return_value=agent), \
             contextlib.redirect_stdout(out):
            cli.cmd_spawn_agent(args)
        text = out.getvalue().lower()
        self.assertIn("runtime: codex", text)
        self.assertIn("runtime is booting", text)
        self.assertNotIn("claude is booting", text)

    def test_dashboard_create_forwards_runtime(self):
        captured = {}

        class Dummy:
            response = None

            def _field(self, data, key):
                return dashboard_app.Handler._field(data, key)

            def _json(self, body, *args, **kwargs):
                self.response = body

        def create(name, **kwargs):
            captured.update(kwargs)
            return {"name": name, "runtime": kwargs["runtime"]}

        dummy = Dummy()
        with mock.patch.object(dashboard_app.spawn, "spawn_agent", side_effect=create):
            dashboard_app.Handler._agent_create(dummy, {
                "name": "api_codex", "runtime": "codex", "launch": False,
            })
        self.assertTrue(dummy.response["ok"])
        self.assertEqual(captured["runtime"], "codex")

    def test_dashboard_form_and_transport_include_runtime(self):
        modal = Path(config.ROOT, "frontend", "src", "components", "modals",
                     "CreateAgentModal.jsx").read_text()
        api = Path(config.ROOT, "frontend", "src", "api.js").read_text()
        self.assertIn('id="a-runtime"', modal)
        self.assertIn('value="claude"', modal)
        self.assertIn('value="codex"', modal)
        self.assertIn('value="custom"', modal)
        self.assertIn("runtime,", modal)
        self.assertIn("runtime, launch_cmd", api)


if __name__ == "__main__":
    unittest.main(verbosity=2)
