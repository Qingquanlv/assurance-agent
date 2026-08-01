"""Strict healing durable-effect payloads and domain reconcilers (D14 / §5.3)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from pydantic import Field, StrictStr, field_validator, model_validator

from assurance_agent.artifacts.canonical import canonical_json_bytes, sha256_bytes
from assurance_agent.artifacts.models.common import StrictWireModel
from assurance_agent.artifacts.models.generated_files import (
    _validate_canonical_strings,
    _validate_prefixed_sha256,
)
from assurance_agent.workflow.core.events import (
    FixerProposalApprovedEvent,
    HealRecordApplyV2Event,
    HealingAttemptAllocatedV2Event,
    LedgerIntegrityError,
    read_events_strict,
)
from assurance_agent.workflow.core.progression import ProgressionLockTimeout, transaction
from assurance_agent.workflow.graph.durable_effects import (
    FIXER_PROPOSAL_APPROVED_V1,
    HEAL_RECORD_APPLY_V2,
    HEALING_ALLOCATION_V2,
    DurableEffectAcknowledgementV1,
    DurableEffectContext,
    DurableEffectIntegrityError,
    DurableEffectIntentV1,
    DurableEffectRetryableError,
    DurableEffectRuntime,
    EffectRegistration,
    EffectRegistry,
    reconciler_semantics_digest,
)


# Closed commit-safety dependency inventory for runtime_commit_safety/v1 (Task 10).
COMMIT_SAFETY_INVENTORY: tuple[tuple[str, str, str], ...] = (
    (
        "assurance_agent.workflow.healing.effects.HealingAllocationEffectV2",
        "model",
        HEALING_ALLOCATION_V2,
    ),
    (
        "assurance_agent.workflow.healing.effects.FixerProposalApprovedEffectV1",
        "model",
        FIXER_PROPOSAL_APPROVED_V1,
    ),
    (
        "assurance_agent.workflow.healing.effects.HealRecordApplyEffectV2",
        "model",
        HEAL_RECORD_APPLY_V2,
    ),
    (
        "assurance_agent.workflow.healing.effects.allocation_idempotency_key",
        "helper",
        HEALING_ALLOCATION_V2,
    ),
    (
        "assurance_agent.workflow.healing.effects.approval_idempotency_key",
        "helper",
        FIXER_PROPOSAL_APPROVED_V1,
    ),
    (
        "assurance_agent.workflow.healing.effects.record_idempotency_key",
        "helper",
        HEAL_RECORD_APPLY_V2,
    ),
    (
        "assurance_agent.workflow.healing.effects.reconcile_healing_allocation",
        "helper",
        HEALING_ALLOCATION_V2,
    ),
    (
        "assurance_agent.workflow.healing.effects.reconcile_fixer_proposal_approved",
        "helper",
        FIXER_PROPOSAL_APPROVED_V1,
    ),
    (
        "assurance_agent.workflow.healing.effects.reconcile_heal_record_apply",
        "helper",
        HEAL_RECORD_APPLY_V2,
    ),
    (
        "assurance_agent.workflow.healing.effects.register_healing_effects",
        "helper",
        "durable_effect_registry",
    ),
)


class HealingAllocationEffectV2(StrictWireModel):
    schema_version: Literal["2"] = "2"
    episode_id: StrictStr
    attempt_id: StrictStr
    attempt_number: int = Field(strict=True, ge=1)
    operation_id: StrictStr
    source_batch_id: StrictStr
    entry_batch_id: StrictStr
    baseline_sha256: StrictStr
    baseline_embedded: bool

    @field_validator(
        "episode_id",
        "attempt_id",
        "operation_id",
        "source_batch_id",
        "entry_batch_id",
        "baseline_sha256",
    )
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("must be a non-empty string")
        return value


class FixerProposalApprovedEffectV1(StrictWireModel):
    schema_version: Literal["1"] = "1"
    approval_id: StrictStr
    root_invocation_id: StrictStr
    interrupt_task_id: StrictStr
    source_gate_attempt_id: StrictStr
    source_tree_id: StrictStr
    proposal_sha256: StrictStr
    fixer_authority_sha256: StrictStr
    entry_baseline_sha256: StrictStr
    policy_sha256: StrictStr
    targets: list[Literal["api", "e2e"]]
    paths: list[StrictStr]
    target_tree_id: StrictStr

    @model_validator(mode="after")
    def validate_fields(self) -> FixerProposalApprovedEffectV1:
        for field_name in (
            "approval_id",
            "root_invocation_id",
            "interrupt_task_id",
            "source_gate_attempt_id",
            "source_tree_id",
            "target_tree_id",
        ):
            if not getattr(self, field_name).strip():
                raise ValueError(f"{field_name} must not be empty")
        for digest in (
            self.proposal_sha256,
            self.fixer_authority_sha256,
            self.entry_baseline_sha256,
            self.policy_sha256,
        ):
            _validate_prefixed_sha256(digest)
        _validate_canonical_strings(self.targets, label="targets")
        _validate_canonical_strings(self.paths, label="paths")
        if not self.targets or not self.paths:
            raise ValueError("targets and paths must be non-empty")
        return self


class HealRecordApplyEffectV2(StrictWireModel):
    schema_version: Literal["2"] = "2"
    record_key: StrictStr
    root_invocation_id: StrictStr
    record_task_id: StrictStr
    fixer_attempt_id: StrictStr
    target: Literal["api", "e2e"]
    entry_batch_id: StrictStr
    intent_sha256: StrictStr
    write_set_id: StrictStr
    outcome: Literal["applied", "no_op", "skipped"]
    proposal_ids: list[StrictStr]
    claimed_modified_paths: list[StrictStr]
    safety_payload_sha256: StrictStr

    @model_validator(mode="after")
    def validate_fields(self) -> HealRecordApplyEffectV2:
        for field_name in (
            "record_key",
            "root_invocation_id",
            "record_task_id",
            "fixer_attempt_id",
            "entry_batch_id",
            "write_set_id",
        ):
            if not getattr(self, field_name).strip():
                raise ValueError(f"{field_name} must not be empty")
        _validate_prefixed_sha256(self.intent_sha256)
        _validate_prefixed_sha256(self.safety_payload_sha256)
        _validate_canonical_strings(self.proposal_ids, label="proposal_ids")
        _validate_canonical_strings(self.claimed_modified_paths, label="claimed_modified_paths")
        return self


_ALLOCATION_RULES = (
    "append_or_reuse_healing_attempt_allocated_v2",
    "first_allocation_embeds_baseline",
    "later_allocation_reuses_baseline_id",
    "exact_key_exact_payload_noop",
    "same_key_drift_is_corruption",
)

_APPROVAL_RULES = (
    "append_or_reuse_fixer_proposal_approved",
    "bind_receipt_and_target_tree",
    "exact_key_exact_payload_noop",
    "same_key_drift_is_corruption",
)

_RECORD_RULES = (
    "append_or_reuse_heal_record_apply_v2",
    "bind_intent_write_set_and_safety",
    "exact_key_exact_payload_noop",
    "same_key_drift_is_corruption",
)


def allocation_idempotency_key(payload: Mapping[str, object]) -> str:
    return str(payload["operation_id"])


def approval_idempotency_key(payload: Mapping[str, object]) -> str:
    return str(payload["approval_id"])


def record_idempotency_key(payload: Mapping[str, object]) -> str:
    return str(payload["record_key"])


def reconcile_healing_allocation(
    intent: DurableEffectIntentV1,
    context: DurableEffectContext,
    runtime: DurableEffectRuntime,
) -> DurableEffectAcknowledgementV1:
    payload = HealingAllocationEffectV2.model_validate(intent.payload)
    event = HealingAttemptAllocatedV2Event(
        episode_id=payload.episode_id,
        attempt_id=payload.attempt_id,
        attempt_number=payload.attempt_number,
        operation_id=payload.operation_id,
        source_batch_id=payload.source_batch_id,
        entry_batch_id=payload.entry_batch_id,
        baseline_sha256=payload.baseline_sha256,
        baseline_embedded=payload.baseline_embedded,
    )
    return _append_or_reuse(
        intent=intent,
        context=context,
        runtime=runtime,
        event=event,
        match=lambda raw: (
            raw.get("type") == "healing_attempt_allocated_v2"
            and raw.get("operation_id") == payload.operation_id
        ),
        expected_wire=_event_wire(event),
    )


def reconcile_fixer_proposal_approved(
    intent: DurableEffectIntentV1,
    context: DurableEffectContext,
    runtime: DurableEffectRuntime,
) -> DurableEffectAcknowledgementV1:
    payload = FixerProposalApprovedEffectV1.model_validate(intent.payload)
    event = FixerProposalApprovedEvent(
        approval_id=payload.approval_id,
        root_invocation_id=payload.root_invocation_id,
        interrupt_task_id=payload.interrupt_task_id,
        source_gate_attempt_id=payload.source_gate_attempt_id,
        source_tree_id=payload.source_tree_id,
        proposal_sha256=payload.proposal_sha256,
        fixer_authority_sha256=payload.fixer_authority_sha256,
        entry_baseline_sha256=payload.entry_baseline_sha256,
        policy_sha256=payload.policy_sha256,
        targets=list(payload.targets),
        paths=list(payload.paths),
        target_tree_id=payload.target_tree_id,
    )
    return _append_or_reuse(
        intent=intent,
        context=context,
        runtime=runtime,
        event=event,
        match=lambda raw: (
            raw.get("type") == "fixer_proposal_approved" and raw.get("approval_id") == payload.approval_id
        ),
        expected_wire=_event_wire(event),
    )


def reconcile_heal_record_apply(
    intent: DurableEffectIntentV1,
    context: DurableEffectContext,
    runtime: DurableEffectRuntime,
) -> DurableEffectAcknowledgementV1:
    payload = HealRecordApplyEffectV2.model_validate(intent.payload)
    event = HealRecordApplyV2Event(
        record_key=payload.record_key,
        root_invocation_id=payload.root_invocation_id,
        record_task_id=payload.record_task_id,
        fixer_attempt_id=payload.fixer_attempt_id,
        target=payload.target,
        entry_batch_id=payload.entry_batch_id,
        intent_sha256=payload.intent_sha256,
        write_set_id=payload.write_set_id,
        outcome=payload.outcome,
        proposal_ids=list(payload.proposal_ids),
        claimed_modified_paths=list(payload.claimed_modified_paths),
        safety_payload_sha256=payload.safety_payload_sha256,
    )
    return _append_or_reuse(
        intent=intent,
        context=context,
        runtime=runtime,
        event=event,
        match=lambda raw: (
            raw.get("type") == "heal_record_apply_v2" and raw.get("record_key") == payload.record_key
        ),
        expected_wire=_event_wire(event),
    )


def register_healing_effects(registry: EffectRegistry) -> None:
    """Register the three healing effect kinds on ``registry``."""
    registry.register(
        EffectRegistration(
            kind=HEALING_ALLOCATION_V2,
            payload_model=HealingAllocationEffectV2,
            reconciler_semantics_digest=reconciler_semantics_digest(
                kind=HEALING_ALLOCATION_V2,
                rules=_ALLOCATION_RULES,
            ),
            domain_idempotency_key=allocation_idempotency_key,
            reconcile=reconcile_healing_allocation,
        )
    )
    registry.register(
        EffectRegistration(
            kind=FIXER_PROPOSAL_APPROVED_V1,
            payload_model=FixerProposalApprovedEffectV1,
            reconciler_semantics_digest=reconciler_semantics_digest(
                kind=FIXER_PROPOSAL_APPROVED_V1,
                rules=_APPROVAL_RULES,
            ),
            domain_idempotency_key=approval_idempotency_key,
            reconcile=reconcile_fixer_proposal_approved,
        )
    )
    registry.register(
        EffectRegistration(
            kind=HEAL_RECORD_APPLY_V2,
            payload_model=HealRecordApplyEffectV2,
            reconciler_semantics_digest=reconciler_semantics_digest(
                kind=HEAL_RECORD_APPLY_V2,
                rules=_RECORD_RULES,
            ),
            domain_idempotency_key=record_idempotency_key,
            reconcile=reconcile_heal_record_apply,
        )
    )


def _event_wire(event: StrictWireModel | object) -> dict[str, object]:
    if hasattr(event, "model_dump"):
        dumped = event.model_dump(mode="json", by_alias=True)  # type: ignore[attr-defined]
        if isinstance(dumped, dict):
            return dumped
    raise DurableEffectIntegrityError("healing domain event is not serializable")


def _append_or_reuse(
    *,
    intent: DurableEffectIntentV1,
    context: DurableEffectContext,
    runtime: DurableEffectRuntime,
    event: object,
    match: object,
    expected_wire: Mapping[str, object],
) -> DurableEffectAcknowledgementV1:
    domain_digest = sha256_bytes(canonical_json_bytes(dict(expected_wire)))
    try:
        with transaction(runtime.change_dir) as txn:
            for raw in read_events_strict(runtime.change_dir):
                if not isinstance(raw, dict) or not match(raw):  # type: ignore[operator]
                    continue
                comparable = {key: value for key, value in raw.items() if key not in {"seq", "ts"}}
                if comparable != dict(expected_wire):
                    raise DurableEffectIntegrityError(
                        f"healing effect payload drift for {intent.kind}:{intent.effect_id}"
                    )
                seq = raw.get("seq")
                if not isinstance(seq, int):
                    raise DurableEffectIntegrityError("healing domain event missing seq")
                return DurableEffectAcknowledgementV1(
                    schema_version="1",
                    root_invocation_id=context.root_invocation_id,
                    invocation_id=context.invocation_id,
                    task_id=context.task_id,
                    attempt_id=context.attempt_id,
                    effect_id=intent.effect_id,
                    kind=intent.kind,
                    reconciler_semantics_digest=intent.reconciler_semantics_digest,
                    payload_sha256=intent.payload_sha256,
                    domain_source_sequence=seq,
                    domain_event_digest=domain_digest,
                )
            txn.append_strict(event)  # type: ignore[arg-type]
        for raw in read_events_strict(runtime.change_dir):
            if isinstance(raw, dict) and match(raw):  # type: ignore[operator]
                seq = raw.get("seq")
                if not isinstance(seq, int):
                    raise DurableEffectIntegrityError("healing domain event missing seq")
                return DurableEffectAcknowledgementV1(
                    schema_version="1",
                    root_invocation_id=context.root_invocation_id,
                    invocation_id=context.invocation_id,
                    task_id=context.task_id,
                    attempt_id=context.attempt_id,
                    effect_id=intent.effect_id,
                    kind=intent.kind,
                    reconciler_semantics_digest=intent.reconciler_semantics_digest,
                    payload_sha256=intent.payload_sha256,
                    domain_source_sequence=seq,
                    domain_event_digest=domain_digest,
                )
        raise DurableEffectIntegrityError("healing domain event missing after append")
    except ProgressionLockTimeout as exc:
        raise DurableEffectRetryableError(str(exc), error_code="lock_timeout") from exc
    except LedgerIntegrityError as exc:
        raise DurableEffectIntegrityError(str(exc)) from exc
    except OSError as exc:
        raise DurableEffectRetryableError(str(exc), error_code="retryable_io") from exc


__all__ = [
    "COMMIT_SAFETY_INVENTORY",
    "FixerProposalApprovedEffectV1",
    "HealRecordApplyEffectV2",
    "HealingAllocationEffectV2",
    "register_healing_effects",
]
