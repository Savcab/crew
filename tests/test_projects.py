"""Unit tests for WAVE 0 — projects (app-key-per-project) + standard home layout.

Pure logic over crew.config's project helpers and crew.spawn._plan_home's
env-driven default branch. No live server, no tmux, no filesystem side effects
beyond a tempdir used as a stand-in for VAR/crew_root() in the tests that need
one — never touches the real var/projects.json or the real crew_root().

Live checks (crew project create / crew --project X spawn-agent ...) live in
tests/live_smoke.py, per SKILL.md (schema-drift can only be caught live).

    python3 -m unittest tests.test_projects   (from repo root)
"""
import argparse
import contextlib
import importlib.util
import io
import multiprocessing
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from crew import cli, config, schema, spawn  # noqa: E402
# Importing the dashboard module binds no socket and starts no thread — the
# foreman-seed tests below call _project_create in-process with every side
# effect (schema, registry, seed subprocess) mocked out.
from crew.server import app as dashboard_app  # noqa: E402


def _register_project_worker(var_dir, runtime_root, name, barrier, results):
    """Force the old registry's read/modify/write window across processes."""
    config.VAR = var_dir
    config.RUNTIME_STATE_ROOT = runtime_root
    original_list = config.list_known_projects

    def synchronized_list():
        names = original_list()
        barrier.wait(timeout=10)
        return names

    # The fixed implementation reads its registry inside the flock rather than
    # calling this public wrapper, so the injected barrier disappears under
    # green instead of deadlocking behind the lock.
    config.list_known_projects = synchronized_list
    try:
        config.register_project(name)
        results.put((name, "ok", ""))
    except Exception as error:
        results.put((name, "error", f"{type(error).__name__}: {error}"))


def _load_live_smoke_module():
    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "tests", "live_smoke.py")
    spec = importlib.util.spec_from_file_location("crew_live_smoke_cleanup_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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

    def test_invalid_env_selector_is_rejected_before_derivation(self):
        with mock.patch.dict(os.environ, _env({"CREW_PROJECT": "../../escape"}),
                             clear=True):
            with self.assertRaisesRegex(ValueError, "invalid project"):
                config.current_project()

    def test_project_app_and_session_name_reject_invalid_direct_values(self):
        for value in ("../escape", "has space", "-leading", "a" * 33):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "invalid project"):
                    config.project_app(value)
                with self.assertRaisesRegex(ValueError, "invalid project"):
                    config.session_name(value, "worker")


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


class CrossAppHomeConflictTests(unittest.TestCase):
    def test_scans_current_and_every_registered_project_app_explicitly(self):
        agents = {
            "crew": [],
            "crew-alpha": [],
            "crew-beta": [{"name": "owner", "home": "/tmp/shared"}],
            "crew-custom": [],
        }

        def listed(*, app=None):
            return agents[app]

        with mock.patch.object(config, "current_app", return_value="crew-custom"), \
             mock.patch.object(
                 config, "list_known_projects",
                 return_value=["default", "alpha", "beta"]), \
             mock.patch.object(spawn.gs, "list_agents", side_effect=listed) as reads:
            conflict = spawn.gs.home_conflict_across_apps("/tmp/shared/nested")

        self.assertEqual(conflict["name"], "owner")
        self.assertEqual(conflict["_app"], "crew-beta")
        self.assertEqual(
            {call.kwargs["app"] for call in reads.call_args_list},
            {"crew", "crew-alpha", "crew-beta", "crew-custom"})

    def test_corrupt_project_registry_blocks_home_claim_with_graph_error(self):
        with mock.patch.object(config, "current_app", return_value="crew"), \
             mock.patch.object(
                 config, "list_known_projects",
                 side_effect=ValueError("project registry is corrupt")):
            with self.assertRaisesRegex(
                    spawn.gs.GraphError, "project registry is corrupt"):
                spawn.gs.home_conflict_across_apps("/tmp/proposed-home")


