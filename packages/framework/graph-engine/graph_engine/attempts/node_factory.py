from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import inspect
from typing import Any, cast

from langgraph.config import get_config
from langgraph.errors import GraphInterrupt
from langgraph.types import Interrupt, interrupt as langgraph_interrupt
from pydantic import BaseModel, ValidationError

from graph_engine.attempts.context import AttemptExecutionContext
from graph_engine.attempts.contracts import ResolvedAttemptContract, TaskAttemptContract
from graph_engine.attempts.contracts import AttemptResultProvenanceV1, TerminalReceiptRef
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
from graph_engine.canonical import JSONValue, canonical_digest
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
        key = derive_attempt_key(
            invocation_id=invocation_id,
            graph_revision=revision,
            public_entrypoint=entrypoint,
            semantic_node_id=semantic_node_id,
            business_activation=business,
            contract_id=task.contract_id,
            validated_input=validated,
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
        if kernel is None:
            raise TypeError("attempt kernel is required")
        resolution = await kernel.execute_or_recover(key, contract, validated, context)
        if isinstance(resolution, (PendingTaskResult, IndeterminateTaskResult)):
            await self._issue_interrupt(
                key,
                context=context,
                issued=issued,
                resolution=resolution,
            )
        authority = None
        if isinstance(resolution, CommittedTaskResult) and _accepts_attempt_authority(publish):
            snapshot = await self._journal.load(key)
            authority = _authenticate_committed_result(snapshot, resolution, key)
        return _map_resolution(resolution, state, publish, key, issued, authority)

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


def _validate_input(select: object, state: object, contract: TaskAttemptContract[Any, Any]) -> BaseModel:
    if not callable(select):
        raise TypeError("select must be callable")
    raw = select(state)
    if isinstance(raw, BaseModel) and isinstance(raw, contract.input_model):
        return raw
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
    key: AttemptKey,
    issued: tuple[ActiveSystemInterrupt, ...],
    authority: AuthenticatedAttemptResult | None,
) -> dict[str, object]:
    if isinstance(resolution, CommittedTaskResult):
        if not callable(publish):
            raise TypeError("publish must be callable")
        if _accepts_attempt_authority(publish):
            published = publish(
                state,
                resolution.output,
                resolution.receipt,
                attempt_authority=authority,
            )
        else:
            published = publish(state, resolution.output, resolution.receipt)
        if not isinstance(published, Mapping):
            raise TypeError("publish must return a mapping")
        update = {str(name): value for name, value in published.items()}
        return _with_completion(update, key, issued)
    if isinstance(resolution, RejectedTaskResult):
        return _with_completion(
            {
                "attempt_failure": {
                    "resolution_kind": "rejected",
                    "reason": resolution.reason,
                    "writes_promoted": False,
                }
            },
            key,
            issued,
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
            key,
            issued,
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
            key,
            issued,
        )
    raise TypeError(f"unsupported attempt resolution: {type(resolution)!r}")


def _accepts_attempt_authority(publish: object) -> bool:
    try:
        parameter = inspect.signature(cast(Callable[..., object], publish)).parameters.get(
            "attempt_authority"
        )
    except (TypeError, ValueError):
        return False
    return parameter is not None and parameter.kind in {
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
        inspect.Parameter.KEYWORD_ONLY,
    }


_AUTHORITY_TOKEN = object()


@dataclass(frozen=True, slots=True, init=False)
class AuthenticatedAttemptResult:
    """In-process capability minted only after reading the durable Attempt journal."""

    provenance: AttemptResultProvenanceV1

    def __init__(self, provenance: AttemptResultProvenanceV1, token: object) -> None:
        if token is not _AUTHORITY_TOKEN:
            raise TypeError("authenticated attempt authority is engine-owned")
        object.__setattr__(self, "provenance", provenance)


def _authenticate_committed_result(
    snapshot: AttemptSnapshot | None,
    resolution: CommittedTaskResult[object],
    key: AttemptKey,
) -> AuthenticatedAttemptResult:
    if snapshot is None or snapshot.attempt_key != key:
        raise ValueError("committed result is absent from the Attempt journal")
    output = cast(
        JSONValue,
        resolution.output.model_dump(mode="json")
        if isinstance(resolution.output, BaseModel)
        else resolution.output,
    )
    output_digest = canonical_digest(output)
    terminal = snapshot.terminal
    required = (
        snapshot.invocation_id,
        snapshot.public_entrypoint,
        snapshot.semantic_node_id,
        snapshot.graph_revision,
        snapshot.contract_digest,
        snapshot.input_digest,
        snapshot.authorization_id,
        snapshot.activity_id,
    )
    if any(value is None or value == "" for value in required):
        raise ValueError("committed result lacks authenticated Attempt provenance")
    assert snapshot.invocation_id is not None
    assert snapshot.public_entrypoint is not None
    assert snapshot.semantic_node_id is not None
    assert snapshot.graph_revision is not None
    assert snapshot.contract_digest is not None
    assert snapshot.input_digest is not None
    assert snapshot.authorization_id is not None
    assert snapshot.activity_id is not None
    if (
        snapshot.activity_outcome != output
        or canonical_digest(snapshot.activity_outcome) != output_digest
        or terminal is None
        or terminal.resolution_kind != "committed"
        or terminal.output != output
        or terminal.receipt_id != resolution.receipt.receipt_id
        or terminal.receipt_digest != resolution.receipt.receipt_digest
        or snapshot.promotion_receipt_id != resolution.receipt.receipt_id
        or snapshot.promotion_receipt_digest != resolution.receipt.receipt_digest
        or not snapshot.released
    ):
        raise ValueError("committed result disagrees with the Attempt journal")
    provenance = AttemptResultProvenanceV1(
        attempt_key=key,
        invocation_id=snapshot.invocation_id,
        public_entrypoint=snapshot.public_entrypoint,
        semantic_node_id=snapshot.semantic_node_id,
        graph_revision=snapshot.graph_revision,
        contract_digest=snapshot.contract_digest,
        input_digest=snapshot.input_digest,
        authorization_id=snapshot.authorization_id,
        activity_id=snapshot.activity_id,
        output_digest=output_digest,
        source_terminal_receipt=(
            None
            if snapshot.source_identity_digest is None or snapshot.source_receipt_digest is None
            else TerminalReceiptRef(
                identity_digest=snapshot.source_identity_digest,
                receipt_digest=snapshot.source_receipt_digest,
            )
        ),
        promotion_receipt=resolution.receipt,
    )
    return AuthenticatedAttemptResult(provenance, _AUTHORITY_TOKEN)


def _with_completion(
    update: dict[str, object],
    key: AttemptKey,
    issued: tuple[ActiveSystemInterrupt, ...],
) -> dict[str, object]:
    if not issued:
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
            for item in issued
        ],
    )
    return update


__all__ = ["AttemptNodeFactory", "AuthenticatedAttemptResult"]
