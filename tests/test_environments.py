"""crew.environments — the environment store, its executor, and its consumers.

An environment's commands run as the OPERATOR, in a directory a new agent is
about to be launched into, on every spawn that uses it. That makes this store
closer to a credential than to a preference file, and these tests pin the
properties that follow from it:

  (1) built-ins are code, not data: they are listed first, cannot be edited,
      shadowed, or removed, and a store that claims one of their names is
      corrupt. "worktree" is NATIVE — it routes crew's own --repo worktree
      machinery instead of shelling out, so it has no commands at all;
  (2) the same validation guards the write path and the read path, so a
      hand-edited store can never hold a command `crew env add` would refuse,
      and unreadable durable state fails closed instead of quietly turning
      "every new agent gets a prepared workspace" into "nobody does";
  (3) run_setup NEVER raises — the prereq runs first, commands stop at the
      first failure, and a timeout/OSError comes back as an honest
      (False, detail) so the spawn's existing cleanup path is always reached;
  (4) spawn resolves explicit --env > crew-wide default > none, refuses an
      agent actor's explicit pick while still applying the operator's default
      to that spawn, records the environment on the durable row, and leaks
      nothing when setup fails;
  (5) the consumers agree: crew.guard keeps writes human-only and the
      `crew env` CLI round-trips add/remove/set-default.

Every test points the store at a temp directory (crew.environments reads
``config.VAR`` live), and every subprocess, tmux, and MorphDB boundary is
mocked — nothing here runs a real setup command or touches a real agent.

    python3 -m unittest tests.test_environments   (from the repo root)
"""
import argparse
import contextlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from crew import cli, config, environments, guard, spawn  # noqa: E402
from crew import graphstore as gs  # noqa: E402


GRAPHITE_COMMANDS = ["git checkout main",
                     "gt create crew/{agent} --no-interactive"]


class FakeRun:
    """A scripted subprocess.run: records every call, answers from a table."""

    def __init__(self, results=None, default=(0, "", "")):
        self.calls = []
        self.results = dict(results or {})
        self.default = default

    @property
    def commands(self):
        return [command for command, _kwargs in self.calls]

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        outcome = self.results.get(command, self.default)
        if isinstance(outcome, BaseException):
            raise outcome
        code, out, err = outcome
        return subprocess.CompletedProcess(command, code, out, err)


class EnvironmentsCase(unittest.TestCase):
    """A throwaway store; nothing reaches the real var/environments.json."""

    def setUp(self):
        self.var = tempfile.mkdtemp(prefix="crew-environments-test-")
        self.addCleanup(shutil.rmtree, self.var, ignore_errors=True)
        # environments resolves var/environments.json through config.VAR on
        # every call, so one patch covers both the reads and the atomic write.
        var_patch = mock.patch.object(config, "VAR", self.var)
        var_patch.start()
        self.addCleanup(var_patch.stop)

    # -- fixture helpers ----------------------------------------------------
    @property
    def store(self):
        return os.path.join(self.var, "environments.json")

    def write_store(self, payload):
        """Put durable state on disk directly, bypassing validation."""
        with open(self.store, "w", encoding="utf-8") as fh:
            if isinstance(payload, str):
                fh.write(payload)
            else:
                json.dump(payload, fh)

    def read_store(self):
        with open(self.store, encoding="utf-8") as fh:
            return fh.read()

    def add_demo(self, name="demo", commands=("echo prepared",), **kwargs):
        return environments.add_environment(name, list(commands), **kwargs)

    def names(self):
        return [row["name"] for row in environments.list_all()]


# --------------------------------------------------------------------------- #
# the built-ins — code, not data
# --------------------------------------------------------------------------- #
class BuiltinTests(EnvironmentsCase):
    def test_builtins_are_listed_first_and_flagged(self):
        self.add_demo()
        rows = environments.list_all()
        self.assertEqual([row["name"] for row in rows[:2]],
                         ["worktree", "graphite-stack"])
        self.assertTrue(all(row["builtin"] for row in rows[:2]))
        self.assertEqual(rows[2]["name"], "demo")
        self.assertFalse(rows[2]["builtin"])

    def test_worktree_is_native_and_runs_no_commands(self):
        # The whole point of the native marker: crew's --repo machinery already
        # creates the checkout, the branch, and the durable worktree row field.
        # Shelling `git worktree add` here would duplicate it and lose the
        # bookkeeping `crew up` needs to revive the agent.
        worktree = environments.get("worktree")
        self.assertEqual(worktree["native"], environments.NATIVE_REPO)
        self.assertEqual(worktree["commands"], [])
        self.assertEqual(worktree["prereq"], "")
        self.assertIn("worktree", worktree["description"])

    def test_graphite_stack_checks_its_tool_then_stacks_off_main(self):
        graphite = environments.get("graphite-stack")
        self.assertEqual(graphite["prereq"], "gt --version")
        self.assertEqual(graphite["commands"], GRAPHITE_COMMANDS)
        self.assertNotIn("native", graphite)
        for word in ("git checkout", "Graphite", "main"):
            self.assertIn(word, graphite["description"])

    def test_reading_never_creates_a_store(self):
        environments.list_all()
        environments.get("worktree")
        environments.default_name()
        environments.resolve()
        self.assertFalse(os.path.exists(self.store))

    def test_a_builtin_name_cannot_be_taken_edited_or_removed(self):
        for name in sorted(environments.BUILTIN_NAMES):
            for call in (lambda: environments.add_environment(name, ["echo x"]),
                         lambda: environments.remove_environment(name)):
                with self.subTest(name=name, call=call):
                    with self.assertRaises(
                            environments.EnvironmentsError) as ctx:
                        call()
                    self.assertIn("built-in", str(ctx.exception))
        self.assertFalse(os.path.exists(self.store))

    def test_a_returned_row_cannot_mutate_the_built_in_table(self):
        row = environments.get("graphite-stack")
        row["commands"].append("rm -rf ~")
        row["prereq"] = "hacked"
        self.assertEqual(environments.get("graphite-stack")["commands"],
                         GRAPHITE_COMMANDS)
        self.assertEqual(environments.get("graphite-stack")["prereq"],
                         "gt --version")

    def test_get_names_every_known_environment_when_asked_for_none(self):
        self.add_demo()
        with self.assertRaises(environments.EnvironmentsError) as ctx:
            environments.get("nope")
        message = str(ctx.exception)
        self.assertIn("unknown environment 'nope'", message)
        for name in ("worktree", "graphite-stack", "demo"):
            self.assertIn(name, message)


