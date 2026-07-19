"""GRANTS WAVE tests: `crew grant`/`crew revoke-grant`/`crew grants` — a
symlink + declared-intent exception to an agent's workspace boundary. Two
honesty-labeled layers per the settled spec: the symlink + agent.grants field
+ identity.md/CLAUDE.md rendering are all REAL side effects (discoverability +
declared intent); nothing is filesystem-enforced (no sandbox) — see
crew.identity.render_file_grants for the exact honesty label asserted below.

Permission shape (crew.guard): human-only. A foreman's `grant` attempt is
queued to the WAVE 4 pending machinery (op="grant") instead of refused outright
— replayed by approve_pending via crew.spawn.grant_path's `_pre_approved`
escape hatch, same pattern as connect/update_edge. `revoke_grant` stays
human-only with no pending exception (same tier as remove/bless/foreman).

Three layers, per SKILL.md:
  * unit — a throwaway MorphDB app (`crewtest-grants-unit`), registered in
    setUpModule and cascade-deleted in tearDownModule. Agent homes + grant
    target dirs live under /tmp/crew_granttest*/ (real symlinks are created —
    this is genuine filesystem I/O, not mocked).
  * live — a throwaway project ("wgtest"), driving the real CLI
    (`./bin/crew`) end-to-end against a real --no-launch spawned agent: `crew
    grant`, `ls <home>/refs/` shows the link, reading THROUGH the symlink
    returns the real target's content, `crew revoke-grant` cleans up.
  * regression — full `python3 -m unittest discover tests` stays green.

    python3 -m unittest tests.test_grants          (from the repo root)
    python3 -m unittest discover tests                (full suite)
"""
import contextlib
import io
import os
import shutil
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_APP = "crewtest-grants-unit"
os.environ["CREW_APP"] = TEST_APP

from crew import cli, config, graphstore as gs, guard, schema, spawn  # noqa: E402

HOME_BASE = "/tmp/crew_granttest"
TARGET_BASE = "/tmp/crew_granttest_targets"


def setUpModule():
    os.environ["CREW_APP"] = TEST_APP
    try:
        gs._req("DELETE", f"/app/{TEST_APP}", app=None)
    except gs.GraphError:
        pass
    schema.ensure_schema(TEST_APP)
    shutil.rmtree(HOME_BASE, ignore_errors=True)
    shutil.rmtree(TARGET_BASE, ignore_errors=True)


def tearDownModule():
    try:
        gs._req("DELETE", f"/app/{TEST_APP}", app=None)
    except gs.GraphError:
        pass
    shutil.rmtree(HOME_BASE, ignore_errors=True)
    shutil.rmtree(TARGET_BASE, ignore_errors=True)


def _agent(name, **kw):
    home = os.path.join(HOME_BASE, name)
    return gs.create_agent(name, home=home, **kw)


def _foreman(name):
    return _agent(name, can_edit_graph=True)


def _target_dir(name):
    d = os.path.join(TARGET_BASE, name)
    os.makedirs(d, exist_ok=True)
    return d


def _audit_rows(actor=None, op=None, result=None):
    res = gs.list_objects("graph_edit", limit=1000, sort="created_at", order="desc")
    rows = (res or {}).get("objects", [])
    if actor is not None:
        rows = [r for r in rows if r.get("actor") == actor]
    if op is not None:
        rows = [r for r in rows if r.get("op") == op]
    if result is not None:
        rows = [r for r in rows if r.get("result") == result]
    return rows


def _pending_rows(actor=None):
    return _audit_rows(actor=actor, result="pending")


def _identity_text(home):
    with open(os.path.join(home, config.IDENTITY_FILE)) as f:
        return f.read()


def _claude_text(home):
    with open(os.path.join(home, "CLAUDE.md")) as f:
        return f.read()


