"""Unit tests for WAVE 0 — projects (app-key-per-project) + standard home layout.

Pure logic over crew.config's project helpers and crew.spawn._plan_home's
env-driven default branch. No live server, no tmux, no filesystem side effects
beyond a tempdir used as a stand-in for VAR/crew_root() in the tests that need
one — never touches the real var/projects.json or the real crew_root().

Live checks (crew project create / crew --project X spawn-agent ...) live in
tests/live_smoke.py, per SKILL.md (schema-drift can only be caught live).

    python3 -m unittest tests.test_projects   (from repo root)
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from crew import config, spawn  # noqa: E402


def _env(overrides, drop=()):
    """A full os.environ copy with `overrides` applied and `drop` keys removed —
    for use with mock.patch.dict(..., clear=True) so a test's env is fully
    deterministic regardless of what's actually exported in the shell."""
    e = dict(os.environ)
    for k in drop:
        e.pop(k, None)
    e.update(overrides)
    return e


# --------------------------------------------------------------------------- #
# config.project_app / current_project / current_app precedence
# --------------------------------------------------------------------------- #
class ProjectAppMappingTests(unittest.TestCase):
    def test_default_project_maps_to_default_app(self):
        self.assertEqual(config.project_app(config.DEFAULT_PROJECT), "crew")

    def test_named_project_maps_to_prefixed_app(self):
        self.assertEqual(config.project_app("demo"), "crew-demo")


class CurrentProjectTests(unittest.TestCase):
    def test_reads_env_live(self):
        with mock.patch.dict(os.environ, _env({"CREW_PROJECT": "demo"}), clear=True):
            self.assertEqual(config.current_project(), "demo")

    def test_defaults_when_unset(self):
        with mock.patch.dict(os.environ, _env({}, drop=("CREW_PROJECT",)), clear=True):
            self.assertEqual(config.current_project(), config.DEFAULT_PROJECT)


class CurrentAppPrecedenceTests(unittest.TestCase):
    def test_follows_project_when_no_override(self):
        env = _env({"CREW_PROJECT": "demo"}, drop=("CREW_APP",))
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(config.current_app(), "crew-demo")

    def test_crew_app_override_wins_over_project(self):
        env = _env({"CREW_PROJECT": "demo", "CREW_APP": "some-pinned-app"})
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(config.current_app(), "some-pinned-app")

    def test_defaults_to_crew_with_nothing_set(self):
        env = _env({}, drop=("CREW_APP", "CREW_PROJECT"))
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(config.current_app(), "crew")

    def test_blank_crew_app_falls_back_to_project(self):
        # mirrors the existing "blank env var means unset" rule current_app()
        # already applies to CREW_APP.
        env = _env({"CREW_PROJECT": "demo", "CREW_APP": "  "})
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(config.current_app(), "crew-demo")


# --------------------------------------------------------------------------- #
# valid_project_name
# --------------------------------------------------------------------------- #
class ValidProjectNameTests(unittest.TestCase):
    def test_accepts_simple_slugs(self):
        self.assertTrue(config.valid_project_name("demo"))
        self.assertTrue(config.valid_project_name("demo-2"))
        self.assertTrue(config.valid_project_name("demo_2"))
        self.assertTrue(config.valid_project_name("a" * 32))

    def test_rejects_over_32_chars(self):
        self.assertFalse(config.valid_project_name("a" * 33))

    def test_rejects_bad_chars_and_leading_dash(self):
        self.assertFalse(config.valid_project_name("-demo"))
        self.assertFalse(config.valid_project_name("de mo"))
        self.assertFalse(config.valid_project_name("de.mo"))
        self.assertFalse(config.valid_project_name("de/mo"))

    def test_rejects_empty_or_non_string(self):
        self.assertFalse(config.valid_project_name(""))
        self.assertFalse(config.valid_project_name(None))
        self.assertFalse(config.valid_project_name(123))


# --------------------------------------------------------------------------- #
# session_name
# --------------------------------------------------------------------------- #
class SessionNameTests(unittest.TestCase):
    def test_default_project_session_is_plain_name(self):
        self.assertEqual(config.session_name(config.DEFAULT_PROJECT, "foo"), "foo")

    def test_named_project_session_is_prefixed(self):
        self.assertEqual(config.session_name("demo", "foo"), "demo__foo")


# --------------------------------------------------------------------------- #
# project registry (var/projects.json) — against a throwaway VAR dir
# --------------------------------------------------------------------------- #
class ProjectRegistryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._patch = mock.patch.object(config, "VAR", self._tmp.name)
        self._patch.start()
        self.addCleanup(self._patch.stop)
        self.addCleanup(self._tmp.cleanup)

    def test_default_always_present_even_with_no_file(self):
        self.assertIn(config.DEFAULT_PROJECT, config.list_known_projects())

    def test_tolerates_missing_file(self):
        self.assertEqual(config.list_known_projects(), [config.DEFAULT_PROJECT])

    def test_register_then_list(self):
        config.register_project("demo")
        names = config.list_known_projects()
        self.assertIn("demo", names)
        self.assertIn(config.DEFAULT_PROJECT, names)

    def test_register_is_idempotent(self):
        config.register_project("demo")
        config.register_project("demo")
        names = config.list_known_projects()
        self.assertEqual(names.count("demo"), 1)

    def test_tolerates_corrupt_file(self):
        with open(os.path.join(self._tmp.name, "projects.json"), "w") as f:
            f.write("not json{{{")
        self.assertEqual(config.list_known_projects(), [config.DEFAULT_PROJECT])


# --------------------------------------------------------------------------- #
# spawn._plan_home default branch: crew_root()/<project>/<name>
# --------------------------------------------------------------------------- #
class PlanHomeDefaultTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def test_default_home_under_crew_root_and_project(self):
        env = _env({"CREW_ROOT": self._tmp.name, "CREW_PROJECT": "demo"})
        with mock.patch.dict(os.environ, env, clear=True):
            home, worktree, plan = spawn._plan_home("myagent")
        expected = os.path.realpath(os.path.join(self._tmp.name, "demo", "myagent"))
        self.assertEqual(home, expected)
        self.assertIsNone(worktree)
        self.assertEqual(plan, ("mkdir",))

    def test_default_home_under_default_project_when_unset(self):
        env = _env({"CREW_ROOT": self._tmp.name}, drop=("CREW_PROJECT",))
        with mock.patch.dict(os.environ, env, clear=True):
            home, _, _ = spawn._plan_home("myagent")
        expected = os.path.realpath(
            os.path.join(self._tmp.name, config.DEFAULT_PROJECT, "myagent"))
        self.assertEqual(home, expected)

    def test_explicit_home_still_wins_over_project_layout(self):
        env = _env({"CREW_ROOT": self._tmp.name, "CREW_PROJECT": "demo"})
        explicit = os.path.join(self._tmp.name, "elsewhere")
        with mock.patch.dict(os.environ, env, clear=True):
            home, _, _ = spawn._plan_home("myagent", home=explicit)
        self.assertEqual(home, os.path.realpath(explicit))

    def test_no_filesystem_side_effects(self):
        """_plan_home only computes a path — it must not create anything."""
        env = _env({"CREW_ROOT": self._tmp.name, "CREW_PROJECT": "demo"})
        with mock.patch.dict(os.environ, env, clear=True):
            home, _, _ = spawn._plan_home("myagent")
        self.assertFalse(os.path.exists(home))


if __name__ == "__main__":
    unittest.main()