# --------------------------------------------------------------------------- #
# the store round trip
# --------------------------------------------------------------------------- #
class StoreTests(EnvironmentsCase):
    def test_add_persists_owner_only_in_the_documented_shape(self):
        entry = self.add_demo(
            "setup", commands=["npm ci", "cp ../.env ."],
            prereq="node --version", description="install deps")
        self.assertEqual(entry, {
            "name": "setup", "description": "install deps",
            "prereq": "node --version",
            "commands": ["npm ci", "cp ../.env ."], "builtin": False})
        self.assertEqual(stat.S_IMODE(os.stat(self.store).st_mode), 0o600)
        self.assertEqual(json.loads(self.read_store()), {
            "default": None,
            "environments": [{"name": "setup", "description": "install deps",
                              "prereq": "node --version",
                              "commands": ["npm ci", "cp ../.env ."]}]})

    def test_values_are_stripped_before_they_land(self):
        entry = self.add_demo(
            "trimmed", commands=["  echo one  ", "echo two\t"],
            prereq="  true ", description="  padded  ")
        self.assertEqual(entry["commands"], ["echo one", "echo two"])
        self.assertEqual(entry["prereq"], "true")
        self.assertEqual(entry["description"], "padded")

    def test_optional_fields_default_to_empty(self):
        entry = self.add_demo("bare")
        self.assertEqual(entry["prereq"], "")
        self.assertEqual(entry["description"], "")

    def test_add_replaces_a_same_named_environment_in_place(self):
        # Editing is the common operation and there is no separate update verb,
        # so a replace must keep the definition's position in the list.
        self.add_demo("first")
        self.add_demo("second")
        self.add_demo("first", commands=["echo replaced"], prereq="true")
        self.assertEqual(self.names(),
                         ["worktree", "graphite-stack", "first", "second"])
        self.assertEqual(environments.get("first")["commands"],
                         ["echo replaced"])
        self.assertEqual(environments.get("first")["prereq"], "true")

    def test_remove_reports_whether_it_existed_and_is_idempotent(self):
        self.add_demo("gone")
        self.assertTrue(environments.remove_environment("gone"))
        self.assertFalse(environments.remove_environment("gone"))
        self.assertEqual(self.names(), ["worktree", "graphite-stack"])

    def test_every_read_is_live(self):
        # The dashboard is a long-running process; a definition added from the
        # CLI has to be visible to it without a restart.
        self.assertEqual(self.names(), ["worktree", "graphite-stack"])
        self.write_store({"default": "late",
                          "environments": [{"name": "late", "description": "",
                                            "prereq": "",
                                            "commands": ["echo late"]}]})
        self.assertIn("late", self.names())
        self.assertEqual(environments.default_name(), "late")


# --------------------------------------------------------------------------- #
# write-time validation
# --------------------------------------------------------------------------- #
class ValidationTests(EnvironmentsCase):
    BAD_NAMES = ("", "   ", "-lead", "has space", "has.dot", "has/slash",
                 "a" * 33, None, 5, ["worktree"])

    def test_an_unsafe_name_is_refused(self):
        for name in self.BAD_NAMES:
            with self.subTest(name=name):
                with self.assertRaises(environments.EnvironmentsError) as ctx:
                    environments.add_environment(name, ["echo x"])
                self.assertIn("invalid environment name", str(ctx.exception))

    def test_a_safe_name_is_accepted(self):
        for name in ("a", "A1", "with_underscore", "with-dash", "9lives",
                     "x" * 32):
            with self.subTest(name=name):
                self.assertEqual(
                    environments.add_environment(name, ["echo x"])["name"],
                    name)

    def test_commands_must_be_a_non_empty_list_of_single_lines(self):
        cases = {
            "no commands at all": ([], "at least one command"),
            "empty command": (["echo ok", "   "], "must not be empty"),
            "a second line": (["git checkout main\nrm -rf ~"],
                              "one printable line"),
            "past the length limit":
                (["echo " + "x" * environments.MAX_LINE_LEN],
                 "one printable line"),
            "a non-string": ([5], "must be a string"),
            "a bare string instead of a list": ("echo x", "list of shell"),
            "a dict": ({"cmd": "echo x"}, "list of shell"),
        }
        for label, (commands, fragment) in cases.items():
            with self.subTest(commands=label):
                with self.assertRaises(environments.EnvironmentsError) as ctx:
                    environments.add_environment("bad", commands)
                self.assertIn(fragment, str(ctx.exception))

    def test_the_prereq_and_description_are_optional_single_lines(self):
        for field in ("prereq", "description"):
            for value, fragment in (("gt --version\nrm -rf ~",
                                     "one printable line"),
                                    (5, "must be a string")):
                with self.subTest(field=field, value=repr(value)[:30]):
                    with self.assertRaises(
                            environments.EnvironmentsError) as ctx:
                        environments.add_environment(
                            "bad", ["echo x"], **{field: value})
                    message = str(ctx.exception)
                    self.assertIn(field, message)
                    self.assertIn(fragment, message)

    def test_a_refused_definition_never_reaches_the_store(self):
        self.add_demo("kept", commands=["echo kept"])
        before = self.read_store()
        with self.assertRaises(environments.EnvironmentsError):
            environments.add_environment("kept", ["echo new\nrm -rf ~"])
        self.assertEqual(self.read_store(), before)
        self.assertEqual(environments.get("kept")["commands"], ["echo kept"])

    def test_a_first_refused_definition_leaves_no_store_behind(self):
        with self.assertRaises(environments.EnvironmentsError):
            environments.add_environment("bad", [])
        self.assertFalse(os.path.exists(self.store))


