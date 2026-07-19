#!/usr/bin/env python3
"""tests/live_smoke.py — rerunnable live write-path smoke check for the `crew`
CLI, run against the configured persistent MorphDB app "crew" (not a throwaway app —
see SKILL.md's schema-drift lesson: a freshly-created app is never stale by
construction, so only the real app can catch drift).

Exercises: spawn-agent --no-launch, connect (with token_cap/cost_cap — the
schema-drift regression), peers, edges, safe durable queueing while no runtime
is running, disconnect, down/up/restart, remove-agent.

Safe to rerun any number of times: every agent it touches is named "test_smoke_*"
and is removed (agent record + tmux session) in a `finally` block, even on
failure or Ctrl-C. It does not require any pre-seeded agents and NEVER passes
--all to a lifecycle command.

Usage:
    python3 tests/live_smoke.py

Exit code 0 if every check passed, 1 otherwise (still cleans up either way).
"""
import os
import shutil
import subprocess
import sys
import time

from operator_harness import run_operator

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CREW_BIN = os.path.join(ROOT, "bin", "crew")
HOME_BASE = "/tmp/crew_tests"
REAL_AGENTS = {"leads", "builder", "sales", "AgentA", "AgentB"}

sys.path.insert(0, ROOT)
from crew import config, graphstore as gs  # noqa: E402

# WAVE 0 — projects: a throwaway project, never the real "crew" app.
W0_PROJECT = f"w0demo{os.getpid()}"
W0_APP = f"crew-{W0_PROJECT}"
W0_AGENT = f"test_w0_a_{os.getpid()}"

RUN_ID = f"{int(time.time())}_{os.getpid()}"
A = f"test_smoke_a_{RUN_ID}"
B = f"test_smoke_b_{RUN_ID}"
C = f"test_smoke_c_{RUN_ID}"  # unconnected outsider, for the blocked-message check
W0_HOME_BASE = os.path.join(HOME_BASE, f"w0root_{RUN_ID}")
NOEXEC_MARKER = f"/tmp/crew_smoke_noexec_{os.getpid()}"

_results = []  # (name, ok, detail)


def _check(name, ok, detail=""):
    _results.append((name, ok, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not ok else ""))
    return ok


