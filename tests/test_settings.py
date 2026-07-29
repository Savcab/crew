"""crew.settings — the durable crew-wide settings store and its consumers.

A stored launch command is typed verbatim into every future agent pane, so
this store is closer to a credential than to a preference file.  These tests
pin the four properties that follow from that:

  (1) precedence is exactly env > stored > built-in default, read LIVE on
      every call (the dashboard is long-running and reconfigures without a
      restart), and a set-but-empty env var is not an override;
  (2) writes are validated and normalized BEFORE they land — the value must be
      one of that key's CURATED CHOICES, owner-only on disk — so the launch
      path can never inherit a command nobody vetted. Reads are deliberately
      looser: a value stored before a choice set tightened still answers, and
      an env override is a per-process escape hatch that skips the menu;
  (3) unreadable durable state fails closed with SettingsError instead of
      silently reverting to a default, which is how a truncated file would
      otherwise undo a deliberate configuration without anyone noticing;
  (4) the consumers agree: crew.runtime resolves launch commands through this
      module, crew.guard keeps writes human-only, and the `crew settings` CLI
      surfaces the source of every effective value.

Every test points the store at a temp directory (crew.settings reads
``config.VAR`` live) and clears the launch-command env vars, so nothing here
reads or writes the real var/settings.json, and a developer shell that exports
$CREW_CLAUDE_LAUNCH_CMD cannot change an outcome.

    python3 -m unittest tests.test_settings   (from the repo root)
"""
import argparse
import contextlib
import io
import json
import os
import shlex
import shutil
import stat
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from crew import cli, config, guard, settings  # noqa: E402
from crew import graphstore as gs  # noqa: E402
from crew import runtime as runtimes  # noqa: E402


# Every env name the store consults, including the legacy claude-only one.
LAUNCH_ENV = ("CREW_CLAUDE_LAUNCH_CMD", "CREW_LAUNCH_CMD",
              "CREW_CODEX_LAUNCH_CMD", "CREW_HERMES_LAUNCH_CMD")

# The curated menus, pinned here on purpose: every command below is a flag
# combination verified against the real CLI, and a settable launch command is
# typed verbatim into a pane. Adding one is a deliberate act that has to change
# this list too — an invented flag cannot arrive by accident.
EXPECTED_CHOICES = {
    "claude_launch_cmd": [
        ("Unattended (default)", "claude --dangerously-skip-permissions"),
        ("Ask for permissions", "claude"),
        ("Unattended, continue last session",
         "claude --dangerously-skip-permissions --continue"),
    ],
    "codex_launch_cmd": [
        ("Unattended (default)",
         "codex --dangerously-bypass-approvals-and-sandbox --disable hooks"),
        ("Sandboxed with approvals", "codex"),
    ],
    "hermes_launch_cmd": [
        ("Default", "hermes"),
        ("Auto-approve (yolo)", "hermes --yolo"),
        ("Continue last session", "hermes --continue"),
    ],
}


class SettingsCase(unittest.TestCase):
    """A throwaway store plus a launch-command-free environment."""

    def setUp(self):
        self.var = tempfile.mkdtemp(prefix="crew-settings-test-")
        self.addCleanup(shutil.rmtree, self.var, ignore_errors=True)
        # settings resolves var/settings.json through config.VAR on every
        # call, so one patch covers both the reads and the atomic write.
        var_patch = mock.patch.object(config, "VAR", self.var)
        var_patch.start()
        self.addCleanup(var_patch.stop)
        env_patch = mock.patch.dict(os.environ, {})
        env_patch.start()
        self.addCleanup(env_patch.stop)
        for name in LAUNCH_ENV:
            os.environ.pop(name, None)

    # -- fixture helpers ----------------------------------------------------
    @property
    def store(self):
        return os.path.join(self.var, "settings.json")

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