# --------------------------------------------------------------------------- #
# corrupt durable state fails closed
# --------------------------------------------------------------------------- #
class CorruptStoreTests(EnvironmentsCase):
    CORRUPTIONS = {
        "invalid json": "{not json at all",
        "truncated": '{"default": null, "environments": [{"name": "de',
        "a json list": [{"name": "demo"}],
        "unknown top-level key": {"default": None, "environments": [],
                                  "shell": "sh"},
        "environments is not a list": {"default": None, "environments": {}},
        "an entry is not an object": {"default": None, "environments": ["demo"]},
        "an entry is missing a key": {
            "default": None,
            "environments": [{"name": "demo", "commands": ["echo x"]}]},
        "an entry has an extra key": {
            "default": None,
            "environments": [{"name": "demo", "description": "", "prereq": "",
                              "commands": ["echo x"], "sudo": True}]},
        "an entry has an unsafe name": {
            "default": None,
            "environments": [{"name": "../etc", "description": "", "prereq": "",
                              "commands": ["echo x"]}]},
        "an entry shadows a built-in": {
            "default": None,
            "environments": [{"name": "worktree", "description": "",
                              "prereq": "", "commands": ["echo x"]}]},
        "duplicate entries": {
            "default": None,
            "environments": [
                {"name": "demo", "description": "", "prereq": "",
                 "commands": ["echo one"]},
                {"name": "demo", "description": "", "prereq": "",
                 "commands": ["echo two"]}]},
        "an entry has no commands": {
            "default": None,
            "environments": [{"name": "demo", "description": "", "prereq": "",
                              "commands": []}]},
        "an entry smuggles a second line": {
            "default": None,
            "environments": [{"name": "demo", "description": "", "prereq": "",
                              "commands": ["echo x\nrm -rf ~"]}]},
        "the default is not a name": {"default": 5, "environments": []},
        "the default is an unsafe name": {"default": "../etc",
                                          "environments": []},
    }

    def test_every_read_fails_closed_instead_of_serving_a_partial_store(self):
        for label, payload in self.CORRUPTIONS.items():
            with self.subTest(store=label):
                self.write_store(payload)
                for name, call in (
                        ("list_all", environments.list_all),
                        ("get", lambda: environments.get("worktree")),
                        ("default_name", environments.default_name),
                        ("resolve", lambda: environments.resolve("worktree"))):
                    with self.subTest(call=name):
                        with self.assertRaises(
                                environments.EnvironmentsError) as ctx:
                            call()
                        self.assertIn("corrupt", str(ctx.exception))

    def test_a_write_over_a_corrupt_store_is_refused_and_changes_nothing(self):
        for label, payload in self.CORRUPTIONS.items():
            with self.subTest(store=label):
                self.write_store(payload)
                before = self.read_store()
                for name, call in (
                        ("add", lambda: environments.add_environment(
                            "fresh", ["echo x"])),
                        ("remove",
                         lambda: environments.remove_environment("demo")),
                        ("set_default",
                         lambda: environments.set_default("worktree"))):
                    with self.subTest(call=name):
                        with self.assertRaises(
                                environments.EnvironmentsError):
                            call()
                # Repairing corruption is the operator's call, not a silent
                # overwrite of state they may still want to read.
                self.assertEqual(self.read_store(), before)

    def test_a_default_naming_a_deleted_environment_stays_repairable(self):
        # Only the SHAPE of `default` is validated on read: a dangling default
        # must not brick `crew env set-default none`, which is how an operator
        # fixes it. The spawn that tries to use it fails cleanly instead.
        self.write_store({"default": "vanished", "environments": []})
        self.assertEqual(environments.default_name(), "vanished")
        with self.assertRaises(environments.EnvironmentsError) as ctx:
            environments.resolve()
        self.assertIn("unknown environment 'vanished'", str(ctx.exception))
        self.assertIsNone(environments.set_default(None))


