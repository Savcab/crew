"""Regression tests for operator-shell subprocess isolation."""

import os
import subprocess
import sys
import unittest
from unittest import mock

from operator_harness import operator_environment, pin_environment, run_operator


class OperatorEnvironmentTests(unittest.TestCase):
    def test_scrubs_parent_identity_and_tenant_selectors(self):
        inherited = {
            "PATH": "/bin",
            "TMUX": "/tmp/default,1,0",
            "TMUX_PANE": "%7",
            "CREW_AGENT": "parent-agent",
            "AGENT_MAIL_NAME": "parent-mail-name",
            "CREW_APP": "parent-app",
            "CREW_PROJECT": "parent-project",
            "CREW_ROOT": "/tmp/parent-root",
        }

        environment = operator_environment(environ=inherited)

        self.assertEqual(environment["PATH"], "/bin")
        for key in (
                "TMUX", "TMUX_PANE", "CREW_AGENT", "AGENT_MAIL_NAME",
                "CREW_APP", "CREW_PROJECT", "CREW_ROOT"):
            self.assertNotIn(key, environment)

    def test_explicit_actor_and_tenant_overrides_win_after_scrubbing(self):
        inherited = {
            "CREW_AGENT": "parent-agent",
            "CREW_APP": "parent-app",
            "CREW_PROJECT": "parent-project",
        }

        environment = operator_environment(
            {
                "CREW_AGENT": "intentional-actor",
                "CREW_APP": "intentional-app",
                "CREW_PROJECT": "intentional-project",
            },
            environ=inherited,
        )

        self.assertEqual(environment["CREW_AGENT"], "intentional-actor")
        self.assertEqual(environment["CREW_APP"], "intentional-app")
        self.assertEqual(environment["CREW_PROJECT"], "intentional-project")

    def test_run_operator_starts_a_new_session(self):
        result = run_operator(
            [sys.executable, "-c", "import os; print(os.getsid(0))"],
            environ={
                "PATH": "/bin:/usr/bin",
                "TMUX": "/tmp/default,1,0",
                "TMUX_PANE": "%7",
                "CREW_AGENT": "parent-agent",
            },
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotEqual(int(result.stdout.strip()), os.getsid(0))

    def test_pin_environment_registers_exact_restoration_before_mutation(self):
        environment = {"CREW_APP": "before", "KEEP": "value"}
        cleanups = []

        pin_environment(
            cleanups.append,
            {"CREW_APP": "during", "CREW_PROJECT": "project"},
            environ=environment,
        )

        self.assertEqual(environment["CREW_APP"], "during")
        self.assertEqual(environment["CREW_PROJECT"], "project")
        self.assertEqual(len(cleanups), 1)
        cleanups[0]()
        self.assertEqual(environment, {"CREW_APP": "before", "KEEP": "value"})

    def test_default_live_cli_wrapper_clears_inherited_project(self):
        import test_cli_live

        completed = subprocess.CompletedProcess([], 0, "", "")
        poisoned = {
            "CREW_APP": "wrong-app",
            "CREW_PROJECT": "wrong-project",
            "TMUX": "/tmp/default,1,0",
            "TMUX_PANE": "%7",
            "CREW_AGENT": "wrong-actor",
            "AGENT_MAIL_NAME": "wrong-mail-name",
            "CREW_ROOT": "/tmp/wrong-root",
        }
        with mock.patch.dict(os.environ, poisoned, clear=False), \
             mock.patch(
                 "operator_harness.subprocess.run",
                 return_value=completed,
             ) as run:
            test_cli_live._run(["agents"])

        environment = run.call_args.kwargs["env"]
        for key in poisoned:
            self.assertNotIn(key, environment)
        self.assertTrue(run.call_args.kwargs["start_new_session"])

    def test_project_live_cli_wrapper_preserves_only_explicit_context(self):
        import test_pending

        completed = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch.dict(os.environ, {
            "CREW_APP": "wrong-app",
            "CREW_PROJECT": "wrong-project",
            "TMUX": "/tmp/default,1,0",
            "CREW_AGENT": "wrong-actor",
        }, clear=False), mock.patch(
            "operator_harness.subprocess.run",
            return_value=completed,
        ) as run:
            test_pending._run(
                ["pending"], env_extra={"CREW_AGENT": "intended-actor"})

        environment = run.call_args.kwargs["env"]
        self.assertEqual(environment["CREW_PROJECT"], test_pending.PROJECT)
        self.assertEqual(environment["CREW_AGENT"], "intended-actor")
        self.assertNotIn("CREW_APP", environment)
        self.assertNotIn("TMUX", environment)
        self.assertTrue(run.call_args.kwargs["start_new_session"])


if __name__ == "__main__":
    unittest.main()
