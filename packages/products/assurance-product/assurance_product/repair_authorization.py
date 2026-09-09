"""Issue the one narrow authorization that crosses quality into healing."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from assurance_execution.contracts.attempts import (
    TASK_ATTEMPT_CONTRACTS,
    activation_execute,
    select_execute,
)
from assurance_execution.contracts.workflow import (
    VerifiedBridgeDefectResultV1,
    VerifiedIncompleteExecutionV1,
)
from assurance_healing.contracts.application import RepairAuthorizationV1
from assurance_quality.contracts.assessment import InspectionOutcomeV1
from graph_engine.attempts import derive_attempt_key
from graph_engine.canonical import JSONValue, canonical_digest
from graph_engine.persistence.attempt_journal import AttemptJournalPort

_CONTRACT_ID = "assurance.execution.task.execute.v1"
_SEMANTIC_NODE_ID = "execution.execute"


@dataclass(frozen=True, slots=True)
class RepairAuthorizationIssuer:
    """Trust the anchored current state and its exact committed execution Attempt."""

    journal: AttemptJournalPort
    invocation_id: str
    public_entrypoint: str
    graph_revision: str

    async def issue(self, state: Mapping[str, object]) -> RepairAuthorizationV1:
        inspection = InspectionOutcomeV1.model_validate(state.get("inspection_outcome"))
        execution = VerifiedIncompleteExecutionV1.model_validate(state.get("execution_result"))
        if (
            inspection.disposition != "repairable_execution_failure"
            or not inspection.verification_repairable_bridge
            or execution.repair_round != 0
        ):
            raise ValueError("quality did not authorize a current bridge repair")
        if (
            inspection.change_id != execution.change_id
            or inspection.coverage_epoch != execution.coverage_epoch
            or inspection.batch_id != execution.batch_id
            or inspection.plan_digest != execution.plan_digest
            or inspection.plan_ref != execution.plan_ref
            or inspection.reviewed_case != execution.reviewed_case
            or inspection.mapping_ref != execution.mapping_ref
        ):
            raise ValueError("quality and execution do not describe the same execution")

        selected = select_execute(state)
        contract = TASK_ATTEMPT_CONTRACTS[_CONTRACT_ID]
        attempt_key = derive_attempt_key(
            invocation_id=self.invocation_id,
            graph_revision=self.graph_revision,
            public_entrypoint=self.public_entrypoint,
            semantic_node_id=_SEMANTIC_NODE_ID,
            business_activation=activation_execute(state),
            contract_id=contract.contract_id,
            validated_input=selected,
        )
        snapshot = await self.journal.load(attempt_key)
        terminal = None if snapshot is None else snapshot.terminal
        receipt = execution.receipt
        raw_result = VerifiedBridgeDefectResultV1(
            defect=execution.defect,
            batch_id=execution.batch_id,
            executed_at=execution.executed_at,
        )
        input_payload: JSONValue = selected.model_dump(mode="json")
        contract_payload: JSONValue = contract.canonical_projection()
        if (
            snapshot is None
            or terminal is None
            or attempt_key != execution.defect.attempt_key
            or snapshot.invocation_id != self.invocation_id
            or snapshot.public_entrypoint != self.public_entrypoint
            or snapshot.semantic_node_id != _SEMANTIC_NODE_ID
            or snapshot.graph_revision != self.graph_revision
            or snapshot.contract_digest != canonical_digest(contract_payload)
            or snapshot.input_digest != canonical_digest(input_payload)
            or snapshot.activity_outcome != raw_result.model_dump(mode="json")
            or terminal.resolution_kind != "committed"
            or terminal.output != raw_result.model_dump(mode="json")
            or terminal.receipt_id != receipt.receipt_id
            or terminal.receipt_digest != receipt.receipt_digest
            or snapshot.promotion_receipt_id != receipt.receipt_id
            or snapshot.promotion_receipt_digest != receipt.receipt_digest
            or not snapshot.released
        ):
            raise ValueError("bridge repair does not match the committed execution Attempt")

        return RepairAuthorizationV1(
            attempt_key=attempt_key,
            invocation_id=self.invocation_id,
            semantic_node_id=_SEMANTIC_NODE_ID,
            defect=execution.defect,
            receipt=receipt,
        )


__all__ = ["RepairAuthorizationIssuer"]
