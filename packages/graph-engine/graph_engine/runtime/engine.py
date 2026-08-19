from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Literal

from pydantic import BaseModel, ConfigDict

from graph_engine.canonical import JSONValue, canonical_digest
from graph_engine.errors import GraphEngineError
from graph_engine.product import ResolvedProduct
from graph_engine.plugin_api import TaskHandler, TaskOutcome, TaskRequest
from graph_engine.runtime.checkpoint import load_checkpoint, write_checkpoint
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
from graph_engine.runtime.ledger import Ledger, LedgerConflictError
from graph_engine.runtime.models import InvocationProjection, fold_events
from graph_engine.runtime.planner import _start_token_id, plan_next
from graph_engine.runtime.scheduler import Clock, Scheduler, SystemClock, TaskExecutionHost
from graph_engine.runtime.workspace import SnapshotStore


class EngineError(GraphEngineError):
    """Raised when an invocation cannot safely make the requested transition."""


class EngineConflictError(EngineError):
    """Raised when another runner wins an invocation transition."""


@dataclass(frozen=True, slots=True)
class InvocationHandle:
    invocation_id: str
    invocation_root: Path
    product_digest: str

    @property
    def workspace(self) -> SnapshotStore:
        return SnapshotStore(self.invocation_root / "workspace")


_RESULT_CONFIG = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


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
        self._products: dict[str, ResolvedProduct] = {}

    def start(
        self,
        product: ResolvedProduct,
        *,
        entrypoint: str,
        invocation_id: str,
    ) -> InvocationHandle:
        invocation_root = self._invocation_root(invocation_id)
        if entrypoint not in product.workflow.entrypoints:
            raise EngineError(f"unknown entrypoint {entrypoint!r}")
        self._products[product.digest] = product
        self._invocations_root.mkdir(parents=True, exist_ok=True)
        try:
            invocation_root.mkdir(mode=0o700)
        except FileExistsError as error:
            self._raise_duplicate_start(invocation_root, product)
            raise EngineError(f"invocation already exists: {invocation_id}") from error

        store = SnapshotStore.create(invocation_root / "workspace", {})
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
        ledger = Ledger(invocation_root / "ledger")
        self._append(ledger, events, ())
        envelopes = ledger.read_all()
        projection = fold_events(envelopes)
        self._write_checkpoint(invocation_root, envelopes, projection)
        assert store.head_tree_id()
        return InvocationHandle(invocation_id, invocation_root, product.digest)

    def open(self, invocation_id: str, product: ResolvedProduct) -> InvocationHandle:
        invocation_root = self._invocation_root(invocation_id)
        if not invocation_root.is_dir() or invocation_root.is_symlink():
            raise EngineError(f"invocation does not exist: {invocation_id}")
        ledger = Ledger(invocation_root / "ledger")
        envelopes = ledger.read_all()
        projection = fold_events(envelopes)
        if projection.invocation_id != invocation_id:
            raise EngineError("invocation ledger identity does not match its path")
        if projection.product_digest != product.digest:
            raise EngineError(
                f"resolved product digest mismatch: expected {projection.product_digest}, "
                f"found {product.digest}"
            )
        self._products[product.digest] = product
        checkpoint = load_checkpoint(invocation_root / "checkpoint.json", ledger_envelopes=envelopes)
        if checkpoint is not None:
            projection = checkpoint.projection
        store = SnapshotStore(invocation_root / "workspace")
        store.head_tree_id()
        if projection.status == "running":
            scheduler = self._scheduler(invocation_id, product, invocation_root, store, ledger)
            scheduler.reclaim_expired()
            envelopes = ledger.read_all()
            projection = fold_events(envelopes)
        self._write_checkpoint(invocation_root, envelopes, projection)
        return InvocationHandle(invocation_id, invocation_root, product.digest)

    def run_until_blocked(self, handle: InvocationHandle) -> RunResult:
        product, invocation_root = self._validated_handle(handle)
        ledger = Ledger(invocation_root / "ledger")
        store = handle.workspace
        store.head_tree_id()
        scheduler = self._scheduler(handle.invocation_id, product, invocation_root, store, ledger)

        while True:
            envelopes = ledger.read_all()
            projection = fold_events(envelopes)
            self._validate_projection_identity(handle, projection)
            result = self._run_result(projection)
            if result is not None:
                self._write_checkpoint(invocation_root, envelopes, projection)
                return result

            try:
                if scheduler.reclaim_expired():
                    continue
            except LedgerConflictError as error:
                raise EngineConflictError("another runner advanced the invocation") from error

            plan = plan_next(product.workflow, projection)
            if plan.events:
                self._append(ledger, plan.events, envelopes)
                continue
            if plan.tasks:
                try:
                    asyncio.run(scheduler.run_wave(plan.tasks))
                except LedgerConflictError as error:
                    raise EngineConflictError("another runner advanced the invocation") from error
                continue
            if plan.terminal == "interrupted":
                refreshed = fold_events(ledger.read_all())
                result = self._run_result(refreshed)
                if result is not None:
                    return result
            raise EngineError("running invocation has no planned transition or runnable task")

    def resume(
        self,
        handle: InvocationHandle,
        *,
        action: str,
        payload: JSONValue,
    ) -> InvocationHandle:
        product, invocation_root = self._validated_handle(handle)
        ledger = Ledger(invocation_root / "ledger")
        envelopes = ledger.read_all()
        projection = fold_events(envelopes)
        self._validate_projection_identity(handle, projection)
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
        self._write_checkpoint(invocation_root, refreshed, fold_events(refreshed))
        return InvocationHandle(handle.invocation_id, invocation_root, handle.product_digest)

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

    def _validated_handle(self, handle: InvocationHandle) -> tuple[ResolvedProduct, Path]:
        expected_root = self._invocation_root(handle.invocation_id)
        if handle.invocation_root.absolute() != expected_root:
            raise EngineError("invocation handle root is outside the engine namespace")
        product = self._products.get(handle.product_digest)
        if product is None:
            raise EngineError("invocation handle product is not open in this engine")
        if product.digest != handle.product_digest:
            raise EngineError("invocation handle product digest mismatch")
        if not expected_root.is_dir() or expected_root.is_symlink():
            raise EngineError("invocation handle no longer names a trusted invocation directory")
        return product, expected_root

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

    def _raise_duplicate_start(self, invocation_root: Path, product: ResolvedProduct) -> None:
        try:
            projection = fold_events(Ledger(invocation_root / "ledger").read_all())
        except GraphEngineError:
            return
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
        fold_events(
            existing
            + tuple(
                EventEnvelope.from_event(expected_next_seq + offset, event)
                for offset, event in enumerate(events)
            )
        )
        try:
            ledger.append_batch(events, expected_next_seq=expected_next_seq)
        except LedgerConflictError as error:
            raise EngineConflictError("another runner advanced the invocation") from error
        except BaseException:
            persisted = ledger.read_all()
            expected = tuple(
                EventEnvelope.from_event(expected_next_seq + offset, event)
                for offset, event in enumerate(events)
            )
            offset = expected_next_seq - 1
            if persisted[offset : offset + len(expected)] == expected:
                return
            raise

    def _write_checkpoint(
        self,
        invocation_root: Path,
        envelopes: tuple[EventEnvelope, ...],
        projection: InvocationProjection,
    ) -> None:
        try:
            write_checkpoint(
                invocation_root / "checkpoint.json",
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


__all__ = [
    "Engine",
    "EngineConflictError",
    "EngineError",
    "InvocationHandle",
    "RunResult",
]
