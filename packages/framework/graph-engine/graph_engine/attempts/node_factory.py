from __future__ import annotations

import asyncio
from dataclasses import replace
from collections.abc import Callable, Mapping
from typing import Any

from langgraph.config import get_config
from langgraph.errors import GraphInterrupt
from langgraph.types import Interrupt, interrupt as langgraph_interrupt
from pydantic import BaseModel, ValidationError

from graph_engine.attempts.context import AttemptExecutionContext
from graph_engine.attempts.contracts import ResolvedAttemptContract, TaskAttemptContract
from graph_engine.attempts.checkpoint import ActiveSystemInterrupt, AttemptCheckpoint
from graph_engine.attempts.keys import AttemptKey, BusinessActivation, AttemptIdentity
from graph_engine.attempts.resolutions import (
    CommittedTaskResult,
    IndeterminateTaskResult,
    PendingTaskResult,
    PermanentTaskFailure,
    RejectedTaskResult,
)
from graph_engine.canonical import JSONValue, canonical_digest
from graph_engine.persistence.attempt_checkpoint import AttemptCheckpointStore
from graph_engine.stategraph.publish import call_publish
from graph_engine.stategraph.checkpoint_bridge import (
    CHECKPOINT_MARKERS_STATE_KEY,
    CheckpointBridgeMarker,
    MAX_ACTIVE_GENERATIONS,
    omit_checkpoint_bridge_fields,
    replace_checkpoint_marker_batch,
)


def interrupt(value: object) -> object:
    try:
        return langgraph_interrupt(value)
    except RuntimeError as error:
        if "outside of a runnable context" not in str(error):
            raise
        raise GraphInterrupt((Interrupt(value=value),)) from error


