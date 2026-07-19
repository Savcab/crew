"""Cross-file contracts for setup and executable QA documentation."""
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
TEST_PLAN = ROOT / "TEST_PLAN.md"
BROWSER_DIR = ROOT / "tests" / "browser"


class ReadmeBrowserInventoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.readme = README.read_text(encoding="utf-8")

    def test_readme_lists_every_shipped_browser_workflow(self):
        for procedure in sorted(BROWSER_DIR.glob("*.md")):
            relative = procedure.relative_to(ROOT).as_posix()
            with self.subTest(procedure=relative):
                self.assertIn(relative, self.readme)

    def test_readme_scopes_fixture_and_cleanup_claim_to_mutating_scripts(self):
        self.assertNotIn(
            "Each script defines its own fixture prefix, capability bootstrap, "
            "expected results, and cleanup.",
            self.readme,
        )
        self.assertIn("Every mutating script", self.readme)

    def test_readme_documents_private_tmux_routing_and_safe_upgrade(self):
        self.assertIn("~/.config/crew/tmux-root", self.readme)
        self.assertIn("CREW_TMUX_TMPDIR", self.readme)
        self.assertIn('tmux -S "$CREW_TMUX_SOCKET" attach-session', self.readme)
        self.assertIn("Neither `crew up` nor `crew restart` interrupts", self.readme)
        self.assertIn("crew down <agent>", self.readme)
        self.assertIn("then `crew up <agent>`", self.readme)


class TestPlanFixtureSafetyTests(unittest.TestCase):
    def test_plan_allows_only_receipt_guarded_fixed_browser_fixtures(self):
        plan = TEST_PLAN.read_text(encoding="utf-8")
        self.assertNotIn("for every fixture", plan)
        self.assertNotIn("repo root, unique fixtures,", plan)
        self.assertIn("fixed-name fixtures", plan)
        self.assertIn("ownership receipt", plan)


if __name__ == "__main__":
    unittest.main()
