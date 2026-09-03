from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Literal, Protocol, cast

from langgraph.errors import GraphRecursionError
from langgraph.graph import START
from langgraph.types import Command

from graph_engine.application.revision_guard import require_revision
from graph_engine.application.runtime_context import AssuranceRuntimeContext
from graph_engine.application.status import (
    GraphSnapshotEnvelope,
    InterruptEnvelope,
    InterruptKind,
    InvocationStatus,
    TerminalEnvelope,
    normalize_graph_snapshot,
    normalize_runtime_error,
    normalize_terminal_envelope,
)
from graph_engine.attempts.resolutions import IndeterminateTaskResult, PendingTaskResult
from graph_engine.boot.graph_revision import BootArtifact
from graph_engine.canonical import JSONValue, canonical_digest
from graph_engine.errors import GraphEngineError
from graph_engine.persistence.journal import InvocationStarted
from graph_engine.persistence.runner_lease import InvocationRunnerLeasePort, RunnerLease
from graph_engine.plugin_api import FrozenModel


DEFAULT_RECURSION_LIMIT = 2048
CONFIGURABLE_KEYS = (
    "thread_id",
    "assurance_revision_id",
    "assurance_product_lock_digest",
    "assurance_root_input_digest",
    "assurance_fencing_token",
    "assurance_initial_checkpoint",
)


class InvocationStartPinPort(Protocol):
    async def pin_start(
        self,
        record: InvocationStarted,
        entrypoint: str,
        *,
        fencing_token: int,
    ) -> None: ...


class AmbiguousResume(GraphEngineError):
    """Raised when a scalar resume cannot be mapped to one pending interrupt."""


class InvalidResume(GraphEngineError):
    """Raised when a resume envelope does not match the pending interrupt kind."""


class HumanResumeAction(FrozenModel):
    action: Literal["approve", "reject", "rework"]


class _CompiledGraph(Protocol):
    async def aupdate_state(self, config: object, values: object, as_node: object) -> object: ...

    async def ainvoke(self, input: object, config: object) -> object: ...

    async def aget_state(self, config: object) -> object: ...


@dataclass(frozen=True, slots=True)
class StartedInvocation:
    thread_id: str
    revision_id: str
    invocation_id: str
    entrypoint: str

    def __post_init__(self) -> None:
        if self.thread_id != self.invocation_id:
            raise ValueError("thread id must equal invocation id")