# --------------------------------------------------------------------------- #
# unit — human grant: symlink + entry + identity + audit
# --------------------------------------------------------------------------- #
class HumanGrantTests(unittest.TestCase):
    def test_grant_creates_symlink_pointing_at_target(self):
        a = _agent("gr_a1")
        target = _target_dir("gr_target1")
        entry = spawn.grant_path("gr_a1", target, actor="human")
        link = os.path.join(a["home"], "refs", entry["name"])
        self.assertTrue(os.path.islink(link))
        self.assertEqual(os.path.realpath(link), os.path.realpath(target))

    def test_grant_entry_fields_default_mode_ro(self):
        a = _agent("gr_a2")
        target = _target_dir("gr_target2")
        entry = spawn.grant_path("gr_a2", target, actor="human")
        self.assertEqual(entry["mode"], "ro")
        self.assertEqual(entry["type"], "path")
        self.assertEqual(entry["granted_by"], "human")
        self.assertEqual(entry["path"], os.path.realpath(target))
        self.assertIn("created_at", entry)
        refreshed = gs.get_agent_by_name("gr_a2")
        grants = refreshed.get("grants") or []
        self.assertEqual(len(grants), 1)
        self.assertEqual(grants[0]["name"], entry["name"])
        self.assertEqual(grants[0]["mode"], "ro")

    def test_rw_mode_recorded(self):
        a = _agent("gr_a3")
        target = _target_dir("gr_target3")
        entry = spawn.grant_path("gr_a3", target, mode="rw", actor="human")
        self.assertEqual(entry["mode"], "rw")
        refreshed = gs.get_agent_by_name("gr_a3")
        self.assertEqual(refreshed["grants"][0]["mode"], "rw")

    def test_identity_md_has_file_grants_section_and_amended_boundary(self):
        a = _agent("gr_a4")
        target = _target_dir("gr_target4")
        entry = spawn.grant_path("gr_a4", target, actor="human")
        text = _identity_text(a["home"])
        self.assertIn("## File grants", text)
        self.assertIn(f"refs/{entry['name']}", text)
        self.assertIn(os.path.realpath(target), text)
        self.assertIn("(ro)", text)
        self.assertIn("unless a grant below explicitly authorizes a specific path",
                      text)
        # honesty label present verbatim-ish
        self.assertIn("not filesystem", text.lower())

    def test_claude_md_also_has_file_grants_section(self):
        a = _agent("gr_a4b")
        target = _target_dir("gr_target4b")
        entry = spawn.grant_path("gr_a4b", target, actor="human")
        text = _claude_text(a["home"])
        self.assertIn("## File grants", text)
        self.assertIn(f"refs/{entry['name']}", text)

    def test_audit_row_applied(self):
        a = _agent("gr_a5")
        target = _target_dir("gr_target5")
        spawn.grant_path("gr_a5", target, actor="human")
        rows = _audit_rows(actor="human", op="grant", result="applied")
        self.assertTrue(rows)

    def test_grant_inside_own_home_refused_with_teaching_message(self):
        a = _agent("gr_a6")
        sub = os.path.join(a["home"], "sub")
        os.makedirs(sub, exist_ok=True)
        with self.assertRaises(gs.GraphError) as ctx:
            spawn.grant_path("gr_a6", sub, actor="human")
        msg = str(ctx.exception).lower()
        self.assertIn("own home", msg)
        self.assertIn("already", msg)
        refreshed = gs.get_agent_by_name("gr_a6")
        self.assertEqual(refreshed.get("grants") or [], [])

    def test_name_collision_dedupes_with_suffix(self):
        a = _agent("gr_a7")
        base = os.path.join(TARGET_BASE, "dup_parent")
        t1 = os.path.join(base, "one", "dup")
        t2 = os.path.join(base, "two", "dup")
        t3 = os.path.join(base, "three", "dup")
        for t in (t1, t2, t3):
            os.makedirs(t, exist_ok=True)
        e1 = spawn.grant_path("gr_a7", t1, actor="human")
        e2 = spawn.grant_path("gr_a7", t2, actor="human")
        e3 = spawn.grant_path("gr_a7", t3, actor="human")
        self.assertEqual(e1["name"], "dup")
        self.assertEqual(e2["name"], "dup-2")
        self.assertEqual(e3["name"], "dup-3")
        link1 = os.path.join(a["home"], "refs", "dup")
        link2 = os.path.join(a["home"], "refs", "dup-2")
        self.assertEqual(os.path.realpath(link1), os.path.realpath(t1))
        self.assertEqual(os.path.realpath(link2), os.path.realpath(t2))

    def test_invalid_mode_rejected(self):
        a = _agent("gr_a8")
        target = _target_dir("gr_target8")
        with self.assertRaises(gs.GraphError):
            spawn.grant_path("gr_a8", target, mode="bogus", actor="human")


