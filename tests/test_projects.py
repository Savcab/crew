"""Unit tests for WAVE 0 — projects (app-key-per-project) + standard home layout.

Pure logic over crew.config's project helpers and crew.spawn._plan_home's
env-driven default branch. No live server, no tmux, no filesystem side effects
beyond a tempdir used as a stand-in for VAR/crew_root() in the tests that need
one — never touches the real var/projects.json or the real crew_root().

Live checks (crew project create / crew --project X spawn-agent ...) live in
tests/live_smoke.py, per SKILL.md (schema-drift can only be caught live).

    python3 -m unittest tests.test_projects   (from repo root)
"""
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

from crew import cli, config, spawn  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
