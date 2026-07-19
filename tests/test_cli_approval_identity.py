"""CLI approval keeps both endpoint identity files synchronized.

These regressions intentionally cross the real ``bin/crew`` subprocess and a
throwaway MorphDB tenant.  A pending approval is a graph mutation just like a
direct ``connect``/``cap`` command, so committing it must publish the same
durable identity update for both endpoints.
"""
import os
import sys
import tempfile
import unittest

from operator_harness import pin_environment, run_operator


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from crew import config, graphstore as gs, schema, spawn  # noqa: E402


CREW_BIN = os.path.join(ROOT, "bin", "crew")
TEST_APP = f"crewtest-cli-approval-identity-{os.getpid()}"
STALE_SENTINEL = "STALE IDENTITY MUST BE REPLACED"


class CliApprovalIdentityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        pin_environment(cls.addClassCleanup, {
            "CREW_APP": TEST_APP,
            "CREW_PROJECT": config.DEFAULT_PROJECT,
        })
        try:
            gs._req("DELETE", f"/app/{TEST_APP}", app=None)
        except gs.GraphError:
            pass
        schema.ensure_schema(TEST_APP)

    @classmethod
    def tearDownClass(cls):
        try:
            gs._req("DELETE", f"/app/{TEST_APP}", app=None)
        except gs.GraphError:
            pass

    def _agent(self, name, root, *, foreman=False):
        home = os.path.join(root, name)
        os.makedirs(home)
        return gs.create_agent(
            name, home=home, runtime="custom", launch_cmd="exec sh",
            can_edit_graph=foreman)

    def _pending(self, actor, op):
        rows = gs.list_objects(
            "graph_edit", result="pending", actor=actor,
            sort="created_at", order="desc", limit=100)
        matches = [row for row in (rows or {}).get("objects", [])
                   if row.get("op") == op]
        self.assertEqual(len(matches), 1, matches)
        return matches[0]

    def _approve(self, guid):
        environment = {
            "CREW_APP": TEST_APP,
            "CREW_PROJECT": config.DEFAULT_PROJECT,
            "MORPHDB_HOST": config.MORPHDB_HOST,
        }
        result = run_operator(
            [sys.executable, CREW_BIN, "approve", guid],
            cwd=ROOT, env_extra=environment, capture_output=True, text=True,
            timeout=30)
        self.assertEqual(
            result.returncode, 0,
            f"crew approve failed: {result.stdout!r} {result.stderr!r}")

    @staticmethod
    def _identity_path(agent):
        return os.path.join(agent["home"], config.IDENTITY_FILE)

    def _make_stale(self, *agents):
        for agent in agents:
            spawn.rewrite_identity(agent)
            with open(self._identity_path(agent), "a") as stream:
                stream.write(f"\n{STALE_SENTINEL}\n")

    def _identity_text(self, agent):
        with open(self._identity_path(agent)) as stream:
            return stream.read()

    def test_approve_connect_rewrites_both_endpoint_identity_files(self):
        with tempfile.TemporaryDirectory(
                prefix="crew-cli-approve-connect-") as root:
            source = self._agent("cli_approve_connect_f", root, foreman=True)
            target = self._agent("cli_approve_connect_h", root)
            self._make_stale(source, target)

            with self.assertRaisesRegex(gs.GraphError, "queued"):
                gs.create_edge(
                    source["_guid"], target["_guid"],
                    actor=source["name"], max_turns=5,
                    token_cap=1000, cost_cap=1.0)
            self._approve(self._pending(source["name"], "connect")["_guid"])

            source_text = self._identity_text(source)
            target_text = self._identity_text(target)
            self.assertNotIn(STALE_SENTINEL, source_text)
            self.assertNotIn(STALE_SENTINEL, target_text)
            self.assertIn(f"**{target['name']}**", source_text)
            self.assertIn("at most 5 message(s) per hour", source_text)
            self.assertIn("1,000 tok/hr + $1/hr", source_text)
            self.assertIn(f"**{source['name']}** may message you", target_text)

    def test_approve_cap_raise_rewrites_both_endpoint_identity_files(self):
        with tempfile.TemporaryDirectory(
                prefix="crew-cli-approve-cap-") as root:
            source = self._agent("cli_approve_cap_a", root)
            target = self._agent("cli_approve_cap_b", root)
            edge = gs.create_edge(
                source["_guid"], target["_guid"], actor="human",
                max_turns=5, token_cap=1000, cost_cap=1.0)
            self._make_stale(source, target)

            with self.assertRaisesRegex(gs.GraphError, "queued"):
                gs.update_edge(
                    edge["_guid"], {"max_turns": 50}, actor=source["name"])
            self._approve(
                self._pending(source["name"], "update_edge")["_guid"])

            source_text = self._identity_text(source)
            target_text = self._identity_text(target)
            self.assertNotIn(STALE_SENTINEL, source_text)
            self.assertNotIn(STALE_SENTINEL, target_text)
            self.assertIn("at most 50 message(s) per hour", source_text)
            self.assertNotIn("at most 5 message(s) per hour", source_text)
            self.assertIn(f"**{source['name']}** may message you", target_text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