# --------------------------------------------------------------------------- #
# unit — revoke
# --------------------------------------------------------------------------- #
class RevokeGrantTests(unittest.TestCase):
    def test_revoke_removes_symlink_entry_and_section_when_last(self):
        a = _agent("rv_a1")
        target = _target_dir("rv_target1")
        entry = spawn.grant_path("rv_a1", target, actor="human")
        link = os.path.join(a["home"], "refs", entry["name"])
        self.assertTrue(os.path.islink(link))
        spawn.revoke_grant("rv_a1", entry["name"], actor="human")
        self.assertFalse(os.path.lexists(link))
        refreshed = gs.get_agent_by_name("rv_a1")
        self.assertEqual(refreshed.get("grants") or [], [])
        text = _identity_text(a["home"])
        self.assertNotIn("## File grants", text)
        rows = _audit_rows(actor="human", op="revoke_grant", result="applied")
        self.assertTrue(rows)

    def test_revoke_missing_symlink_is_fine(self):
        a = _agent("rv_a2")
        target = _target_dir("rv_target2")
        entry = spawn.grant_path("rv_a2", target, actor="human")
        link = os.path.join(a["home"], "refs", entry["name"])
        os.remove(link)  # simulate it already gone
        spawn.revoke_grant("rv_a2", entry["name"], actor="human")  # must not raise
        refreshed = gs.get_agent_by_name("rv_a2")
        self.assertEqual(refreshed.get("grants") or [], [])

    def test_revoke_unknown_name_raises(self):
        a = _agent("rv_a3")
        with self.assertRaises(gs.GraphError):
            spawn.revoke_grant("rv_a3", "nope", actor="human")

    def test_revoke_keeps_section_when_others_remain(self):
        a = _agent("rv_a4")
        t1 = _target_dir("rv_target4a")
        t2 = _target_dir("rv_target4b")
        e1 = spawn.grant_path("rv_a4", t1, actor="human")
        spawn.grant_path("rv_a4", t2, actor="human")
        spawn.revoke_grant("rv_a4", e1["name"], actor="human")
        refreshed = gs.get_agent_by_name("rv_a4")
        self.assertEqual(len(refreshed.get("grants") or []), 1)
        text = _identity_text(a["home"])
        self.assertIn("## File grants", text)

    def test_non_human_revoke_refused(self):
        f = _foreman("rv_f1")
        a = _agent("rv_a5")
        entry = spawn.grant_path("rv_a5", _target_dir("rv_target5"), actor="human")
        with self.assertRaises(gs.GraphError) as ctx:
            spawn.revoke_grant("rv_a5", entry["name"], actor="rv_f1")
        self.assertIn("human", str(ctx.exception).lower())
        refreshed = gs.get_agent_by_name("rv_a5")
        self.assertEqual(len(refreshed.get("grants") or []), 1)  # unchanged


# --------------------------------------------------------------------------- #
# unit — grants listing (read-only, no gate)
# --------------------------------------------------------------------------- #
class GrantsListingTests(unittest.TestCase):
    def test_cli_grants_listing_shows_name_path_mode_age(self):
        a = _agent("ls_a1")
        target = _target_dir("ls_target1")
        entry = spawn.grant_path("ls_a1", target, mode="rw", actor="human")
        p = cli.build_parser()
        buf = io.StringIO()
        with mock.patch.object(cli, "_ACTOR", "human"), contextlib.redirect_stdout(buf):
            args = p.parse_args(["grants", "ls_a1"])
            rc = args.fn(args)
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("ls_a1", out)
        self.assertIn(entry["name"], out)
        self.assertIn(os.path.realpath(target), out)
        self.assertIn("rw", out)

    def test_cli_grants_listing_no_gate_for_agent_actor(self):
        # read-only listing — no guard.check at all, so even a plain agent may
        # run it without being refused.
        _agent("ls_a2")
        spawn.grant_path("ls_a2", _target_dir("ls_target2"), actor="human")
        p = cli.build_parser()
        buf = io.StringIO()
        with mock.patch.object(cli, "_ACTOR", "ls_a2"), contextlib.redirect_stdout(buf):
            args = p.parse_args(["grants"])
            rc = args.fn(args)
        self.assertEqual(rc, 0)


# --------------------------------------------------------------------------- #
# unit — foreman grant -> pending, no side effects
# --------------------------------------------------------------------------- #
class ForemanGrantPendingTests(unittest.TestCase):
    def test_foreman_grant_yields_pending_no_side_effects(self):
        f = _foreman("pd_f1")
        target_agent = _agent("pd_target1")
        target = _target_dir("pd_targetdir1")
        with self.assertRaises(gs.GraphError) as ctx:
            spawn.grant_path("pd_target1", target, actor="pd_f1")
        msg = str(ctx.exception).lower()
        self.assertIn("queued", msg)
        refreshed = gs.get_agent_by_name("pd_target1")
        self.assertEqual(refreshed.get("grants") or [], [])
        self.assertFalse(os.path.isdir(os.path.join(target_agent["home"], "refs")))
        rows = _pending_rows(actor="pd_f1")
        self.assertTrue(any(r.get("op") == "grant" for r in rows), rows)
        refused = _audit_rows(actor="pd_f1", op="grant", result="refused")
        self.assertEqual(refused, [])

    def test_pending_row_captures_full_grant_args(self):
        f = _foreman("pd_f2")
        _agent("pd_target2")
        target = _target_dir("pd_targetdir2")
        with self.assertRaises(gs.GraphError):
            spawn.grant_path("pd_target2", target, mode="rw", actor="pd_f2")
        rows = _pending_rows(actor="pd_f2")
        row = next(r for r in rows if r.get("op") == "grant")
        args = row.get("args") or {}
        self.assertEqual(args.get("name"), "pd_target2")
        self.assertEqual(args.get("path"), os.path.realpath(target))
        self.assertEqual(args.get("mode"), "rw")