# --------------------------------------------------------------------------- #
# no store at all: the built-in defaults
# --------------------------------------------------------------------------- #
class DefaultsTests(SettingsCase):
    def test_every_key_reads_its_built_in_default(self):
        for key, default in (
                ("claude_launch_cmd", config.CLAUDE_LAUNCH_CMD_DEFAULT),
                ("codex_launch_cmd", config.CODEX_LAUNCH_CMD_DEFAULT),
                ("hermes_launch_cmd", config.HERMES_LAUNCH_CMD_DEFAULT)):
            self.assertEqual(settings.effective(key), (default, "default"))

    def test_reading_does_not_create_a_store(self):
        settings.describe()
        settings.effective("claude_launch_cmd")
        self.assertFalse(os.path.exists(self.store))

    def test_describe_lists_exactly_the_known_keys_sorted(self):
        rows = settings.describe()
        self.assertEqual([row["key"] for row in rows], sorted(settings.KEYS))
        self.assertEqual(len(rows), 3)
        for row in rows:
            self.assertIsNone(row["override"])
            self.assertEqual(row["source"], "default")
            self.assertEqual(row["effective"], row["default"])
            self.assertTrue(row["label"])

    def test_launch_cmd_answers_for_every_harness_runtime(self):
        self.assertEqual(settings.launch_cmd("claude"),
                         config.CLAUDE_LAUNCH_CMD_DEFAULT)
        self.assertEqual(settings.launch_cmd("codex"),
                         config.CODEX_LAUNCH_CMD_DEFAULT)
        self.assertEqual(settings.launch_cmd("hermes"),
                         config.HERMES_LAUNCH_CMD_DEFAULT)


# --------------------------------------------------------------------------- #
# the curated choice sets — a launch command is picked, never typed
# --------------------------------------------------------------------------- #
class ChoiceTests(SettingsCase):
    def test_each_key_offers_exactly_its_curated_menu(self):
        for key, expected in EXPECTED_CHOICES.items():
            with self.subTest(key=key):
                self.assertEqual(
                    [(c["label"], c["command"])
                     for c in settings.KEYS[key]["choices"]],
                    expected)

    def test_the_first_choice_of_every_key_is_its_built_in_default(self):
        # The UI shows choices[0] preselected for an unconfigured key, so a
        # drift here would silently offer a different command than the one an
        # unconfigured crew actually launches.
        for key, spec in settings.KEYS.items():
            with self.subTest(key=key):
                self.assertEqual(spec["choices"][0]["command"], spec["default"])

    def test_every_choice_is_a_label_and_a_launchable_command(self):
        for key, spec in settings.KEYS.items():
            commands = [c["command"] for c in spec["choices"]]
            self.assertEqual(len(commands), len(set(commands)), key)
            for choice in spec["choices"]:
                with self.subTest(key=key, command=choice.get("command")):
                    self.assertEqual(sorted(choice), ["command", "label"])
                    self.assertTrue(choice["label"].strip())
                    command = choice["command"]
                    # Every curated command is the thing the old free-text
                    # validation used to demand of user input: one printable,
                    # shlex-parsable line that fits the pane input limit.
                    self.assertEqual(command, command.strip())
                    self.assertTrue(command.isprintable())
                    self.assertLessEqual(len(command), settings.MAX_VALUE_LEN)
                    self.assertTrue(shlex.split(command))

    def test_every_choice_command_is_accepted_for_its_own_key(self):
        for key, spec in settings.KEYS.items():
            for choice in spec["choices"]:
                with self.subTest(key=key, command=choice["command"]):
                    self.assertEqual(
                        settings.set_value(key, choice["command"]),
                        choice["command"])
                    self.assertEqual(settings.effective(key),
                                     (choice["command"], "settings"))

    def test_a_choice_command_is_matched_after_stripping(self):
        self.assertEqual(
            settings.set_value("hermes_launch_cmd", "  hermes --yolo \t"),
            "hermes --yolo")
        self.assertEqual(settings.effective("hermes_launch_cmd")[0],
                         "hermes --yolo")

    def test_a_plausible_command_that_is_not_a_choice_is_refused(self):
        # Syntactically perfect and a real flag — but nobody vetted it, so the
        # menu is the whole contract. The message has to name every way out.
        with self.assertRaises(settings.SettingsError) as ctx:
            settings.set_value("claude_launch_cmd", "claude --model opus")
        message = str(ctx.exception)
        self.assertIn("claude_launch_cmd", message)
        for _, command in EXPECTED_CHOICES["claude_launch_cmd"]:
            self.assertIn(command, message)
        self.assertFalse(os.path.exists(self.store))

    def test_another_keys_choice_is_not_a_choice_here(self):
        for key, other in (("claude_launch_cmd", "hermes --yolo"),
                           ("hermes_launch_cmd", "claude"),
                           ("codex_launch_cmd", "hermes")):
            with self.subTest(key=key, value=other):
                with self.assertRaises(settings.SettingsError) as ctx:
                    settings.set_value(key, other)
                self.assertIn(key, str(ctx.exception))

    def test_describe_carries_the_choices_for_every_key(self):
        for row in settings.describe():
            with self.subTest(key=row["key"]):
                self.assertEqual(row["choices"],
                                 settings.KEYS[row["key"]]["choices"])
                self.assertEqual(row["choices"][0]["command"], row["default"])


