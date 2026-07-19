"""Contract checks for the public product explainer.

The marketing page is part of Crew's setup surface: it must describe the same
runtime choices, advisory graph semantics, terminal controls, and checkout-local
commands that README and the shipped dashboard actually implement.
"""
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "site" / "index.html"


class PublicSiteContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = SITE.read_text(encoding="utf-8")

    def test_runtime_copy_is_not_claude_only(self):
        self.assertIn("Claude Code", self.html)
        self.assertIn("Codex CLI", self.html)
        self.assertIn("custom", self.html.lower())
        self.assertNotIn("Each agent is a persistent Claude Code session", self.html)
        self.assertNotIn("boots a real Claude", self.html)
        self.assertNotIn("wire up a team of Claude Code agents", self.html)

    def test_conditions_are_described_as_advisory_not_automatic(self):
        self.assertIn("advisory", self.html.lower())
        self.assertIn("crew message", self.html)
        self.assertNotIn("live send condition", self.html)

    def test_terminal_copy_does_not_advertise_removed_message_box(self):
        self.assertNotIn("box at the bottom", self.html.lower())
        self.assertIn("type directly into", self.html.lower())

    def test_checkout_quickstart_names_runtime_and_operator_url(self):
        self.assertIn("./bin/crew init", self.html)
        self.assertIn("--runtime</span> claude", self.html)
        self.assertIn("--runtime</span> codex", self.html)
        self.assertIn("capability", self.html.lower())

    def test_message_example_uses_a_real_durable_outcome(self):
        self.assertNotIn("[crew] sent to 'builder'", self.html)
        self.assertIn("[crew] delivered to 'builder'", self.html)

    def test_identity_example_uses_the_project_scoped_default_home(self):
        self.assertNotIn("# ~/crew/leads/identity.md", self.html)
        self.assertIn("# ~/crew/default/leads/identity.md", self.html)

    def test_authorization_copy_requires_exactly_one_matching_edge(self):
        self.assertNotIn("single index-backed query", self.html)
        self.assertIn("forward and reverse-undirected", self.html)
        self.assertIn("exactly one authorizing edge", self.html)


if __name__ == "__main__":
    unittest.main()