# --------------------------------------------------------------------------- #
# unit — approve / reject a pending grant
# --------------------------------------------------------------------------- #
class ApproveRejectGrantTests(unittest.TestCase):
    def test_approve_grant_executes_full_side_effect_set(self):
        f = _foreman("ap_f1")
        target_agent = _agent("ap_target1")
        target = _target_dir("ap_targetdir1")
        with self.assertRaises(gs.GraphError):
            spawn.grant_path("ap_target1", target, actor="ap_f1")
        row = _pending_rows(actor="ap_f1")[0]
        guard.approve_pending(row["_guid"], actor="human")

        refreshed = gs.get_agent_by_name("ap_target1")
        grants = refreshed.get("grants") or []
        self.assertEqual(len(grants), 1)
        self.assertEqual(grants[0]["granted_by"], "human")

        link = os.path.join(target_agent["home"], "refs", grants[0]["name"])
        self.assertTrue(os.path.islink(link))
        self.assertEqual(os.path.realpath(link), os.path.realpath(target))

        text = _identity_text(target_agent["home"])
        self.assertIn("## File grants", text)

        applied = _audit_rows(op="grant", result="applied")
        self.assertTrue(applied)

        refreshed_row = gs.get_object(row["_guid"])
        self.assertEqual(refreshed_row.get("result"), "approved")

    def test_reject_grant_leaves_no_side_effects(self):
        f = _foreman("rj_f1")
        target_agent = _agent("rj_target1")
        target = _target_dir("rj_targetdir1")
        with self.assertRaises(gs.GraphError):
            spawn.grant_path("rj_target1", target, actor="rj_f1")
        row = _pending_rows(actor="rj_f1")[0]
        guard.reject_pending(row["_guid"], reason="not needed", actor="human")

        refreshed = gs.get_agent_by_name("rj_target1")
        self.assertEqual(refreshed.get("grants") or [], [])
        self.assertFalse(os.path.isdir(os.path.join(target_agent["home"], "refs")))
        refreshed_row = gs.get_object(row["_guid"])
        self.assertEqual(refreshed_row.get("result"), "rejected")


# --------------------------------------------------------------------------- #
# unit — non-foreman agent grant attempt -> plain refusal + audit
# --------------------------------------------------------------------------- #
class NonForemanGrantRefusalTests(unittest.TestCase):
    def test_plain_agent_grant_refused_not_pending(self):
        _agent("nf_plain1")
        target_agent = _agent("nf_target1")
        target = _target_dir("nf_targetdir1")
        with self.assertRaises(gs.GraphError) as ctx:
            spawn.grant_path("nf_target1", target, actor="nf_plain1")
        msg = str(ctx.exception).lower()
        self.assertNotIn("queued", msg)
        rows = _audit_rows(actor="nf_plain1", op="grant", result="refused")
        self.assertTrue(rows)
        pending = _pending_rows(actor="nf_plain1")
        self.assertEqual(pending, [])
        refreshed = gs.get_agent_by_name("nf_target1")
        self.assertEqual(refreshed.get("grants") or [], [])