# --------------------------------------------------------------------------- #
# writing and clearing an override
# --------------------------------------------------------------------------- #
class StoredValueTests(SettingsCase):
    CLAUDE_CONTINUE = "claude --dangerously-skip-permissions --continue"

    def test_set_value_persists_owner_only_and_flips_the_source(self):
        settings.set_value("claude_launch_cmd", self.CLAUDE_CONTINUE)
        self.assertEqual(stat.S_IMODE(os.stat(self.store).st_mode), 0o600)
        self.assertEqual(json.loads(self.read_store()),
                         {"claude_launch_cmd": self.CLAUDE_CONTINUE})
        self.assertEqual(settings.effective("claude_launch_cmd"),
                         (self.CLAUDE_CONTINUE, "settings"))
        self.assertEqual(settings.launch_cmd("claude"), self.CLAUDE_CONTINUE)

    def test_setting_one_key_leaves_the_others_on_their_defaults(self):
        settings.set_value("codex_launch_cmd", "codex")
        rows = {row["key"]: row for row in settings.describe()}
        self.assertEqual(rows["codex_launch_cmd"]["override"], "codex")
        self.assertEqual(rows["codex_launch_cmd"]["source"], "settings")
        self.assertIsNone(rows["claude_launch_cmd"]["override"])
        self.assertEqual(rows["claude_launch_cmd"]["source"], "default")

    def test_clear_value_removes_the_override_and_is_idempotent(self):
        settings.set_value("claude_launch_cmd", self.CLAUDE_CONTINUE)
        self.assertTrue(settings.clear_value("claude_launch_cmd"))
        self.assertEqual(
            settings.effective("claude_launch_cmd"),
            (config.CLAUDE_LAUNCH_CMD_DEFAULT, "default"))
        # A second clear is a no-op, not an error: the CLI reports "had no
        # stored override" off this False.
        self.assertFalse(settings.clear_value("claude_launch_cmd"))
        self.assertEqual(json.loads(self.read_store()), {})


# --------------------------------------------------------------------------- #
# a value stored before the menu tightened still answers
# --------------------------------------------------------------------------- #
class LegacyStoredValueTests(SettingsCase):
    """Choice membership is a WRITE rule. A crew that stored a free-text
    command under the old policy keeps launching it verbatim until someone
    picks a new one: a read that failed (or silently reverted to the default)
    because the menu changed would swap a running crew's launch command out
    from under it, which is exactly the failure the store exists to prevent."""

    LEGACY = "claude --model opus"

    def setUp(self):
        super().setUp()
        self.write_store({"claude_launch_cmd": self.LEGACY})

    def test_the_legacy_value_still_reads_back_as_a_stored_override(self):
        self.assertEqual(settings.effective("claude_launch_cmd"),
                         (self.LEGACY, "settings"))
        self.assertEqual(settings.launch_cmd("claude"), self.LEGACY)

    def test_the_legacy_value_launches_verbatim(self):
        self.assertEqual(
            runtimes.launch_command("claude", "/tmp/crew-settings-test-home"),
            self.LEGACY)

    def test_describe_reports_it_as_stored_and_off_the_menu(self):
        row = {r["key"]: r for r in settings.describe()}["claude_launch_cmd"]
        self.assertEqual(row["override"], self.LEGACY)
        self.assertEqual(row["source"], "settings")
        self.assertEqual(row["effective"], self.LEGACY)
        # The UI has to render a current value that is not on the menu, so the
        # menu is reported unchanged rather than silently growing an entry.
        self.assertNotIn(self.LEGACY,
                         [c["command"] for c in row["choices"]])

    def test_rewriting_the_same_legacy_value_is_refused(self):
        with self.assertRaises(settings.SettingsError):
            settings.set_value("claude_launch_cmd", self.LEGACY)
        self.assertEqual(settings.effective("claude_launch_cmd")[0],
                         self.LEGACY)

    def test_picking_a_choice_replaces_it_and_clearing_drops_it(self):
        settings.set_value("claude_launch_cmd", "claude")
        self.assertEqual(settings.effective("claude_launch_cmd"),
                         ("claude", "settings"))
        self.assertTrue(settings.clear_value("claude_launch_cmd"))
        self.assertEqual(settings.effective("claude_launch_cmd"),
                         (config.CLAUDE_LAUNCH_CMD_DEFAULT, "default"))