def _run(args, env_extra=None, timeout=30):
    p = run_operator(
        [sys.executable, CREW_BIN, *args], cwd=ROOT, env_extra=env_extra,
        capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


def _tmux_has_session(name):
    return subprocess.run(
        config.tmux_command("has-session", "-t", f"={name}"),
        env=config.tmux_environment(), capture_output=True,
        timeout=5).returncode == 0


def _kill_tmux_session(name):
    return subprocess.run(
        config.tmux_command("kill-session", "-t", f"={name}"),
        env=config.tmux_environment(), capture_output=True)


def _cleanup():
    """Idempotent, never raises: safe to call from `finally` regardless of how
    far setup got. Only ever touches test_smoke_-prefixed names."""
    print("\ncleanup:")
    for name in (A, B, C):
        assert name.startswith("test_smoke_") and name not in REAL_AGENTS
        rc, out, err = _run(["remove-agent", name], timeout=15)
        if rc == 0:
            print(f"  removed {name}")
        elif "no such agent" in (err or ""):
            pass  # never existed / already removed — fine
        else:
            print(f"  WARN: remove-agent {name} → rc={rc} err={err.strip()!r}")
        if _tmux_has_session(name):
            _kill_tmux_session(name)
            print(f"  killed leftover tmux session {name}")
        shutil.rmtree(os.path.join(HOME_BASE, name), ignore_errors=True)
    try:
        os.unlink(NOEXEC_MARKER)
    except FileNotFoundError:
        pass


def _wave0_cleanup():
    """Idempotent, never raises: safe to call from `finally`. Only ever touches
    the per-run w0demo* project / test_w0_-prefixed agent — never the real
    'crew' app or its agents."""
    print("\nWAVE 0 cleanup:")
    assert W0_AGENT.startswith("test_w0_") and W0_AGENT not in REAL_AGENTS
    rc, out, err = _run(["remove-agent", W0_AGENT], env_extra={"CREW_PROJECT": W0_PROJECT}, timeout=15)
    if rc == 0:
        print(f"  removed {W0_AGENT}")
    elif "no such agent" in (err or ""):
        pass  # never existed / already removed — fine
    else:
        print(f"  WARN: remove-agent {W0_AGENT} → rc={rc} err={err.strip()!r}")
    sess = f"{W0_PROJECT}__{W0_AGENT}"
    if _tmux_has_session(sess):
        _kill_tmux_session(sess)
        print(f"  killed leftover tmux session {sess}")
    app_gone = False
    try:
        gs._req("DELETE", f"/app/{W0_APP}", app=None)
        app_gone = True
        print(f"  deleted app '{W0_APP}'")
    except gs.GraphError as e:
        if "404" in str(e):
            app_gone = True
        else:
            print(f"  WARN: delete app {W0_APP} → {e}")
    # Only hide the tenant from global home-ownership scans after its backend
    # app is confirmed absent. If MorphDB is unavailable, retain the registry
    # entry so a future scan cannot miss a possibly-live agent home.
    if app_gone:
        try:
            if config.unregister_project(W0_PROJECT):
                print(f"  unregistered project '{W0_PROJECT}'")
        except (OSError, ValueError) as e:
            print(f"  WARN: unregister project {W0_PROJECT} → {e}")
    shutil.rmtree(W0_HOME_BASE, ignore_errors=True)


def _check_wave0_projects():
    """WAVE 0 spec: `crew project create` + `crew --project X spawn-agent` land
    in an isolated MorphDB app (`crew-w0demo*`) and an isolated home subtree,
    and the agent is invisible to the plain (default
    project) 'crew agents' — never assumes the default app has seed data."""
    print("\nWAVE 0 — projects (app-key-per-project):")
    try:
        rc, out, err = _run(["project", "create", W0_PROJECT])
        _check(f"crew project create {W0_PROJECT}", rc == 0 and W0_APP in out,
              f"rc={rc} out={out!r} err={err!r}")

        try:
            sch = gs._req("GET", "/schema/agent", app=W0_APP)
            has_schema = isinstance(sch, dict) and "fields" in sch
        except gs.GraphError as e:
            has_schema, sch = False, str(e)
        _check(f"{W0_APP} has the agent schema", has_schema, f"schema={sch!r}")

        env = {"CREW_PROJECT": W0_PROJECT, "CREW_ROOT": W0_HOME_BASE}
        rc, out, err = _run(["spawn-agent", W0_AGENT, "--no-launch",
                             "--launch-cmd", "true"], env_extra=env)
        _check(f"spawn-agent under --project {W0_PROJECT} (via $CREW_PROJECT)",
              rc == 0 and f"spawned agent '{W0_AGENT}'" in out,
              f"rc={rc} out={out!r} err={err!r}")

        sess = f"{W0_PROJECT}__{W0_AGENT}"
        _check(f"tmux session is project-prefixed ({sess!r})",
              _tmux_has_session(sess))

        rc, out, err = _run(["agents"])  # default project → real app "crew"
        _check(f"plain 'crew agents' does NOT list the {W0_PROJECT} agent",
              rc == 0 and W0_AGENT not in out, f"out={out!r}")
        _check("plain 'crew agents' works with the existing default baseline",
              rc == 0, f"out={out!r} err={err!r}")

        try:
            res = gs._req("GET", "/objects/agent", app=W0_APP) or {}
            rows = res.get("objects", [])
        except gs.GraphError as e:
            rows = []
        row = next((r for r in rows if r.get("name") == W0_AGENT), None)
        _check(f"agent row exists in {W0_APP} ONLY", row is not None, f"rows={rows!r}")
        expected_home = os.path.realpath(os.path.join(W0_HOME_BASE, W0_PROJECT, W0_AGENT))
        _check(f"home lands under crew_root/{W0_PROJECT}/",
              bool(row) and row.get("home") == expected_home,
              f"home={row.get('home') if row else None!r} expected={expected_home!r}")
        _check("session field stored project-prefixed",
              bool(row) and row.get("session") == sess,
              f"session={row.get('session') if row else None!r}")
    finally:
        _wave0_cleanup()


def main():
    print(f"live_smoke: run id {RUN_ID}, app 'crew' at "
         f"{os.environ.get('MORPHDB_HOST', '127.0.0.1:8787')}")
    try:
        _check_wave0_projects()

        print("\nspawn-agent --no-launch:")
        rc, out, err = _run(["spawn-agent", A, "--home", os.path.join(HOME_BASE, A),
                             "--role", "smoke test agent", "--no-launch"])
        _check("spawn A", rc == 0 and f"spawned agent '{A}'" in out, f"rc={rc} out={out!r} err={err!r}")
        _check("spawn A creates a real (bare-bash) tmux session", _tmux_has_session(A))

        rc, out, err = _run(["spawn-agent", B, "--home", os.path.join(HOME_BASE, B),
                             "--no-launch"])
        _check("spawn B", rc == 0 and f"spawned agent '{B}'" in out, f"rc={rc} out={out!r} err={err!r}")

        rc, out, err = _run(["spawn-agent", C, "--home", os.path.join(HOME_BASE, C),
                             "--no-launch"])
        _check("spawn C (unconnected outsider)", rc == 0, f"rc={rc} out={out!r} err={err!r}")

        print("\nconnect (schema-drift regression: token_cap/cost_cap against the REAL app):")
        rc, out, err = _run(["connect", A, B, "--label", "smoke-link",
                             "--when", "when smoke-testing",
                             "--token-cap", "2000", "--cost-cap", "1.25"])
        _check("connect A->B with budget caps", rc == 0 and f"connected {A} -> {B}" in out,
              f"rc={rc} out={out!r} err={err!r}")

        rc, out, err = _run(["edges"])
        _check("edges shows the link + budget round-tripped",
              rc == 0 and f"{A} -> {B}" in out and "2,000 tok/hr" in out and "$1.25/hr" in out,
              f"out={out!r}")

        rc, out, err = _run(["peers", A])
        _check("peers A lists B as messageable", rc == 0 and f"→ {B}" in out, f"out={out!r}")

        print("\nbudget + message safety (bare shell is never a runtime input pane):")
        try:
            os.unlink(NOEXEC_MARKER)
        except FileNotFoundError:
            pass
        rc, out, err = _run(
            ["message", B, "touch", NOEXEC_MARKER],
            env_extra={"CREW_AGENT": A}, timeout=20)
        _check("unavailable usage meter fails the configured budget closed",
              rc == 1 and "budget unavailable" in err,
              f"rc={rc} out={out!r} err={err!r}")
        rc, out, err = _run(
            ["cap", A, B, "--token-cap", "0", "--cost-cap", "0"])
        _check("operator can remove the test-only usage caps after verification",
              rc == 0 and "token_cap:" in out and "cost_cap:" in out,
              f"rc={rc} out={out!r} err={err!r}")
        rc, out, err = _run(
            ["message", B, "touch", NOEXEC_MARKER],
            env_extra={"CREW_AGENT": A}, timeout=20)
        _check("authorized message queues until B's runtime starts",
              rc == 0 and "queued for" in out and "delivered to" not in out,
              f"rc={rc} out={out!r} err={err!r}")
        _check("message text never executes in B's bare shell",
              not os.path.exists(NOEXEC_MARKER))

        rc, out, err = _run(["mail", B, "--status", "queued", "-n", "10"])
        _check("mail log shows the durable queued message",
              rc == 0 and f"{A} → {B}" in out and "queued" in out,
              f"out={out!r}")

        rc, out, err = _run(["message", B, "hello"], env_extra={"CREW_AGENT": C}, timeout=20)
        _check("unauthorized (outsider) message is BLOCKED", rc == 1 and "BLOCKED" in err, f"rc={rc} err={err!r}")

        print("\ndisconnect:")
        rc, out, err = _run(["disconnect", A, B])
        _check("disconnect A B", rc == 0 and "disconnected" in out, f"out={out!r}")
        rc, out, err = _run(["edges"])
        _check("edge gone after disconnect", rc == 0 and f"{A} -> {B}" not in out, f"out={out!r}")

        print("\ndown / up / restart (never --all — real agents must be untouched):")
        rc, out, err = _run(["down", A])
        _check("down A", rc == 0 and f"{A}  stopped" in out, f"out={out!r}")
        _check("session actually gone after down", not _tmux_has_session(A))

        rc, out, err = _run(["up", A])
        _check("up A revives it", rc == 0 and f"{A}  started" in out, f"out={out!r}")
        _check("session actually back after up", _tmux_has_session(A))

        rc, out, err = _run(["restart", A])
        _check("restart A", rc == 0 and f"{A}  restarted" in out, f"out={out!r}")

        print("\nremove-agent:")
        rc, out, err = _run(["remove-agent", B])
        _check("remove-agent B", rc == 0 and f"removed agent '{B}'" in out and "killed session" in out,
              f"out={out!r}")
        _check("B's tmux session is gone", not _tmux_has_session(B))
        rc, out, err = _run(["agents"])
        _check("B no longer listed", rc == 0 and B not in out, f"out={out!r}")

    finally:
        _cleanup()

    failed = [n for n, ok, _ in _results if not ok]
    print(f"\n{len(_results) - len(failed)}/{len(_results)} checks passed.")
    if failed:
        print("FAILED: " + ", ".join(failed))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