class AttemptNodeFactory:
    def __init__(
        self,
        *,
        checkpoints: AttemptCheckpointStore,
        kernel: object | None = None,
        trace: list[str] | None = None,
        regenerate: bool = False,
    ) -> None:
        self._regenerate = regenerate
        self._checkpoints = checkpoints
        self._kernel = kernel
        self.trace = [] if trace is None else trace

    def attempt(
        self,
        contract: TaskAttemptContract[Any, Any] | ResolvedAttemptContract[Any, Any],
        *,
        semantic_node_id: str,
        activation: object,
        select: object,
        publish: object,
    ) -> Callable[..., Any]:
        async def node(state: Any, runtime: Any = None) -> Any:
            return await self._run(
                contract,
                semantic_node_id=semantic_node_id,
                activation=activation,
                select=select,
                publish=publish,
                state=state,
                runtime=runtime,
            )

        return node

    async def _run(
        self,
        contract: TaskAttemptContract[Any, Any] | ResolvedAttemptContract[Any, Any],
        *,
        semantic_node_id: str,
        activation: object,
        select: object,
        publish: object,
        state: Any,
        runtime: Any,
    ) -> Any:
        self.trace.clear()
        invocation_id, revision, fencing_token, entrypoint, kernel = _runtime_identity(runtime, self._kernel)
        task = _task_contract(contract)
        business = _resolve_activation(activation, state)
        selected_state = omit_checkpoint_bridge_fields(state) if isinstance(state, Mapping) else state
        if not callable(select):
            raise TypeError("select must be callable")
        raw = select(selected_state)
        try:
            validated = _validated_selection(raw, task)
        except ValidationError as error:
            return {
                "attempt_failure": {
                    "resolution_kind": "permanent",
                    "kind": "invalid_input",
                    "message": str(error),
                    "writes_promoted": False,
                }
            }
        if kernel is None:
            raise TypeError("attempt kernel is required")
        completions: list[tuple[AttemptKey, ActiveSystemInterrupt]] = []
        seed_from: AttemptKey | None = None
        identity = AttemptIdentity(
            invocation_id=invocation_id,
            graph_revision=revision,
            public_entrypoint=entrypoint,
            semantic_node_id=semantic_node_id,
            business_activation=business,
            contract_id=task.contract_id,
        )
        scope: dict[str, JSONValue] = {
            **identity.model_dump(mode="json"),
            "contract_digest": canonical_digest(task.canonical_projection()),
        }
        latest = await self._checkpoints.latest_generation(scope) if self._regenerate else None
        waiting = None
        if latest is not None:
            previous = await self._checkpoints.load(latest[1])
            if previous is not None and _active_issued(previous):
                if latest[2]:
                    completions.extend((latest[1], item) for item in _active_issued(previous))
                else:
                    waiting = latest
        for local_attempt in range(1, task.retry.max_attempts + 1):

            def make_key(ordinal: int) -> AttemptKey:
                return identity.derive_key(validated.model_dump(mode="json"), technical_attempt=ordinal)

            if waiting is not None:
                technical_attempt, key, _, saved_input = waiting
                if saved_input is not None:
                    validated = _validated_selection(saved_input, task)
                if make_key(technical_attempt) != key:
                    raise ValueError("waiting generation input identity drifted")
                waiting = None
            elif self._regenerate:
                registered = await self._checkpoints.register_generation(
                    scope,
                    make_key,
                    max_attempts=task.retry.max_attempts,
                    validated_input=validated.model_dump(mode="json"),
                )
                if registered is None:
                    return _map_resolution(
                        PermanentTaskFailure(
                            kind="internal", message="attempt generation budget exhausted", retryable=False
                        ),
                        state,
                        publish,
                        tuple(completions),
                    )
                technical_attempt, key = registered
            else:
                technical_attempt = local_attempt
                key = make_key(technical_attempt)
            snapshot = await self._checkpoints.load(key)
            issued = _active_issued(snapshot)
            for item in issued:
                self.trace.append(f"interrupt:generation={item.generation}:ordinal={item.ordinal}")
                interrupt(_interrupt_payload(key, item))
            if issued:
                self.trace.append("kernel:recover")
            context = AttemptExecutionContext(
                invocation_id=invocation_id,
                public_entrypoint=entrypoint,
                semantic_node_id=semantic_node_id,
                attempt_key=key,
                fencing_token=fencing_token,
                seed_attempt_key=seed_from,
            )
            resolution = await kernel.execute_or_recover(key, contract, validated, context)
            completions.extend((key, item) for item in issued)
            if isinstance(resolution, (PendingTaskResult, IndeterminateTaskResult)):
                await self._issue_interrupt(
                    key,
                    context=context,
                    issued=issued,
                    resolution=resolution,
                )
            if (
                isinstance(resolution, PermanentTaskFailure)
                and resolution.retryable
                and technical_attempt < task.retry.max_attempts
            ):
                if resolution.kind == "invalid_output":
                    validated = _with_latest_validation_error(validated, resolution.message)
                    if task.retry.carry_invalid_output:
                        seed_from = key
                self.trace.append(f"retry:{technical_attempt + 1}/{task.retry.max_attempts}")
                if task.retry.interval_seconds:
                    await asyncio.sleep(task.retry.interval_seconds)
                continue
            return _map_resolution(resolution, state, publish, tuple(completions))
        raise AssertionError("attempt retry loop exhausted without a resolution")

    async def _issue_interrupt(
        self,
        key: AttemptKey,
        *,
        context: AttemptExecutionContext,
        issued: tuple[ActiveSystemInterrupt, ...],
        resolution: PendingTaskResult | IndeterminateTaskResult,
    ) -> None:
        if len(issued) >= MAX_ACTIVE_GENERATIONS:
            raise ValueError("checkpoint marker batch exceeds the active-generation bound")
        generation = max((item.generation for item in issued), default=0) + 1
        ordinal = max((item.ordinal for item in issued), default=-1) + 1
        kind = "system_wake" if isinstance(resolution, PendingTaskResult) else "system_block"
        reference_id = (
            resolution.wakeup.reference_id
            if isinstance(resolution, PendingTaskResult)
            else resolution.reconciliation.reference_id
        )
        envelope_digest = canonical_digest(
            {
                "attempt_key": key.digest,
                "generation": generation,
                "interrupt_kind": kind,
                "ordinal": ordinal,
                "reference_id": reference_id,
            }
        )
        snapshot = await self._checkpoints.load(key)
        if snapshot is None:
            raise ValueError("interrupt requires a persisted Attempt")
        retained = snapshot.active_interrupts
        if len(tuple(item for item in retained if not item.retired)) >= MAX_ACTIVE_GENERATIONS:
            raise ValueError("checkpoint marker batch exceeds the active-generation bound")
        generation = max((item.generation for item in retained), default=0) + 1
        ordinal = max((item.ordinal for item in retained), default=-1) + 1
        envelope_digest = canonical_digest(
            {
                "attempt_key": key.digest,
                "generation": generation,
                "interrupt_kind": kind,
                "ordinal": ordinal,
                "reference_id": reference_id,
            }
        )
        current = ActiveSystemInterrupt(
            generation=generation, ordinal=ordinal, envelope_digest=envelope_digest
        )
        await self._checkpoints.commit(
            replace(
                snapshot,
                fencing_token=context.fencing_token,
                active_interrupt=current,
                active_interrupts=(*retained, current),
            ),
            expected_revision=snapshot.revision,
            fencing_token=context.fencing_token,
        )
        payload = _interrupt_payload(
            key,
            ActiveSystemInterrupt(generation=generation, ordinal=ordinal, envelope_digest=envelope_digest),
            kind=kind,
            reference_id=reference_id,
            resolution=resolution,
        )
        self.trace.append(f"interrupt:generation={generation}:ordinal={ordinal}")
        interrupt(payload)