# --------------------------------------------------------------------------- #
# precedence: env > stored > default, live on every read
# --------------------------------------------------------------------------- #
class EnvPrecedenceTests(SettingsCase):
    def test_an_env_override_beats_the_stored_value(self):
        settings.set_value("codex_launch_cmd", "codex")
        os.environ["CREW_CODEX_LAUNCH_CMD"] = "codex --from-env"
        self.assertEqual(settings.effective("codex_launch_cmd"),
                         ("codex --from-env", "env"))
        self.assertEqual(settings.launch_cmd("codex"), "codex --from-env")
        # The stored value is still reported — it is shadowed, not lost.
        row = {r["key"]: r for r in settings.describe()}["codex_launch_cmd"]
        self.assertEqual(row["override"], "codex")
        self.assertEqual(row["source"], "env")

    def test_an_env_override_does_not_have_to_be_a_choice(self):
        # The menu governs what an operator can STORE for the whole crew. A
        # per-process env var is the deliberate escape hatch for the one-off
        # experiment, so it is taken verbatim.
        os.environ["CREW_CLAUDE_LAUNCH_CMD"] = "claude --model opus"
        self.assertEqual(settings.effective("claude_launch_cmd"),
                         ("claude --model opus", "env"))
        self.assertEqual(settings.launch_cmd("claude"), "claude --model opus")

    def test_the_env_override_is_read_live(self):
        self.assertEqual(settings.effective("hermes_launch_cmd")[1], "default")
        os.environ["CREW_HERMES_LAUNCH_CMD"] = "hermes --live"
        self.assertEqual(settings.effective("hermes_launch_cmd"),
                         ("hermes --live", "env"))
        del os.environ["CREW_HERMES_LAUNCH_CMD"]
        self.assertEqual(settings.effective("hermes_launch_cmd")[1], "default")

    def test_the_legacy_env_name_covers_claude_only(self):
        os.environ["CREW_LAUNCH_CMD"] = "claude --legacy"
        self.assertEqual(settings.effective("claude_launch_cmd"),
                         ("claude --legacy", "env"))
        self.assertEqual(settings.effective("codex_launch_cmd")[1], "default")
        self.assertEqual(settings.effective("hermes_launch_cmd")[1], "default")

    def test_the_specific_env_name_wins_over_the_legacy_one(self):
        os.environ["CREW_LAUNCH_CMD"] = "claude --legacy"
        os.environ["CREW_CLAUDE_LAUNCH_CMD"] = "claude --specific"
        self.assertEqual(settings.effective("claude_launch_cmd"),
                         ("claude --specific", "env"))

    def test_an_empty_env_override_counts_as_unset(self):
        # Exporting an empty variable is how a shell "unsets" one by accident;
        # it must fall through instead of launching an empty command.
        settings.set_value("claude_launch_cmd", "claude")
        for blank in ("", "   "):
            with self.subTest(blank=repr(blank)):
                os.environ["CREW_CLAUDE_LAUNCH_CMD"] = blank
                self.assertEqual(settings.effective("claude_launch_cmd"),
                                 ("claude", "settings"))
        settings.clear_value("claude_launch_cmd")
        os.environ["CREW_CLAUDE_LAUNCH_CMD"] = ""
        self.assertEqual(settings.effective("claude_launch_cmd")[1], "default")

    def test_a_surrounding_env_value_is_stripped(self):
        os.environ["CREW_CODEX_LAUNCH_CMD"] = "  codex --padded  "
        self.assertEqual(settings.effective("codex_launch_cmd"),
                         ("codex --padded", "env"))