class HomeCanonicalizationTests(unittest.TestCase):
    def test_case_insensitive_filesystems_collapse_unicode_aliases(self):
        """APFS treats NFC and NFD spellings as one physical directory."""
        composed = "/tmp/crew-canonical/\N{LATIN SMALL LETTER E WITH ACUTE}"
        decomposed = "/tmp/crew-canonical/e\N{COMBINING ACUTE ACCENT}"
        with mock.patch.object(spawn.gs, "_CASE_INSENSITIVE_FS", True):
            self.assertEqual(
                spawn.gs.normalize_home(composed),
                spawn.gs.normalize_home(decomposed),
            )
            owner = {"name": "owner", "home": composed}
            self.assertIsNotNone(
                spawn.gs.home_conflict(decomposed, agents=[owner]))

    def test_case_sensitive_filesystems_keep_unicode_spellings_distinct(self):
        """Linux filesystems commonly permit NFC and NFD as separate names."""
        composed = "/tmp/crew-canonical/\N{LATIN SMALL LETTER E WITH ACUTE}"
        decomposed = "/tmp/crew-canonical/e\N{COMBINING ACUTE ACCENT}"
        with mock.patch.object(spawn.gs, "_CASE_INSENSITIVE_FS", False):
            self.assertNotEqual(
                spawn.gs.normalize_home(composed),
                spawn.gs.normalize_home(decomposed),
            )

    @unittest.skipUnless(sys.platform == "darwin", "requires macOS path aliases")
    def test_real_macos_unicode_alias_is_detected_as_the_same_home(self):
        with tempfile.TemporaryDirectory(prefix="crew-home-alias-") as root:
            composed = os.path.join(
                root, "\N{LATIN SMALL LETTER E WITH ACUTE}")
            decomposed = os.path.join(
                root, "e\N{COMBINING ACUTE ACCENT}")
            os.mkdir(composed)
            self.assertTrue(os.path.exists(decomposed),
                            "fixture volume is not Unicode-normalizing")
            owner = {"name": "owner", "home": composed}
            self.assertIsNotNone(
                spawn.gs.home_conflict(decomposed, agents=[owner]))


