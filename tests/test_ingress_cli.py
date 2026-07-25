"""CLI contract tests for foreground public webhook ingress."""

import contextlib
import io
import unittest
from unittest import mock

from crew import cli, graphstore as gs, ingress, ingress_state


class _Lease:
    def __init__(self, events):
        self.events = events
        self.origin = "http://127.0.0.1:8787"
        self.app = "crew-example"
        self.state_dir = "/private/crew-ingress-test"
        self.config_path = (
            "/private/crew-ingress-test/example.cf.json")
        self.published = []

    def __enter__(self):
        self.events.append("lease.enter")
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.events.append(("lease.exit", exc_type))
        return False

    def publish(self, public_url):
        self.events.append("lease.publish")
        self.published.append(public_url)

    def clear(self):
        self.events.append("lease.clear")


class IngressCliTests(unittest.TestCase):
    def test_parser_exposes_run_and_status(self):
        parser = cli.build_parser()
        run = parser.parse_args(["ingress", "run"])
        status = parser.parse_args(["ingress", "status"])
        self.assertIs(run.fn, cli.cmd_ingress)
        self.assertEqual("run", run.action)
        self.assertIs(status.fn, cli.cmd_ingress)
        self.assertEqual("status", status.action)

    def test_run_holds_exact_scope_lease_before_runner_and_publication(self):
        events = []
        lease = _Lease(events)

        def acquire():
            events.append("lease.acquire")
            return lease

        def run_ingress(**kwargs):
            events.append("runner.start")
            self.assertEqual(lease.state_dir, kwargs["runtime_dir"])
            self.assertEqual(lease.config_path, kwargs["config_path"])
            self.assertEqual(lease.origin, kwargs["morphdb_origin"])
            self.assertEqual(lease.app, kwargs["app"])
            self.assertEqual([], lease.published)
            kwargs["on_ready"](
                "https://public-demo.trycloudflare.com")
            kwargs["on_stopping"]()
            events.append("runner.stop")

        output = io.StringIO()
        args = cli.build_parser().parse_args(["ingress", "run"])
        with mock.patch.object(cli, "_ACTOR", "human"), \
             mock.patch.object(ingress_state, "acquire_lease", acquire), \
             mock.patch.object(ingress, "run_ingress", run_ingress), \
             contextlib.redirect_stdout(output):
            self.assertEqual(0, args.fn(args))

        self.assertEqual(
            [
                "lease.acquire",
                "lease.enter",
                "runner.start",
                "lease.publish",
                "lease.clear",
                "runner.stop",
                ("lease.exit", None),
            ],
            events,
        )
        self.assertEqual(
            ["https://public-demo.trycloudflare.com"],
            lease.published,
        )
        rendered = output.getvalue()
        self.assertIn(
            "online → https://public-demo.trycloudflare.com", rendered)
        self.assertIn("crew webhook show <name>", rendered)
        self.assertIn("stopped", rendered)
        self.assertNotIn("/hooks/", rendered)

    def test_run_failure_releases_lease_and_translates_expected_error(self):
        events = []
        lease = _Lease(events)

        def fail(**_kwargs):
            events.append("runner.start")
            raise ingress.IngressError("tunnel launch failed")

        args = cli.build_parser().parse_args(["ingress", "run"])
        with mock.patch.object(cli, "_ACTOR", "human"), \
             mock.patch.object(
                 ingress_state, "acquire_lease", return_value=lease), \
             mock.patch.object(ingress, "run_ingress", fail), \
             self.assertRaisesRegex(gs.GraphError, "tunnel launch failed"):
            args.fn(args)
        self.assertEqual(
            ["lease.enter", "runner.start",
             ("lease.exit", ingress.IngressError)],
            events,
        )

    def test_run_is_human_only_and_never_acquires_for_an_agent(self):
        args = cli.build_parser().parse_args(["ingress", "run"])
        with mock.patch.object(cli, "_ACTOR", "worker"), \
             mock.patch.object(
                 gs, "get_agent_by_name",
                 return_value={
                     "_guid": "worker-guid",
                     "name": "worker",
                     "can_edit_graph": True,
                 }), \
             mock.patch.object(cli.guard, "audit") as audit, \
             mock.patch.object(ingress_state, "acquire_lease") as acquire:
            with self.assertRaisesRegex(gs.GraphError, "human operator"):
                args.fn(args)
        audit.assert_called_once()
        acquire.assert_not_called()

    def test_status_is_read_only_and_never_prints_a_hook_capability(self):
        args = cli.build_parser().parse_args(["ingress", "status"])
        online = {
            "public_base_url":
                "https://public-demo.trycloudflare.com",
        }
        output = io.StringIO()
        with mock.patch.object(cli, "_ACTOR", "worker"), \
             mock.patch.object(
                 ingress_state, "read_active_state",
                 return_value=online), \
             mock.patch.object(cli.guard, "check") as check, \
             contextlib.redirect_stdout(output):
            self.assertEqual(0, args.fn(args))
        self.assertEqual(
            "public webhook ingress online → "
            "https://public-demo.trycloudflare.com\n",
            output.getvalue(),
        )
        self.assertNotIn("/hooks/", output.getvalue())
        check.assert_not_called()

        output = io.StringIO()
        with mock.patch.object(
                ingress_state, "read_active_state", return_value=None), \
             contextlib.redirect_stdout(output):
            self.assertEqual(0, args.fn(args))
        self.assertEqual("public webhook ingress offline\n", output.getvalue())


if __name__ == "__main__":
    unittest.main()
