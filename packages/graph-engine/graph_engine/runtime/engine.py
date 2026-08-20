from __future__ import annotations

import asyncio
import fcntl
import os
import stat
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath
from typing import Literal

from pydantic import BaseModel, ConfigDict

from graph_engine.canonical import JSONValue, canonical_digest
from graph_engine.errors import GraphEngineError
from graph_engine.product import ResolvedProduct
from graph_engine.plugin_api import TaskHandler, TaskOutcome, TaskRequest
from graph_engine.runtime.checkpoint import load_checkpoint_at, write_checkpoint_at
from graph_engine.runtime.events import (
    EventEnvelope,
    GraphStarted,
    InterruptResumed,
    InvocationStarted,
    NodeCompleted,
    RuntimeEvent,
    TokenOffered,
)
from graph_engine.runtime.frozen_json import FrozenJSONValue
from graph_engine.runtime.ledger import (
    Ledger,
    LedgerConflictError,
    LedgerPublicationIndeterminate,
    append_validated_batch,
)
from graph_engine.runtime.models import InvocationProjection, fold_events
from graph_engine.runtime.planner import (
    PlanningError,
    _start_token_id,
    plan_next,
    plan_running_tasks,
    validate_projection,
)
from graph_engine.runtime.scheduler import (
    Clock,
    LeaseUnavailableError,
    Scheduler,
    SystemClock,
    TaskExecutionHost,
)
from graph_engine.runtime.workspace import (
    FinalizationRolledBack,
    HeadPublicationIndeterminate,
    SnapshotStore,
)


class EngineError(GraphEngineError):
    """Raised when an invocation cannot safely make the requested transition."""


class EngineConflictError(EngineError):
    """Raised when another runner wins an invocation transition."""


class EnginePublicationIndeterminate(EngineError):
    """Raised when authoritative publication cannot be reconciled."""


@dataclass(frozen=True, slots=True)
class InvocationHandle:
    invocation_id: str
    invocation_root: Path
    product_digest: str
    _product: ResolvedProduct = field(repr=False, compare=False)
    _invocation_fd: int = field(repr=False, compare=False)
    _closed: bool = field(default=False, init=False, repr=False, compare=False)

    @property
    def workspace(self) -> SnapshotStore:
        if self._closed:
            raise EngineError("invocation handle is closed")
        return SnapshotStore.at(
            self._invocation_fd,
            "workspace",
            display_root=self.invocation_root / "workspace",
        )

    def close(self) -> None:
        if self._closed:
            return
        descriptor = self._invocation_fd
        object.__setattr__(self, "_closed", True)
        object.__setattr__(self, "_invocation_fd", -1)
        os.close(descriptor)

    def __enter__(self) -> InvocationHandle:
        if self._closed:
            raise EngineError("invocation handle is closed")
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except BaseException:
            pass


_RESULT_CONFIG = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)
_RUNNER_LOCK = ".engine-runner.lock"
_EMPTY_TREE_ID = canonical_digest([])


class RunResult(BaseModel):
    model_config = _RESULT_CONFIG

    status: Literal["succeeded", "failed", "stopped", "interrupted"]
    output: FrozenJSONValue = None
    terminal_reason: str | None = None
    actions: tuple[str, ...] = ()
    projection: InvocationProjection

    @property
    def reason(self) -> str | None:
        return self.terminal_reason


class _UnavailableTaskHost:
    async def execute(
        self,
        handler: TaskHandler,
        request: TaskRequest,
        *,
        workspace_root: Path,
        heartbeat: Callable[[], None],
    ) -> TaskOutcome:
        del handler, request, workspace_root, heartbeat
        return TaskOutcome.failed("internal", "task execution host is not configured")