# --------------------------------------------------------------------------- #
# unknown keys
# --------------------------------------------------------------------------- #
class UnknownKeyTests(SettingsCase):
    def test_every_entry_point_refuses_an_unknown_key(self):
        for call in (lambda: settings.set_value("nope", "claude"),
                     lambda: settings.clear_value("nope"),
                     lambda: settings.effective("nope"),
                     lambda: settings.validated_value("nope", "claude"),
                     lambda: settings.launch_cmd("nope")):
            with self.assertRaises(settings.SettingsError) as ctx:
                call()
            self.assertIn("nope", str(ctx.exception))

    def test_a_runtime_with_no_launch_setting_is_refused(self):
        # "custom" is a real runtime whose command is stored per agent; there
        # is deliberately no crew-wide default for it.
        with self.assertRaises(settings.SettingsError) as ctx:
            settings.launch_cmd("custom")
        self.assertIn("custom", str(ctx.exception))


# --------------------------------------------------------------------------- #
# write-time validation — the store never holds an unusable command
# --------------------------------------------------------------------------- #
class ValidationTests(SettingsCase):
    KEY = "claude_launch_cmd"

    def assert_refused(self, value, fragment):
        with self.assertRaises(settings.SettingsError) as ctx:
            settings.set_value(self.KEY, value)
        message = str(ctx.exception)
        self.assertIn(self.KEY, message)
        self.assertIn(fragment, message)

    def test_a_non_string_is_refused(self):
        for value in (None, 5, ["claude"], {"cmd": "claude"}):
            with self.subTest(value=value):
                self.assert_refused(value, "must be a string")

    def test_an_empty_or_whitespace_value_is_refused(self):
        # Distinct from "off the menu": blank is how a UI reports "no pick",
        # and the fix is to clear the override, not to choose harder.
        for value in ("", "   ", "\t\n "):
            with self.subTest(value=repr(value)):
                self.assert_refused(value, "non-empty")

    def test_a_hand_typed_command_is_refused_however_well_formed(self):
        # Everything free-text validation used to accept or reject one rule at
        # a time — extra flags, a second line, an unbalanced quote, a value
        # past the length limit — is now the same answer: it is not a choice.
        for value in ("claude --model opus",
                      "claude --dangerously-skip-permissions\nrm -rf ~",
                      'claude --append "unterminated',
                      "claude " + "x" * settings.MAX_VALUE_LEN):
            with self.subTest(value=value[:40]):
                self.assert_refused(value, "claude --dangerously-skip-permissions")

    def test_a_refused_value_never_reaches_the_store(self):
        settings.set_value(self.KEY, "claude")
        with self.assertRaises(settings.SettingsError):
            settings.set_value(self.KEY, "claude 'broken")
        self.assertEqual(json.loads(self.read_store()), {self.KEY: "claude"})
        self.assertEqual(settings.effective(self.KEY)[0], "claude")

    def test_a_first_refused_value_leaves_no_store_behind(self):
        with self.assertRaises(settings.SettingsError):
            settings.set_value(self.KEY, "")
        self.assertFalse(os.path.exists(self.store))


# --------------------------------------------------------------------------- #
# corrupt durable state fails closed
# --------------------------------------------------------------------------- #
class CorruptStoreTests(SettingsCase):
    CORRUPTIONS = {
        "invalid json": "{not json at all",
        "truncated": '{"claude_launch_cmd": "clau',
        "json list": ["claude_launch_cmd", "claude"],
        "non-string value": {"claude_launch_cmd": 5},
        "unknown stored key": {"claude_launch_cmd": "claude", "shell": "sh"},
    }

    def test_every_read_fails_closed_instead_of_reverting_to_defaults(self):
        for label, payload in self.CORRUPTIONS.items():
            with self.subTest(store=label):
                self.write_store(payload)
                for name, call in (
                        ("effective",
                         lambda: settings.effective("claude_launch_cmd")),
                        ("describe", settings.describe),
                        ("launch_cmd",
                         lambda: settings.launch_cmd("claude"))):
                    with self.subTest(call=name):
                        with self.assertRaises(settings.SettingsError) as ctx:
                            call()
                        self.assertIn("corrupt", str(ctx.exception))

    def test_a_write_over_a_corrupt_store_is_refused_and_changes_nothing(self):
        for label, payload in self.CORRUPTIONS.items():
            with self.subTest(store=label):
                self.write_store(payload)
                before = self.read_store()
                for name, call in (
                        ("set_value",
                         lambda: settings.set_value("claude_launch_cmd",
                                                    "claude --new")),
                        ("clear_value",
                         lambda: settings.clear_value("claude_launch_cmd"))):
                    with self.subTest(call=name):
                        with self.assertRaises(settings.SettingsError):
                            call()
                # Repairing corruption is the operator's call, not a silent
                # overwrite of state they may still want to read.
                self.assertEqual(self.read_store(), before)

    def test_an_env_override_still_answers_over_a_corrupt_store(self):
        # effective() resolves the env first and never opens the file, so a
        # corrupt store cannot strand a process that carries its own launch
        # command. `crew settings list` still reports the corruption.
        self.write_store("{not json at all")
        os.environ["CREW_CLAUDE_LAUNCH_CMD"] = "claude --from-env"
        self.assertEqual(settings.effective("claude_launch_cmd"),
                         ("claude --from-env", "env"))
        with self.assertRaises(settings.SettingsError):
            settings.describe()


