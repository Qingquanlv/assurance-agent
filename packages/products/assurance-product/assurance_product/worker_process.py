"""Native process identity and identity-checked process termination."""

from __future__ import annotations
import ctypes
import errno
import os
import select
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from collections.abc import Mapping
from typing import Any
from assurance_product.worker_state import ExecutionConflict, ProcessIdentity


def process_identity(pid: int) -> ProcessIdentity | None:
    """Return a native creation identity; None means confirmed exit (including zombie)."""
    if sys.platform.startswith("linux"):
        try:
            stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
            if stat[0] == "Z":
                return None
            created = stat[19]
            boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
            pgid = int(stat[2])
        except FileNotFoundError:
            return None
    elif sys.platform == "darwin":

        class BsdInfo(ctypes.Structure):
            _fields_ = [
                ("ids", ctypes.c_uint32 * 12),
                ("comm", ctypes.c_char * 16),
                ("name", ctypes.c_char * 32),
                ("nfiles", ctypes.c_uint32),
                ("pgid", ctypes.c_uint32),
                ("jobc", ctypes.c_uint32),
                ("tdev", ctypes.c_uint32),
                ("tpgid", ctypes.c_uint32),
                ("nice", ctypes.c_int32),
                ("sec", ctypes.c_uint64),
                ("usec", ctypes.c_uint64),
            ]

        lib = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        lib.proc_pidinfo.argtypes = (
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint64,
            ctypes.c_void_p,
            ctypes.c_int,
        )
        lib.proc_pidinfo.restype = ctypes.c_int
        info = BsdInfo()
        size = lib.proc_pidinfo(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info))
        if size != ctypes.sizeof(info):
            if ctypes.get_errno() == errno.ESRCH:
                return None
            raise ExecutionConflict("native process creation identity unavailable")
        if info.ids[1] == 5:
            return None
        created = f"{info.sec}:{info.usec}"
        boot = subprocess.check_output(["/usr/sbin/sysctl", "-n", "kern.bootsessionuuid"], text=True).strip()  # noqa: S603
        pgid = int(info.pgid)
    else:
        raise ExecutionConflict("process identity unsupported on this platform")
    return {"pid": pid, "created": created, "boot": boot, "host": socket.gethostname(), "pgid": pgid}


def verified_exit(identity: object) -> bool:
    if not isinstance(identity, dict) or not isinstance(identity.get("pid"), int):
        raise ExecutionConflict("missing verifiable worker identity; migration required")
    observed = process_identity(identity["pid"])
    if observed is None:
        # A foreign host or boot cannot prove local process exit.
        local = process_identity(os.getpid())
        if local is None or identity.get("host") != local["host"] or identity.get("boot") != local["boot"]:
            raise ExecutionConflict("worker host or boot identity differs")
        if identity.get("pgid") == identity["pid"]:
            from graph_engine.attempts.production_host import _collect_process_group_descendants

            return all(
                process_identity(int(pid)) is None
                for pid in _collect_process_group_descendants(process_group=identity["pid"])
            )
        return True
    if observed != {key: value for key, value in identity.items() if key != "call_digest"}:
        raise ExecutionConflict("worker creation identity differs; refusing signal")
    return False


def _pidfd_exited(fd: int) -> bool:
    poller = select.poll()
    poller.register(fd, select.POLLIN)
    events = poller.poll(0)
    if any(mask & (select.POLLNVAL | select.POLLERR) for _, mask in events):
        raise ExecutionConflict("native process handle status unavailable")
    return any(mask & select.POLLIN for _, mask in events)


def _pidfd_signal(fd: int, sig: signal.Signals) -> bool:
    try:
        getattr(signal, "pidfd_send_signal")(fd, sig)
    except ProcessLookupError:
        return False
    except OSError as error:
        raise ExecutionConflict("identity-bound process signal unavailable") from error
    return True


def _linux_group_members(
    group: int, *, terminating: bool = False, deadline: float | None = None
) -> dict[int, ProcessIdentity]:
    """Rescan vanished entries before accepting a complete freeze snapshot."""
    if deadline is None:
        deadline = time.monotonic() + 0.1
    try:
        while True:
            members = {}
            vanished = False
            entries = list(Path("/proc").iterdir())
            for entry in entries:
                if not entry.name.isdigit():
                    continue
                pid = int(entry.name)
                try:
                    stat = (entry / "stat").read_text().rsplit(")", 1)[1].split()
                except FileNotFoundError:
                    vanished = True
                    continue
                if int(stat[2]) != group:
                    continue
                if stat[0] == "Z":
                    if not terminating:
                        raise ExecutionConflict("exited group member prevents freeze confirmation")
                    continue
                observed = process_identity(pid)
                if observed is None or observed["pgid"] != group:
                    raise ExecutionConflict("process group changed during enumeration")
                members[pid] = observed
            # After whole-group freeze and bound-handle exit, no authenticated
            # member can spawn. Reaping then cannot hide a newly born descendant.
            if not vanished or terminating:
                return members
            # An unknown parent may have spawned after the directory snapshot
            # and vanished before stat. A fresh complete scan must find its child.
            if time.monotonic() >= deadline:
                raise ExecutionConflict("cannot verify owned process group before freeze deadline")
    except (OSError, ValueError, IndexError) as error:
        raise ExecutionConflict("cannot verify owned process group") from error


