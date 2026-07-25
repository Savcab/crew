"""Foreman-owned webhook control, from guarded CLI configuration to delivery.

The app is deleted and recreated for every test so singleton, quota, ownership,
and audit assertions never depend on another test's graph. All fixtures are
synthetic and the exact throwaway app is deleted in class cleanup.

    python3 tests/test_foreman_webhooks.py
"""
import contextlib
import io
import json
import multiprocessing
import os
import subprocess
import sys
import threading
import unittest
from unittest import mock


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from crew import cli, config, graphstore as gs, schema, webhooks  # noqa: E402


TEST_APP = f"crewtest-foreman-webhooks-{os.getpid()}"
CREW_BIN = os.path.join(ROOT, "bin", "crew")


def _quota_create_worker(host, app, actor, name, barrier, results):
    """Race the real app-scoped flock from an independent Python process."""
    config.MORPHDB_HOST = host
    os.environ["CREW_APP"] = app
    os.environ.pop("CREW_PROJECT", None)
    config.MAX_WEBHOOKS_PER_FOREMAN = 1
    try:
        barrier.wait(timeout=15)
        gs.create_webhook(name, actor=actor)
        results.put((name, "created"))
    except Exception as error:
        results.put((name, f"error:{type(error).__name__}"))


class ForemanWebhookTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._environment = mock.patch.dict(os.environ, {
            "CREW_APP": TEST_APP,
            "CREW_PROJECT": config.DEFAULT_PROJECT,
        })
        cls._environment.start()
        cls.addClassCleanup(cls._environment.stop)
        cls.addClassCleanup(cls._delete_test_app)

    @classmethod
    def _delete_test_app(cls):
        try:
            gs._req("DELETE", f"/app/{TEST_APP}", app=None)
        except gs.GraphError:
            pass

    def setUp(self):
        self._delete_test_app()
        schema.ensure_schema(TEST_APP)

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
    def _run_real_cli(actor, argv):
        """Run bin/crew detached so CREW_AGENT is the compatibility identity."""
        environment = dict(os.environ)
        for key in ("TMUX", "TMUX_PANE", "AGENT_MAIL_NAME", "CREW_PROJECT"):
            environment.pop(key, None)
        environment.update({
            "CREW_AGENT": actor,
            "CREW_APP": TEST_APP,
        })
        return subprocess.run(
            [sys.executable, CREW_BIN, *argv],
            cwd=ROOT,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=45,
            start_new_session=True,
        )

    def _assert_real_cli_ok(self, completed, command):
        if completed.returncode != 0:
            self.fail(
                f"detached bin/crew {command!r} failed with "
                f"status {completed.returncode}; output redacted")

    def _assert_hook_secrets_absent(self, value, hook, *, extras=()):
        """Fail by secret category without echoing the secret or haystack."""
        serialized = (
            value if isinstance(value, str)
            else json.dumps(value, sort_keys=True))
        secrets = (
            ("token", hook.get("webhook_token")),
            ("token hash", hook.get("webhook_token_hash")),
            ("public URL", webhooks.public_url(hook)),
            ("template", hook.get("webhook_template")),
            *[(f"extra value {index}", item)
              for index, item in enumerate(extras, 1)],
        )
        for label, secret in secrets:
            if secret and str(secret) in serialized:
                self.fail(f"webhook {label} leaked into redacted output")

    @staticmethod
    def _audit(actor=None, op=None):
        rows = (gs.list_objects(
            "graph_edit", sort="created_order",
            order="desc", limit=1000) or {}).get("objects", [])
        if actor is not None:
            rows = [row for row in rows if row.get("actor") == actor]
        if op is not None:
            rows = [row for row in rows if row.get("op") == op]
        return rows

    @staticmethod
    def _receive(hook, delivery_id):
        zero_usage = {
            "tokens": {"available": True, "value": 0, "reason": ""},
            "cost": {"available": True, "value": 0, "reason": ""},
        }
        with mock.patch.object(
                webhooks.mail.usage, "hourly_usage",
                return_value=zero_usage):
            return webhooks.receive(
                hook["webhook_token"],
                json.dumps({
                    "issue": {"title": "Queue retries"},
                }).encode(),
                "application/json",
                {"Idempotency-Key": delivery_id},
            )

    def _run_identity_wait_race(
            self, operation, transition, *, blocked_agent_lock=1):
        """Pause after actor GUID pinning, mutate identity, then resume."""
        real_lock = gs._invariant_lock
        entered = threading.Event()
        release = threading.Event()
        agent_lock_count = 0
        errors = []

        @contextlib.contextmanager
        def controlled_lock(scope, *args, **kwargs):
            nonlocal agent_lock_count
            should_pause = (
                threading.current_thread().name == "fwh-stale-operation"
                and scope == "agent")
            if should_pause:
                agent_lock_count += 1
                if agent_lock_count == blocked_agent_lock:
                    entered.set()
                    if not release.wait(15):
                        raise AssertionError(
                            "timed out waiting to resume stale hook operation")
            with real_lock(scope, *args, **kwargs):
                yield

        def run():
            try:
                operation()
            except Exception as error:
                errors.append(error)

        worker = threading.Thread(
            target=run, name="fwh-stale-operation")
        try:
            with mock.patch.object(
                    gs, "_invariant_lock", controlled_lock):
                worker.start()
                self.assertTrue(
                    entered.wait(10),
                    "hook operation did not reach its guarded wait point")
                transition()
                release.set()
                worker.join(20)
        finally:
            release.set()
            worker.join(5)
        self.assertFalse(worker.is_alive(), "stale hook operation hung")
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], gs.GraphError)
        return errors[0]

    def test_foreman_cli_lifecycle_route_and_delivery(self):
        foreman = self._foreman("fwh_e2e_foreman")
        child = self._child(foreman, "fwh_e2e_child")
        initial_template = "Issue {{ payload.issue.title }}"

        status, output, error = self._run(foreman["name"], [
            "webhook", "create", "fwh_e2e_hook",
            "--description", "issue events",
            "--template", initial_template,
        ])
        self.assertEqual(status, 0, error)
        self.assertIn("POST ", output)
        hook = gs.get_webhook_by_name("fwh_e2e_hook")
        self.assertEqual(hook.get("created_by"), foreman["name"])
        self.assertEqual(hook.get("created_by_guid"), foreman["_guid"])
        self.assertFalse(hook.get("blessed"))

        status, output, error = self._run(
            foreman["name"], ["webhook", "list"])
        self.assertEqual(status, 0, error)
        self.assertIn(hook["name"], output)
        self.assertNotIn(hook["webhook_token"], output)
        self.assertNotIn(webhooks.public_url(hook), output)

        status, output, error = self._run(
            foreman["name"], ["webhook", "show", hook["name"]])
        self.assertEqual(status, 0, error)
        self.assertIn(webhooks.public_url(hook), output)
        self.assertIn(initial_template, output)
        reads = self._audit(foreman["name"], "webhook_read")
        self.assertTrue(any(
            row.get("result") == "applied"
            and row.get("actor_guid") == foreman["_guid"]
            for row in reads), reads)

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
        self.assertFalse(edge.get("blessed"))

        result = self._receive(hook, "fwh-e2e-delivery")
        self.assertEqual(result.get("accepted"), 1, result)
        messages = [
            row for row in gs.list_messages(target=child["name"], limit=20)
            if row.get("sender_guid") == hook["_guid"]
        ]
        self.assertEqual(len(messages), 1, messages)
        self.assertEqual(messages[0].get("body"), "Opened: Queue retries")
        self.assertEqual(messages[0].get("edge_guid"), edge["_guid"])
        self.assertEqual(messages[0].get("target_guid"), child["_guid"])

        old_token = hook["webhook_token"]
        status, output, error = self._run(
            foreman["name"], ["webhook", "rotate", hook["name"]])
        self.assertEqual(status, 0, error)
        self.assertIn("POST ", output)
        hook = gs.get_webhook_by_name(hook["name"])
        self.assertNotEqual(hook["webhook_token"], old_token)
        self.assertIsNone(gs.get_webhook_by_token(old_token))

        status, _output, error = self._run(
            foreman["name"], ["webhook", "remove", hook["name"]])
        self.assertEqual(status, 0, error)
        self.assertIsNone(gs.get_webhook_by_name(hook["name"]))
        self.assertEqual(gs.edges_touching(child["_guid"]), [])

        relevant = {
            "webhook_create", "webhook_read", "webhook_update",
            "webhook_rotate", "webhook_remove", "connect",
        }
        applied = [
            row for row in self._audit(foreman["name"])
            if row.get("result") == "applied"
            and row.get("op") in relevant
        ]
        self.assertTrue(applied)
        self._assert_hook_secrets_absent(
            applied, hook, extras=(old_token,))
        self.assertTrue(all(
            row.get("actor_guid") == foreman["_guid"] for row in applied))

    def test_detached_bin_crew_uses_real_agent_identity_for_full_lifecycle(self):
        """No in-process whoami mock: a detached CLI resolves CREW_AGENT."""
        foreman = self._foreman("fwh_real_cli_foreman")
        name = "fwh_real_cli_hook"

        created = self._run_real_cli(foreman["name"], [
            "webhook", "create", name,
            "--description", "detached CLI",
            "--template", "Real {{ payload.message }}",
        ])
        self._assert_real_cli_ok(created, "webhook create")
        hook = gs.get_webhook_by_name(name)
        self.assertIsNotNone(hook)
        self.assertEqual(hook.get("created_by_guid"), foreman["_guid"])
        if webhooks.public_url(hook) not in created.stdout:
            self.fail("detached create did not return its guarded hook URL")

        listed = self._run_real_cli(
            foreman["name"], ["webhook", "list"])
        self._assert_real_cli_ok(listed, "webhook list")
        self.assertIn(name, listed.stdout)
        self._assert_hook_secrets_absent(listed.stdout, hook)

        shown = self._run_real_cli(
            foreman["name"], ["webhook", "show", name])
        self._assert_real_cli_ok(shown, "webhook show")
        if webhooks.public_url(hook) not in shown.stdout:
            self.fail("detached show did not return the authorized hook URL")

        updated = self._run_real_cli(foreman["name"], [
            "webhook", "update", name,
            "--description", "updated through real CLI",
            "--template", "Updated {{ payload.message }}",
        ])
        self._assert_real_cli_ok(updated, "webhook update")
        hook = gs.get_webhook_by_name(name)
        self.assertEqual(hook.get("role"), "updated through real CLI")
        self.assertEqual(
            hook.get("webhook_template"),
            "Updated {{ payload.message }}")

        old_token = hook["webhook_token"]
        rotated = self._run_real_cli(
            foreman["name"], ["webhook", "rotate", name])
        self._assert_real_cli_ok(rotated, "webhook rotate")
        hook = gs.get_webhook_by_name(name)
        if hook.get("webhook_token") == old_token:
            self.fail("detached rotate did not replace the capability")

        removed = self._run_real_cli(
            foreman["name"], ["webhook", "remove", name])
        self._assert_real_cli_ok(removed, "webhook remove")
        self.assertIsNone(gs.get_webhook_by_name(name))

    def test_plain_agent_denial_audits_never_persist_hook_secrets(self):
        plain = gs.create_agent(
            "fwh_plain", home="/tmp/crew_foreman_hooks/fwh_plain")
        old_template = "SENSITIVE_TEMPLATE_{{ payload.private }}"
        hook = gs.create_webhook(
            "fwh_human_hook", template=old_template)
        requested_template = "REQUESTED_PRIVATE_TEMPLATE"

        commands = (
            ["webhook", "show", hook["name"]],
            ["webhook", "update", hook["name"],
             "--description", "forged",
             "--template", requested_template],
            ["webhook", "rotate", hook["name"]],
            ["webhook", "remove", hook["name"]],
        )
        combined_output = ""
        for command in commands:
            with self.subTest(command=command[1]):
                status, output, error = self._run(plain["name"], command)
                combined_output += output + error
                self.assertEqual(status, 1)
                self.assertIsNotNone(
                    gs.get_webhook_by_name(hook["name"]))

        rows = self._audit(plain["name"])
        self._assert_hook_secrets_absent(
            combined_output, hook, extras=(requested_template,))
        self._assert_hook_secrets_absent(
            rows, hook, extras=(requested_template,))
        serialized = json.dumps(rows, sort_keys=True)
        if "/hooks/" in serialized:
            self.fail("a hook path leaked into a refused audit")
        self.assertTrue(rows)
        self.assertTrue(all(
            row.get("result") == "refused"
            for row in rows))

    def test_graphstore_rejects_fieldless_webhook_update(self):
        owner = self._foreman("fwh_fieldless_owner")
        hook = gs.create_webhook(
            "fwh_fieldless_hook", actor=owner["name"])

        with self.assertRaisesRegex(gs.GraphError, "nothing|field|change"):
            gs.update_webhook(
                hook["_guid"], actor=owner["name"])

        current = gs.get_webhook_by_name(hook["name"])
        self.assertEqual(current.get("role"), hook.get("role"))
        if current.get("webhook_token") != hook.get("webhook_token"):
            self.fail("fieldless update unexpectedly rotated the capability")

    def test_list_resolves_current_active_owner_by_guid(self):
        original_name = "fwh_list_owner"
        owner = self._foreman(original_name)
        hook = gs.create_webhook(
            "fwh_list_hook", actor=owner["name"])

        status, active, error = self._run("human", ["webhook", "list"])
        self.assertEqual(status, 0, error)
        self.assertIn(f"owner:{original_name}", active)
        self._assert_hook_secrets_absent(active, hook)

        renamed = "fwh_list_renamed"
        gs.update_agent(owner["_guid"], actor="human", name=renamed)
        status, active_renamed, error = self._run(
            "human", ["webhook", "list"])
        self.assertEqual(status, 0, error)
        self.assertIn(f"owner:{renamed}", active_renamed)
        self.assertNotIn("owner unavailable", active_renamed)
        self._assert_hook_secrets_absent(active_renamed, hook)

        gs.set_foreman(owner["_guid"], revoke=True, actor="human")
        status, revoked, error = self._run("human", ["webhook", "list"])
        self.assertEqual(status, 0, error)
        self.assertIn("human-managed (owner unavailable)", revoked)
        self.assertNotIn(f"owner:{renamed}", revoked)
        self._assert_hook_secrets_absent(revoked, hook)

        gs.delete_agent(owner["_guid"], actor="human")
        replacement = self._foreman(renamed)
        self.assertNotEqual(replacement["_guid"], owner["_guid"])
        status, replaced, error = self._run("human", ["webhook", "list"])
        self.assertEqual(status, 0, error)
        self.assertIn("human-managed (owner unavailable)", replaced)
        self.assertNotIn(f"owner:{renamed}", replaced)
        self._assert_hook_secrets_absent(replaced, hook)

    def test_other_foreman_guid_cannot_inherit_owned_hook(self):
        first = self._foreman("fwh_owner_one")
        hook = gs.create_webhook(
            "fwh_owned_one",
            template="OWNER_ONE_TEMPLATE",
            actor=first["name"])
        gs.read_webhook(hook["_guid"], actor=first["name"])
        gs.set_foreman(first["_guid"], revoke=True, actor="human")
        second = self._foreman("fwh_owner_two")

        for command in (
                ["webhook", "show", hook["name"]],
                ["webhook", "update", hook["name"],
                 "--description", "forged"],
                ["webhook", "rotate", hook["name"]],
                ["webhook", "remove", hook["name"]]):
            with self.subTest(command=command[1]):
                status, output, error = self._run(
                    second["name"], command)
                self.assertEqual(status, 1)
                self._assert_hook_secrets_absent(output + error, hook)
                self.assertIsNotNone(
                    gs.get_webhook_by_name(hook["name"]))

        reads = self._audit(first["name"], "webhook_read")
        self.assertTrue(any(
            row.get("result") == "applied"
            and row.get("actor_guid") == first["_guid"]
            for row in reads), reads)
        denied = self._audit(second["name"])
        self.assertTrue(denied)
        self._assert_hook_secrets_absent(denied, hook)
        self.assertTrue(all(
            row.get("actor_guid") == second["_guid"]
            for row in denied))

    def test_revoked_and_deleted_owner_leaves_live_human_managed_hook(self):
        original = self._foreman("fwh_reused_owner")
        child = self._child(original, "fwh_orphan_child")
        hook = gs.create_webhook(
            "fwh_orphan_hook",
            template="Issue {{ payload.issue.title }}",
            actor=original["name"])
        edge = gs.create_edge(
            hook["_guid"], child["_guid"],
            max_turns=5, token_cap=1000, cost_cap=1,
            actor=original["name"])

        gs.set_foreman(original["_guid"], revoke=True, actor="human")
        with self.assertRaises(gs.GraphError):
            gs.read_webhook(hook["_guid"], actor=original["name"])
        revoked_rows = [
            row for row in self._audit(original["name"])
            if row.get("result") == "refused"
            and row.get("actor_guid") == original["_guid"]
        ]
        self.assertTrue(revoked_rows)
        self._assert_hook_secrets_absent(revoked_rows, hook)
        first = self._receive(hook, "fwh-orphan-before-delete")
        self.assertEqual(first.get("accepted"), 1, first)

        gs.delete_agent(original["_guid"], actor="human")
        self.assertIsNotNone(gs.get_webhook_by_name(hook["name"]))
        self.assertEqual(
            gs.authorizing_edge(hook["name"], child["name"])["_guid"],
            edge["_guid"])

        replacement = self._foreman(original["name"])
        self.assertNotEqual(replacement["_guid"], original["_guid"])
        with self.assertRaises(gs.GraphError):
            gs.read_webhook(hook["_guid"], actor=replacement["name"])
        with self.assertRaises(gs.GraphError):
            gs.update_webhook(
                hook["_guid"], description="stolen",
                actor=replacement["name"])
        replacement_rows = [
            row for row in self._audit(replacement["name"])
            if row.get("result") == "refused"
            and row.get("actor_guid") == replacement["_guid"]
        ]
        self.assertTrue(replacement_rows)
        self._assert_hook_secrets_absent(replacement_rows, hook)

        second = self._receive(hook, "fwh-orphan-after-delete")
        self.assertEqual(second.get("accepted"), 1, second)
        self.assertEqual(
            gs.read_webhook(hook["_guid"], actor="human")["_guid"],
            hook["_guid"])
        updated = gs.update_webhook(
            hook["_guid"], description="operator-owned recovery",
            actor="human")
        self.assertEqual(updated.get("role"), "operator-owned recovery")

    def test_cross_process_creates_cannot_exceed_last_quota_slot(self):
        foreman = self._foreman("fwh_quota_foreman")
        ctx = multiprocessing.get_context("spawn")
        barrier = ctx.Barrier(2)
        results = ctx.Queue()
        names = ("fwh_quota_one", "fwh_quota_two")
        processes = [
            ctx.Process(
                target=_quota_create_worker,
                args=(
                    config.MORPHDB_HOST, TEST_APP, foreman["name"],
                    name, barrier, results),
            )
            for name in names
        ]
        try:
            for process in processes:
                process.start()
            for process in processes:
                process.join(30)
            self.assertTrue(
                all(not process.is_alive() for process in processes),
                "cross-process webhook quota race hung")
            self.assertTrue(
                all(process.exitcode == 0 for process in processes),
                [process.exitcode for process in processes])
            outcomes = [results.get(timeout=5) for _ in processes]
            self.assertEqual(
                sum(status == "created" for _, status in outcomes), 1,
                outcomes)
            self.assertEqual(
                sum(status.startswith("error:") for _, status in outcomes), 1,
                outcomes)
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                    process.join(5)
            results.close()

        with mock.patch.object(
                config, "MAX_WEBHOOKS_PER_FOREMAN", 1):
            owned = [
                item for item in gs.list_webhooks()
                if item.get("created_by_guid") == foreman["_guid"]
            ]
            self.assertEqual(len(owned), 1, owned)

            # Removing an owned hook releases exactly one slot.
            gs.delete_webhook(
                owned[0]["_guid"], actor=foreman["name"])
            replacement = gs.create_webhook(
                "fwh_quota_replacement", actor=foreman["name"])
            self.assertEqual(
                replacement.get("created_by_guid"), foreman["_guid"])

    def test_quota_uses_exact_owner_count_beyond_graph_list_page(self):
        foreman = self._foreman("fwh_saturated_foreman")
        saturated_page = [
            {
                "_guid": f"unrelated-{index}",
                "name": f"unrelated_{index}",
                "kind": "agent",
            }
            for index in range(1000)
        ]
        with mock.patch.object(
                gs, "list_nodes", return_value=saturated_page), \
             mock.patch.object(
                 gs, "count_webhooks_by_owner", return_value=12) as count, \
             mock.patch.object(
                 config, "MAX_WEBHOOKS_PER_FOREMAN", 12):
            with self.assertRaisesRegex(gs.GraphError, "limit"):
                gs.create_webhook(
                    "fwh_saturated_refused", actor=foreman["name"])

        count.assert_called_with(foreman["_guid"])
        self.assertIsNone(
            gs.get_webhook_by_name("fwh_saturated_refused"))

    def test_owned_hook_envelope_ignores_saturated_canvas_page(self):
        foreman = self._foreman("fwh_paged_envelope_foreman")
        child = self._child(foreman, "fwh_paged_envelope_child")
        hook = gs.create_webhook(
            "fwh_paged_envelope_hook", actor=foreman["name"])
        saturated_page = [
            {
                "_guid": f"unrelated-{index}",
                "name": f"unrelated_{index}",
                "kind": "agent",
            }
            for index in range(1000)
        ]

        with mock.patch.object(
                gs, "list_nodes", return_value=saturated_page) as canvas_page:
            edge = gs.create_edge(
                hook["_guid"], child["_guid"],
                max_turns=5, token_cap=1000, cost_cap=1,
                actor=foreman["name"])

        canvas_page.assert_not_called()
        self.assertEqual(edge.get("created_by_guid"), foreman["_guid"])
        self.assertEqual(edge.get("source"), hook["_guid"])
        self.assertEqual(edge.get("target"), child["_guid"])

    def test_cli_list_pages_past_one_thousand_indexed_webhooks(self):
        hooks = [
            {
                "_guid": f"paged-hook-{index}",
                "name": f"paged_hook_{index:04d}",
                "kind": gs.WEBHOOK_KIND,
                "status": "listening",
                "created_by_guid": "",
            }
            for index in range(1001)
        ]
        calls = []

        def list_page(otype, **kwargs):
            calls.append(dict(kwargs))
            self.assertEqual(otype, "agent")
            self.assertEqual(kwargs.get("kind"), gs.WEBHOOK_KIND)
            offset = int(kwargs.get("offset") or 0)
            limit = int(kwargs.get("limit") or 1000)
            return {
                "objects": hooks[offset:offset + limit],
                "total": len(hooks),
            }

        output, error = io.StringIO(), io.StringIO()
        with mock.patch.object(gs, "list_objects", side_effect=list_page), \
             contextlib.redirect_stdout(output), \
             contextlib.redirect_stderr(error):
            status = cli.cmd_webhook_list(None)

        self.assertEqual(status, 0, error.getvalue())
        self.assertIn(hooks[0]["name"], output.getvalue())
        self.assertIn(hooks[-1]["name"], output.getvalue())
        self.assertEqual([call.get("offset") for call in calls], [0, 1000])

    def test_read_rechecks_foreman_after_waiting_for_agent_lock(self):
        owner = self._foreman("fwh_wait_read")
        hook = gs.create_webhook(
            "fwh_wait_read_hook", actor=owner["name"])

        self._run_identity_wait_race(
            lambda: gs.read_webhook(
                hook["_guid"], actor=owner["name"]),
            lambda: gs.set_foreman(
                owner["_guid"], revoke=True, actor="human"),
        )

        refused = [
            row for row in self._audit(owner["name"], "webhook_read")
            if row.get("result") == "refused"
        ]
        self.assertTrue(refused)
        self._assert_hook_secrets_absent(refused, hook)

    def test_update_rechecks_deleted_actor_after_waiting_for_agent_lock(self):
        owner = self._foreman("fwh_wait_update")
        hook = gs.create_webhook(
            "fwh_wait_update_hook", actor=owner["name"])

        self._run_identity_wait_race(
            lambda: gs.update_webhook(
                hook["_guid"], description="must not apply",
                actor=owner["name"]),
            lambda: gs.delete_agent(owner["_guid"], actor="human"),
        )

        self.assertEqual(
            gs.get_webhook_by_name(hook["name"]).get("role"), "")
        self._assert_hook_secrets_absent(
            self._audit(owner["name"]), hook)

    def test_rotate_rechecks_same_name_replacement_after_waiting(self):
        owner = self._foreman("fwh_wait_rotate")
        hook = gs.create_webhook(
            "fwh_wait_rotate_hook", actor=owner["name"])

        replacement = []

        def replace_owner():
            gs.delete_agent(owner["_guid"], actor="human")
            replacement.append(self._foreman(owner["name"]))

        self._run_identity_wait_race(
            lambda: gs.update_webhook(
                hook["_guid"], rotate=True, actor=owner["name"]),
            replace_owner,
        )

        self.assertNotEqual(replacement[0]["_guid"], owner["_guid"])
        current = gs.get_webhook_by_name(hook["name"])
        if current.get("webhook_token") != hook.get("webhook_token"):
            self.fail("stale rotate changed the hook capability")
        with self.assertRaises(gs.GraphError):
            gs.read_webhook(
                hook["_guid"], actor=replacement[0]["name"])
        denied = [
            row for row in self._audit(replacement[0]["name"])
            if row.get("result") == "refused"
            and row.get("actor_guid") == replacement[0]["_guid"]
        ]
        self.assertTrue(denied)
        self._assert_hook_secrets_absent(denied, hook)

    def test_remove_rechecks_replacement_at_final_mutation_lock(self):
        owner = self._foreman("fwh_wait_remove")
        hook = gs.create_webhook(
            "fwh_wait_remove_hook", actor=owner["name"])

        replacement = []

        def replace_owner():
            # The delete path holds the app-wide identity transaction while it
            # waits for its final agent lock, so a second ordinary Crew delete
            # correctly serializes behind it. Simulate an out-of-band storage
            # replacement to prove the commit-point identity recheck still
            # fails closed if durable state changes outside that coordinator.
            gs.delete_object("agent", owner["_guid"])
            replacement.append(self._foreman(owner["name"]))

        # delete_webhook has an optimistic ownership preflight and a second
        # agent-lock acquisition at the irreversible mutation boundary.
        self._run_identity_wait_race(
            lambda: gs.delete_webhook(
                hook["_guid"], actor=owner["name"]),
            replace_owner,
            blocked_agent_lock=2,
        )

        self.assertNotEqual(replacement[0]["_guid"], owner["_guid"])
        self.assertIsNotNone(gs.get_webhook_by_name(hook["name"]))
        with self.assertRaises(gs.GraphError):
            gs.delete_webhook(
                hook["_guid"], actor=replacement[0]["name"])
        denied = [
            row for row in self._audit(replacement[0]["name"])
            if row.get("result") == "refused"
            and row.get("actor_guid") == replacement[0]["_guid"]
        ]
        self.assertTrue(denied)
        self._assert_hook_secrets_absent(denied, hook)

    def test_owned_hook_joins_foreman_envelope_but_stays_source_only(self):
        foreman = self._foreman("fwh_envelope_foreman")
        child = self._child(foreman, "fwh_envelope_child")
        owned = gs.create_webhook(
            "fwh_envelope_owned", actor=foreman["name"])
        second_owned = gs.create_webhook(
            "fwh_envelope_second", actor=foreman["name"])
        human_hook = gs.create_webhook("fwh_envelope_human")

        edge = gs.create_edge(
            owned["_guid"], child["_guid"],
            max_turns=5, token_cap=1000, cost_cap=1,
            actor=foreman["name"])
        self.assertEqual(edge.get("created_by_guid"), foreman["_guid"])

        with self.assertRaises(gs.GraphError):
            gs.create_edge(
                human_hook["_guid"], child["_guid"],
                max_turns=5, token_cap=1000, cost_cap=1,
                actor=foreman["name"])
        self.assertIsNone(
            gs.authorizing_edge(human_hook["name"], child["name"]))

        with self.assertRaisesRegex(gs.GraphError, "source-only|webhook"):
            gs.create_edge(
                owned["_guid"], second_owned["_guid"],
                max_turns=5, token_cap=1000, cost_cap=1,
                actor=foreman["name"])
        with self.assertRaisesRegex(gs.GraphError, "source-only|webhook"):
            gs.create_edge(
                child["_guid"], owned["_guid"],
                max_turns=5, token_cap=1000, cost_cap=1,
                actor=foreman["name"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
