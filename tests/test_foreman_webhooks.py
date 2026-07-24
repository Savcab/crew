"""Foreman-owned webhook control, from CLI configuration through delivery."""
import contextlib
import io
import json
import os
import sys
import unittest
from unittest import mock


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from crew import cli, config, graphstore as gs, schema, webhooks  # noqa: E402


TEST_APP = f"crewtest-foreman-webhooks-{os.getpid()}"


class ForemanWebhookTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._environment = mock.patch.dict(os.environ, {
            "CREW_APP": TEST_APP,
            "CREW_PROJECT": config.DEFAULT_PROJECT,
        })
        cls._environment.start()
        cls.addClassCleanup(cls._environment.stop)
        try:
            gs._req("DELETE", f"/app/{TEST_APP}", app=None)
        except gs.GraphError:
            pass
        schema.ensure_schema(TEST_APP)
        cls._limits = (
            config.MAX_AGENTS,
            config.SPAWN_RATE,
            config.MAX_WEBHOOKS_PER_FOREMAN,
        )
        config.MAX_AGENTS = 10_000
        config.SPAWN_RATE = 10_000
        config.MAX_WEBHOOKS_PER_FOREMAN = 12

    @classmethod
    def tearDownClass(cls):
        (
            config.MAX_AGENTS,
            config.SPAWN_RATE,
            config.MAX_WEBHOOKS_PER_FOREMAN,
        ) = cls._limits
        try:
            gs._req("DELETE", f"/app/{TEST_APP}", app=None)
        except gs.GraphError:
            pass

    def setUp(self):
        for agent in gs.list_agents():
            if agent.get("can_edit_graph"):
                gs.patch_object(
                    "agent", agent["_guid"], {"can_edit_graph": False})

    @staticmethod
    def _foreman(name):
        return gs.create_agent(
            name, home=f"/tmp/crew_foreman_hooks/{name}",
            can_edit_graph=True)

    @staticmethod
    def _child(foreman, name):
        return gs.create_agent(
            name, home=f"/tmp/crew_foreman_hooks/{name}",
            actor=foreman["name"])

    @staticmethod
    def _run(actor, argv):
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(
                cli.mail, "whoami", return_value=actor), \
             contextlib.redirect_stdout(stdout), \
             contextlib.redirect_stderr(stderr):
            status = cli.main(argv)
        return status, stdout.getvalue(), stderr.getvalue()

    @staticmethod
    def _audit(actor):
        rows = (gs.list_objects(
            "graph_edit", actor=actor, sort="created_at",
            order="desc", limit=1000) or {}).get("objects", [])
        return rows

    def test_foreman_cli_configures_hook_route_and_delivers_message(self):
        foreman = self._foreman("fwh_e2e_foreman")
        child = self._child(foreman, "fwh_e2e_child")
        template = "Issue {{ payload.issue.title }}"

        status, output, error = self._run(foreman["name"], [
            "webhook", "create", "fwh_e2e_hook",
            "--description", "issue events",
            "--template", template,
        ])
        self.assertEqual(status, 0, error)
        self.assertIn("POST /hooks/", output)
        hook = gs.get_webhook_by_name("fwh_e2e_hook")
        self.assertEqual(hook.get("created_by"), foreman["name"])
        self.assertEqual(hook.get("created_by_guid"), foreman["_guid"])
        self.assertFalse(hook.get("blessed"))

        status, output, error = self._run(
            foreman["name"], ["webhook", "list"])
        self.assertEqual(status, 0, error)
        self.assertIn(hook["name"], output)
        self.assertNotIn(hook["webhook_token"], output)

        status, output, error = self._run(
            foreman["name"], ["webhook", "show", hook["name"]])
        self.assertEqual(status, 0, error)
        self.assertIn(webhooks.public_url(hook), output)
        self.assertIn(template, output)

        status, _output, error = self._run(foreman["name"], [
            "webhook", "update", hook["name"],
            "--description", "GitHub issue events",
            "--template", "Opened: {{ payload.issue.title }}",
        ])
        self.assertEqual(status, 0, error)
        hook = gs.get_webhook_by_name(hook["name"])
        self.assertEqual(hook.get("role"), "GitHub issue events")

        status, _output, error = self._run(foreman["name"], [
            "connect", hook["name"], child["name"],
            "--when", "an issue opens",
            "--max-turns", "5",
            "--token-cap", "1000",
            "--cost-cap", "1",
        ])
        self.assertEqual(status, 0, error)
        edge = gs.authorizing_edge(hook["name"], child["name"])
        self.assertIsNotNone(edge)
        self.assertEqual(edge.get("created_by_guid"), foreman["_guid"])

        zero_usage = {
            "tokens": {"available": True, "value": 0, "reason": ""},
            "cost": {"available": True, "value": 0, "reason": ""},
        }
        with mock.patch.object(
                webhooks.mail.usage, "hourly_usage",
                return_value=zero_usage):
            result = webhooks.receive(
                hook["webhook_token"],
                json.dumps({"issue": {"title": "Queue retries"}}).encode(),
                "application/json",
                {"Idempotency-Key": "fwh-e2e-delivery"},
            )
        self.assertEqual(result.get("accepted"), 1, result)
        messages = [
            row for row in gs.list_messages(target=child["name"], limit=20)
            if row.get("sender_guid") == hook["_guid"]
        ]
        self.assertEqual(len(messages), 1, messages)
        self.assertEqual(messages[0].get("body"), "Opened: Queue retries")
        self.assertEqual(messages[0].get("edge_guid"), edge["_guid"])

        status, _output, error = self._run(foreman["name"], [
            "cap", hook["name"], child["name"], "--max-turns", "3",
        ])
        self.assertEqual(status, 0, error)
        self.assertEqual(gs.get_object(edge["_guid"]).get("max_turns"), 3)

        status, output, error = self._run(
            foreman["name"], ["edges"])
        self.assertEqual(status, 0, error)
        self.assertIn(f"{hook['name']} -> {child['name']}", output)

        status, _output, error = self._run(foreman["name"], [
            "disconnect", hook["name"], child["name"],
        ])
        self.assertEqual(status, 0, error)
        self.assertIsNone(
            gs.authorizing_edge(hook["name"], child["name"]))

        status, _output, error = self._run(foreman["name"], [
            "connect", hook["name"], child["name"],
            "--max-turns", "3",
            "--token-cap", "1000",
            "--cost-cap", "1",
        ])
        self.assertEqual(status, 0, error)
        self.assertIsNotNone(
            gs.authorizing_edge(hook["name"], child["name"]))

        old_token = hook["webhook_token"]
        status, output, error = self._run(
            foreman["name"], ["webhook", "rotate", hook["name"]])
        self.assertEqual(status, 0, error)
        self.assertIn("POST /hooks/", output)
        hook = gs.get_webhook_by_name(hook["name"])
        self.assertNotEqual(hook["webhook_token"], old_token)
        self.assertIsNone(gs.get_webhook_by_token(old_token))

        status, _output, error = self._run(
            foreman["name"], ["webhook", "remove", hook["name"]])
        self.assertEqual(status, 0, error)
        self.assertIsNone(gs.get_webhook_by_name(hook["name"]))
        self.assertEqual(gs.edges_touching(child["_guid"]), [])

        applied = [
            row for row in self._audit(foreman["name"])
            if row.get("result") == "applied"
            and row.get("op") in {
                "webhook_create", "webhook_update", "webhook_rotate",
                "webhook_remove", "connect", "disconnect", "update_edge",
            }
        ]
        self.assertTrue(applied)
        self.assertTrue(all(
            row.get("actor_guid") == foreman["_guid"] for row in applied))

    def test_plain_agent_cannot_create_or_read_webhook_secret(self):
        plain = gs.create_agent(
            "fwh_plain", home="/tmp/crew_foreman_hooks/fwh_plain")
        hook = gs.create_webhook("fwh_human_hook")

        status, _output, error = self._run(
            plain["name"], ["webhook", "create", "fwh_plain_denied"])
        self.assertEqual(status, 1)
        self.assertIn("foreman", error.lower())
        self.assertIsNone(gs.get_webhook_by_name("fwh_plain_denied"))

        for command in (
                ["webhook", "show", hook["name"]],
                ["webhook", "update", hook["name"],
                 "--description", "forged"],
                ["webhook", "rotate", hook["name"]],
                ["webhook", "remove", hook["name"]]):
            with self.subTest(command=command[1]):
                status, output, error = self._run(
                    plain["name"], command)
                self.assertEqual(status, 1)
                self.assertNotIn(hook["webhook_token"], output)
                self.assertIn("foreman", error.lower())
                self.assertIsNotNone(
                    gs.get_webhook_by_name(hook["name"]))

    def test_foreman_cannot_manage_user_or_other_foreman_hooks(self):
        first = self._foreman("fwh_owner_one")
        owned = gs.create_webhook("fwh_owned_one", actor=first["name"])
        human = gs.create_webhook("fwh_human_owned")
        gs.patch_object(
            "agent", first["_guid"], {"can_edit_graph": False})
        second = self._foreman("fwh_owner_two")

        for hook in (owned, human):
            for command in (
                    ["webhook", "show", hook["name"]],
                    ["webhook", "update", hook["name"],
                     "--description", "forged"],
                    ["webhook", "rotate", hook["name"]],
                    ["webhook", "remove", hook["name"]]):
                with self.subTest(hook=hook["name"], command=command[1]):
                    status, output, error = self._run(
                        second["name"], command)
                    self.assertEqual(status, 1)
                    self.assertNotIn(hook["webhook_token"], output)
                    self.assertIn("created", error.lower())
                    self.assertIsNotNone(
                        gs.get_webhook_by_name(hook["name"]))

    def test_per_foreman_hook_quota_releases_after_remove(self):
        foreman = self._foreman("fwh_quota_foreman")
        with mock.patch.object(
                config, "MAX_WEBHOOKS_PER_FOREMAN", 2):
            first = gs.create_webhook(
                "fwh_quota_one", actor=foreman["name"])
            gs.create_webhook(
                "fwh_quota_two", actor=foreman["name"])
            with self.assertRaisesRegex(gs.GraphError, "limit reached"):
                gs.create_webhook(
                    "fwh_quota_three", actor=foreman["name"])
            gs.delete_webhook(first["_guid"], actor=foreman["name"])
            replacement = gs.create_webhook(
                "fwh_quota_replacement", actor=foreman["name"])
        self.assertEqual(replacement.get("created_by_guid"), foreman["_guid"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
