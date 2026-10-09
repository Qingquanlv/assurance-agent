from __future__ import annotations

import inspect
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Literal, Protocol, cast

from langgraph.errors import GraphRecursionError
from langgraph.graph import START
from langgraph.types import Command

from graph_engine.plugin_api import WorkspaceProvider

from graph_engine.application.revision_guard import require_revision
from graph_engine.application.runtime_context import (
    AssuranceRuntimeContext,
    AttemptKernelPort,
    SecretResolverPort,
)
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
from graph_engine.attempts.models.resolutions import IndeterminateTaskResult, PendingTaskResult
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
    reason: str | None = None


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


@dataclass(frozen=True, slots=True)
class InvocationBoundExecution:
    artifact: BootArtifact
    runtime_context: AssuranceRuntimeContext
    recover_outbox: Callable[[], Awaitable[None]] | None = None


class InvocationBoundExecutionFactory(Protocol):
    def bind(self, runner_lease: RunnerLease) -> InvocationBoundExecution: ...


@dataclass(frozen=True, slots=True)
class FixedExecutionFactory:
    artifact: BootArtifact
    attempt_kernel: AttemptKernelPort
    secret_resolver: SecretResolverPort
    workspace_provider: WorkspaceProvider

    def bind(self, runner_lease: RunnerLease) -> InvocationBoundExecution:
        if not isinstance(runner_lease, RunnerLease):
            raise ValueError("fencing token")
        return InvocationBoundExecution(
            artifact=self.artifact,
            runtime_context=AssuranceRuntimeContext(
                revision_id=self.artifact.manifest.revision.revision_id,
                fencing_token=runner_lease.fencing_token,
                attempt_kernel=self.attempt_kernel,
                secret_resolver=self.secret_resolver,
                workspace_provider=self.workspace_provider,
            ),
        )


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
        invocation_id: str,
        entrypoint: str,
        graph_input: Mapping[str, JSONValue],
        execution_factory: InvocationBoundExecutionFactory,
    ) -> StartedInvocation:
        async with self._hold_lease(invocation_id, execution_factory) as (lease, bound):
            return await self._start_locked(
                artifact=bound.artifact,
                invocation_id=invocation_id,
                entrypoint=entrypoint,
                graph_input=graph_input,
                runtime_context=bound.runtime_context,
                lease=lease,
            )

    async def start_and_run(
        self,
        *,
        invocation_id: str,
        entrypoint: str,
        graph_input: Mapping[str, JSONValue],
        execution_factory: InvocationBoundExecutionFactory,
    ) -> InvocationStatus:
        async with self._hold_lease(invocation_id, execution_factory) as (lease, bound):
            await self._start_locked(
                artifact=bound.artifact,
                invocation_id=invocation_id,
                entrypoint=entrypoint,
                graph_input=graph_input,
                runtime_context=bound.runtime_context,
                lease=lease,
            )
            return await self._run_locked(
                artifact=bound.artifact,
                invocation_id=invocation_id,
                runtime_context=bound.runtime_context,
                lease=lease,
                command=None,
            )

    async def run(
        self,
        *,
        invocation_id: str,
        execution_factory: InvocationBoundExecutionFactory,
    ) -> InvocationStatus:
        async with self._hold_lease(invocation_id, execution_factory) as (lease, bound):
            snapshot = await self._read_snapshot(bound.artifact, invocation_id, bound.runtime_context)
            return await self._run_locked(
                artifact=bound.artifact,
                invocation_id=invocation_id,
                runtime_context=bound.runtime_context,
                lease=lease,
                command=_system_wake_command(tuple(getattr(snapshot, "interrupts", ()))),
                snapshot=snapshot,
            )

    async def resume(
        self,
        *,
        invocation_id: str,
        execution_factory: InvocationBoundExecutionFactory,
        resume: object,
    ) -> InvocationStatus:
        async with self._hold_lease(invocation_id, execution_factory) as (lease, bound):
            snapshot = await self._read_snapshot(bound.artifact, invocation_id, bound.runtime_context)
            payload = _resume_payload(resume, tuple(getattr(snapshot, "interrupts", ())))
            return await self._run_locked(
                artifact=bound.artifact,
                invocation_id=invocation_id,
                runtime_context=bound.runtime_context,
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
        snapshot = await self._read_snapshot(
            _read_only_artifact(artifact),
            invocation_id,
            runtime_context,
        )
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
        _require_matching_tokens(
            lease,
            runtime_context,
            config_token=lease.fencing_token,
            stored_token=None,
            artifact=artifact,
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
        _require_matching_tokens(
            lease,
            runtime_context,
            config_token=lease.fencing_token,
            stored_token=None,
            artifact=artifact,
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
        config = {
            "recursion_limit": self._recursion_limits.get(entrypoint, DEFAULT_RECURSION_LIMIT),
            "configurable": configurable,
        }
        if fencing_token < 1:
            raise ValueError("fencing token")
        return config

    @asynccontextmanager
    async def _hold_lease(
        self,
        invocation_id: str,
        factory: InvocationBoundExecutionFactory,
    ) -> AsyncIterator[tuple[RunnerLease, InvocationBoundExecution]]:
        lease = await self._lease.acquire(invocation_id, owner_id=self._owner_id)
        bound: InvocationBoundExecution | None = None
        try:
            bound = factory.bind(lease)
            _require_bound_fence(lease, bound)
            yield lease, bound
        finally:
            if bound is not None:
                await _recover_bound(bound, invocation_id)
            await self._lease.release(lease)


def _require_bound_fence(lease: RunnerLease, bound: InvocationBoundExecution) -> None:
    _require_matching_tokens(
        lease,
        bound.runtime_context,
        config_token=bound.runtime_context.fencing_token,
        stored_token=None,
        artifact=bound.artifact,
    )


def _require_matching_tokens(
    lease: RunnerLease,
    runtime_context: AssuranceRuntimeContext,
    *,
    config_token: int,
    stored_token: int | None,
    artifact: BootArtifact,
) -> None:
    token = lease.fencing_token
    if token < 1:
        raise ValueError("fencing token")
    if runtime_context.fencing_token != token:
        raise ValueError("fencing token mismatch")
    if config_token != token:
        raise ValueError("fencing token mismatch")
    identity = _checkpointer_identity(artifact)
    if identity is not None and identity != token:
        raise ValueError("fencing token mismatch")
    if stored_token is not None and stored_token != token:
        raise ValueError("fencing token mismatch")


def _checkpointer_identity(artifact: BootArtifact) -> int | None:
    for graph in artifact.entrypoints.values():
        checkpointer = getattr(graph, "checkpointer", None)
        identity = getattr(checkpointer, "_identity", None)
        if identity is not None:
            return int(identity.fencing_token)
    return None


async def _recover_bound(bound: InvocationBoundExecution, invocation_id: str) -> None:
    if bound.recover_outbox is not None:
        await bound.recover_outbox()
        return
    for graph in bound.artifact.entrypoints.values():
        checkpointer = getattr(graph, "checkpointer", None)
        recover = getattr(checkpointer, "arecover", None)
        if callable(recover):
            outcome = recover(thread_id=invocation_id)
            if inspect.isawaitable(outcome):
                await outcome
            return


class _ReadOnlyGraph:
    def __init__(self, graph: object) -> None:
        self._graph = graph

    async def aget_state(self, config: object) -> object:
        return await self._graph.aget_state(config)  # type: ignore[no-any-return]

    async def aupdate_state(self, config: object, values: object, as_node: object) -> object:
        del config, values, as_node
        raise RuntimeError("read-only invocation view cannot mutate")

    async def ainvoke(self, input: object, config: object) -> object:
        del input, config
        raise RuntimeError("read-only invocation view cannot mutate")


def _read_only_artifact(artifact: BootArtifact) -> BootArtifact:
    return BootArtifact(
        manifest=artifact.manifest,
        entrypoints={name: _ReadOnlyGraph(graph) for name, graph in artifact.entrypoints.items()},  # type: ignore[arg-type]
        attempt_contracts=artifact.attempt_contracts,
        checkpointer_backend_id=artifact.checkpointer_backend_id,
    )


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
    if len(pending) == 1 and isinstance(resume, dict) and getattr(first, "id", None) not in resume:
        return _validate_one(resume, first)
    if len(pending) > 1 and not isinstance(resume, dict):
        raise AmbiguousResume("multiple pending interrupts require {interrupt_id: validated_value}")
    if isinstance(resume, dict) and all(hasattr(item, "id") for item in pending):
        pending_ids = {getattr(item, "id") for item in pending}
        if set(resume) != pending_ids:
            raise InvalidResume("resume mapping must cover pending interrupt ids")
        return {getattr(item, "id"): _validate_one(resume[getattr(item, "id")], item) for item in pending}
    return _validate_one(resume, first)


def _system_wake_command(pending: tuple[object, ...]) -> Command | None:
    if not pending or any(_interrupt_kind(item) != "system_wake" for item in pending):
        return None
    resumptions = tuple(_pending_wakeup(item) for item in pending)
    resume: object
    if len(pending) == 1:
        resume = resumptions[0]
    else:
        if not all(hasattr(item, "id") for item in pending):
            raise InvalidResume("multiple system wake interrupts require interrupt ids")
        resume = {getattr(item, "id"): value for item, value in zip(pending, resumptions, strict=True)}
    return Command(resume=_resume_payload(resume, pending))


def _pending_wakeup(item: object) -> PendingTaskResult:
    value = getattr(item, "value", item)
    if not isinstance(value, Mapping) or "wakeup" not in value:
        raise InvalidResume("system wake interrupt is missing its wakeup envelope")
    return PendingTaskResult.model_validate({"wakeup": value["wakeup"]})


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
    "FixedExecutionFactory",
    "HumanResumeAction",
    "InvalidResume",
    "InvocationBoundExecution",
    "InvocationBoundExecutionFactory",
    "InvocationStartPinPort",
    "StartedInvocation",
]