# --------------------------------------------------------------------------- #
# unit — CLI grant/revoke-grant dispatch
# --------------------------------------------------------------------------- #
class GrantCliDispatchTests(unittest.TestCase):
    def test_cli_grant_and_revoke_grant_round_trip(self):
        a = _agent("cli_a1")
        target = _target_dir("cli_target1")
        p = cli.build_parser()
        with mock.patch.object(cli, "_ACTOR", "human"):
            args = p.parse_args(["grant", "cli_a1", target])
            self.assertEqual(args.fn(args), 0)
        refreshed = gs.get_agent_by_name("cli_a1")
        grants = refreshed.get("grants") or []
        self.assertEqual(len(grants), 1)
        name = grants[0]["name"]
        link = os.path.join(a["home"], "refs", name)
        self.assertTrue(os.path.islink(link))

        with mock.patch.object(cli, "_ACTOR", "human"):
            args = p.parse_args(["revoke-grant", "cli_a1", name])
            self.assertEqual(args.fn(args), 0)
        refreshed2 = gs.get_agent_by_name("cli_a1")
        self.assertEqual(refreshed2.get("grants") or [], [])
        self.assertFalse(os.path.lexists(link))

    def test_cli_grant_defaults_ro_rw_flag_works(self):
        _agent("cli_a2")
        target = _target_dir("cli_target2")
        p = cli.build_parser()
        with mock.patch.object(cli, "_ACTOR", "human"):
            args = p.parse_args(["grant", "cli_a2", target, "--rw"])
            self.assertEqual(args.fn(args), 0)
        refreshed = gs.get_agent_by_name("cli_a2")
        self.assertEqual(refreshed["grants"][0]["mode"], "rw")


# --------------------------------------------------------------------------- #
# live — throwaway project "wgtest", real CLI + real symlink read-through
# --------------------------------------------------------------------------- #
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CREW_BIN = os.path.join(ROOT, "bin", "crew")
LIVE_HOME_BASE = "/tmp/crew_tests_grants"
PROJECT = "wgtest"
PROJECT_APP = f"crew-{PROJECT}"


def _run(args, env_extra=None, timeout=30):
    env = dict(os.environ)
    env.pop("CREW_APP", None)
    env["CREW_PROJECT"] = PROJECT
    if env_extra:
        env.update(env_extra)
    p = subprocess.run([sys.executable, CREW_BIN, *args], cwd=ROOT, env=env,
                       capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


@unittest.skipUnless(os.environ.get("CREW_LIVE_TESTS", "1") == "1",
                     "set CREW_LIVE_TESTS=0 to skip live pane tests")
class LiveGrantCliTests(unittest.TestCase):
    def setUp(self):
        self.agent = "test_wg_gragent"
        self.home = os.path.join(LIVE_HOME_BASE, self.agent)
        self.target_dir = os.path.join(LIVE_HOME_BASE, "target")
        os.makedirs(self.target_dir, exist_ok=True)
        with open(os.path.join(self.target_dir, "hello.txt"), "w") as f:
            f.write("hello from grant target\n")

    def tearDown(self):
        try:
            _run(["remove-agent", self.agent], timeout=15)
        except Exception:
            pass
        try:
            gs._req("DELETE", f"/app/{PROJECT_APP}", app=None)
        except gs.GraphError:
            pass
        try:
            import json as _json
            names = [n for n in config.list_known_projects() if n != PROJECT]
            os.makedirs(config.VAR, exist_ok=True)
            with open(config._projects_file(), "w") as fh:
                _json.dump([n for n in names if n != config.DEFAULT_PROJECT], fh)
        except OSError:
            pass
        shutil.rmtree(LIVE_HOME_BASE, ignore_errors=True)

    def test_grant_revoke_round_trip_via_real_cli(self):
        rc, out, err = _run(["project", "create", PROJECT])
        self.assertEqual(rc, 0, f"project create failed: {out!r} {err!r}")

        rc, out, err = _run(["spawn-agent", self.agent, "--home", self.home,
                             "--launch-cmd", "true", "--no-launch"])
        self.assertEqual(rc, 0, f"spawn failed: {out!r} {err!r}")

        rc, out, err = _run(["grant", self.agent, self.target_dir])
        self.assertEqual(rc, 0, f"grant failed: {out!r} {err!r}")

        refs_dir = os.path.join(self.home, "refs")
        entries = os.listdir(refs_dir)
        self.assertEqual(len(entries), 1, entries)
        link_name = entries[0]
        link_path = os.path.join(refs_dir, link_name)
        self.assertTrue(os.path.islink(link_path))

        # cat THROUGH the symlink reads the real target's content
        with open(os.path.join(link_path, "hello.txt")) as f:
            content = f.read()
        self.assertEqual(content, "hello from grant target\n")

        rc, out, err = _run(["grants", self.agent])
        self.assertEqual(rc, 0, f"grants failed: {out!r} {err!r}")
        self.assertIn(link_name, out)

        rc, out, err = _run(["revoke-grant", self.agent, link_name])
        self.assertEqual(rc, 0, f"revoke-grant failed: {out!r} {err!r}")
        self.assertFalse(os.path.lexists(link_path))


if __name__ == "__main__":
    unittest.main(verbosity=2)