# --------------------------------------------------------------------------- #
# project registry (var/projects.json) — against a throwaway VAR dir
# --------------------------------------------------------------------------- #
class ProjectRegistryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._runtime_tmp = tempfile.TemporaryDirectory()
        self._patch = mock.patch.object(config, "VAR", self._tmp.name)
        self._runtime_patch = mock.patch.object(
            config, "RUNTIME_STATE_ROOT", self._runtime_tmp.name)
        self._patch.start()
        self._runtime_patch.start()
        self.addCleanup(self._runtime_patch.stop)
        self.addCleanup(self._patch.stop)
        self.addCleanup(self._runtime_tmp.cleanup)
        self.addCleanup(self._tmp.cleanup)

    def test_default_always_present_even_with_no_file(self):
        self.assertIn(config.DEFAULT_PROJECT, config.list_known_projects())

    def test_tolerates_missing_file(self):
        self.assertEqual(config.list_known_projects(), [config.DEFAULT_PROJECT])

    def test_read_does_not_require_creating_an_unwritable_var_directory(self):
        with tempfile.TemporaryDirectory() as parent:
            missing_var = os.path.join(parent, "repo-var-does-not-exist")
            os.chmod(parent, 0o500)
            try:
                with mock.patch.object(config, "VAR", missing_var):
                    self.assertEqual(
                        config.list_known_projects(),
                        [config.DEFAULT_PROJECT])
            finally:
                os.chmod(parent, 0o700)
        self.assertFalse(os.path.exists(missing_var))

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

    def test_registry_lock_uses_private_runtime_state_not_repo_var(self):
        config.register_project("demo")

        lock_path = os.path.join(
            self._runtime_tmp.name, "project-registry-locks",
            "projects.json.lock")
        self.assertTrue(os.path.isfile(lock_path), lock_path)
        self.assertEqual(os.stat(lock_path).st_mode & 0o777, 0o600)
        self.assertFalse(os.path.lexists(
            os.path.join(self._tmp.name, "projects.json.lock")))

    def test_registry_lock_refuses_a_symlink(self):
        directory = config.runtime_state_dir("project-registry-locks")
        target = os.path.join(self._runtime_tmp.name, "outside-target")
        with open(target, "w") as stream:
            stream.write("do not touch")
        os.symlink(target, os.path.join(directory, "projects.json.lock"))

        with self.assertRaisesRegex(
                config.ProjectRegistryError, "project registry lock"):
            config.list_known_projects()
        with open(target) as stream:
            self.assertEqual(stream.read(), "do not touch")

    def test_registry_lock_tightens_an_existing_file_mode(self):
        directory = config.runtime_state_dir("project-registry-locks")
        lock_path = os.path.join(directory, "projects.json.lock")
        with open(lock_path, "w"):
            pass
        os.chmod(lock_path, 0o666)

        config.list_known_projects()

        self.assertEqual(os.stat(lock_path).st_mode & 0o777, 0o600)

    def test_registry_lock_wraps_an_unsafe_runtime_directory(self):
        with mock.patch.object(
                config, "runtime_state_dir",
                side_effect=OSError("unsafe runtime fixture")):
            with self.assertRaisesRegex(
                    config.ProjectRegistryError,
                    "project registry lock.*unsafe runtime fixture"):
                config.list_known_projects()

    def test_corrupt_file_fails_closed_with_an_explicit_error(self):
        with open(os.path.join(self._tmp.name, "projects.json"), "w") as f:
            f.write("not json{{{")
        with self.assertRaisesRegex(ValueError, "project registry|corrupt"):
            config.list_known_projects()

    def test_project_list_reports_corrupt_registry_without_a_traceback(self):
        with open(os.path.join(self._tmp.name, "projects.json"), "w") as f:
            f.write("not json{{{")
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cli.mail, "whoami", return_value="human"), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            result = cli.main(["project", "list"])
        self.assertEqual(result, 1)
        self.assertIn("project registry", err.getvalue().lower())
        self.assertNotIn("traceback", err.getvalue().lower())

    def test_concurrent_registrations_preserve_both_projects(self):
        ctx = multiprocessing.get_context("spawn")
        barrier = ctx.Barrier(2)
        results = ctx.Queue()
        processes = [
            ctx.Process(
                target=_register_project_worker,
                args=(self._tmp.name, self._runtime_tmp.name,
                      name, barrier, results),
            )
            for name in ("alpha", "beta")
        ]
        try:
            for process in processes:
                process.start()
            for process in processes:
                process.join(timeout=15)
            self.assertTrue(
                all(not process.is_alive() for process in processes),
                "project registry worker hung",
            )
            self.assertTrue(
                all(process.exitcode == 0 for process in processes),
                [process.exitcode for process in processes],
            )
            outcomes = [results.get(timeout=3) for _ in processes]
            self.assertEqual(
                [status for _, status, _ in outcomes].count("ok"), 2,
                outcomes,
            )
            names = config.list_known_projects()
            self.assertIn("alpha", names)
            self.assertIn("beta", names)
            leftovers = [
                name for name in os.listdir(self._tmp.name)
                if name.endswith(".tmp")
            ]
            self.assertEqual(leftovers, [])
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=3)
            results.close()

    def test_unregister_is_idempotent_and_preserves_other_projects(self):
        config.register_project("alpha")
        config.register_project("beta")
        config.unregister_project("alpha")
        config.unregister_project("alpha")
        self.assertEqual(
            config.list_known_projects(),
            [config.DEFAULT_PROJECT, "beta"],
        )


