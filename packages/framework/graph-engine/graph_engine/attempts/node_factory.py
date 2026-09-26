from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from typing import Any

from langgraph.config import get_config
from langgraph.errors import GraphInterrupt
from langgraph.types import Interrupt, interrupt as langgraph_interrupt
from pydantic import BaseModel, ValidationError

from graph_engine.attempts.context import AttemptExecutionContext
from graph_engine.attempts.contracts import ResolvedAttemptContract, TaskAttemptContract
from graph_engine.attempts.events import (
    ActiveSystemInterrupt,
    AttemptSnapshot,
    SystemInterruptIssued,
)
from graph_engine.attempts.keys import AttemptKey, BusinessActivation, derive_attempt_key
from graph_engine.attempts.resolutions import (
    CommittedEffectFailure,
    CommittedTaskResult,
    IndeterminateTaskResult,
    PendingTaskResult,
    PermanentTaskFailure,
    RejectedTaskResult,
)
from graph_engine.canonical import canonical_digest
from graph_engine.persistence.attempt_journal import AttemptJournalPort
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
        journal: AttemptJournalPort,
        kernel: object | None = None,
        trace: list[str] | None = None,
    ) -> None:
        self._journal = journal
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
        validated = _validate_input(select, selected_state, task)
        if kernel is None:
            raise TypeError("attempt kernel is required")
        completions: list[tuple[AttemptKey, ActiveSystemInterrupt]] = []
        for technical_attempt in range(1, task.retry.max_attempts + 1):
            key = derive_attempt_key(
                invocation_id=invocation_id,
                graph_revision=revision,
                public_entrypoint=entrypoint,
                semantic_node_id=semantic_node_id,
                business_activation=business,
                contract_id=task.contract_id,
                validated_input=validated,
                technical_attempt=technical_attempt,
            )
            snapshot = await self._journal.load(key)
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
        event = SystemInterruptIssued(
            generation=generation,
            ordinal=ordinal,
            envelope_digest=envelope_digest,
        )
        snapshot = await self._journal.load(key)
        await self._journal.append(
            key,
            (event,),
            expected_revision=0 if snapshot is None else snapshot.revision,
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
    raw = select(state)
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


def _active_issued(snapshot: AttemptSnapshot | None) -> tuple[ActiveSystemInterrupt, ...]:
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
        published = publish(state, resolution.output, resolution.receipt)
        if not isinstance(published, Mapping):
            raise TypeError("publish must return a mapping")
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
    if isinstance(resolution, CommittedEffectFailure):
        return _with_completion(
            {
                "attempt_failure": {
                    "resolution_kind": "committed_effect_failure",
                    "reason": resolution.reason,
                    "writes_promoted": True,
                    "promotion_receipt": resolution.promotion_receipt,
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
