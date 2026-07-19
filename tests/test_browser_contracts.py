"""Executable contracts for maintained browser procedures."""
from pathlib import Path
import re
import subprocess
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[1]
BROWSER = ROOT / "tests" / "browser"

MUTATING_PROCEDURES = (
    "create-agent.md",
    "connect-edge.md",
    "edit-edge.md",
    "revive-agent.md",
    "foreman-bless.md",
    "pending-tray.md",
    "one-blob-config.md",
    "canvas-navigation.md",
    "terminal-dock.md",
    "resilience-accessibility.md",
)


def procedure(name):
    return (BROWSER / name).read_text(encoding="utf-8")


class MutatingProcedureSafetyTests(unittest.TestCase):
    def test_every_mutating_procedure_pins_the_isolated_default_project(self):
        for name in MUTATING_PROCEDURES:
            with self.subTest(procedure=name):
                text = procedure(name)
                self.assertIn('test "$CREW_PORT" = "18788"', text)
                self.assertIn('test "${CREW_PROJECT:-default}" = "default"', text)
                self.assertIn('test "$CREW_APP" != "crew"', text)
                self.assertIn('dashboard app does not match CREW_APP', text)

    def test_every_mutating_procedure_aborts_on_existing_fixture_and_uses_receipts(self):
        for name in MUTATING_PROCEDURES:
            with self.subTest(procedure=name):
                text = procedure(name)
                self.assertIn("crew_qa_assert_unused", text)
                self.assertIn("crew_qa_capture_agent", text)
                self.assertIn("crew_qa_cleanup_agent", text)
                self.assertIn("owner.json", text)
                self.assertIn("_guid", text)
                self.assertIn("session", text)
                self.assertRegex(
                    text,
                    r'workspace_key.*r\["app"\]',
                    "ownership revalidation must stay in the captured app",
                )

    def test_every_reserved_fixture_has_executable_capture_and_cleanup_calls(self):
        for name in MUTATING_PROCEDURES:
            text = procedure(name)
            blocks = re.findall(
                r"^[ \t]*```sh[ \t]*\n(.*?)^[ \t]*```[ \t]*$",
                text, flags=re.DOTALL | re.MULTILINE)
            shell = "\n".join(textwrap.dedent(block) for block in blocks)
            reserved = set(re.findall(
                r"^crew_qa_assert_unused (test_[A-Za-z0-9_-]+) ",
                shell, flags=re.MULTILINE))
            captured = set(re.findall(
                r"^[ \t]*crew_qa_capture_agent (test_[A-Za-z0-9_-]+) ",
                shell, flags=re.MULTILINE))
            cleaned = set(re.findall(
                r"^[ \t]*crew_qa_cleanup_agent (test_[A-Za-z0-9_-]+)$",
                shell, flags=re.MULTILINE))
            with self.subTest(procedure=name):
                self.assertTrue(reserved)
                self.assertEqual(captured, reserved)
                self.assertEqual(cleaned, reserved)

    def test_no_procedure_blindly_kills_or_removes_a_static_fixture(self):
        unsafe_kill = re.compile(r"tmux kill-session -t (?:test_|\"\$name\")")
        unsafe_remove = re.compile(
            r"(?:\./bin/crew|crew) remove-agent test_")
        unsafe_home = re.compile(r"rm -rf /tmp/crew_tests/")
        for name in MUTATING_PROCEDURES:
            with self.subTest(procedure=name):
                text = procedure(name)
                self.assertIsNone(unsafe_kill.search(text))
                self.assertIsNone(unsafe_remove.search(text))
                self.assertIsNone(unsafe_home.search(text))
                self.assertRegex(
                    text,
                    re.compile(
                        r"crew_qa_cleanup_agent\(\).*?"
                        r"crew_qa_assert_owned_agent \"\$name\".*?"
                        r"/api/agent/remove",
                        flags=re.DOTALL,
                    ),
                )

    def test_shell_fences_are_syntax_checked(self):
        for name in MUTATING_PROCEDURES:
            text = procedure(name)
            blocks = re.findall(
                r"^[ \t]*```sh[ \t]*\n(.*?)^[ \t]*```[ \t]*$",
                text, flags=re.DOTALL | re.MULTILINE)
            self.assertTrue(blocks, name)
            for index, block in enumerate(blocks, 1):
                with self.subTest(procedure=name, block=index):
                    result = subprocess.run(
                        ["zsh", "-n"], input=textwrap.dedent(block), text=True,
                        capture_output=True, check=False)
                    self.assertEqual(result.returncode, 0, result.stderr)

    def test_single_quoted_python_commands_compile_exactly_as_written(self):
        for name in MUTATING_PROCEDURES:
            commands = re.findall(r"python3 -c '([^']*)'", procedure(name))
            self.assertTrue(commands, name)
            for index, source in enumerate(commands, 1):
                with self.subTest(procedure=name, command=index):
                    compile(source, f"{name}:python-c-{index}", "exec")

    def test_double_quoted_python_commands_compile_exactly_as_written(self):
        for name in MUTATING_PROCEDURES:
            commands = re.findall(
                r'python3 -c "(.*?)"', procedure(name), flags=re.DOTALL)
            for index, source in enumerate(commands, 1):
                with self.subTest(procedure=name, command=index):
                    compile(source, f"{name}:python-c-double-{index}", "exec")

    def test_python_heredocs_compile(self):
        for name in MUTATING_PROCEDURES:
            commands = re.findall(
                r"<<'PY'\n(.*?)\nPY", procedure(name), flags=re.DOTALL)
            for index, source in enumerate(commands, 1):
                with self.subTest(procedure=name, command=index):
                    compile(source, f"{name}:python-heredoc-{index}", "exec")