class AssuranceApplication:
    def __init__(
        self,
        *,
        lease: InvocationRunnerLeasePort,
        owner_id: str,
        recursion_limits: Mapping[str, int] | None = None,
        start_pins: InvocationStartPinPort | None = None,
    ) -> None:
        if not owner_id:
            raise ValueError("owner id must be nonempty")
        self._lease = lease
        self._owner_id = owner_id
        self._recursion_limits = dict(recursion_limits or {})
        self._start_pins = start_pins
        self._started: dict[str, StartedInvocation] = {}

    async def start(
        self,
        *,
        artifact: BootArtifact,
        invocation_id: str,
        entrypoint: str,
        graph_input: Mapping[str, JSONValue],
        runtime_context: AssuranceRuntimeContext,
    ) -> StartedInvocation:
        async with self._hold_lease(invocation_id) as lease:
            return await self._start_locked(
                artifact=artifact,
                invocation_id=invocation_id,
                entrypoint=entrypoint,
                graph_input=graph_input,
                runtime_context=runtime_context,
                lease=lease,
            )

    async def start_and_run(
        self,
        *,
        artifact: BootArtifact,
        invocation_id: str,
        entrypoint: str,
        graph_input: Mapping[str, JSONValue],
        runtime_context: AssuranceRuntimeContext,
    ) -> InvocationStatus:
        async with self._hold_lease(invocation_id) as lease:
            await self._start_locked(
                artifact=artifact,
                invocation_id=invocation_id,
                entrypoint=entrypoint,
                graph_input=graph_input,
                runtime_context=runtime_context,
                lease=lease,
            )
            return await self._run_locked(
                artifact=artifact,
                invocation_id=invocation_id,
                runtime_context=runtime_context,
                lease=lease,
                command=None,
            )

    async def run(
        self,
        *,
        artifact: BootArtifact,
        invocation_id: str,
        runtime_context: AssuranceRuntimeContext,
    ) -> InvocationStatus:
        async with self._hold_lease(invocation_id) as lease:
            return await self._run_locked(
                artifact=artifact,
                invocation_id=invocation_id,
                runtime_context=runtime_context,
                lease=lease,
                command=None,
            )

    async def resume(
        self,
        *,
        artifact: BootArtifact,
        invocation_id: str,
        runtime_context: AssuranceRuntimeContext,
        resume: object,
    ) -> InvocationStatus:
        async with self._hold_lease(invocation_id) as lease:
            snapshot = await self._read_snapshot(artifact, invocation_id, runtime_context)
            payload = _resume_payload(resume, tuple(getattr(snapshot, "interrupts", ())))
            return await self._run_locked(
                artifact=artifact,
                invocation_id=invocation_id,
                runtime_context=runtime_context,
                lease=lease,
                command=Command(resume=payload),
                snapshot=snapshot,
            )

    async def status(
        self,
        *,
        artifact: BootArtifact,
        invocation_id: str,
        runtime_context: AssuranceRuntimeContext,
    ) -> InvocationStatus:
        snapshot = await self._read_snapshot(artifact, invocation_id, runtime_context)
        return _status_from_snapshot(snapshot)

    async def _start_locked(
        self,
        *,
        artifact: BootArtifact,
        invocation_id: str,
        entrypoint: str,
        graph_input: Mapping[str, JSONValue],
        runtime_context: AssuranceRuntimeContext,
        lease: RunnerLease,
    ) -> StartedInvocation:
        revision_id = _require_context_revision(artifact, runtime_context)
        graph = _graph(artifact, entrypoint)
        root_input_digest = canonical_digest(dict(graph_input))
        if self._start_pins is not None:
            await self._start_pins.pin_start(
                InvocationStarted(
                    invocation_id=invocation_id,
                    thread_id=invocation_id,
                    graph_revision=revision_id,
                    product_lock_digest=artifact.manifest.revision.product_lock_digest,
                    root_input_digest=root_input_digest,
                    fencing_token=lease.fencing_token,
                ),
                entrypoint,
                fencing_token=lease.fencing_token,
            )
        config = self._config(
            artifact,
            invocation_id=invocation_id,
            entrypoint=entrypoint,
            root_input_digest=root_input_digest,
            fencing_token=lease.fencing_token,
            initial_checkpoint=True,
        )
        await graph.aupdate_state(config, dict(graph_input), as_node=START)
        started = StartedInvocation(
            thread_id=invocation_id,
            revision_id=revision_id,
            invocation_id=invocation_id,
            entrypoint=entrypoint,
        )
        self._started[invocation_id] = started
        return started

    async def _run_locked(
        self,
        *,
        artifact: BootArtifact,
        invocation_id: str,
        runtime_context: AssuranceRuntimeContext,
        lease: RunnerLease,
        command: Command | None,
        snapshot: object | None = None,
    ) -> InvocationStatus:
        if snapshot is None:
            snapshot = await self._read_snapshot(artifact, invocation_id, runtime_context)
        else:
            _require_context_revision(artifact, runtime_context)
            _require_pinned_revision(artifact, snapshot)
        entrypoint = self._entrypoint_for(artifact, invocation_id)
        graph = _graph(artifact, entrypoint)
        config = self._config(
            artifact,
            invocation_id=invocation_id,
            entrypoint=entrypoint,
            root_input_digest=_required_metadata(snapshot, "assurance_root_input_digest"),
            fencing_token=lease.fencing_token,
            initial_checkpoint=False,
        )
        try:
            await graph.ainvoke(command, config)
        except GraphRecursionError as error:
            return normalize_runtime_error(error)
        return _status_from_snapshot(await graph.aget_state(config))

    async def _read_snapshot(
        self,
        artifact: BootArtifact,
        invocation_id: str,
        runtime_context: AssuranceRuntimeContext,
    ) -> object:
        _require_context_revision(artifact, runtime_context)
        entrypoint = self._entrypoint_for(artifact, invocation_id)
        graph = _graph(artifact, entrypoint)
        snapshot = await graph.aget_state({"configurable": {"thread_id": invocation_id}})
        _require_pinned_revision(artifact, snapshot)
        return snapshot

    def _entrypoint_for(self, artifact: BootArtifact, invocation_id: str) -> str:
        started = self._started.get(invocation_id)
        if started is not None:
            return started.entrypoint
        if len(artifact.entrypoints) == 1:
            return next(iter(artifact.entrypoints))
        raise ValueError("entrypoint is unknown for invocation")

    def _config(
        self,
        artifact: BootArtifact,
        *,
        invocation_id: str,
        entrypoint: str,
        root_input_digest: str,
        fencing_token: int,
        initial_checkpoint: bool,
    ) -> dict[str, object]:
        configurable = {
            "thread_id": invocation_id,
            "assurance_revision_id": artifact.manifest.revision.revision_id,
            "assurance_product_lock_digest": artifact.manifest.revision.product_lock_digest,
            "assurance_root_input_digest": root_input_digest,
            "assurance_fencing_token": fencing_token,
            "assurance_initial_checkpoint": initial_checkpoint,
        }
        if tuple(configurable) != CONFIGURABLE_KEYS:
            raise ValueError("application config keys drifted")
        return {
            "recursion_limit": self._recursion_limits.get(entrypoint, DEFAULT_RECURSION_LIMIT),
            "configurable": configurable,
        }

    @asynccontextmanager
    async def _hold_lease(self, invocation_id: str) -> AsyncIterator[RunnerLease]:
        lease = await self._lease.acquire(invocation_id, owner_id=self._owner_id)
        try:
            yield lease
        finally:
            await self._lease.release(lease)


