"""Allocation, approval, dispatch, apply, and re-exported agent handlers."""

from __future__ import annotations

from typing import Any, cast

from graph_engine.canonical import JSONValue
from graph_engine.plugin_api import EffectIntent, TaskContext, TaskOutcome, TaskRequest

from assurance_healing.contracts.agent import (
    AllocateHealingInputV1,
    RecordApprovalInputV1,
    RecordApplyInputV1,
)
from assurance_healing.contracts.effects import (
    HealApplyIntentV2,
    HealingAllocationIntentV2,
    ProposalApprovedIntentV1,
)
from assurance_healing.effects.allocation import ALLOCATION_KIND
from assurance_healing.effects.apply import HEAL_APPLY_KIND
from assurance_healing.effects.approval import APPROVAL_KIND
from assurance_healing.operations.agent import (
    CoverageRepairFinalizeHandler,
    CoverageRepairPrepareHandler,
    FixProposalFinalizeHandler,
    FixProposalPrepareHandler,
)
from assurance_healing.operations.application import (
    ApplyTestRepairFinalizeHandler,
    ApplyTestRepairPrepareHandler,
)
from assurance_healing.operations.common import InputError, failed_input, validate_input
from assurance_healing.operations.keys import (
    derive_allocation_ids,
    derive_approval_id,
    derive_heal_record_key,
)


class AllocateHealingAttemptHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del context
        try:
            payload = validate_input(AllocateHealingInputV1, request.input)
            ids = derive_allocation_ids(
                change_id=payload.change_id,
                source_batch_id=payload.source_batch_id,
                entry_batch_id=payload.entry_batch_id,
                candidate_digest=payload.candidate_digest,
                attempt_number=payload.attempt_number,
            )
            intent = HealingAllocationIntentV2(
                schema_version="2",
                episode_id=str(ids["episode_id"]),
                attempt_id=str(ids["attempt_id"]),
                attempt_number=payload.attempt_number,
                operation_id=str(ids["operation_id"]),
                change_id=payload.change_id,
                owner_id=payload.owner_id,
                source_batch_id=payload.source_batch_id,
                entry_batch_id=payload.entry_batch_id,
                candidate_digest=payload.candidate_digest,
                baseline_digest=payload.baseline_digest,
                policy_digest=payload.policy_digest,
                execution_evidence_digest=payload.execution_evidence_digest,
                baseline_embedded=not payload.prior_operation_ids,
            )
            output = intent.model_dump(mode="json")
            return TaskOutcome.succeeded(
                cast(JSONValue, output),
                effects=(EffectIntent(kind=ALLOCATION_KIND, payload=cast(JSONValue, output)),),
            )
        except InputError as error:
            return failed_input(error)


class RecordFixerApprovalHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del context
        try:
            payload = validate_input(RecordApprovalInputV1, request.input)
            approval_id = derive_approval_id(
                owner_id=payload.owner_id,
                candidate_digest=payload.candidate_digest,
                baseline_digest=payload.baseline_digest,
                policy_digest=payload.policy_digest,
                proposal_digest=payload.proposal_digest,
            )
            if payload.approval_id is not None and payload.approval_id != approval_id:
                raise InputError("approval_id does not match the derived key")
            intent = ProposalApprovedIntentV1(
                schema_version="1",
                approval_id=approval_id,
                change_id=payload.change_id,
                owner_id=payload.owner_id,
                root_invocation_id=payload.root_invocation_id,
                interrupt_task_id=payload.interrupt_task_id,
                source_gate_attempt_id=payload.source_gate_attempt_id,
                source_tree_id=payload.source_tree_id,
                target_tree_id=payload.target_tree_id,
                proposal_digest=payload.proposal_digest,
                fixer_authority_digest=payload.fixer_authority_digest,
                candidate_digest=payload.candidate_digest,
                baseline_digest=payload.baseline_digest,
                policy_digest=payload.policy_digest,
                targets=payload.targets,
                paths=payload.paths,
                action=payload.action,
            )
            output = intent.model_dump(mode="json")
            return TaskOutcome.succeeded(
                cast(JSONValue, output),
                effects=(EffectIntent(kind=APPROVAL_KIND, payload=cast(JSONValue, output)),),
            )
        except InputError as error:
            return failed_input(error)


class RecordCodegenFixApplyHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del context
        try:
            payload = validate_input(RecordApplyInputV1, request.input)
            record_key = derive_heal_record_key(
                owner_id=payload.owner_id,
                write_set_id=payload.write_set_id,
                candidate_digest=payload.candidate_digest,
                safety_payload_digest=payload.safety_payload_digest,
                target=payload.target,
            )
            if payload.record_key is not None and payload.record_key != record_key:
                raise InputError("record_key does not match the derived key")
            intent = HealApplyIntentV2(
                schema_version="2",
                record_key=record_key,
                change_id=payload.change_id,
                owner_id=payload.owner_id,
                target=payload.target,
                entry_batch_id=payload.entry_batch_id,
                outcome=payload.outcome,
                candidate_digest=payload.candidate_digest,
                baseline_digest=payload.baseline_digest,
                policy_digest=payload.policy_digest,
                write_set_id=payload.write_set_id,
                proposal_ids=payload.proposal_ids,
                claimed_modified_paths=payload.claimed_modified_paths,
                safety_payload_digest=payload.safety_payload_digest,
            )
            output = intent.model_dump(mode="json")
            return TaskOutcome.succeeded(
                cast(JSONValue, output),
                effects=(EffectIntent(kind=HEAL_APPLY_KIND, payload=cast(JSONValue, output)),),
            )
        except InputError as error:
            return failed_input(error)


class FixerDispatchHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del context
        try:
            raw = request.input
            if not isinstance(raw, dict):
                raise InputError("fixer-dispatch requires a proposal document")
            proposal_raw = raw.get("proposal", raw)
            if not isinstance(proposal_raw, dict):
                raise InputError("fixer-dispatch requires a proposal document")
            items = proposal_raw.get("proposals")
            if not isinstance(items, list):
                raise InputError("fixer-dispatch requires proposals")
            active: list[str] = []
            for item in items:
                if not isinstance(item, dict) or not item.get("eligible"):
                    continue
                target = item.get("target")
                if target in {"api", "e2e"} and target not in active:
                    active.append(str(target))
            return TaskOutcome.succeeded(cast(JSONValue, {"active_targets": sorted(active)}))
        except Exception as error:
            return failed_input(InputError(str(error)))


def healing_handlers() -> dict[str, Any]:
    from assurance_healing.operations.authority import FixerAuthorityReadyHandler
    from assurance_healing.operations.safety import (
        CombineFixerSafetyHandler,
        ComputeCoverageRepairSafetyHandler,
    )
    from assurance_healing.operations.status import (
        AllocateCoverageRepairAttemptHandler,
        ProjectEpisodeHandler,
        RecordCoverageRepairStatusHandler,
        RecordHealingStatusHandler,
    )

    from assurance_healing.operations.workflow_state import (
        REPAIR_ROUND_ADVANCE_ID,
        HealingRepairRoundAdvanceHandler,
    )

    return {
        REPAIR_ROUND_ADVANCE_ID: HealingRepairRoundAdvanceHandler(),
        "assurance.healing.apply-test-repair.finalize": ApplyTestRepairFinalizeHandler(),
        "assurance.healing.apply-test-repair.prepare": ApplyTestRepairPrepareHandler(),
        "assurance.healing.allocate-coverage-repair-attempt": AllocateCoverageRepairAttemptHandler(),
        "assurance.healing.allocate-healing-attempt": AllocateHealingAttemptHandler(),
        "assurance.healing.combine-fixer-safety": CombineFixerSafetyHandler(),
        "assurance.healing.compute-coverage-repair-safety": ComputeCoverageRepairSafetyHandler(),
        "assurance.healing.coverage-repair.finalize": CoverageRepairFinalizeHandler(),
        "assurance.healing.coverage-repair.prepare": CoverageRepairPrepareHandler(),
        "assurance.healing.fix-proposal.finalize": FixProposalFinalizeHandler(),
        "assurance.healing.fix-proposal.prepare": FixProposalPrepareHandler(),
        "assurance.healing.fixer-authority-ready": FixerAuthorityReadyHandler(),
        "assurance.healing.fixer-dispatch": FixerDispatchHandler(),
        "assurance.healing.project-episode": ProjectEpisodeHandler(),
        "assurance.healing.record-codegen-fix-apply": RecordCodegenFixApplyHandler(),
        "assurance.healing.record-coverage-repair-status": RecordCoverageRepairStatusHandler(),
        "assurance.healing.record-fixer-approval": RecordFixerApprovalHandler(),
        "assurance.healing.record-healing-status": RecordHealingStatusHandler(),
    }