# --------------------------------------------------------------------------- #
# the crew-wide default
# --------------------------------------------------------------------------- #
class DefaultTests(EnvironmentsCase):
    def test_there_is_no_default_until_one_is_set(self):
        self.assertIsNone(environments.default_name())
        self.assertIsNone(environments.resolve())

    def test_a_builtin_or_a_custom_may_be_the_default(self):
        self.add_demo("mine")
        for name in ("worktree", "graphite-stack", "mine"):
            with self.subTest(name=name):
                self.assertEqual(environments.set_default(name), name)
                self.assertEqual(environments.default_name(), name)
                self.assertEqual(environments.resolve()["name"], name)

    def test_the_default_is_cleared_by_none_or_a_blank(self):
        for blank in (None, "", "   "):
            with self.subTest(blank=repr(blank)):
                environments.set_default("worktree")
                self.assertIsNone(environments.set_default(blank))
                self.assertIsNone(environments.default_name())

    def test_an_unknown_default_is_refused_and_the_old_one_survives(self):
        environments.set_default("worktree")
        with self.assertRaises(environments.EnvironmentsError) as ctx:
            environments.set_default("nope")
        self.assertIn("unknown environment 'nope'", str(ctx.exception))
        self.assertEqual(environments.default_name(), "worktree")

    def test_removing_the_default_environment_clears_it(self):
        # A default naming a deleted environment would fail every later spawn.
        self.add_demo("chosen")
        environments.set_default("chosen")
        self.assertTrue(environments.remove_environment("chosen"))
        self.assertIsNone(environments.default_name())

    def test_removing_another_environment_leaves_the_default_alone(self):
        self.add_demo("chosen")
        self.add_demo("other")
        environments.set_default("chosen")
        environments.remove_environment("other")
        self.assertEqual(environments.default_name(), "chosen")

    def test_resolve_prefers_an_explicit_pick_over_the_default(self):
        self.add_demo("mine")
        environments.set_default("mine")
        self.assertEqual(environments.resolve("worktree")["name"], "worktree")
        self.assertEqual(environments.resolve("  worktree  ")["name"],
                         "worktree")
        self.assertEqual(environments.resolve("")["name"], "mine")
        self.assertEqual(environments.resolve(None)["name"], "mine")

    def test_resolve_refuses_an_unknown_explicit_pick(self):
        with self.assertRaises(environments.EnvironmentsError) as ctx:
            environments.resolve("nope")
        self.assertIn("unknown environment 'nope'", str(ctx.exception))


# --------------------------------------------------------------------------- #
# run_setup — the executor
# --------------------------------------------------------------------------- #
class RunSetupTests(EnvironmentsCase):
    HOME = "/tmp/crew-environments-test-home"
    ENV = {"CREW_AGENT": "worker", "PATH": "/crew/bin:/usr/bin"}

    @contextlib.contextmanager
    def fake_subprocess(self, results=None, default=(0, "", "")):
        runner = FakeRun(results, default)
        with mock.patch.object(environments.subprocess, "run", runner):
            yield runner

    def entry(self, **overrides):
        base = {"name": "demo", "description": "", "prereq": "",
                "commands": ["echo one", "echo two"]}
        base.update(overrides)
        return base

    def test_a_successful_routine_runs_the_prereq_then_every_command(self):
        with self.fake_subprocess() as runner:
            self.assertEqual(
                environments.run_setup(
                    self.entry(prereq="gt --version"), self.HOME, "worker",
                    self.ENV),
                (True, ""))
        self.assertEqual(runner.commands,
                         ["gt --version", "echo one", "echo two"])

    def test_every_command_runs_as_one_shell_line_in_the_agents_home(self):
        with self.fake_subprocess() as runner:
            environments.run_setup(
                self.entry(prereq="true"), self.HOME, "worker", self.ENV)
        for command, kwargs in runner.calls:
            with self.subTest(command=command):
                self.assertTrue(kwargs["shell"])
                self.assertEqual(kwargs["cwd"], self.HOME)
                self.assertEqual(kwargs["env"], self.ENV)
                self.assertTrue(kwargs["capture_output"])
                self.assertTrue(kwargs["text"])
        self.assertEqual(runner.calls[0][1]["timeout"],
                         environments.PREREQ_TIMEOUT)
        self.assertEqual(runner.calls[1][1]["timeout"],
                         environments.COMMAND_TIMEOUT)

    def test_the_agent_placeholder_is_substituted_everywhere(self):
        with self.fake_subprocess() as runner:
            environments.run_setup(
                environments.get("graphite-stack"), self.HOME, "felix-bot",
                self.ENV)
        self.assertEqual(runner.commands, [
            "gt --version", "git checkout main",
            "gt create crew/felix-bot --no-interactive"])

    def test_other_braces_survive_substitution(self):
        # A plain replace, never str.format: shell and awk braces are ordinary
        # characters in a setup command.
        entry = self.entry(commands=["awk '{print $1}' f > ${OUT}-{agent}"])
        with self.fake_subprocess() as runner:
            environments.run_setup(entry, self.HOME, "worker", self.ENV)
        self.assertEqual(runner.commands,
                         ["awk '{print $1}' f > ${OUT}-worker"])

    def test_a_failing_prereq_stops_before_any_command(self):
        with self.fake_subprocess(
                {"gt --version": (127, "", "gt: command not found")}) as runner:
            ok, detail = environments.run_setup(
                self.entry(prereq="gt --version"), self.HOME, "worker",
                self.ENV)
        self.assertFalse(ok)
        self.assertIn("prereq failed: gt --version", detail)
        self.assertIn("gt: command not found", detail)
        self.assertEqual(runner.commands, ["gt --version"])

    def test_a_failing_command_stops_the_rest(self):
        with self.fake_subprocess(
                {"echo one": (1, "", "fatal: not a git repository")}) as runner:
            ok, detail = environments.run_setup(
                self.entry(), self.HOME, "worker", self.ENV)
        self.assertFalse(ok)
        self.assertIn("command failed: echo one", detail)
        self.assertIn("fatal: not a git repository", detail)
        self.assertEqual(runner.commands, ["echo one"])

    def test_a_silent_failure_still_reports_its_exit_status(self):
        with self.fake_subprocess({"echo one": (3, "", "")}):
            ok, detail = environments.run_setup(
                self.entry(), self.HOME, "worker", self.ENV)
        self.assertFalse(ok)
        self.assertIn("exit status 3", detail)

    def test_a_timeout_is_reported_honestly_and_never_raises(self):
        timeout = subprocess.TimeoutExpired("echo one", 300)
        with self.fake_subprocess({"echo one": timeout}):
            ok, detail = environments.run_setup(
                self.entry(), self.HOME, "worker", self.ENV)
        self.assertFalse(ok)
        self.assertIn("timed out after", detail)
        self.assertIn("echo one", detail)

    def test_an_oserror_is_reported_honestly_and_never_raises(self):
        with self.fake_subprocess({"echo one": OSError("no such shell")}):
            ok, detail = environments.run_setup(
                self.entry(), self.HOME, "worker", self.ENV)
        self.assertFalse(ok)
        self.assertIn("no such shell", detail)

    def test_a_broken_entry_comes_back_as_a_failure_not_an_exception(self):
        # The caller runs this between materializing a home and opening a
        # session; an exception escaping here would skip its cleanup.
        with self.fake_subprocess():
            ok, detail = environments.run_setup(
                {"name": "demo", "commands": [object()]}, self.HOME, "worker",
                self.ENV)
        self.assertFalse(ok)
        self.assertTrue(detail)

    def test_a_long_failure_is_tailed_not_truncated_from_the_front(self):
        noise = "\n".join(f"line {index}" for index in range(500))
        with self.fake_subprocess({"echo one": (1, "", noise + "\nTHE CAUSE")}):
            ok, detail = environments.run_setup(
                self.entry(), self.HOME, "worker", self.ENV)
        self.assertFalse(ok)
        self.assertIn("THE CAUSE", detail)
        self.assertLessEqual(len(detail.split(": ", 2)[-1]),
                             environments.MAX_DETAIL_LEN)

    def test_an_environment_with_nothing_to_run_succeeds_without_a_shell(self):
        with self.fake_subprocess() as runner:
            for entry in (None, {}, environments.get("worktree")):
                with self.subTest(entry=entry):
                    self.assertEqual(
                        environments.run_setup(
                            entry, self.HOME, "worker", self.ENV),
                        (True, ""))
        self.assertEqual(runner.calls, [])