class TerminalDockProcedureTests(unittest.TestCase):
    def test_terminal_dock_is_independently_executable(self):
        text = procedure("terminal-dock.md")
        for expected in (
            "/api/auth/bootstrap",
            "/api/health",
            "/api/agent/create",
            "/api/graph/snapshot",
            "test_ba_terminal_up",
            "test_ba_terminal_down",
            "finally",
        ):
            self.assertIn(expected, text)

    def test_utf8_command_prints_a_real_newline_escape(self):
        text = procedure("terminal-dock.md")
        self.assertIn("printf 'crew-pty-utf8: héllo 世界\\n'", text)
        self.assertNotIn("世界\\\\n", text)


class CanvasProcedureTests(unittest.TestCase):
    def test_edge_is_created_before_the_edit_modal_is_opened(self):
        text = procedure("canvas-navigation.md")
        self.assertIn("Create the regression edge", text)
        self.assertRegex(text, r"click the created edge's\s+on-canvas label")
        self.assertNotIn("skip if step 17 was closed without creating an edge", text)

    def test_console_instructions_are_browser_tool_agnostic(self):
        all_text = "\n".join(procedure(name) for name in MUTATING_PROCEDURES)
        self.assertNotIn("mcp__plugin_playwright", all_text)
        self.assertIn("browser tool's console", procedure("canvas-navigation.md"))


class ForemanProcedureTests(unittest.TestCase):
    def test_agent_authored_spawn_uses_default_home_and_runtime(self):
        text = procedure("foreman-bless.md")
        self.assertIn("CREW_ROOT=/tmp/crew_tests", text)
        self.assertIn("/tmp/crew_tests/default/test_w3ui_kid", text)
        self.assertNotIn("spawn.spawn_agent('test_w3ui_kid', home=", text)


class OneBlobConfigProcedureTests(unittest.TestCase):
    def test_expander_environment_is_restored_exactly(self):
        text = procedure("one-blob-config.md")
        for expected in (
            "CREW_QA_HAD_CREW_EXPAND_CMD",
            "CREW_QA_ORIG_CREW_EXPAND_CMD",
            "CREW_QA_HAD_EXPAND_STUB_MODE",
            "CREW_QA_ORIG_EXPAND_STUB_MODE",
        ):
            self.assertIn(expected, text)
        self.assertNotIn("unset CREW_EXPAND_CMD EXPAND_STUB_MODE", text)

    def test_every_reauthentication_rechecks_the_dashboard_app(self):
        text = procedure("one-blob-config.md")
        self.assertGreaterEqual(
            text.count("dashboard app does not match CREW_APP"), 2)


class PendingTrayProcedureTests(unittest.TestCase):
    def test_cleanup_resolves_pending_rows_but_preserves_audit_history(self):
        text = procedure("pending-tray.md")
        self.assertIn("guard.reject_pending", text)
        self.assertIn("durable audit history", text)
        self.assertIn("must not be deleted", text)
        self.assertNotIn(
            "AND any leftover pending/graph_edit rows this run created",
            text,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