class Engine:
    def __init__(
        self,
        root: Path,
        *,
        clock: Clock | None = None,
        host: TaskExecutionHost | None = None,
    ) -> None:
        self._root = Path(root).absolute()
        self._invocations_root = self._root / "invocations"
        self._clock = clock or SystemClock()
        self._host = host
        self._invocations_fd = _open_or_create_namespace(self._root)
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        descriptor = self._invocations_fd
        self._closed = True
        self._invocations_fd = -1
        os.close(descriptor)

    def __enter__(self) -> Engine:
        if self._closed:
            raise EngineError("engine is closed")
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except BaseException:
            pass

    def start(
        self,
        product: ResolvedProduct,
        *,
        entrypoint: str,
        invocation_id: str,
    ) -> InvocationHandle:
        self._assert_namespace_path_current()
        invocation_root = self._invocation_root(invocation_id)
        if entrypoint not in product.workflow.entrypoints:
            raise EngineError(f"unknown entrypoint {entrypoint!r}")
        if _entry_exists(self._invocations_fd, invocation_id):
            self._raise_duplicate_start(invocation_id, product)
            raise EngineError(f"invocation already exists: {invocation_id}")
        staging_name = f".{invocation_id}.invocation-init-{uuid.uuid4().hex}"
        staging_fd: int | None = None
        installed = False
        try:
            os.mkdir(staging_name, mode=0o700, dir_fd=self._invocations_fd)
            os.fsync(self._invocations_fd)
            staging_fd = _open_directory_at(
                self._invocations_fd,
                staging_name,
                "invocation initialization staging",
            )
            store = SnapshotStore.create_at(
                staging_fd,
                "workspace",
                {},
                display_root=invocation_root / "workspace",
            )
            _initialization_boundary("workspace_ready")
            graph_id = product.workflow.entrypoints[entrypoint]
            graph = product.workflow.graphs[graph_id]
            graph_instance_id = graph_id
            events: tuple[RuntimeEvent, ...] = (
                InvocationStarted(
                    invocation_id=invocation_id,
                    product_digest=product.digest,
                    entrypoint=entrypoint,
                ),
                GraphStarted(graph_instance_id=graph_instance_id, graph_id=graph_id),
                TokenOffered(
                    token_id=_start_token_id(graph_instance_id, graph.start),
                    graph_instance_id=graph_instance_id,
                    source=None,
                    target=graph.start,
                    payload=None,
                ),
            )
            ledger = Ledger.at(
                staging_fd,
                "ledger",
                display_root=invocation_root / "ledger",
            )
            self._append(ledger, events, ())
            envelopes = ledger.read_all()
            projection = fold_events(envelopes)
            self._validate_workflow(product, projection)
            self._write_checkpoint(staging_fd, envelopes, projection)
            os.fsync(staging_fd)
            self._install_invocation(staging_name, invocation_id)
            installed = True
            assert store.head_tree_id()
            return InvocationHandle(
                invocation_id,
                invocation_root,
                product.digest,
                product,
                os.dup(staging_fd),
            )
        except Exception:
            if not installed and _entry_exists(self._invocations_fd, staging_name):
                if staging_fd is not None:
                    os.close(staging_fd)
                    staging_fd = None
                _remove_entry_at(self._invocations_fd, staging_name)
                os.fsync(self._invocations_fd)
            raise
        finally:
            if staging_fd is not None:
                os.close(staging_fd)

    def open(self, invocation_id: str, product: ResolvedProduct) -> InvocationHandle:
        self._assert_namespace_path_current()
        invocation_root = self._invocation_root(invocation_id)
        invocation_fd = self._open_invocation(invocation_id)
        claim_fd: int | None = None
        try:
            claim_fd = self._acquire_runner_claim(invocation_fd)
            ledger = Ledger.at(
                invocation_fd,
                "ledger",
                display_root=invocation_root / "ledger",
            )
            envelopes = ledger.read_all()
            projection = fold_events(envelopes)
            if projection.invocation_id != invocation_id:
                raise EngineError("invocation ledger identity does not match its path")
            if projection.product_digest != product.digest:
                raise EngineError(
                    f"resolved product digest mismatch: expected {projection.product_digest}, "
                    f"found {product.digest}"
                )
            self._validate_workflow(product, projection)
            checkpoint = load_checkpoint_at(
                invocation_fd,
                "checkpoint.json",
                ledger_envelopes=envelopes,
            )
            if checkpoint is not None:
                projection = checkpoint.projection
            store = SnapshotStore.at(
                invocation_fd,
                "workspace",
                display_root=invocation_root / "workspace",
            )
            try:
                ledger.ensure_durable()
            except OSError as error:
                raise EnginePublicationIndeterminate(
                    "authoritative ledger durability is indeterminate"
                ) from error
            store.recover_head_transaction(projection.head_tree_id)
            actual_tree_id = store.head_tree_id()
            expected_tree_id = projection.head_tree_id or _EMPTY_TREE_ID
            if actual_tree_id != expected_tree_id:
                raise EngineError("workspace HEAD disagrees with the authoritative ledger")
            if projection.status == "running":
                scheduler = self._scheduler(invocation_id, product, invocation_root, store, ledger)
                try:
                    scheduler.reclaim_expired()
                except LedgerConflictError as error:
                    raise EngineConflictError("another runner advanced the invocation") from error
                except LedgerPublicationIndeterminate as error:
                    raise EnginePublicationIndeterminate(
                        "lease reclamation publication is indeterminate"
                    ) from error
                envelopes = ledger.read_all()
                projection = fold_events(envelopes)
                self._validate_workflow(product, projection)
            self._write_checkpoint(invocation_fd, envelopes, projection)
            return InvocationHandle(
                invocation_id,
                invocation_root,
                product.digest,
                product,
                invocation_fd,
            )
        except BaseException:
            os.close(invocation_fd)
            raise
        finally:
            if claim_fd is not None:
                os.close(claim_fd)

    def run_until_blocked(self, handle: InvocationHandle) -> RunResult:
        _product, _invocation_root, invocation_fd = self._validated_handle(handle)
        claim_fd = self._acquire_runner_claim(invocation_fd)
        try:
            return self._run_until_blocked_claimed(handle)
        finally:
            os.close(claim_fd)

    def _run_until_blocked_claimed(self, handle: InvocationHandle) -> RunResult:
        product, invocation_root, invocation_fd = self._validated_handle(handle)
        ledger = Ledger.at(
            invocation_fd,
            "ledger",
            display_root=invocation_root / "ledger",
        )
        store = handle.workspace
        store.head_tree_id()
        scheduler = self._scheduler(handle.invocation_id, product, invocation_root, store, ledger)

        while True:
            envelopes = ledger.read_all()
            projection = fold_events(envelopes)
            self._validate_projection_identity(handle, projection)
            self._validate_workflow(product, projection)
            result = self._run_result(projection)
            if result is not None:
                self._write_checkpoint(invocation_fd, envelopes, projection)
                return result

            try:
                if scheduler.reclaim_expired():
                    continue
            except LedgerConflictError as error:
                raise EngineConflictError("another runner advanced the invocation") from error
            except LedgerPublicationIndeterminate as error:
                raise EnginePublicationIndeterminate(
                    "lease reclamation publication is indeterminate"
                ) from error

            running_tasks = plan_running_tasks(product.workflow, projection)
            if running_tasks:
                try:
                    asyncio.run(scheduler.resume_running(running_tasks))
                except (LedgerConflictError, LeaseUnavailableError) as error:
                    raise EngineConflictError("another runner owns or advanced the running task") from error
                except LedgerPublicationIndeterminate as error:
                    raise EnginePublicationIndeterminate(
                        "running task publication is indeterminate"
                    ) from error
                except HeadPublicationIndeterminate as error:
                    raise EnginePublicationIndeterminate(
                        "running task publication is indeterminate"
                    ) from error
                except FinalizationRolledBack as error:
                    raise EngineError("running task publication failed and was rolled back") from error
                continue

            plan = plan_next(product.workflow, projection)
            if plan.events:
                self._append(ledger, plan.events, envelopes)
                continue
            if plan.tasks:
                try:
                    asyncio.run(scheduler.run_wave(plan.tasks))
                except LedgerConflictError as error:
                    raise EngineConflictError("another runner advanced the invocation") from error
                except LedgerPublicationIndeterminate as error:
                    raise EnginePublicationIndeterminate("task publication is indeterminate") from error
                except HeadPublicationIndeterminate as error:
                    raise EnginePublicationIndeterminate("task publication is indeterminate") from error
                except FinalizationRolledBack as error:
                    raise EngineError("task publication failed and was rolled back") from error
                continue
            if plan.terminal == "interrupted":
                refreshed = fold_events(ledger.read_all())
                self._validate_workflow(product, refreshed)
                result = self._run_result(refreshed)
                if result is not None:
                    return result
            raise EngineError("running invocation has no planned transition or runnable task")

    def _acquire_runner_claim(self, invocation_fd: int) -> int:
        try:
            descriptor = os.open(
                _RUNNER_LOCK,
                os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=invocation_fd,
            )
        except OSError as error:
            raise EngineError("cannot open the invocation runner claim") from error
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
                raise EngineError("invocation runner claim is not a stable regular file")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise EngineConflictError("another runner holds the invocation claim") from error
            locked = os.fstat(descriptor)
            try:
                current = os.stat(
                    _RUNNER_LOCK,
                    dir_fd=invocation_fd,
                    follow_symlinks=False,
                )
            except OSError as error:
                raise EngineError("invocation runner claim lost its stable anchor") from error
            if (
                not stat.S_ISREG(locked.st_mode)
                or locked.st_nlink != 1
                or not stat.S_ISREG(current.st_mode)
                or current.st_nlink != 1
                or (locked.st_dev, locked.st_ino) != (current.st_dev, current.st_ino)
            ):
                raise EngineError("invocation runner claim lost its stable anchor")
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def resume(
        self,
        handle: InvocationHandle,
        *,
        action: str,
        payload: JSONValue,
    ) -> InvocationHandle:
        _product, _invocation_root, invocation_fd = self._validated_handle(handle)
        claim_fd = self._acquire_runner_claim(invocation_fd)
        try:
            return self._resume_claimed(handle, action=action, payload=payload)
        finally:
            os.close(claim_fd)

    def _resume_claimed(
        self,
        handle: InvocationHandle,
        *,
        action: str,
        payload: JSONValue,
    ) -> InvocationHandle:
        product, invocation_root, invocation_fd = self._validated_handle(handle)
        ledger = Ledger.at(
            invocation_fd,
            "ledger",
            display_root=invocation_root / "ledger",
        )
        envelopes = ledger.read_all()
        projection = fold_events(envelopes)
        self._validate_projection_identity(handle, projection)
        self._validate_workflow(product, projection)
        pending = projection.pending_interrupt
        if projection.status != "running" or pending is None:
            raise EngineError("invocation has no pending interrupt")
        if action not in pending.actions:
            raise EngineError(f"resume action is not allowed: {action!r}")
        activation = next(
            item for item in projection.activations if item.activation_id == pending.activation_id
        )
        graph = next(
            item
            for item in projection.graph_instances
            if item.graph_instance_id == activation.graph_instance_id
        )
        node = product.workflow.graphs[graph.graph_id].nodes[activation.node_id]
        if node.definition.kind != "interrupt":
            raise EngineError("pending interrupt does not reference an interrupt node")
        output: JSONValue = {"action": action, "payload": payload}
        events: tuple[RuntimeEvent, ...] = (
            InterruptResumed(interrupt_id=pending.interrupt_id, action=action, payload=payload),
            NodeCompleted(activation_id=activation.activation_id, output=output),
        )
        self._append(ledger, events, envelopes)
        refreshed = ledger.read_all()
        refreshed_projection = fold_events(refreshed)
        self._validate_workflow(product, refreshed_projection)
        self._write_checkpoint(invocation_fd, refreshed, refreshed_projection)
        return InvocationHandle(
            handle.invocation_id,
            invocation_root,
            handle.product_digest,
            product,
            os.dup(invocation_fd),
        )

    def _scheduler(
        self,
        invocation_id: str,
        product: ResolvedProduct,
        invocation_root: Path,
        store: SnapshotStore,
        ledger: Ledger,
    ) -> Scheduler:
        owner_id = canonical_digest(
            {
                "invocation_id": invocation_id,
                "product_digest": product.digest,
                "role": "engine-scheduler",
            }
        )
        return Scheduler(
            product.registry,
            store,
            ledger,
            self._host or _UnavailableTaskHost(),
            owner_id=owner_id,
            clock=self._clock,
        )

    def _validated_handle(self, handle: InvocationHandle) -> tuple[ResolvedProduct, Path, int]:
        self._assert_namespace_path_current()
        if handle._closed:
            raise EngineError("invocation handle is closed")
        expected_root = self._invocation_root(handle.invocation_id)
        if handle.invocation_root.absolute() != expected_root:
            raise EngineError("invocation handle root is outside the engine namespace")
        product = handle._product
        if product.digest != handle.product_digest:
            raise EngineError("invocation handle product digest mismatch")
        try:
            current_fd = self._open_invocation(handle.invocation_id)
        except EngineError as error:
            raise EngineError("invocation handle no longer names a trusted invocation directory") from error
        try:
            current = os.fstat(current_fd)
            bound = os.fstat(handle._invocation_fd)
            if (current.st_dev, current.st_ino) != (bound.st_dev, bound.st_ino):
                raise EngineError("invocation handle is stale after namespace replacement")
        finally:
            os.close(current_fd)
        return product, expected_root, handle._invocation_fd

    def _assert_namespace_path_current(self) -> None:
        if self._closed:
            raise EngineError("engine is closed")
        try:
            current_fd = _open_absolute_directory(self._invocations_root)
        except (OSError, EngineError) as error:
            raise EngineError("invocation namespace no longer names the trusted directory") from error
        try:
            current = os.fstat(current_fd)
            trusted = os.fstat(self._invocations_fd)
            if (current.st_dev, current.st_ino) != (trusted.st_dev, trusted.st_ino):
                raise EngineError("invocation namespace identity changed")
        finally:
            os.close(current_fd)

    def _validate_workflow(self, product: ResolvedProduct, projection: InvocationProjection) -> None:
        try:
            validate_projection(product.workflow, projection)
        except PlanningError as error:
            raise EngineError(f"invocation does not match resolved product workflow: {error}") from error

    def _validate_projection_identity(
        self, handle: InvocationHandle, projection: InvocationProjection
    ) -> None:
        if (
            projection.invocation_id != handle.invocation_id
            or projection.product_digest != handle.product_digest
        ):
            raise EngineError("invocation handle does not match the authoritative ledger")

    def _invocation_root(self, invocation_id: str) -> Path:
        windows = PureWindowsPath(invocation_id)
        if (
            not invocation_id
            or "\x00" in invocation_id
            or invocation_id in {".", ".."}
            or "/" in invocation_id
            or "\\" in invocation_id
            or windows.is_absolute()
            or bool(windows.drive)
            or os.path.sep in invocation_id
            or (os.path.altsep is not None and os.path.altsep in invocation_id)
        ):
            raise EngineError(f"invalid invocation id: {invocation_id!r}")
        return self._invocations_root / invocation_id

    def _open_invocation(self, invocation_id: str) -> int:
        self._invocation_root(invocation_id)
        try:
            return _open_directory_at(
                self._invocations_fd,
                invocation_id,
                "invocation",
            )
        except FileNotFoundError as error:
            raise EngineError(f"invocation does not exist: {invocation_id}") from error

    def _install_invocation(self, staging_name: str, invocation_id: str) -> None:
        lock_fd = os.open(
            ".namespace.lock",
            os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=self._invocations_fd,
        )
        try:
            if not stat.S_ISREG(os.fstat(lock_fd).st_mode):
                raise EngineError("invocation namespace lock is not a regular file")
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            if _entry_exists(self._invocations_fd, invocation_id):
                raise EngineError(f"invocation already exists: {invocation_id}")
            os.rename(
                staging_name,
                invocation_id,
                src_dir_fd=self._invocations_fd,
                dst_dir_fd=self._invocations_fd,
            )
            os.fsync(self._invocations_fd)
        finally:
            os.close(lock_fd)

    def _raise_duplicate_start(self, invocation_id: str, product: ResolvedProduct) -> None:
        invocation_fd = self._open_invocation(invocation_id)
        try:
            projection = fold_events(
                Ledger.at(
                    invocation_fd,
                    "ledger",
                    display_root=self._invocation_root(invocation_id) / "ledger",
                ).read_all()
            )
        except GraphEngineError:
            return
        finally:
            os.close(invocation_fd)
        if projection.product_digest != product.digest:
            raise EngineError(
                f"resolved product digest mismatch: expected {projection.product_digest}, "
                f"found {product.digest}"
            )

    def _append(
        self,
        ledger: Ledger,
        events: tuple[RuntimeEvent, ...],
        existing: tuple[EventEnvelope, ...],
    ) -> None:
        expected_next_seq = existing[-1].seq + 1 if existing else 1
        try:
            append_validated_batch(ledger, events, expected_next_seq=expected_next_seq)
        except LedgerConflictError as error:
            raise EngineConflictError("another runner advanced the invocation") from error
        except LedgerPublicationIndeterminate as error:
            raise EnginePublicationIndeterminate("ledger publication outcome is indeterminate") from error

    def _write_checkpoint(
        self,
        invocation_fd: int,
        envelopes: tuple[EventEnvelope, ...],
        projection: InvocationProjection,
    ) -> None:
        try:
            write_checkpoint_at(
                invocation_fd,
                "checkpoint.json",
                projection,
                envelopes[-1].seq if envelopes else 0,
                ledger_envelopes=envelopes,
            )
        except (OSError, ValueError):
            pass

    def _run_result(self, projection: InvocationProjection) -> RunResult | None:
        pending = projection.pending_interrupt
        if pending is not None:
            return RunResult(
                status="interrupted",
                terminal_reason=pending.reason,
                actions=pending.actions,
                projection=projection,
            )
        if projection.status == "running":
            return None
        if projection.status == "not_started":
            raise EngineError("authoritative ledger has not started the invocation")
        root = next(
            (item for item in projection.graph_instances if item.parent_graph_instance_id is None),
            None,
        )
        return RunResult(
            status=projection.status,
            output=None if root is None else root.output,
            terminal_reason=projection.terminal_reason,
            projection=projection,
        )