# --------------------------------------------------------------------------- #
# crew.runtime reads its launch commands through this store
# --------------------------------------------------------------------------- #
class RuntimeWiringTests(SettingsCase):
    HOME = "/tmp/crew-settings-test-home"

    def codex_environment(self, prefix):
        return {key: f"{prefix}-{key.lower()}"
                for key in runtimes.CODEX_CRITICAL_ENV}

    def test_claude_and_hermes_launch_with_the_stored_command_verbatim(self):
        settings.set_value("claude_launch_cmd",
                           "claude --dangerously-skip-permissions --continue")
        settings.set_value("hermes_launch_cmd", "hermes --yolo")
        self.assertEqual(
            runtimes.launch_command("claude", self.HOME),
            "claude --dangerously-skip-permissions --continue")
        self.assertEqual(runtimes.launch_command("hermes", self.HOME),
                         "hermes --yolo")

    def test_the_codex_command_is_the_stored_base_plus_generated_config(self):
        settings.set_value("codex_launch_cmd", "codex")
        command = runtimes.launch_command(
            "codex", self.HOME, environment={})
        self.assertTrue(command.startswith("codex -c "), command)
        self.assertIn(runtimes._codex_trust_config(self.HOME),
                      shlex.split(command))

    def test_an_explicit_override_beats_the_stored_command(self):
        # A per-agent launch command is stored on the agent, not in this
        # crew-wide menu, so it is free-text and stays that way.
        settings.set_value("claude_launch_cmd", "claude")
        self.assertEqual(
            runtimes.launch_command("claude", self.HOME,
                                    override="claude --per-agent"),
            "claude --per-agent")

    def test_revive_regenerates_a_generated_row_from_the_current_base(self):
        settings.set_value("codex_launch_cmd", "codex")
        stored = runtimes.launch_command(
            "codex", self.HOME, environment=self.codex_environment("stale"))
        current = self.codex_environment("current")

        refreshed = runtimes.revive_launch_command(
            "codex", self.HOME, stored, environment=current)

        self.assertNotEqual(refreshed, stored)
        self.assertTrue(refreshed.startswith("codex -c "))
        tokens = shlex.split(refreshed)
        for key, value in current.items():
            self.assertIn(
                f"shell_environment_policy.set.{key}=" + json.dumps(value),
                tokens)

    def test_revive_preserves_a_custom_command_byte_for_byte(self):
        settings.set_value("codex_launch_cmd", "codex")
        generated = runtimes.launch_command(
            "codex", self.HOME, environment=self.codex_environment("current"))
        custom = generated + " --model user-selected"
        self.assertEqual(
            runtimes.revive_launch_command(
                "codex", self.HOME, custom,
                environment=self.codex_environment("current")),
            custom)

    def test_a_row_generated_under_a_previous_base_is_left_alone(self):
        # Revive only recognizes a generated shape when the row still starts
        # with the CURRENT base, so changing the base setting does not rewrite
        # commands stored under the old one — they stay byte-for-byte until
        # the agent is respawned. Preserving an unrecognized command is the
        # safe half of the rule; see the note in the suite report.
        settings.set_value("codex_launch_cmd", "codex")
        stored = runtimes.launch_command(
            "codex", self.HOME, environment=self.codex_environment("stale"))
        settings.clear_value("codex_launch_cmd")

        revived = runtimes.revive_launch_command(
            "codex", self.HOME, stored,
            environment=self.codex_environment("current"))

        self.assertEqual(revived, stored)

    def test_revive_of_an_empty_row_uses_the_current_stored_command(self):
        settings.set_value("claude_launch_cmd", "claude")
        self.assertEqual(
            runtimes.revive_launch_command("claude", self.HOME, ""),
            "claude")


