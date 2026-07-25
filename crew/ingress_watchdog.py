"""Exact-child watchdog for Crew's foreground public ingress.

The foreground CLI retains the write side of a private pipe.  This process
retains the read side and owns the cloudflared child.  EOF therefore proves
that the CLI exited, including via SIGKILL, without relying on a guessed PID or
process name.  The watchdog then terminates and reaps only its exact child.
"""

import argparse
import os
import select
import signal
import subprocess
import threading


POLL_INTERVAL = 0.1
DEFAULT_SHUTDOWN_TIMEOUT = 5.0


def _parent_closed(parent_fd, timeout):
    readable, _, _ = select.select([parent_fd], [], [], max(0.0, timeout))
    if not readable:
        return False
    try:
        return os.read(parent_fd, 1) == b""
    except InterruptedError:
        return False


def _stop_child(child, timeout):
    if child.poll() is not None:
        return child.wait()
    try:
        child.terminate()
    except ProcessLookupError:
        return child.wait()
    try:
        return child.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        child.kill()
        return child.wait(timeout=timeout)


def supervise(
        parent_fd, child_argv, *,
        shutdown_timeout=DEFAULT_SHUTDOWN_TIMEOUT,
        popen_factory=subprocess.Popen):
    """Supervise one exact child until it exits or the owning CLI disappears."""
    if (
            type(parent_fd) is not int or parent_fd < 0
            or not child_argv
            or not all(isinstance(value, str) and value for value in child_argv)
            or isinstance(shutdown_timeout, bool)
            or not isinstance(shutdown_timeout, (int, float))
            or shutdown_timeout <= 0):
        raise ValueError("invalid watchdog arguments")

    requested_stop = threading.Event()

    def request_stop(_signum, _frame):
        requested_stop.set()

    for signum in filter(
            None,
            (
                getattr(signal, "SIGINT", None),
                getattr(signal, "SIGTERM", None),
                getattr(signal, "SIGHUP", None),
            )):
        signal.signal(signum, request_stop)

    os.set_inheritable(parent_fd, False)
    child = None
    try:
        # Parent death can race the watchdog's own exec. Never create a child
        # after the only process authorized to own it is already gone.
        if _parent_closed(parent_fd, 0):
            return 0
        child = popen_factory(
            list(child_argv),
            stdin=subprocess.DEVNULL,
            stdout=None,
            stderr=None,
            shell=False,
            close_fds=True,
        )
        while True:
            returncode = child.poll()
            if returncode is not None:
                return int(returncode)
            if requested_stop.is_set() or _parent_closed(
                    parent_fd, POLL_INTERVAL):
                _stop_child(child, float(shutdown_timeout))
                return 0
    finally:
        try:
            os.close(parent_fd)
        except OSError:
            pass
        if child is not None and child.poll() is None:
            _stop_child(child, float(shutdown_timeout))


def _build_parser():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--parent-fd", required=True, type=int)
    parser.add_argument(
        "--shutdown-timeout",
        type=float,
        default=DEFAULT_SHUTDOWN_TIMEOUT,
    )
    parser.add_argument("child", nargs=argparse.REMAINDER)
    return parser


def main(argv=None):
    args = _build_parser().parse_args(argv)
    child = list(args.child)
    if child[:1] == ["--"]:
        child = child[1:]
    if not child:
        return 2
    try:
        return supervise(
            args.parent_fd,
            child,
            shutdown_timeout=args.shutdown_timeout,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return 111


if __name__ == "__main__":
    raise SystemExit(main())