# --------------------------------------------------------------------------- #
# crew.spawn wiring
# --------------------------------------------------------------------------- #
class SpawnWiringTests(EnvironmentsCase):
    HOME = "/tmp/crew-environments-test-home"

    def setUp(self):
        super().setUp()
        self.steps = []
        self.created = {}
        self.setup_calls = []

    @contextlib.contextmanager
    def boundaries(self, plan=("mkdir",), setup_result=(True, ""),
                   patch_setup=True):
        """Every external boundary of a spawn, mocked. Nothing here touches
        tmux, MorphDB, or a real directory."""
        row = {"_guid": "spawned-guid", "name": "worker", "home": self.HOME,
               "session": "worker", "runtime": "custom", "launch_cmd": "true",
               "status": "not_started", "environment": ""}
        state = {"created": False}

        def create_agent(*_args, **kwargs):
            self.steps.append("create_agent")
            self.created.update(kwargs)
            state["created"] = True
            row["environment"] = kwargs.get("environment", "")
            return dict(row)

        def get_agent_by_name(_name):
            return dict(row) if state["created"] else None

        def run_setup(entry, home, name, environment):
            self.steps.append("run_setup")
            self.setup_calls.append((entry, home, name, environment))
            return setup_result

        with contextlib.ExitStack() as stack:
            patch = stack.enter_context
            patch(mock.patch.object(spawn.guard, "check"))
            patch(mock.patch.object(spawn.guard, "audit"))
            patch(mock.patch.object(
                spawn.gs, "get_node_by_name", return_value=None))
            patch(mock.patch.object(
                spawn.gs, "get_agent_by_name", side_effect=get_agent_by_name))
            patch(mock.patch.object(
                spawn.gs, "unsafe_home_reason", return_value=None))
            patch(mock.patch.object(
                spawn.gs, "home_conflict_across_apps", return_value=None))
            patch(mock.patch.object(spawn.gs, "_require_actor_guid"))
            patch(mock.patch.object(
                spawn.gs, "create_agent", side_effect=create_agent))
            patch(mock.patch.object(
                spawn, "_plan_home", return_value=(self.HOME, None, plan)))
            patch(mock.patch.object(
                spawn, "_tmux", return_value=(False, "")))
            patch(mock.patch.object(spawn, "rewrite_identity"))
            mocks = {
                "materialize": patch(mock.patch.object(
                    spawn, "_materialize_home",
                    side_effect=lambda *a: self.steps.append("materialize"))),
                "open_session": patch(mock.patch.object(
                    spawn, "_open_session",
                    side_effect=lambda *a: self.steps.append(
                        "open_session") or "%1")),
                "rollback": patch(mock.patch.object(
                    spawn, "_rollback_materialized_home",
                    side_effect=lambda *a: self.steps.append("rollback"))),
                "launch": patch(mock.patch.object(spawn, "_launch_runtime")),
            }
            if patch_setup:
                mocks["run_setup"] = patch(mock.patch.object(
                    environments, "run_setup", side_effect=run_setup))
            yield mocks

    def spawn(self, actor="human", **kwargs):
        fields = {"runtime": "custom", "launch_cmd": "true", "launch": False}
        if actor != "human":
            # An agent actor may not pass either of those; the spawn falls back
            # to the project default runtime.
            fields = {"launch": False}
            kwargs.setdefault("_actor_guid", "agent-guid")
        fields.update(kwargs)
        return spawn._spawn_agent_locked("worker", actor=actor, **fields)

    # -- resolution ---------------------------------------------------------
    def test_no_environment_at_all_records_an_empty_provenance(self):
        with self.boundaries() as mocks:
            self.spawn()
        self.assertEqual(self.created["environment"], "")
        mocks["run_setup"].assert_not_called()

    def test_the_crew_wide_default_prepares_a_spawn_that_asks_for_nothing(self):
        self.add_demo("prep")
        environments.set_default("prep")
        with self.boundaries():
            agent = self.spawn()
        self.assertEqual(self.created["environment"], "prep")
        self.assertEqual(agent["environment"], "prep")
        self.assertEqual(self.setup_calls[0][0]["name"], "prep")

    def test_an_explicit_environment_beats_the_default(self):
        self.add_demo("prep")
        self.add_demo("special", commands=["echo special"])
        environments.set_default("prep")
        with self.boundaries():
            self.spawn(environment="special")
        self.assertEqual(self.created["environment"], "special")
        self.assertEqual(self.setup_calls[0][0]["commands"], ["echo special"])

    def test_an_unknown_environment_fails_before_any_side_effect(self):
        with self.boundaries() as mocks:
            with self.assertRaises(gs.GraphError) as ctx:
                self.spawn(environment="nope")
        self.assertIn("unknown environment 'nope'", str(ctx.exception))
        mocks["materialize"].assert_not_called()
        mocks["run_setup"].assert_not_called()
        self.assertEqual(self.steps, [])

    # -- setup execution ----------------------------------------------------
    def test_setup_runs_after_the_home_exists_and_before_the_session(self):
        self.add_demo("prep")
        with self.boundaries():
            self.spawn(environment="prep")
        self.assertEqual(
            self.steps,
            ["materialize", "run_setup", "open_session", "create_agent"])

    def test_setup_sees_the_home_the_agent_name_and_the_session_context(self):
        self.add_demo("prep")
        with self.boundaries():
            self.spawn(environment="prep")
        _entry, home, name, environment = self.setup_calls[0]
        self.assertEqual(home, self.HOME)
        self.assertEqual(name, "worker")
        # The pane inherits the ambient environment with crew's context laid
        # over it; a bare context dict would drop $HOME, which git and gt read.
        self.assertEqual(environment["CREW_AGENT"], "worker")
        self.assertEqual(environment["AGENT_MAIL_NAME"], "worker")
        self.assertEqual(environment["CREW_PROJECT"],
                         config.current_project())
        for key in ("HOME", "PATH"):
            self.assertIn(key, environment)

    def test_a_failed_setup_fails_the_spawn_and_leaks_nothing(self):
        self.add_demo("prep")
        with self.boundaries(
                setup_result=(False, "command failed: npm ci: ENOENT")) as m:
            with self.assertRaises(gs.GraphError) as ctx:
                self.spawn(environment="prep")
        message = str(ctx.exception)
        self.assertIn("environment 'prep' failed to prepare", message)
        self.assertIn("npm ci: ENOENT", message)
        # No session, no durable row, and the home crew created is rolled back.
        m["open_session"].assert_not_called()
        m["launch"].assert_not_called()
        self.assertNotIn("create_agent", self.steps)
        self.assertEqual(self.steps, ["materialize", "run_setup", "rollback"])

    def test_a_real_setup_command_runs_in_the_agents_home(self):
        # The executor is mocked out of the other spawn tests; run one spawn
        # through the real run_setup with only the shell mocked, so the seam
        # between spawn and crew.environments is exercised end to end.
        self.add_demo("prep", commands=["echo {agent} > READY"])
        runner = FakeRun()
        with self.boundaries(patch_setup=False), \
                mock.patch.object(environments.subprocess, "run", runner):
            self.spawn(environment="prep")
        self.assertEqual(runner.commands, ["echo worker > READY"])
        self.assertEqual(runner.calls[0][1]["cwd"], self.HOME)
        self.assertEqual(runner.calls[0][1]["env"]["CREW_AGENT"], "worker")

    # -- the native worktree environment ------------------------------------
    def test_the_worktree_environment_routes_the_repo_machinery(self):
        # --repo IS this environment: the same mechanism, so nothing extra runs
        # and the durable row still records which environment prepared it.
        plan = ("worktree", "/tmp/repo", "main", "crew/default/worker")
        with self.boundaries(plan=plan) as mocks:
            self.spawn(environment="worktree", repo="/tmp/repo")
        self.assertEqual(self.created["environment"], "worktree")
        mocks["run_setup"].assert_not_called()
        self.assertIn("open_session", self.steps)

    def test_the_worktree_environment_refuses_to_guess_a_repository(self):
        for kwargs, extra in (({}, None), ({"home": "/tmp/plain"}, "--home")):
            with self.subTest(spawn=kwargs or "default home"):
                self.steps.clear()
                with self.boundaries() as mocks:
                    with self.assertRaises(gs.GraphError) as ctx:
                        self.spawn(environment="worktree", **kwargs)
                message = str(ctx.exception)
                self.assertIn("--repo", message)
                self.assertIn("worktree", message)
                if extra:
                    self.assertIn(extra, message)
                mocks["materialize"].assert_not_called()
                self.assertEqual(self.steps, [])

    def test_a_worktree_default_refuses_a_spawn_without_a_repository(self):
        environments.set_default("worktree")
        with self.boundaries() as mocks:
            with self.assertRaises(gs.GraphError) as ctx:
                self.spawn()
        self.assertIn("--repo", str(ctx.exception))
        mocks["materialize"].assert_not_called()

    # -- agent-actor confinement --------------------------------------------
    def test_an_agent_actor_may_not_pick_an_environment(self):
        self.add_demo("prep")
        with self.boundaries() as mocks:
            with self.assertRaises(gs.GraphError) as ctx:
                self.spawn(actor="foreman", environment="prep")
        message = str(ctx.exception)
        self.assertIn("agents may not pass --env", message)
        self.assertIn("ask the user", message)
        mocks["materialize"].assert_not_called()

    def test_the_refusal_is_audited_like_the_other_confinements(self):
        with self.boundaries():
            with mock.patch.object(spawn.guard, "audit") as audit:
                with self.assertRaises(gs.GraphError):
                    self.spawn(actor="foreman", environment="worktree")
        self.assertEqual(audit.call_args.args[1:2], ("spawn",))
        self.assertEqual(audit.call_args.args[2].get("refused_arg"),
                         "environment")
        self.assertEqual(audit.call_args.args[3], "refused")

    def test_the_crew_wide_default_still_applies_to_an_agents_spawn(self):
        # An agent may not CHOOSE what runs in a new workspace, but it does not
        # get to skip what the operator chose either.
        self.add_demo("prep")
        environments.set_default("prep")
        with self.boundaries():
            self.spawn(actor="foreman")
        self.assertEqual(self.created["environment"], "prep")
        self.assertEqual(self.setup_calls[0][0]["name"], "prep")