# --------------------------------------------------------------------------- #
# crew.guard — writing a launch command is human-only
# --------------------------------------------------------------------------- #
class GuardTests(SettingsCase):
    """A stored launch command runs verbatim in every future pane, so this is
    the same tier as `remove`/`bless`: not even the foreman flag covers it."""

    def agent_row(self, name="settings_writer", foreman=False):
        return {"_guid": f"{name}-guid", "name": name,
                "home": f"/tmp/crew_settingstest/{name}",
                "can_edit_graph": foreman}

    def refuse(self, agent):
        with mock.patch.object(gs, "get_agent_by_name", return_value=agent), \
                mock.patch.object(guard, "audit") as audit:
            with self.assertRaises(gs.GraphError) as ctx:
                guard.check(agent["name"], "settings_write",
                            name="claude_launch_cmd")
        return str(ctx.exception), audit

    def test_settings_write_is_a_human_only_op(self):
        self.assertIn("settings_write", guard.HUMAN_ONLY_OPS)

    def test_an_agent_actor_is_refused_and_told_what_to_ask_for(self):
        message, audit = self.refuse(self.agent_row())
        self.assertIn("human operator", message)
        self.assertIn("crew settings set", message)
        self.assertEqual(audit.call_args.args[1:4],
                         ("settings_write", mock.ANY, "refused"))

    def test_a_foreman_is_refused_too(self):
        message, _ = self.refuse(self.agent_row("settings_foreman",
                                                foreman=True))
        self.assertIn("crew settings set", message)

    def test_an_unregistered_actor_is_refused(self):
        with mock.patch.object(gs, "get_agent_by_name",
                               side_effect=gs.GraphError("no such agent")), \
                mock.patch.object(guard, "audit"):
            with self.assertRaises(gs.GraphError) as ctx:
                guard.check("ghost", "settings_write", name="codex_launch_cmd")
        self.assertIn("crew settings set", str(ctx.exception))

    def test_a_human_is_cleared_without_consulting_the_graph(self):
        with mock.patch.object(gs, "get_agent_by_name") as lookup:
            self.assertIsNone(
                guard.check("human", "settings_write",
                            name="claude_launch_cmd"))
        lookup.assert_not_called()


