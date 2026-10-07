"""Exclusive product writer admission and identity-checked stop control.

The lifetime lock and the short control lock deliberately have different files:
stop must remain available while a worker owns the lifetime lock.
"""

from __future__ import annotations

import asyncio
import contextvars
import fcntl
import os
import secrets
import stat
import subprocess
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from assurance_product.worker_state import (
    ExecutionConflict as ExecutionConflict,
    WorkerRecord,
    control_root as control_root,
    read_owner,
    write_owner,
    control_lock,
)
from assurance_product.worker_process import process_identity, verified_exit, terminate
from assurance_product.worker_cleanup import (
    validated_stop_checkpoint,
    has_owned_generations,
    assert_no_legacy_attempts,
)


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
    with control_lock(root):
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
            old = read_owner(root)
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
                    assert_no_legacy_attempts(workspace)
                nonce = secrets.token_hex(16)
            identity = process_identity(os.getpid())
            if identity is None:
                raise ExecutionConflict("current process identity unavailable")
            stat = workspace.stat()
            write_owner(
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
            with control_lock(root):
                record = read_owner(root)
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
                            verified_exit(child) for child in record["children"] + record.get("servers", [])
                        )
                    ):
                        record["state"] = (
                            "stopping" if failed and has_owned_generations(record) else "stopped"
                        )
                        write_owner(root, record)
        finally:
            os.close(fd)


def update_owner(owner: ExecutionOwner, update: Callable[[WorkerRecord], None]) -> None:
    root = control_root(owner.workspace)
    with control_lock(root):
        record = read_owner(root)
        if (
            record is None
            or record.get("nonce") != owner.nonce
            or record.get("state") not in {"running", "stopping"}
        ):
            raise ExecutionConflict("worker owner is no longer current")
        update(record)
        write_owner(root, record)


def request_stop(
    workspace: Path,
    *,
    force: bool = False,
    timeout: float = 2,
    confirm_external: Callable[[WorkerRecord], bool] | None = None,
    cleanup: Callable[[WorkerRecord], None] | None = None,
    expected_invocation: str | None = None,
) -> str:
    root = control_root(workspace)
    observed = read_owner(root)
    expected_nonce = None if observed is None else observed.get("nonce")
    with control_lock(root):
        owner = read_owner(root)
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
                    or not all(verified_exit(child) for child in owner["children"] + owner.get("servers", []))
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
        write_owner(root, owner)
        admission_fd = None
        try:
            exited = terminate(owner["process"], timeout) if force else verified_exit(owner["process"])
            if not exited:
                return "stopping"
            for child in owner["children"]:
                exited = terminate(child, timeout) if force else verified_exit(child)
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
                    write_owner(root, owner)
                    return "unconfirmed"
            elif owner["calls"]:
                owner["stop_error"] = "owned runtime calls require authenticated terminal cancellation"
                write_owner(root, owner)
                return "unconfirmed"
            if owner.get("launching_service"):
                return "unconfirmed"
            for server in owner.get("servers", []):
                if not (terminate(server, timeout) if force else verified_exit(server)):
                    return "stopping"
            if cleanup is not None:
                cleanup(owner)
            owner["state"] = "stopped"
            owner["calls"] = []
            write_owner(root, owner)
            return "stopped"
        except Exception as error:
            owner["stop_error"] = str(error)
            write_owner(root, owner)
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
        with control_lock(control_root(owner.workspace)):
            record = read_owner(control_root(owner.workspace))
            if (
                record is not None
                and record.get("nonce") == owner.nonce
                and record.get("state") == "reserved"
            ):
                record["children"].append(identity)
                write_owner(control_root(owner.workspace), record)
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


def assert_generation_ready(owner: ExecutionOwner) -> None:
    with control_lock(control_root(owner.workspace)):
        record = read_owner(control_root(owner.workspace))
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


def stop_diagnostic(workspace: Path) -> str:
    record = read_owner(control_root(workspace))
    return (
        str(record.get("stop_error", "owned process exit or scoped cleanup is unconfirmed"))
        if record is not None
        else "legacy execution has no verifiable owner; use a fresh isolated run"
    )