# --------------------------------------------------------------------------- #
# crew.guard — defining an environment is human-only
# --------------------------------------------------------------------------- #
class GuardTests(EnvironmentsCase):
    """An environment's commands run as the operator in every spawn that uses
    it, so this is the same tier as `remove`/`bless`/`settings_write`: not even
    the foreman flag covers it."""

    def agent_row(self, name="env_writer", foreman=False):
        return {"_guid": f"{name}-guid", "name": name,
                "home": f"/tmp/crew_environmentstest/{name}",
                "can_edit_graph": foreman}

    def refuse(self, agent):
        with mock.patch.object(gs, "get_agent_by_name", return_value=agent), \
                mock.patch.object(guard, "audit") as audit:
            with self.assertRaises(gs.GraphError) as ctx:
                guard.check(agent["name"], "environments_write", name="demo")
        return str(ctx.exception), audit

    def test_environments_write_is_a_human_only_op(self):
        self.assertIn("environments_write", guard.HUMAN_ONLY_OPS)

    def test_an_agent_actor_is_refused_and_told_what_to_ask_for(self):
        message, audit = self.refuse(self.agent_row())
        self.assertIn("human operator", message)
        self.assertIn("crew env", message)
        self.assertIn("run as the operator", message)
        self.assertEqual(audit.call_args.args[1:4],
                         ("environments_write", mock.ANY, "refused"))

    def test_a_foreman_is_refused_too(self):
        message, _ = self.refuse(self.agent_row("env_foreman", foreman=True))
        self.assertIn("crew env", message)

    def test_an_unregistered_actor_is_refused(self):
        with mock.patch.object(gs, "get_agent_by_name",
                               side_effect=gs.GraphError("no such agent")), \
                mock.patch.object(guard, "audit"):
            with self.assertRaises(gs.GraphError) as ctx:
                guard.check("ghost", "environments_write", name="demo")
        self.assertIn("crew env", str(ctx.exception))

    def test_a_human_is_cleared_without_consulting_the_graph(self):
        with mock.patch.object(gs, "get_agent_by_name") as lookup:
            self.assertIsNone(
                guard.check("human", "environments_write", name="demo"))
        lookup.assert_not_called()

    def test_the_environment_provenance_field_is_not_agent_writable(self):
        # A foreman may update only descriptive metadata on its children; which
        # environment prepared a workspace is a durable fact, not a note.
        self.assertIn("environment", guard.PROTECTED_AGENT_FIELDS)
        self.assertNotIn("environment", guard.FOREMAN_AGENT_FIELDS)