def _runtime_identity(
    runtime: object,
    factory_kernel: object,
) -> tuple[str, str, int, str, Any]:
    configurable: dict[str, object] = {}
    try:
        live = get_config()
        raw = live.get("configurable")
        if isinstance(raw, Mapping):
            configurable.update(raw)
    except RuntimeError:
        pass
    invocation_id = _first_str(
        getattr(runtime, "invocation_id", None), configurable.get("thread_id"), "inv-1"
    )
    revision = _first_str(
        getattr(runtime, "revision_id", None),
        configurable.get("assurance_revision_id"),
        "",
    )
    fencing = getattr(runtime, "fencing_token", None)
    if fencing is None:
        fencing = configurable.get("assurance_fencing_token", 1)
    entrypoint = _first_str(
        getattr(runtime, "public_entrypoint", None),
        configurable.get("assurance_entrypoint"),
        "execute",
    )
    kernel = getattr(runtime, "attempt_kernel", None) or factory_kernel
    return invocation_id, revision, _fencing_token(fencing), entrypoint, kernel


def _first_str(*values: object) -> str:
    for value in values:
        if isinstance(value, str) and value:
            return value
    return ""


def _fencing_token(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("fencing token must be a positive integer")
    return value


def _task_contract(
    contract: TaskAttemptContract[Any, Any] | ResolvedAttemptContract[Any, Any],
) -> TaskAttemptContract[Any, Any]:
    if isinstance(contract, ResolvedAttemptContract):
        return contract.contract
    return contract


def _resolve_activation(activation: object, state: object) -> BusinessActivation:
    value = activation(state) if callable(activation) else activation
    if value is None or value == "":
        raise ValueError("business activation is empty")
    if isinstance(value, BusinessActivation):
        return BusinessActivation.model_validate(value.model_dump())
    try:
        return BusinessActivation.model_validate(value)
    except ValidationError:
        raise
    except (TypeError, ValueError):
        raise ValueError("business activation is not canonical") from None


def _with_latest_validation_error(validated: BaseModel, message: str) -> BaseModel:
    """Carry this attempt's invalid_output into the next technical retry."""
    fields = type(validated).model_fields
    if "validation_error" not in fields:
        return validated
    latest = message[:8192]
    if not latest or getattr(validated, "validation_error", None) == latest:
        return validated
    payload = validated.model_dump(mode="json")
    payload["validation_error"] = latest
    try:
        return type(validated).model_validate(payload)
    except ValidationError:
        return validated


def _validate_input(select: object, state: object, contract: TaskAttemptContract[Any, Any]) -> BaseModel:
    if not callable(select):
        raise TypeError("select must be callable")
    return _validated_selection(select(state), contract)


def _validated_selection(raw: object, contract: TaskAttemptContract[Any, Any]) -> BaseModel:
    if isinstance(raw, BaseModel) and isinstance(raw, contract.input_model):
        return raw
    # Graph bundles can be loaded from independently installed wheels.  A
    # Pydantic model then has a different Python class identity even when its
    # JSON contract is identical to the contract owner's model.  Attempt
    # boundaries are value boundaries, so validate the canonical payload rather
    # than leaking that implementation identity across the wheel seam.
    if isinstance(raw, BaseModel):
        raw = raw.model_dump(mode="json")
    return contract.input_model.model_validate(raw)


def _active_issued(snapshot: AttemptCheckpoint | None) -> tuple[ActiveSystemInterrupt, ...]:
    if snapshot is None:
        return ()
    active = tuple(item for item in snapshot.active_interrupts if not item.retired)
    if active:
        return tuple(sorted(active, key=lambda item: (item.ordinal, item.generation)))
    if snapshot.active_interrupt is not None and not snapshot.active_interrupt.retired:
        return (snapshot.active_interrupt,)
    return ()


def _interrupt_payload(
    key: AttemptKey,
    item: ActiveSystemInterrupt,
    *,
    kind: str = "system_wake",
    reference_id: str | None = None,
    resolution: PendingTaskResult | IndeterminateTaskResult | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "kind": kind,
        "pending_generation": item.generation,
        "ordinal": item.ordinal,
        "attempt_key": key.digest,
        "envelope_digest": item.envelope_digest,
        CHECKPOINT_MARKERS_STATE_KEY: [
            CheckpointBridgeMarker(
                kind="system_interrupt_issued",
                attempt_key=key.digest,
                generation=item.generation,
                ordinal=item.ordinal,
                envelope_digest=item.envelope_digest,
            ).model_dump(mode="json")
        ],
    }
    if reference_id:
        payload["reason"] = reference_id
    if isinstance(resolution, PendingTaskResult):
        payload["wakeup"] = resolution.wakeup.model_dump(mode="json")
    elif isinstance(resolution, IndeterminateTaskResult):
        payload["reconciliation"] = resolution.reconciliation.model_dump(mode="json")
    return payload


def _map_resolution(
    resolution: object,
    state: object,
    publish: object,
    completions: tuple[tuple[AttemptKey, ActiveSystemInterrupt], ...],
) -> dict[str, object]:
    if isinstance(resolution, CommittedTaskResult):
        if not callable(publish):
            raise TypeError("publish must be callable")
        published = call_publish(
            publish,
            state if isinstance(state, Mapping) else {},
            resolution.output,
            resolution.receipt,
            resolution.committed_artifacts,
        )
        update = {str(name): value for name, value in published.items()}
        return _with_completion(update, completions)
    if isinstance(resolution, RejectedTaskResult):
        return _with_completion(
            {
                "attempt_failure": {
                    "resolution_kind": "rejected",
                    "reason": resolution.reason,
                    "writes_promoted": False,
                }
            },
            completions,
        )
    if isinstance(resolution, PermanentTaskFailure):
        return _with_completion(
            {
                "attempt_failure": {
                    "resolution_kind": "permanent",
                    "kind": resolution.kind,
                    "message": resolution.message,
                    "writes_promoted": False,
                }
            },
            completions,
        )
    raise TypeError(f"unsupported attempt resolution: {type(resolution)!r}")


def _with_completion(
    update: dict[str, object],
    completions: tuple[tuple[AttemptKey, ActiveSystemInterrupt], ...],
) -> dict[str, object]:
    if not completions:
        return update
    existing = update.get(CHECKPOINT_MARKERS_STATE_KEY)
    update[CHECKPOINT_MARKERS_STATE_KEY] = replace_checkpoint_marker_batch(
        existing if isinstance(existing, list) else None,
        [
            CheckpointBridgeMarker(
                kind="system_interrupt_completed",
                attempt_key=key.digest,
                generation=item.generation,
                ordinal=item.ordinal,
                envelope_digest=item.envelope_digest,
            )
            for key, item in completions
        ],
    )
    return update


__all__ = ["AttemptNodeFactory"]