class LiveSmokeProjectCleanupTests(unittest.TestCase):
    def test_wave0_cleanup_unregisters_project_after_app_is_deleted(self):
        smoke = _load_live_smoke_module()
        with mock.patch.object(
                 smoke, "_run", return_value=(1, "", "no such agent")), \
             mock.patch.object(smoke, "_tmux_has_session", return_value=False), \
             mock.patch.object(smoke.gs, "_req", return_value=None), \
             mock.patch.object(
                 smoke.config, "unregister_project") as unregister, \
             mock.patch.object(smoke.shutil, "rmtree"):
            smoke._wave0_cleanup()
        unregister.assert_called_once_with(smoke.W0_PROJECT)

    def test_wave0_cleanup_keeps_registry_when_app_deletion_is_unverified(self):
        smoke = _load_live_smoke_module()
        with mock.patch.object(
                 smoke, "_run", return_value=(1, "", "no such agent")), \
             mock.patch.object(smoke, "_tmux_has_session", return_value=False), \
             mock.patch.object(
                 smoke.gs, "_req",
                 side_effect=smoke.gs.GraphError("backend unavailable")), \
             mock.patch.object(
                 smoke.config, "unregister_project") as unregister, \
             mock.patch.object(smoke.shutil, "rmtree"):
            smoke._wave0_cleanup()
        unregister.assert_not_called()


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

    def test_invalid_project_cannot_escape_crew_root(self):
        env = _env({"CREW_ROOT": self._tmp.name,
                    "CREW_PROJECT": "../../escape"})
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaisesRegex(ValueError, "invalid project"):
                spawn._plan_home("myagent")
        self.assertEqual(os.listdir(self._tmp.name), [])

    def test_named_projects_get_distinct_worktree_names(self):
        """The same agent slug in two projects must never adopt one shared
        `<repo>-worktrees/<name>` directory."""
        repo = os.path.join(self._tmp.name, "repo")
        with mock.patch.object(
                spawn, "_worktree_paths",
                return_value=(os.path.join(self._tmp.name, "demo__myagent"), "main")) as paths, \
             mock.patch.dict(os.environ, {"CREW_PROJECT": "demo"}):
            home, worktree, plan = spawn._plan_home("myagent", repo=repo)
        paths.assert_called_once_with(os.path.abspath(repo), "demo__myagent")
        self.assertEqual(worktree, "demo__myagent")
        self.assertEqual(plan, ("worktree", os.path.abspath(repo), "main",
                                "crew/demo/myagent"))

    def test_default_project_keeps_legacy_worktree_name(self):
        repo = os.path.join(self._tmp.name, "repo")
        with mock.patch.object(
                spawn, "_worktree_paths",
                return_value=(os.path.join(self._tmp.name, "myagent"), "main")) as paths, \
             mock.patch.dict(os.environ, {"CREW_PROJECT": "default"}):
            _, worktree, _ = spawn._plan_home("myagent", repo=repo)
        paths.assert_called_once_with(os.path.abspath(repo), "myagent")
        self.assertEqual(worktree, "myagent")

    def test_materialized_worktree_has_a_named_branch_not_detached_head(self):
        repo = self._fixture_repo("repo")
        worktree = os.path.join(self._tmp.name, "repo-worktrees", "demo__writer")
        spawn._create_worktree(repo, worktree, "main", "crew/demo/writer")

        branch = subprocess.run(
            ["git", "-C", worktree, "branch", "--show-current"],
            check=True, capture_output=True, text=True).stdout.strip()
        self.assertEqual(branch, "crew/demo/writer")

    def test_existing_plain_directory_is_not_adopted_as_a_worktree(self):
        repo = self._fixture_repo("plain-repo")
        worktree = os.path.join(
            self._tmp.name, "plain-repo-worktrees", "demo__writer")
        os.makedirs(worktree)
        sentinel = os.path.join(worktree, "user-file.txt")
        with open(sentinel, "w") as fh:
            fh.write("preserve me\n")

        with self.assertRaisesRegex(
                spawn.gs.GraphError, "worktree|existing path|registered"):
            spawn._create_worktree(
                repo, worktree, "main", "crew/demo/writer")
        with open(sentinel) as fh:
            self.assertEqual(fh.read(), "preserve me\n")

    @unittest.skipUnless(sys.platform == "darwin", "requires case-insensitive FS")
    def test_case_alias_of_plain_directory_is_not_adopted_as_a_worktree(self):
        repo = self._fixture_repo("case-repo")
        actual = os.path.join(
            self._tmp.name, "case-repo-worktrees", "Demo__Writer")
        os.makedirs(actual)
        alias = os.path.join(
            self._tmp.name, "case-repo-worktrees", "demo__writer")
        self.assertTrue(os.path.exists(alias),
                        "fixture volume is not case-insensitive")

        with self.assertRaisesRegex(
                spawn.gs.GraphError, "worktree|existing path|registered"):
            spawn._create_worktree(
                repo, alias, "main", "crew/demo/writer")

    def test_existing_worktree_must_match_the_expected_branch(self):
        repo = self._fixture_repo("branch-repo")
        worktree = os.path.join(
            self._tmp.name, "branch-repo-worktrees", "demo__writer")
        spawn._create_worktree(
            repo, worktree, "main", "crew/demo/writer")
        # A verified existing worktree on the expected branch is idempotent.
        spawn._create_worktree(
            repo, worktree, "main", "crew/demo/writer")
        subprocess.run(
            ["git", "-C", worktree, "switch", "-c", "other-branch"],
            check=True, capture_output=True, text=True)

        with self.assertRaisesRegex(spawn.gs.GraphError, "branch"):
            spawn._create_worktree(
                repo, worktree, "main", "crew/demo/writer")

    def _fixture_repo(self, name):
        repo = os.path.join(self._tmp.name, name)
        subprocess.run(["git", "init", "-b", "main", repo], check=True,
                       capture_output=True, text=True)
        subprocess.run(["git", "-C", repo, "config", "user.email",
                        "crew@test.invalid"], check=True)
        subprocess.run(["git", "-C", repo, "config", "user.name", "Crew Test"],
                       check=True)
        readme = os.path.join(repo, "README.md")
        with open(readme, "w") as fh:
            fh.write("fixture\n")
        subprocess.run(["git", "-C", repo, "add", "README.md"], check=True)
        subprocess.run(["git", "-C", repo, "commit", "-m", "fixture"],
                       check=True, capture_output=True, text=True)
        return repo