# --------------------------------------------------------------------------- #
# the `crew env` CLI
# --------------------------------------------------------------------------- #
class CliTests(EnvironmentsCase):
    def run_handler(self, handler, actor="human", **fields):
        out = io.StringIO()
        with mock.patch.object(cli, "_ACTOR", actor), \
                contextlib.redirect_stdout(out):
            code = handler(argparse.Namespace(**fields))
        return code, out.getvalue()

    def test_list_shows_the_builtins_their_prereq_and_their_commands(self):
        code, out = self.run_handler(cli.cmd_env_list, json=False)
        self.assertEqual(code, 0)
        self.assertIn("worktree (built-in)", out)
        self.assertIn("graphite-stack (built-in)", out)
        self.assertIn("prereq: gt --version", out)
        self.assertIn("$ gt create crew/{agent} --no-interactive", out)
        self.assertIn("no crew-wide default", out)

    def test_list_json_carries_the_default_and_every_environment(self):
        self.add_demo("prep")
        environments.set_default("prep")
        code, out = self.run_handler(cli.cmd_env_list, json=True)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out),
                         {"default": "prep",
                          "environments": environments.list_all()})

    def test_add_list_set_default_remove_round_trip(self):
        code, out = self.run_handler(
            cli.cmd_env_add, name="prep", command=["npm ci", "cp ../.env ."],
            prereq="node --version", description="install deps")
        self.assertEqual(code, 0)
        self.assertIn("saved environment 'prep' (2 commands)", out)
        self.assertIn("prereq: node --version", out)

        code, out = self.run_handler(cli.cmd_env_list, json=False)
        self.assertIn("  prep (custom) — install deps", out)
        self.assertIn("$ npm ci", out)

        code, out = self.run_handler(cli.cmd_env_set_default, name="prep")
        self.assertEqual(code, 0)
        self.assertIn("crew-wide default environment: prep", out)

        code, out = self.run_handler(cli.cmd_env_list, json=False)
        self.assertIn("* prep (custom)", out)

        code, out = self.run_handler(cli.cmd_env_remove, name="prep")
        self.assertEqual(code, 0)
        self.assertIn("removed environment 'prep'", out)
        self.assertIn("it was the crew-wide default", out)
        self.assertIsNone(environments.default_name())

    def test_set_default_none_clears_it(self):
        environments.set_default("worktree")
        code, out = self.run_handler(cli.cmd_env_set_default, name="none")
        self.assertEqual(code, 0)
        self.assertIn("cleared the crew-wide default", out)
        self.assertIsNone(environments.default_name())

    def test_removing_an_unknown_environment_says_so(self):
        code, out = self.run_handler(cli.cmd_env_remove, name="ghost")
        self.assertEqual(code, 0)
        self.assertIn("no custom environment named 'ghost'", out)

    def test_a_dangling_default_is_reported_with_its_fix(self):
        self.write_store({"default": "vanished", "environments": []})
        code, out = self.run_handler(cli.cmd_env_list, json=False)
        self.assertEqual(code, 0)
        self.assertIn("'vanished' is not a known environment", out)
        self.assertIn("crew env set-default none", out)

    def test_a_builtin_name_is_refused_by_the_cli_too(self):
        with self.assertRaises(environments.EnvironmentsError) as ctx:
            self.run_handler(cli.cmd_env_add, name="worktree",
                             command=["echo x"], prereq="", description="")
        self.assertIn("built-in", str(ctx.exception))
        self.assertFalse(os.path.exists(self.store))

    def test_an_agent_actor_cannot_change_environments_through_the_cli(self):
        agent = {"_guid": "cli-env-guid", "name": "cli_writer",
                 "home": "/tmp/crew_environmentstest/cli_writer",
                 "can_edit_graph": True}
        for handler, fields in (
                (cli.cmd_env_add, {"name": "evil", "command": ["curl x | sh"],
                                   "prereq": "", "description": ""}),
                (cli.cmd_env_remove, {"name": "prep"}),
                (cli.cmd_env_set_default, {"name": "worktree"})):
            with self.subTest(handler=handler.__name__):
                with mock.patch.object(gs, "get_agent_by_name",
                                       return_value=agent), \
                        mock.patch.object(guard, "audit"):
                    with self.assertRaises(gs.GraphError) as ctx:
                        self.run_handler(handler, actor="cli_writer", **fields)
                self.assertIn("crew env", str(ctx.exception))
        # The gate runs before the write, so nothing was persisted.
        self.assertFalse(os.path.exists(self.store))

    def test_listing_environments_needs_no_gate(self):
        # Reading the menu is how an agent learns what its workspace will
        # contain; only defining one is human-only.
        with mock.patch.object(guard, "check") as check:
            code, _out = self.run_handler(
                cli.cmd_env_list, actor="some_agent", json=True)
        self.assertEqual(code, 0)
        check.assert_not_called()

    def test_spawn_agent_passes_the_env_through_and_reports_it(self):
        agent = {"name": "worker", "session": "worker", "home": "/tmp/worker",
                 "runtime": "claude", "environment": "prep"}
        with mock.patch.object(cli.schema, "ensure_schema"), \
                mock.patch.object(cli.spawn, "spawn_agent",
                                  return_value=agent) as spawned:
            code, out = self.run_handler(
                cli.cmd_spawn_agent, name="worker", role=None, identity=None,
                home=None, repo=None, no_launch=True, launch_cmd=None,
                runtime=None, foreman=False, environment="prep")
        self.assertEqual(code, 0)
        self.assertEqual(spawned.call_args.kwargs["environment"], "prep")
        self.assertIn("environment: prep", out)

    def test_the_env_parser_documents_the_agent_placeholder(self):
        parser = cli.build_parser()
        with contextlib.redirect_stdout(io.StringIO()) as out:
            with self.assertRaises(SystemExit):
                parser.parse_args(["env", "add", "--help"])
        help_text = out.getvalue()
        self.assertIn("{agent}", help_text)
        self.assertIn("--prereq", help_text)

    def test_spawn_agent_accepts_env_and_defaults_it_to_none(self):
        parser = cli.build_parser()
        self.assertEqual(
            parser.parse_args(["spawn-agent", "w", "--env", "prep"]).environment,
            "prep")
        self.assertIsNone(parser.parse_args(["spawn-agent", "w"]).environment)

    def test_the_cli_reports_an_environments_error_cleanly(self):
        self.write_store("{not json at all")
        err = io.StringIO()
        with mock.patch.object(cli, "build_parser") as parser, \
                mock.patch.object(cli.mail, "whoami", return_value="unknown"), \
                contextlib.redirect_stderr(err):
            parser.return_value.parse_args.return_value = argparse.Namespace(
                project=None, json=False,
                fn=lambda _a: environments.list_all())
            code = cli.main([])
        self.assertEqual(code, 1)
        self.assertIn("corrupt", err.getvalue())


if __name__ == "__main__":
    unittest.main()