def _linux_stopped(pid: int) -> bool:
    try:
        tasks = Path(f"/proc/{pid}/task")
        entries = list(tasks.iterdir())
        for entry in entries:
            state = (entry / "stat").read_text().rsplit(")", 1)[1].split()[0]
            if state not in {"T", "t"}:
                return False
        # A stopped leader alone is insufficient if another thread still runs.
        # Confirm every thread stopped and reject a changing thread snapshot.
        return bool(entries) and {entry.name for entry in entries} == {
            entry.name for entry in tasks.iterdir()
        }
    except FileNotFoundError:
        return False
    except (OSError, IndexError) as error:
        raise ExecutionConflict("cannot confirm process group freeze") from error


def _terminate_linux(identity: Mapping[str, Any], timeout: float) -> bool:
    if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
        raise ExecutionConflict("Linux stop requires native pidfd support")
    pid = int(identity["pid"])
    if pid == os.getpid():
        raise ExecutionConflict("stop control cannot terminate itself")
    group = identity.get("pgid") == pid
    handles: dict[int, int] = {}
    paused: set[int] = set()
    freeze_deadline = time.monotonic() + max(timeout, 0.1)

    def bind(expected: Mapping[str, Any]) -> int | None:
        target = int(expected["pid"])
        if target == os.getpid():
            raise ExecutionConflict("stop control cannot terminate itself")
        try:
            fd = getattr(os, "pidfd_open")(target, 0)
        except ProcessLookupError:
            return None
        except OSError as error:
            raise ExecutionConflict("native process handle unavailable") from error
        handles[target] = fd
        observed = process_identity(target)
        if observed is not None and observed != {
            key: value for key, value in expected.items() if key != "call_digest"
        }:
            raise ExecutionConflict("worker creation identity differs; refusing signal")
        if observed is None:
            # An acquired handle must itself confirm exit; an absent/reused PID
            # alone is not authority to signal or declare a process dead.
            if not _pidfd_exited(fd):
                raise ExecutionConflict("process handle exit is unconfirmed")
            return None
        return fd

    def freeze(target: int, fd: int) -> None:
        if _pidfd_exited(fd):
            return
        already_stopped = _linux_stopped(target)
        if not _pidfd_signal(fd, signal.SIGSTOP):
            return
        if not already_stopped:
            paused.add(target)
        while not _pidfd_exited(fd):
            if _linux_stopped(target):
                return
            if time.monotonic() >= freeze_deadline:
                raise ExecutionConflict("owned process freeze is unconfirmed")
            time.sleep(0.01)

    try:
        leader = bind(identity)
        if leader is None:
            # No signal is sent in this branch. Preserve the existing natural
            # exit protocol, including host/boot and recorded group containment.
            if verified_exit(identity):
                return True
            raise ExecutionConflict("exited group leader cannot authenticate remaining members")
        if group:
            freeze(pid, leader)
            while True:
                if _pidfd_exited(leader):
                    raise ExecutionConflict("group leader exited before quiescence confirmation")
                members = _linux_group_members(pid, deadline=freeze_deadline)
                added = False
                for target, observed in members.items():
                    if target not in handles:
                        fd = bind(observed)
                        if fd is not None:
                            freeze(target, fd)
                        added = True
                    elif _pidfd_exited(handles[target]):
                        # This snapshot contains a live process. A readable old
                        # handle cannot authenticate a replacement at its PID.
                        raise ExecutionConflict("owned process group member identity changed")
                    elif not _linux_stopped(target):
                        raise ExecutionConflict("owned group resumed during freeze")
                if not added:
                    # Every live member was confirmed stopped before this scan;
                    # none can create another child during quiescence verification.
                    break
                if time.monotonic() >= freeze_deadline:
                    raise ExecutionConflict("owned process group did not quiesce")
        for sig in (signal.SIGKILL,) if group else (signal.SIGTERM, signal.SIGKILL):
            for fd in handles.values():
                _pidfd_signal(fd, sig)
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if all(_pidfd_exited(fd) for fd in handles.values()):
                    return not group or not _linux_group_members(pid, terminating=True)
                time.sleep(0.01)
        return all(_pidfd_exited(fd) for fd in handles.values()) and (
            not group or not _linux_group_members(pid, terminating=True)
        )
    finally:
        # On ambiguity, undo only pauses introduced by this stop controller.
        # Signaling the same handles cannot resume a replacement PID.
        active_error = sys.exc_info()[0] is not None
        resume_error = None
        try:
            for target in paused:
                try:
                    if not _pidfd_exited(handles[target]):
                        _pidfd_signal(handles[target], signal.SIGCONT)
                except ExecutionConflict as error:
                    resume_error = error
        finally:
            for fd in handles.values():
                os.close(fd)
        if resume_error is not None and not active_error:
            raise ExecutionConflict("owned process resume is unconfirmed") from resume_error


def terminate(identity: Mapping[str, Any], timeout: float) -> bool:
    if sys.platform.startswith("linux"):
        return _terminate_linux(identity, timeout)
    # macOS retains a creation-check/signal TOCTOU; no native stable handle
    # primitive is used on that platform.
    if verified_exit(identity):
        return True
    pid = int(identity["pid"])
    if pid == os.getpid():
        raise ExecutionConflict("stop control cannot terminate itself")
    # Only the dedicated session leader's group is owned. Foreground shells are excluded.
    group = identity.get("pgid") == pid
    for sig in (signal.SIGTERM, signal.SIGKILL):
        if verified_exit(identity):
            return True
        if group:
            os.killpg(pid, sig)
        else:
            os.kill(pid, sig)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if verified_exit(identity):
                return True
            time.sleep(0.01)
    return verified_exit(identity)