class ProjectDescriptionTests(unittest.TestCase):
    """Registry metadata for the apps gallery: each project (graph) carries a
    human description; legacy plain-name registries keep reading."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._runtime_tmp = tempfile.TemporaryDirectory()
        self._patch = mock.patch.object(config, "VAR", self._tmp.name)
        self._runtime_patch = mock.patch.object(
            config, "RUNTIME_STATE_ROOT", self._runtime_tmp.name)
        self._patch.start()
        self._runtime_patch.start()
        self.addCleanup(self._runtime_patch.stop)
        self.addCleanup(self._patch.stop)
        self.addCleanup(self._runtime_tmp.cleanup)
        self.addCleanup(self._tmp.cleanup)

    def test_register_with_description_and_roundtrip(self):
        config.register_project("demo", description="lead-gen crew")
        self.assertIn("demo", config.list_known_projects())
        self.assertEqual(config.project_descriptions()["demo"], "lead-gen crew")

    def test_legacy_plain_name_registry_still_reads(self):
        os.makedirs(config.VAR, exist_ok=True)
        with open(os.path.join(config.VAR, "projects.json"), "w") as f:
            f.write('["legacyone", "legacytwo"]')
        self.assertEqual(
            config.list_known_projects(),
            [config.DEFAULT_PROJECT, "legacyone", "legacytwo"])
        descriptions = config.project_descriptions()
        self.assertEqual(descriptions.get("legacyone", ""), "")

    def test_set_description_updates_and_covers_default(self):
        config.register_project("meta1", description="first")
        config.set_project_description("meta1", "second")
        self.assertEqual(config.project_descriptions()["meta1"], "second")
        config.set_project_description(config.DEFAULT_PROJECT, "the home graph")
        self.assertEqual(
            config.project_descriptions()[config.DEFAULT_PROJECT],
            "the home graph")
        # default never leaks into the named-project list
        self.assertEqual(
            config.list_known_projects().count(config.DEFAULT_PROJECT), 1)

    def test_corrupt_dict_entries_fail_closed(self):
        os.makedirs(config.VAR, exist_ok=True)
        with open(os.path.join(config.VAR, "projects.json"), "w") as f:
            f.write('[{"description": "no name key"}]')
        with self.assertRaises(config.ProjectRegistryError):
            config.list_known_projects()


# --------------------------------------------------------------------------- #
# spawn.seed_foreman — the one seeding path `crew project create` and the
# dashboard gallery share. The child `bin/crew` process is ALWAYS mocked: these
# tests assert the argv/env contract and the (ok, detail) outcomes, never boot
# a runtime, and never touch a real graph.
# --------------------------------------------------------------------------- #
def _completed(returncode, stdout="", stderr=""):
    return subprocess.CompletedProcess(
        args=["bin/crew"], returncode=returncode, stdout=stdout, stderr=stderr)


class ForemanSeedCommandTests(unittest.TestCase):
    """What seed_foreman actually asks the child process to do."""

    def test_argv_is_the_documented_spawn_agent_command(self):
        with mock.patch.object(
                subprocess, "run", return_value=_completed(0)) as run:
            self.assertEqual(spawn.seed_foreman("demo"), (True, ""))
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0], [
            os.path.join(config.ROOT, "bin", "crew"), "--project", "demo",
            "spawn-agent", "foreman", "--foreman",
            "--role", spawn.FOREMAN_SEED_ROLE,
            "--identity", spawn.FOREMAN_SEED_IDENTITY,
        ])
        self.assertEqual(run.call_args.kwargs["cwd"], config.ROOT)

    def test_no_launch_appends_the_flag_and_changes_nothing_else(self):
        with mock.patch.object(
                subprocess, "run", return_value=_completed(0)) as launched:
            spawn.seed_foreman("demo")
        with mock.patch.object(
                subprocess, "run", return_value=_completed(0)) as quiet:
            spawn.seed_foreman("demo", launch=False)
        self.assertEqual(quiet.call_args.args[0],
                         launched.call_args.args[0] + ["--no-launch"])

    def test_child_env_drops_this_process_app_port_and_capability_pins(self):
        """A seed must never hand the new graph this dashboard's app key, port,
        or capability token — the child derives its own from --project."""
        env = _env({"CREW_APP": "crew-parent", "CREW_PORT": "8788",
                    "CREW_DASHBOARD_CAPABILITY": "parent-secret",
                    "CREW_PROJECT": "parent"})
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch.object(
                 subprocess, "run", return_value=_completed(0)) as run:
            spawn.seed_foreman("demo")
            # The parent's own environment is copied, never mutated.
            self.assertEqual(os.environ["CREW_APP"], "crew-parent")
        child_env = run.call_args.kwargs["env"]
        for key in ("CREW_APP", "CREW_PORT", "CREW_DASHBOARD_CAPABILITY"):
            self.assertNotIn(key, child_env)
        # Only those three pins are dropped; CREW_PROJECT survives because the
        # child's own `--project demo` argument overrides it.
        self.assertEqual(child_env.get("CREW_PROJECT"), "parent")

    def test_timeout_is_forwarded_to_the_child(self):
        with mock.patch.object(
                subprocess, "run", return_value=_completed(0)) as run:
            spawn.seed_foreman("demo", timeout=7)
        self.assertEqual(run.call_args.kwargs["timeout"], 7)


class ForemanSeedOutcomeTests(unittest.TestCase):
    """(ok, detail) for every way the child can end. seed_foreman never raises
    — the graph already exists and the caller reports the outcome honestly."""

    def _seed(self, **run_kwargs):
        with mock.patch.object(subprocess, "run", **run_kwargs):
            return spawn.seed_foreman("demo")

    def test_success_reports_no_detail(self):
        self.assertEqual(
            self._seed(return_value=_completed(0, stdout="spawned")),
            (True, ""))

    def test_failure_reports_stderr(self):
        ok, detail = self._seed(
            return_value=_completed(1, stdout="progress", stderr="  boom  \n"))
        self.assertFalse(ok)
        self.assertEqual(detail, "boom")

    def test_failure_falls_back_to_stdout_when_stderr_is_empty(self):
        ok, detail = self._seed(
            return_value=_completed(2, stdout="only on stdout\n", stderr=""))
        self.assertFalse(ok)
        self.assertEqual(detail, "only on stdout")

    def test_failure_with_no_output_still_reports_something(self):
        ok, detail = self._seed(return_value=_completed(1))
        self.assertFalse(ok)
        self.assertEqual(detail, "?")

    def test_failure_detail_is_capped_at_the_last_400_chars(self):
        noise = "x" * 500 + "THE-ACTUAL-ERROR"
        ok, detail = self._seed(return_value=_completed(1, stderr=noise))
        self.assertFalse(ok)
        self.assertEqual(len(detail), 400)
        self.assertEqual(detail, noise[-400:])
        self.assertTrue(detail.endswith("THE-ACTUAL-ERROR"))

    def test_unlaunchable_child_is_reported_not_raised(self):
        ok, detail = self._seed(
            side_effect=OSError("bin/crew: no such file"))
        self.assertFalse(ok)
        self.assertIn("no such file", detail)

    def test_timeout_is_reported_not_raised(self):
        ok, detail = self._seed(side_effect=subprocess.TimeoutExpired(
            cmd=["bin/crew"], timeout=120))
        self.assertFalse(ok)
        self.assertTrue(detail.strip())


# --------------------------------------------------------------------------- #
# `crew project create` — the CLI half of the shared seed path
# --------------------------------------------------------------------------- #
class ProjectCreateCliSeedTests(unittest.TestCase):
    """cmd_project_create's contract with spawn.seed_foreman. Every side effect
    (MorphDB, schema, registry, the seed subprocess) is mocked, so nothing here
    creates a project, an app, or an agent."""

    def _create(self, seed=(True, ""), **overrides):
        """Run cmd_project_create; return a small record of what it did."""
        fields = dict(name="seedcli", description="", title="",
                      no_foreman=False, no_launch=False)
        fields.update(overrides)
        args = argparse.Namespace(**fields)
        order = mock.Mock()
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cli, "_ACTOR", "human"), \
             mock.patch.object(cli, "_ensure_morphdb"), \
             mock.patch.object(schema, "ensure_schema",
                               return_value="crew-" + fields["name"]), \
             mock.patch.object(config, "register_project") as register, \
             mock.patch.object(spawn, "seed_foreman",
                               return_value=seed) as seed_call, \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            order.attach_mock(register, "register_project")
            order.attach_mock(seed_call, "seed_foreman")
            code = cli.cmd_project_create(args)
        return {"code": code, "out": out.getvalue(), "err": err.getvalue(),
                "register": register, "seed": seed_call,
                "order": [call[0] for call in order.mock_calls]}

    def test_parser_exposes_title_no_foreman_and_no_launch(self):
        parser = cli.build_parser()
        args = parser.parse_args(["project", "create", "demo"])
        self.assertIs(args.fn, cli.cmd_project_create)
        self.assertEqual(args.title, "")
        self.assertEqual(args.description, "")
        self.assertFalse(args.no_foreman)
        self.assertFalse(args.no_launch)

        args = parser.parse_args([
            "project", "create", "demo", "--title", "My Graph",
            "--description", "lead-gen", "--no-foreman", "--no-launch"])
        self.assertEqual(args.title, "My Graph")
        self.assertEqual(args.description, "lead-gen")
        self.assertTrue(args.no_foreman)
        self.assertTrue(args.no_launch)

    def test_default_seeds_a_launched_foreman_after_registering(self):
        result = self._create()
        self.assertEqual(result["code"], 0)
        result["seed"].assert_called_once_with("seedcli", launch=True)
        # A seed can only succeed against a graph that is already registered.
        self.assertEqual(result["order"],
                         ["register_project", "seed_foreman"])
        self.assertIn("seeded foreman", result["out"])
        self.assertIn("booting", result["out"])
        self.assertEqual(result["err"], "")

    def test_no_foreman_leaves_the_graph_empty(self):
        result = self._create(no_foreman=True)
        self.assertEqual(result["code"], 0)
        result["seed"].assert_not_called()
        result["register"].assert_called_once()
        self.assertIn("spawn-agent", result["out"])

    def test_no_launch_seeds_without_starting_the_runtime(self):
        result = self._create(no_launch=True)
        self.assertEqual(result["code"], 0)
        result["seed"].assert_called_once_with("seedcli", launch=False)
        self.assertIn("not started", result["out"])

    def test_title_and_description_reach_the_registry(self):
        result = self._create(title="My Graph", description="lead-gen")
        result["register"].assert_called_once_with(
            "seedcli", description="lead-gen", title="My Graph")

    def test_failed_seed_keeps_the_graph_and_offers_a_manual_fallback(self):
        result = self._create(seed=(False, "boom"))
        self.assertEqual(result["code"], 0, "the graph exists — do not fail")
        self.assertIn("foreman seed failed", result["err"])
        self.assertIn("boom", result["err"])
        self.assertIn("seed it yourself", result["out"])
        self.assertIn("spawn-agent foreman --foreman", result["out"])

    def test_invalid_name_is_rejected_before_anything_is_created(self):
        result = self._create(name="bad name!")
        self.assertEqual(result["code"], 1)
        result["register"].assert_not_called()
        result["seed"].assert_not_called()

    def test_the_default_project_is_never_recreated_or_seeded(self):
        result = self._create(name=config.DEFAULT_PROJECT)
        self.assertEqual(result["code"], 0)
        result["register"].assert_not_called()
        result["seed"].assert_not_called()


# --------------------------------------------------------------------------- #
# POST /api/project/create — the dashboard-gallery half of the same seed path
# --------------------------------------------------------------------------- #
class DashboardProjectCreateSeedTests(unittest.TestCase):
    """_project_create's JSON contract. In-process, fully mocked: no HTTP, no
    schema call, no registry write, no seed subprocess."""

    def _create(self, data, seed=(True, ""), known=("default",)):
        with mock.patch.object(config, "list_known_projects",
                               return_value=list(known)), \
             mock.patch.object(schema, "ensure_schema",
                               return_value="crew-x"), \
             mock.patch.object(config, "register_project") as register, \
             mock.patch.object(spawn, "seed_foreman",
                               return_value=seed) as seed_call:
            body = dashboard_app._project_create(data)
        return body, register, seed_call

    def test_success_reports_the_seeded_foreman(self):
        body, register, seed_call = self._create(
            {"name": "tpseed", "title": "Seed Graph", "description": "d"})
        self.assertEqual(body, {"ok": True, "project": "tpseed",
                                "title": "Seed Graph", "foreman": "foreman"})
        register.assert_called_once_with(
            "tpseed", description="d", title="Seed Graph")
        seed_call.assert_called_once_with("tpseed", launch=True)

    def test_title_defaults_to_the_slug_when_absent(self):
        body, _, _ = self._create({"name": "tpseed"})
        self.assertEqual(body["title"], "tpseed")

    def test_derived_slug_is_what_gets_seeded(self):
        """Free-text title path: the foreman lands in the derived machine slug,
        not the display title."""
        body, _, seed_call = self._create({"title": "Seed Graph"})
        self.assertEqual(body["project"], "Seed-Graph")
        self.assertEqual(body["title"], "Seed Graph")
        seed_call.assert_called_once_with("Seed-Graph", launch=True)

    def test_launch_false_is_forwarded(self):
        _, _, seed_call = self._create({"name": "tpseed", "launch": False})
        seed_call.assert_called_once_with("tpseed", launch=False)

    def test_foreman_false_skips_seeding_entirely(self):
        body, register, seed_call = self._create(
            {"name": "tpseed", "foreman": False})
        seed_call.assert_not_called()
        register.assert_called_once()
        self.assertTrue(body["ok"])
        self.assertIsNone(body["foreman"])
        self.assertNotIn("warning", body)

    def test_failed_seed_still_reports_the_created_graph_with_a_warning(self):
        body, register, _ = self._create(
            {"name": "tpseed", "title": "Seed Graph"}, seed=(False, "boom"))
        register.assert_called_once()
        self.assertTrue(body["ok"], "the graph exists — do not report failure")
        self.assertEqual(body["project"], "tpseed")
        self.assertEqual(body["title"], "Seed Graph")
        self.assertIsNone(body["foreman"])
        self.assertTrue(body["warning"].startswith("foreman seed failed: "),
                        body["warning"])
        self.assertTrue(body["warning"].endswith("boom"), body["warning"])

    def test_duplicate_and_invalid_names_never_reach_the_seed(self):
        for data in ({"name": "default"}, {"name": "bad name!"}):
            with self.subTest(data=data):
                body, register, seed_call = self._create(data)
                self.assertFalse(body["ok"])
                register.assert_not_called()
                seed_call.assert_not_called()


if __name__ == "__main__":
    unittest.main()
