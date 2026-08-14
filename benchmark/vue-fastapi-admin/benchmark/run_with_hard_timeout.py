#!/usr/bin/env python3
"""Run a command with a hard wall-clock timeout, killing its *entire* process
group (not just the top-level process) when the deadline is hit.

Why this exists:
  `cursor-agent` (and the headless driver that spawns it per phase) sometimes
  starts background SUT processes (frontend/backend dev servers) as children.
  In headless `--print` mode, cursor-agent may not exit while those children
  are still running, so a plain `timeout <n> cursor-agent ...` only kills
  cursor-agent itself and leaves the servers alive — or, on systems without
  `timeout`/`gtimeout`/`setsid` (e.g. this macOS box), the step hangs with
  no wall-clock cap.

This script avoids depending on `timeout`, `gtimeout`, or `setsid` binaries
(none of which are guaranteed to be installed) by using Python's own
subprocess machinery:
  - `start_new_session=True` puts the child in a new process group (POSIX
    setsid equivalent) so we can address the whole tree with killpg().
  - On timeout: SIGTERM the process group, wait a grace period, then
    SIGKILL any stragglers.
  - Even on a clean exit, we sweep the process group once more, since
    cursor-agent may have exited "successfully" while leaving background
    dev servers behind in the same group.

Usage:
  run_with_hard_timeout.py <timeout_seconds> <logfile> [--pgid-file <path>] -- <cmd> [args...]

`--pgid-file <path>` writes the child's process-group id to <path> as soon as
it is spawned. This lets an external supervisor (e.g. a bash loop polling a
separate readiness signal such as `aa status`) kill the whole group early -
before the hard timeout - once it independently determines the underlying
task is actually done, without needing to wait for cursor-agent's own
process to exit on its own.

Exit codes:
  - the child's own exit code, if it exited before the timeout
  - 124 (matching GNU `timeout` convention), if killed due to timeout
"""
import os
import signal
import subprocess
import sys
import time

GRACE_PERIOD_SECONDS = 10


def main() -> int:
    args = sys.argv[1:]
    if "--" not in args:
        print("usage: run_with_hard_timeout.py <timeout_s> <logfile> [--pgid-file <path>] -- <cmd> [args...]", file=sys.stderr)
        return 2

    sep = args.index("--")
    head = args[:sep]
    cmd = args[sep + 1:]

    pgid_file = None
    if len(head) >= 4 and head[2] == "--pgid-file":
        pgid_file = head[3]
        head = head[:2]

    if len(head) != 2 or not cmd:
        print("usage: run_with_hard_timeout.py <timeout_s> <logfile> [--pgid-file <path>] -- <cmd> [args...]", file=sys.stderr)
        return 2

    timeout_s = float(head[0])
    logfile = head[1]

    with open(logfile, "ab", buffering=0) as log_fh:
        proc = subprocess.Popen(
            cmd,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,  # new process group -> killpg() reaches the whole tree
        )
        pgid = os.getpgid(proc.pid)

        if pgid_file:
            with open(pgid_file, "w") as fh:
                fh.write(str(pgid))

        timed_out = False
        deadline = time.monotonic() + timeout_s if timeout_s > 0 else None
        try:
            while True:
                remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
                try:
                    proc.wait(timeout=remaining if remaining is not None else None)
                    break
                except subprocess.TimeoutExpired:
                    if deadline is not None and time.monotonic() >= deadline:
                        timed_out = True
                        break
                    continue
        except KeyboardInterrupt:
            timed_out = True

        try:
            if timed_out:
                _killpg_wait(pgid, signal.SIGTERM, GRACE_PERIOD_SECONDS)
                _killpg_wait(pgid, signal.SIGKILL, 3)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
                return 124

            # Clean exit on cursor-agent's part does not guarantee its background
            # SUT children (dev servers) are gone - sweep the whole group so the
            # next benchmark item/attempt starts from a clean slate.
            _killpg_wait(pgid, signal.SIGTERM, 5)
            _killpg_wait(pgid, signal.SIGKILL, 2)

            return proc.returncode if proc.returncode is not None else 1
        finally:
            if pgid_file:
                try:
                    os.remove(pgid_file)
                except OSError:
                    pass


def _killpg_wait(pgid: int, sig: int, wait_seconds: float) -> None:
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        return
    except PermissionError:
        return
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        try:
            os.killpg(pgid, 0)  # probe: raises once the group is empty/unreachable
        except (ProcessLookupError, PermissionError):
            # ESRCH -> group is gone; EPERM can occur on some platforms once
            # the group no longer contains any process we can signal (e.g.
            # already reaped). Either way, stop waiting - the caller sends a
            # follow-up SIGKILL pass regardless.
            return
        time.sleep(0.3)


if __name__ == "__main__":
    sys.exit(main())