# --------------------------------------------------------------------------- #
# the `crew settings` CLI handlers
# --------------------------------------------------------------------------- #
class CliTests(SettingsCase):
    def run_handler(self, handler, actor="human", **fields):
        out = io.StringIO()
        with mock.patch.object(cli, "_ACTOR", actor), \
                contextlib.redirect_stdout(out):
            code = handler(argparse.Namespace(**fields))
        return code, out.getvalue()

    def test_list_json_carries_the_choices_of_every_setting(self):
        settings.set_value("codex_launch_cmd", "codex")
        code, out = self.run_handler(cli.cmd_settings_list, json=True)
        self.assertEqual(code, 0)
        rows = json.loads(out)
        self.assertEqual(rows, settings.describe())
        self.assertEqual(
            {row["key"]: [c["command"] for c in row["choices"]]
             for row in rows},
            {key: [command for _, command in choices]
             for key, choices in EXPECTED_CHOICES.items()})

    def test_plain_list_output_stays_one_line_per_setting(self):
        # The menu belongs to `--json` and the dashboard; the human-readable
        # listing is unchanged by this rework.
        settings.set_value("claude_launch_cmd", "claude")
        code, out = self.run_handler(cli.cmd_settings_list, json=False)
        self.assertEqual(code, 0)
        self.assertEqual(len(out.strip().splitlines()), len(settings.KEYS))
        self.assertIn("claude_launch_cmd (settings): claude", out)
        self.assertIn("codex_launch_cmd (default): "
                      + config.CODEX_LAUNCH_CMD_DEFAULT, out)

    def test_list_names_the_stored_value_an_env_override_shadows(self):
        settings.set_value("claude_launch_cmd", "claude")
        os.environ["CREW_CLAUDE_LAUNCH_CMD"] = "claude --from-env"
        code, out = self.run_handler(cli.cmd_settings_list, json=False)
        self.assertEqual(code, 0)
        self.assertIn("claude_launch_cmd (env): claude --from-env", out)
        self.assertIn("stored (inactive while the env override is set): "
                      "claude", out)

    def test_set_list_clear_round_trip(self):
        code, out = self.run_handler(cli.cmd_settings_set,
                                     key="hermes_launch_cmd",
                                     value="  hermes --yolo  ")
        self.assertEqual(code, 0)
        self.assertIn("set hermes_launch_cmd = hermes --yolo", out)
        self.assertNotIn("note:", out)

        code, out = self.run_handler(cli.cmd_settings_list, json=False)
        self.assertEqual(code, 0)
        self.assertIn("hermes_launch_cmd (settings): hermes --yolo", out)

        code, out = self.run_handler(cli.cmd_settings_clear,
                                     key="hermes_launch_cmd")
        self.assertEqual(code, 0)
        self.assertIn(f"back to: {config.HERMES_LAUNCH_CMD_DEFAULT}", out)

        code, out = self.run_handler(cli.cmd_settings_list, json=False)
        self.assertIn("hermes_launch_cmd (default): "
                      + config.HERMES_LAUNCH_CMD_DEFAULT, out)

    def test_clear_reports_when_nothing_was_stored(self):
        code, out = self.run_handler(cli.cmd_settings_clear,
                                     key="codex_launch_cmd")
        self.assertEqual(code, 0)
        self.assertIn("had no stored override", out)

    def test_set_warns_that_an_env_override_still_wins(self):
        os.environ["CREW_CODEX_LAUNCH_CMD"] = "codex --from-env"
        code, out = self.run_handler(cli.cmd_settings_set,
                                     key="codex_launch_cmd",
                                     value="codex")
        self.assertEqual(code, 0)
        self.assertIn("set codex_launch_cmd = codex", out)
        self.assertIn("note: an env override is active", out)
        self.assertIn("codex --from-env", out)

    def test_a_value_off_the_menu_is_refused_and_the_menu_is_printed(self):
        # `crew settings set` is how an operator discovers the menu when they
        # guess wrong, so the error has to carry every command they can pick.
        with self.assertRaises(settings.SettingsError) as ctx:
            self.run_handler(cli.cmd_settings_set, key="claude_launch_cmd",
                             value="claude --model opus")
        message = str(ctx.exception)
        for _, command in EXPECTED_CHOICES["claude_launch_cmd"]:
            self.assertIn(command, message)
        self.assertFalse(os.path.exists(self.store))

    def test_the_set_parser_points_at_the_choices(self):
        parser = cli.build_parser()
        with contextlib.redirect_stdout(io.StringIO()) as out:
            with self.assertRaises(SystemExit):
                parser.parse_args(["settings", "set", "--help"])
        self.assertIn("choices", out.getvalue())

    def test_an_agent_actor_cannot_write_settings_through_the_cli(self):
        agent = {"_guid": "cli-writer-guid", "name": "cli_writer",
                 "home": "/tmp/crew_settingstest/cli_writer",
                 "can_edit_graph": True}
        for handler, fields in ((cli.cmd_settings_set,
                                 {"key": "claude_launch_cmd",
                                  "value": "claude --agent-chosen"}),
                                (cli.cmd_settings_clear,
                                 {"key": "claude_launch_cmd"})):
            with self.subTest(handler=handler.__name__):
                with mock.patch.object(gs, "get_agent_by_name",
                                       return_value=agent), \
                        mock.patch.object(guard, "audit"):
                    with self.assertRaises(gs.GraphError) as ctx:
                        self.run_handler(handler, actor="cli_writer", **fields)
                self.assertIn("crew settings set", str(ctx.exception))
        # The gate runs before the write, so nothing was persisted.
        self.assertFalse(os.path.exists(self.store))


if __name__ == "__main__":
    unittest.main()
