"""Exclusive product writer admission and identity-checked stop control.

The lifetime lock and the short control lock deliberately have different files:
stop must remain available while a worker owns the lifetime lock.
"""

from __future__ import annotations

import asyncio
import contextvars
import ctypes
import errno
import fcntl
import json
import os
import secrets
import select
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ExecutionConflict(RuntimeError):
    """The workspace still belongs to an execution that has not been stopped."""


def control_root(workspace: Path) -> Path:
    return workspace.resolve() / ".aa" / "runtime" / "worker"


def process_identity(pid: int) -> dict[str, object] | None:
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


def _read(root: Path) -> dict[str, Any] | None:
    path = root / "owner.json"
    if not path.exists():
        return None
    if path.is_symlink():
        raise ExecutionConflict("worker ownership must be a regular file")
    owner = json.loads(path.read_text())
    if not isinstance(owner, dict) or owner.get("schema") != 1:
        raise ExecutionConflict("legacy worker ownership cannot be resumed; use a fresh isolated run")
    return owner


def _write(root: Path, owner: Mapping[str, object]) -> None:
    temporary = root / f".owner-{secrets.token_hex(8)}"
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(owner, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, root / "owner.json")
        directory = os.open(root, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def _control(root: Path) -> Iterator[None]:
    for directory in (root.parent.parent, root.parent, root):
        if directory.is_symlink():
            raise ExecutionConflict("worker control directories must be real directories")
        directory.mkdir(exist_ok=True, mode=0o700)
        if not directory.is_dir():
            raise ExecutionConflict("worker control path is not a directory")
    fd = os.open(root / "control.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _task() -> object:
    try:
        return asyncio.current_task()
    except RuntimeError:
        return None


@dataclass
class ExecutionOwner:
    workspace: Path
    invocation: str
    nonce: str
    fd: int
    thread: int
    task: object
    stop_authority_digest: str | None = None


_current: contextvars.ContextVar[ExecutionOwner | None] = contextvars.ContextVar(
    "aa_worker_owner", default=None
)


def current_owner() -> ExecutionOwner | None:
    return _current.get()


@dataclass
class PreparationReservation:
    workspace: Path
    fd: int
    thread: int
    task: object
    pid: int

    def release(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1


_preparation: contextvars.ContextVar[PreparationReservation | None] = contextvars.ContextVar(
    "aa_preparation", default=None
)


@contextmanager
def reserve_preparation(workspace: Path) -> Iterator[PreparationReservation]:
    """Cross-entrypoint admission without creating anything inside the target."""
    workspace = workspace.resolve()
    current = _preparation.get()
    if current is not None:
        if (current.workspace, current.thread, current.task, current.pid) != (
            workspace,
            threading.get_ident(),
            _task(),
            os.getpid(),
        ):
            raise ExecutionConflict("nested preparation requires the exact current target")
        yield current
        return
    # Use the target basename unchanged: the filesystem itself applies case and
    # Unicode filename equivalence, including before the target becomes visible.
    # Casefold is only a reserved-infrastructure policy, never a lock-key rule.
    namespace = ".aa-preparation-locks"
    if not workspace.name or any(part.casefold() == namespace for part in workspace.parts):
        raise ExecutionConflict("workspace targets reserved preparation infrastructure")
    root = workspace.parent / namespace
    try:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if root.is_symlink() or not root.is_dir():
            raise ExecutionConflict("preparation namespace must be a real directory")
        fd = os.open(root / workspace.name, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise ExecutionConflict("preparation reservation must be a regular file")
    except OSError as error:
        raise ExecutionConflict("workspace preparation reservation unavailable") from error
    reservation = PreparationReservation(workspace, fd, threading.get_ident(), _task(), os.getpid())
    token = None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ExecutionConflict("workspace preparation already has a live worker") from error
        token = _preparation.set(reservation)
        yield reservation
    finally:
        if token is not None:
            _preparation.reset(token)
        reservation.release()


@contextmanager
def acquire_execution(
    workspace: Path,
    invocation: str,
    *,
    run_dir: Path | None = None,
    inherited_fd: int | None = None,
    inherited_nonce: str | None = None,
) -> Iterator[ExecutionOwner]:
    with reserve_preparation(workspace) as reservation:
        with _acquire_execution(
            workspace,
            invocation,
            run_dir=run_dir,
            inherited_fd=inherited_fd,
            inherited_nonce=inherited_nonce,
        ) as owner:
            # The lifetime flock now excludes every writer. Background admission
            # can authenticate its inherited lifetime FD without a preparation gap.
            reservation.release()
            yield owner


@contextmanager
def _acquire_execution(
    workspace: Path,
    invocation: str,
    *,
    run_dir: Path | None = None,
    inherited_fd: int | None = None,
    inherited_nonce: str | None = None,
) -> Iterator[ExecutionOwner]:
    workspace = workspace.resolve()
    current = _current.get()
    if current is not None:
        if (current.workspace, current.invocation, current.thread, current.task) != (
            workspace,
            invocation,
            threading.get_ident(),
            _task(),
        ):
            raise ExecutionConflict("nested execution requires the exact current owner")
        yield current
        return
    workspace.mkdir(parents=True, exist_ok=True)
    root = control_root(workspace)
    with _control(root):
        fd = (
            inherited_fd
            if inherited_fd is not None
            else os.open(root / "execution.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        )
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise ExecutionConflict("workspace already has a live worker") from error
            old = _read(root)
            if inherited_fd is not None:
                if (
                    old is None
                    or old.get("nonce") != inherited_nonce
                    or old.get("state") != "reserved"
                    or old.get("invocation") != invocation
                ):
                    raise ExecutionConflict("background reservation identity mismatch")
                nonce = str(inherited_nonce)
            else:
                if old is not None and old.get("state") != "stopped":
                    raise ExecutionConflict(
                        "previous worker exit and external cleanup are unconfirmed; use explicit stop"
                    )
                if old is not None:
                    validated_stop_checkpoint(old)
                if old is None:
                    db = workspace / "qa" / ".runtime" / "langgraph" / "checkpoints.sqlite3"
                    if db.is_file():
                        import sqlite3

                        connection = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
                        try:
                            exists = connection.execute(
                                "SELECT name FROM sqlite_master WHERE name = 'assurance_attempt_batches'"
                            ).fetchone()
                            if (
                                exists
                                and connection.execute(
                                    "SELECT 1 FROM assurance_attempt_batches LIMIT 1"
                                ).fetchone()
                            ):
                                raise ExecutionConflict(
                                    "legacy execution has no verifiable owner; unsupported resume, use a fresh isolated run"
                                )
                        finally:
                            connection.close()
                nonce = secrets.token_hex(16)
            identity = process_identity(os.getpid())
            if identity is None:
                raise ExecutionConflict("current process identity unavailable")
            stat = workspace.stat()
            _write(
                root,
                {
                    "schema": 1,
                    "workspace": str(workspace),
                    "directory": [stat.st_dev, stat.st_ino],
                    "invocation": invocation,
                    "run_dir": str(run_dir) if run_dir else None,
                    "nonce": nonce,
                    "process": identity,
                    "state": "running",
                    "children": [],
                    "servers": [],
                    "launching_service": False,
                    "calls": [],
                    "attempts": [],
                },
            )
        except BaseException:
            os.close(fd)
            raise
    owner = ExecutionOwner(workspace, invocation, nonce, fd, threading.get_ident(), _task())
    context_token = _current.set(owner)
    failed = False
    try:
        yield owner
    except BaseException:
        failed = True
        raise
    finally:
        _current.reset(context_token)
        try:
            with _control(root):
                record = _read(root)
                if (
                    record is not None
                    and record.get("nonce") == owner.nonce
                    and record.get("process") == process_identity(os.getpid())
                ):
                    # Only a quiescent owned lifetime may close admission automatically.
                    if (
                        not record["calls"]
                        and not record.get("launching_service")
                        and all(
                            _verified_exit(child) for child in record["children"] + record.get("servers", [])
                        )
                    ):
                        record["state"] = (
                            "stopping" if failed and _has_owned_generations(record) else "stopped"
                        )
                        _write(root, record)
        finally:
            os.close(fd)


def update_owner(owner: ExecutionOwner, update: Callable[[dict[str, Any]], None]) -> None:
    root = control_root(owner.workspace)
    with _control(root):
        record = _read(root)
        if (
            record is None
            or record.get("nonce") != owner.nonce
            or record.get("state") not in {"running", "stopping"}
        ):
            raise ExecutionConflict("worker owner is no longer current")
        update(record)
        _write(root, record)


def _verified_exit(identity: object) -> bool:
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


def _linux_group_members(group: int, *, terminating: bool = False) -> dict[int, dict[str, object]]:
    """Reject incomplete snapshots; an omitted exiting parent could spawn a child."""
    members = {}
    try:
        entries = list(Path("/proc").iterdir())
        for entry in entries:
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            # Disappearance, including an unrelated process, makes this snapshot
            # incomplete. Do not infer group membership from missing metadata.
            stat = (entry / "stat").read_text().rsplit(")", 1)[1].split()
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
    except (OSError, ValueError, IndexError) as error:
        raise ExecutionConflict("cannot verify owned process group") from error
    return members


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


def _terminate_linux(identity: dict[str, Any], timeout: float) -> bool:
    if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
        raise ExecutionConflict("Linux stop requires native pidfd support")
    pid = int(identity["pid"])
    if pid == os.getpid():
        raise ExecutionConflict("stop control cannot terminate itself")
    group = identity.get("pgid") == pid
    handles: dict[int, int] = {}
    paused: set[int] = set()
    freeze_deadline = time.monotonic() + max(timeout, 0.1)

    def bind(expected: dict[str, Any]) -> int | None:
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
            if _verified_exit(identity):
                return True
            raise ExecutionConflict("exited group leader cannot authenticate remaining members")
        if group:
            freeze(pid, leader)
            while True:
                if _pidfd_exited(leader):
                    raise ExecutionConflict("group leader exited before quiescence confirmation")
                members = _linux_group_members(pid)
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
        for sig in (signal.SIGTERM, signal.SIGKILL):
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


def _terminate(identity: dict[str, Any], timeout: float) -> bool:
    if sys.platform.startswith("linux"):
        return _terminate_linux(identity, timeout)
    # macOS retains a creation-check/signal TOCTOU; no native stable handle
    # primitive is used on that platform.
    if _verified_exit(identity):
        return True
    pid = int(identity["pid"])
    if pid == os.getpid():
        raise ExecutionConflict("stop control cannot terminate itself")
    # Only the dedicated session leader's group is owned. Foreground shells are excluded.
    group = identity.get("pgid") == pid
    for sig in (signal.SIGTERM, signal.SIGKILL):
        if _verified_exit(identity):
            return True
        if group:
            os.killpg(pid, sig)
        else:
            os.kill(pid, sig)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if _verified_exit(identity):
                return True
            time.sleep(0.01)
    return _verified_exit(identity)


def request_stop(
    workspace: Path,
    *,
    force: bool = False,
    timeout: float = 2,
    confirm_external: Callable[[dict[str, Any]], bool] | None = None,
    cleanup: Callable[[dict[str, Any]], None] | None = None,
    expected_invocation: str | None = None,
) -> str:
    root = control_root(workspace)
    observed = _read(root)
    expected_nonce = None if observed is None else observed.get("nonce")
    with _control(root):
        owner = _read(root)
        if (
            owner is None
            or owner.get("nonce") != expected_nonce
            or (expected_invocation is not None and owner.get("invocation") != expected_invocation)
        ):
            return "unconfirmed"
        if owner.get("workspace") != str(workspace.resolve()):
            return "unconfirmed"
        if owner.get("state") == "stopped":
            released_fd = os.open(root / "execution.lock", os.O_RDWR | os.O_NOFOLLOW)
            try:
                try:
                    fcntl.flock(released_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return "stopping"
                if (
                    owner["calls"]
                    or owner.get("launching_service")
                    or not all(
                        _verified_exit(child) for child in owner["children"] + owner.get("servers", [])
                    )
                ):
                    return "unconfirmed"
                return "stopped"
            finally:
                os.close(released_fd)
        if owner.get("invocation") is None or owner.get("nonce") is None:
            return "unconfirmed"
        stat = workspace.resolve().stat()
        if owner.get("directory") != [stat.st_dev, stat.st_ino]:
            return "unconfirmed"
        owner["state"] = "stopping"
        _write(root, owner)
        admission_fd = None
        try:
            exited = _terminate(owner["process"], timeout) if force else _verified_exit(owner["process"])
            if not exited:
                return "stopping"
            for child in owner["children"]:
                exited = _terminate(child, timeout) if force else _verified_exit(child)
                if not exited:
                    return "stopping"
            admission_fd = os.open(root / "execution.lock", os.O_RDWR | os.O_NOFOLLOW)
            try:
                fcntl.flock(admission_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BaseException:
                os.close(admission_fd)
                admission_fd = None
                raise
            validated_stop_checkpoint(owner)
            if confirm_external is not None:
                if not confirm_external(owner):
                    owner["stop_error"] = "owned external activity termination remains unconfirmed"
                    _write(root, owner)
                    return "unconfirmed"
            elif owner["calls"]:
                owner["stop_error"] = "owned runtime calls require authenticated terminal cancellation"
                _write(root, owner)
                return "unconfirmed"
            if owner.get("launching_service"):
                return "unconfirmed"
            for server in owner.get("servers", []):
                if not (_terminate(server, timeout) if force else _verified_exit(server)):
                    return "stopping"
            if cleanup is not None:
                cleanup(owner)
            owner["state"] = "stopped"
            owner["calls"] = []
            _write(root, owner)
            return "stopped"
        except Exception as error:
            owner["stop_error"] = str(error)
            _write(root, owner)
            return "unconfirmed"
        finally:
            if admission_fd is not None:
                os.close(admission_fd)


def launch_reserved(
    owner: ExecutionOwner, argv: list[str], environ: Mapping[str, str]
) -> subprocess.Popen[bytes]:
    import select

    read_fd, write_fd = os.pipe()
    env = dict(environ)
    env.update(
        {
            "AA_WORKER_LOCK_FD": str(owner.fd),
            "AA_WORKER_NONCE": owner.nonce,
            "AA_WORKER_ADMISSION_FD": str(write_fd),
        }
    )
    update_owner(owner, lambda record: record.update(state="reserved"))
    process = None
    try:
        process = subprocess.Popen(
            argv,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            pass_fds=(owner.fd, write_fd),
        )  # noqa: S603
        identity = process_identity(process.pid)
        if identity is None:
            raise ExecutionConflict("background child identity unavailable")
        with _control(control_root(owner.workspace)):
            record = _read(control_root(owner.workspace))
            if (
                record is not None
                and record.get("nonce") == owner.nonce
                and record.get("state") == "reserved"
            ):
                record["children"].append(identity)
                _write(control_root(owner.workspace), record)
        os.close(write_fd)
        write_fd = -1
        ready, _, _ = select.select([read_fd], [], [], 10)
        if not ready or os.read(read_fd, 1) != b"1":
            raise ExecutionConflict("background worker admission was not confirmed; explicit stop required")
        return process
    finally:
        os.close(read_fd)
        if write_fd != -1:
            os.close(write_fd)


@contextmanager
def admit_background(
    workspace: Path, invocation: str, *, run_dir: Path | None = None
) -> Iterator[ExecutionOwner]:
    fd = os.environ.pop("AA_WORKER_LOCK_FD", None)
    nonce = os.environ.pop("AA_WORKER_NONCE", None)
    acknowledgment = os.environ.pop("AA_WORKER_ADMISSION_FD", None)
    if fd is None or nonce is None or acknowledgment is None:
        raise ExecutionConflict("detached worker requires an authenticated startup reservation")
    with acquire_execution(
        workspace, invocation, run_dir=run_dir, inherited_fd=int(fd), inherited_nonce=nonce
    ) as owner:
        os.set_inheritable(owner.fd, False)
        try:
            os.write(int(acknowledgment), b"1")
        finally:
            os.close(int(acknowledgment))
        yield owner


def run_workspace(run_dir: Path) -> tuple[Path, str]:
    from assurance_product.bootstrap.status import read_run_manifest

    manifest = read_run_manifest(run_dir)
    from assurance_product.bootstrap.status import read_bootstrap_status

    invocation = manifest.get("invocation_id") or read_bootstrap_status(run_dir).change_id
    return Path(str(manifest["project_dir"])), str(invocation)


def exclusive_application(function: Callable[..., Any]) -> Callable[..., Any]:
    from functools import wraps

    @wraps(function)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        workspace = kwargs["workspace"]
        with acquire_execution(workspace.paths.project_root, kwargs["invocation_id"]):
            return function(*args, **kwargs)

    return wrapped


def exclusive_resume(function: Callable[..., Any]) -> Callable[..., Any]:
    from functools import wraps

    @wraps(function)
    def wrapped(run_dir: Path, *args: Any, **kwargs: Any) -> Any:
        workspace, invocation = run_workspace(run_dir)
        with acquire_execution(workspace, invocation, run_dir=run_dir):
            return function(run_dir, *args, **kwargs)

    return wrapped


def exclusive_bootstrap(function: Callable[..., Any]) -> Callable[..., Any]:
    from functools import wraps

    @wraps(function)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        from assurance_product.bootstrap.status import derive_bootstrap_change_id
        from assurance_product.bootstrap.driver import utc_stamp
        from assurance_product.sut_worktree import ensure_run_worktree, resolve_run_worktree

        invocation = kwargs.get("change_id") or derive_bootstrap_change_id(
            stamp=utc_stamp(), nonce=secrets.token_hex(4)
        )
        kwargs["change_id"] = invocation
        workspace = kwargs.get("task_directory")
        if workspace is not None and not workspace.is_dir():
            raise ValueError("task directory does not exist")
        target = workspace or resolve_run_worktree(kwargs["project_dir"], invocation)
        with reserve_preparation(target):
            if workspace is None:
                workspace = ensure_run_worktree(kwargs["project_dir"], invocation)
            with acquire_execution(workspace, invocation, run_dir=kwargs["runs_root"] / invocation):
                return function(*args, **kwargs)

    return wrapped


def assert_generation_ready(owner: ExecutionOwner) -> None:
    with _control(control_root(owner.workspace)):
        record = _read(control_root(owner.workspace))
        if (
            record is None
            or record.get("nonce") != owner.nonce
            or record.get("state") != "running"
            or record["calls"]
        ):
            raise ExecutionConflict(
                "previous external call termination is unconfirmed; stop before regeneration"
            )


def observe_child(pid: int, call_digest: str) -> None:
    owner = current_owner()
    if owner is None:
        return
    identity = process_identity(pid)
    if identity is None:
        raise ExecutionConflict("phase worker exited before identity registration")
    identity["call_digest"] = call_digest
    update_owner(owner, lambda record: record["children"].append(identity))


def validated_stop_checkpoint(owner: dict[str, Any]) -> Path | None:
    """Reject unsafe paths and lost dispatch evidence before stop opens SQLite."""
    import sqlite3
    import stat
    from assurance_product.change_workspace import require_real_directory

    workspace = require_real_directory(Path(owner["workspace"]))
    db = workspace / "qa" / ".runtime" / "langgraph" / "checkpoints.sqlite3"
    evidence = bool(owner.get("attempts") or owner.get("calls") or owner.get("children"))
    for path in (workspace / "qa", db.parent.parent, db.parent, db):
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            if evidence:
                raise ExecutionConflict(
                    "registered execution persistence is missing; unsupported cleanup"
                ) from None
            return None
        required = stat.S_ISREG if path == db else stat.S_ISDIR
        if not required(mode):
            raise ExecutionConflict("stop checkpoint and runtime ancestors must be real paths")
    if owner.get("attempts") or owner.get("calls"):
        connection = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            generations = {
                str(row[0])
                for row in connection.execute(
                    "SELECT attempt_key_digest FROM assurance_attempt_generations WHERE owner_nonce = ?",
                    (owner["nonce"],),
                )
            }
            calls = {
                str(row[0])
                for row in connection.execute(
                    "SELECT call_digest FROM assurance_host_calls WHERE owner_nonce = ?", (owner["nonce"],)
                )
            }
            if not set(owner.get("attempts", [])).issubset(generations) or not set(
                owner.get("calls", [])
            ).issubset(calls):
                raise ExecutionConflict("registered execution rows are missing; unsupported cleanup")
        except sqlite3.DatabaseError as error:
            raise ExecutionConflict(
                "registered execution persistence is incomplete; unsupported cleanup"
            ) from error
        finally:
            connection.close()
    return db


def cleanup_owned_resources(owner: dict[str, Any]) -> None:
    import sqlite3
    from assurance_product.bootstrap.resources import release_active_resource_authorizations

    db = validated_stop_checkpoint(owner)
    if db is None:
        return
    connection = sqlite3.connect(f"file:{db}?mode=rw", uri=True)
    try:
        rows = connection.execute(
            "SELECT scope, attempt_key_digest, ordinal, input_payload FROM assurance_attempt_generations WHERE owner_nonce = ?",
            (owner["nonce"],),
        ).fetchall()
        attempts = set()
        for scope, key, ordinal, input_payload in rows:
            decoded = json.loads(bytes(scope))
            if decoded.get("invocation_id") != owner["invocation"]:
                raise ExecutionConflict("generation owner disagrees with Invocation")
            from graph_engine.canonical import canonical_digest

            fields = (
                "invocation_id",
                "graph_revision",
                "public_entrypoint",
                "semantic_node_id",
                "business_activation",
                "contract_id",
            )
            if not all(field in decoded for field in fields):
                raise ExecutionConflict(
                    "generation activation identity is unreconstructible; unsupported cleanup"
                )
            projection = {field: decoded[field] for field in fields}
            projection.update(
                technical_attempt=int(ordinal),
                task_input_digest=canonical_digest(json.loads(bytes(input_payload))),
            )
            if canonical_digest(projection) != key:
                raise ExecutionConflict("generation key disagrees with retained activation/input")
            attempts.add(str(key))
        calls = connection.execute(
            "SELECT attempt_key_digest FROM assurance_host_calls WHERE owner_nonce = ?", (owner["nonce"],)
        ).fetchall()
        attempts.update(str(row[0]) for row in calls)
    finally:
        connection.close()
    validated_stop_checkpoint(owner)
    release_active_resource_authorizations(db, attempt_key_digests=attempts)
    validated_stop_checkpoint(owner)
    connection = sqlite3.connect(f"file:{db}?mode=rw", uri=True)
    try:
        connection.execute(
            "UPDATE assurance_attempt_generations SET abandoned = 1 WHERE owner_nonce = ?", (owner["nonce"],)
        )
        connection.commit()
    finally:
        connection.close()


def stop_run(run_dir: Path, *, force: bool = False, timeout: float = 2, change_id: str | None = None) -> str:
    from assurance_product.bootstrap.status import write_stop_request, read_bootstrap_status
    from assurance_product.retained_host import confirm_owned_calls

    invocation = change_id or read_bootstrap_status(run_dir).change_id
    write_stop_request(run_dir, change_id=invocation)
    if not force:
        return "stopping"
    workspace, recorded_invocation = run_workspace(run_dir)
    if recorded_invocation != invocation:
        return "unconfirmed"
    return request_stop(
        workspace,
        force=force,
        timeout=timeout,
        confirm_external=lambda owner: asyncio.run(confirm_owned_calls(owner)),
        cleanup=cleanup_owned_resources,
        expected_invocation=invocation,
    )


def exclusive_cli(function: Callable[..., Any]) -> Callable[..., Any]:
    from functools import wraps

    @wraps(function)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        from assurance_product.cli import _engine_failures, _project_for_run

        from assurance_product.sut_worktree import resolve_run_worktree
        from assurance_product.cli import CommandError, require_real_directory

        workspace = kwargs["project_dir"]
        prepare = function.__name__ != "_resume_invocation" and not kwargs.get("reuse_directory", False)
        with _engine_failures():
            try:
                target = (
                    resolve_run_worktree(require_real_directory(workspace), kwargs["change_id"])
                    if prepare
                    else workspace
                )
            except ValueError as error:
                raise CommandError(str(error)) from error
            with reserve_preparation(target):
                if prepare:
                    workspace = _project_for_run(workspace, kwargs["change_id"])
                    kwargs["project_dir"] = workspace
                    kwargs["reuse_directory"] = True
                with acquire_execution(workspace, kwargs["invocation_id"]):
                    return function(*args, **kwargs)

    return wrapped


def stop_diagnostic(workspace: Path) -> str:
    record = _read(control_root(workspace))
    return (
        str(record.get("stop_error", "owned process exit or scoped cleanup is unconfirmed"))
        if record is not None
        else "legacy execution has no verifiable owner; use a fresh isolated run"
    )


def _has_owned_generations(owner: dict[str, Any]) -> bool:
    import sqlite3

    if owner.get("attempts"):
        return True

    db = Path(owner["workspace"]) / "qa" / ".runtime" / "langgraph" / "checkpoints.sqlite3"
    if db.is_symlink() or not db.is_file():
        # Runtime validation rejects this before dispatch; never open a rejected
        # symlink here (even SQLite read-only connections may create WAL files).
        return False
    connection = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return (
            connection.execute(
                "SELECT 1 FROM assurance_attempt_generations WHERE owner_nonce = ? LIMIT 1", (owner["nonce"],)
            ).fetchone()
            is not None
        )
    except sqlite3.DatabaseError:
        return True
    finally:
        connection.close()
