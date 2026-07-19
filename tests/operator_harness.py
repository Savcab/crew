"""Shared isolation for CLI calls made from an operator test process.

Live tests are sometimes launched from inside a Crew-managed tmux pane.  A
child CLI must still behave like an operator shell unless a test deliberately
supplies an actor identity.  Tenant selectors are likewise opt-in so a parent
test or developer shell cannot silently redirect a live fixture.
"""

import os
import subprocess


_INHERITED_CREW_CONTEXT = (
    "TMUX",
    "TMUX_PANE",
    "CREW_AGENT",
    "AGENT_MAIL_NAME",
    "CREW_APP",
    "CREW_PROJECT",
    "CREW_ROOT",
)
_MISSING = object()


def operator_environment(overrides=None, *, environ=None):
    """Return a clean operator environment plus explicit test overrides."""
    environment = dict(os.environ if environ is None else environ)
    for key in _INHERITED_CREW_CONTEXT:
        environment.pop(key, None)
    if overrides:
        environment.update(overrides)
    return environment


def pin_environment(add_cleanup, overrides, *, environ=None):
    """Pin keys now and register exact restoration before callers can fail."""
    environment = os.environ if environ is None else environ
    previous = {
        key: environment.get(key, _MISSING)
        for key in overrides
    }

    def restore():
        for key, value in previous.items():
            if value is _MISSING:
                environment.pop(key, None)
            else:
                environment[key] = value

    add_cleanup(restore)
    environment.update(overrides)


def run_operator(command, *, env_extra=None, environ=None, **kwargs):
    """Run a detached operator CLI with inherited Crew context removed."""
    if "env" in kwargs:
        raise TypeError("pass env_extra, not env, to run_operator")
    if "start_new_session" in kwargs:
        raise TypeError("run_operator always starts a new session")
    return subprocess.run(
        command,
        env=operator_environment(env_extra, environ=environ),
        start_new_session=True,
        **kwargs,
    )