_DIRECTORY_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)


def _open_or_create_namespace(root: Path) -> int:
    try:
        root_fd = _open_or_create_absolute_directory(root)
        try:
            try:
                os.mkdir("invocations", mode=0o700, dir_fd=root_fd)
                os.fsync(root_fd)
            except FileExistsError:
                pass
            return _open_directory_at(root_fd, "invocations", "invocation namespace")
        finally:
            os.close(root_fd)
    except (OSError, EngineError) as error:
        if isinstance(error, EngineError):
            raise
        raise EngineError("cannot establish trusted invocation namespace") from error


def _open_or_create_absolute_directory(path: Path) -> int:
    descriptor = os.open("/", _DIRECTORY_FLAGS)
    try:
        for component in path.absolute().parts[1:]:
            try:
                child = _open_directory_at(descriptor, component, "engine root component")
            except FileNotFoundError:
                try:
                    os.mkdir(component, mode=0o700, dir_fd=descriptor)
                    os.fsync(descriptor)
                except FileExistsError:
                    pass
                child = _open_directory_at(descriptor, component, "engine root component")
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_absolute_directory(path: Path) -> int:
    descriptor = os.open("/", _DIRECTORY_FLAGS)
    try:
        for component in path.absolute().parts[1:]:
            child = _open_directory_at(descriptor, component, "engine root component")
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_directory_at(parent_fd: int, name: str, kind: str) -> int:
    try:
        enumerated = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
    except FileNotFoundError:
        raise
    except OSError as error:
        raise EngineError(f"{kind} must be a trusted no-follow directory: {name}") from error
    opened = os.fstat(descriptor)
    if not stat.S_ISDIR(opened.st_mode) or (opened.st_dev, opened.st_ino) != (
        enumerated.st_dev,
        enumerated.st_ino,
    ):
        os.close(descriptor)
        raise EngineError(f"{kind} identity changed while opening: {name}")
    return descriptor


def _entry_exists(parent_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _remove_entry_at(parent_fd: int, name: str) -> None:
    entry = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if not stat.S_ISDIR(entry.st_mode) or stat.S_ISLNK(entry.st_mode):
        os.unlink(name, dir_fd=parent_fd)
        return
    child_fd = _open_directory_at(parent_fd, name, "owned initialization staging")
    try:
        for child_name in os.listdir(child_fd):
            _remove_entry_at(child_fd, child_name)
        os.fsync(child_fd)
    finally:
        os.close(child_fd)
    os.rmdir(name, dir_fd=parent_fd)


def _initialization_boundary(name: str) -> None:
    del name


__all__ = [
    "Engine",
    "EngineConflictError",
    "EngineError",
    "EnginePublicationIndeterminate",
    "InvocationHandle",
    "RunResult",
]