def _graph(artifact: BootArtifact, entrypoint: str) -> _CompiledGraph:
    try:
        return cast(_CompiledGraph, artifact.entrypoints[entrypoint])
    except KeyError as error:
        raise ValueError(f"unknown entrypoint: {entrypoint}") from error


def _require_context_revision(artifact: BootArtifact, runtime_context: AssuranceRuntimeContext) -> str:
    return require_revision(
        required=artifact.manifest.revision.revision_id,
        installed=runtime_context.revision_id,
    )


def _require_pinned_revision(artifact: BootArtifact, snapshot: object) -> str:
    stored = _required_metadata(snapshot, "assurance_revision_id")
    return require_revision(required=stored, installed=artifact.manifest.revision.revision_id)


def _required_metadata(snapshot: object, key: str) -> str:
    metadata = getattr(snapshot, "metadata", None)
    if not isinstance(metadata, Mapping) or key not in metadata:
        raise ValueError("invocation has not started")
    value = metadata[key]
    if not isinstance(value, str) or not value:
        raise ValueError("invocation identity is missing")
    return value


def _status_from_snapshot(snapshot: object) -> InvocationStatus:
    values = getattr(snapshot, "values", {})
    raw_terminal = values.get("terminal") if isinstance(values, Mapping) else None
    if raw_terminal:
        return normalize_terminal_envelope(TerminalEnvelope.model_validate(raw_terminal))
    return normalize_graph_snapshot(_graph_snapshot(snapshot))


def _graph_snapshot(snapshot: object) -> GraphSnapshotEnvelope:
    interrupts = tuple(_interrupt_envelope(item) for item in getattr(snapshot, "interrupts", ()))
    return GraphSnapshotEnvelope(next=tuple(getattr(snapshot, "next", ())), interrupts=interrupts)


def _interrupt_envelope(item: object) -> InterruptEnvelope:
    value = getattr(item, "value", item)
    kind: InterruptKind = "human"
    reason: str | None = None
    if isinstance(value, Mapping):
        raw_kind = value.get("kind")
        if raw_kind in {"human", "system_wake", "system_block"}:
            kind = cast(InterruptKind, raw_kind)
        raw_reason = value.get("reason")
        if isinstance(raw_reason, str):
            reason = raw_reason
    return InterruptEnvelope(kind=kind, reason=reason)


def _resume_payload(resume: object, pending: tuple[object, ...]) -> object:
    first = next(iter(pending), None)
    if first is None:
        raise InvalidResume("no pending interrupt")
    if len(pending) > 1 and not isinstance(resume, dict):
        raise AmbiguousResume("multiple pending interrupts require {interrupt_id: validated_value}")
    if isinstance(resume, dict) and all(hasattr(item, "id") for item in pending):
        pending_ids = {getattr(item, "id") for item in pending}
        if set(resume) != pending_ids:
            raise InvalidResume("resume mapping must cover pending interrupt ids")
        return {getattr(item, "id"): _validate_one(resume[getattr(item, "id")], item) for item in pending}
    return _validate_one(resume, first)


def _validate_one(resume: object, item: object) -> object:
    if _interrupt_kind(item) == "human":
        return _validate_human(resume, item)
    return _validate_system(resume)


def _interrupt_kind(item: object) -> InterruptKind:
    return _interrupt_envelope(item).kind


def _validate_human(resume: object, item: object) -> str:
    action = (
        HumanResumeAction(action=cast(Literal["approve", "reject", "rework"], resume)).action
        if isinstance(resume, str)
        else HumanResumeAction.model_validate(resume).action
    )
    actions = _human_actions(item)
    if actions is not None and action not in actions:
        raise InvalidResume(f"action {action!r} is not allowed for this interrupt")
    return action


def _human_actions(item: object) -> tuple[str, ...] | None:
    value = getattr(item, "value", item)
    if not isinstance(value, Mapping):
        return None
    actions = value.get("actions")
    if isinstance(actions, list) and all(isinstance(action, str) for action in actions):
        return tuple(actions)
    return None


def _validate_system(resume: object) -> dict[str, object]:
    if isinstance(resume, PendingTaskResult):
        return resume.model_dump(mode="json")
    if isinstance(resume, IndeterminateTaskResult):
        return resume.model_dump(mode="json")
    if isinstance(resume, Mapping):
        if "wakeup" in resume:
            return PendingTaskResult.model_validate(resume).model_dump(mode="json")
        if "reconciliation" in resume:
            return IndeterminateTaskResult.model_validate(resume).model_dump(mode="json")
    raise InvalidResume("system interrupt accepts only its wakeup/reconciliation envelope")


__all__ = [
    "AmbiguousResume",
    "AssuranceApplication",
    "CONFIGURABLE_KEYS",
    "HumanResumeAction",
    "InvalidResume",
    "InvocationStartPinPort",
    "StartedInvocation",
]
