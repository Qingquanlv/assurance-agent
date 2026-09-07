"""Independent store authentication for repairable pre-dispatch generation defects."""

from __future__ import annotations

from pathlib import Path

from assurance_execution.contracts.workflow import (
    ExecutionAttemptBindingV1,
    VerifiedGenerationDefectCycleV1,
)
from assurance_generation.contracts.workflow import VerifiedGenerationDefectV1
from graph_engine.attempts.host_receipts import TerminalReceiptError, TerminalReceiptStore
from graph_engine.attempts.production_host import invocation_activity_receipts_root
from graph_engine.attempts.workspace import TaskWorkspaceViolation, authenticate_promotion_receipt
from graph_engine.canonical import JSONValue, canonical_digest
from graph_engine.plugin_api import TaskOutcome


def authenticate_generation_defect_cycle(
    project_root: Path,
    cycle: VerifiedGenerationDefectCycleV1,
    expected: ExecutionAttemptBindingV1,
) -> VerifiedGenerationDefectV1:
    """Authenticate both host terminal and kernel promotion stores for this defect."""

    attempt = cycle.attempt
    identity = attempt.authority_identity
    provenance = cycle.execution_provenance
    defect = attempt.defect
    generation_payload: JSONValue = defect.generation.model_dump(mode="json")
    if (
        provenance.invocation_id != expected.invocation_id
        or provenance.public_entrypoint != expected.public_entrypoint
        or provenance.semantic_node_id != expected.semantic_node_id
        or provenance.attempt_key != expected.attempt_key
        or provenance.graph_revision != expected.graph_revision
        or provenance.contract_digest != expected.contract_digest
        or provenance.input_digest != expected.input_digest
        or identity.invocation_id != expected.invocation_id
        or identity.activation_id != expected.semantic_node_id
        or defect.attempt_key != expected.attempt_key
        or defect.generation.change_id != expected.change_id
        or defect.generation.coverage_epoch != expected.coverage_epoch
        or defect.validation_profile != expected.validation_profile
        or canonical_digest(generation_payload) != expected.generation_digest
    ):
        raise ValueError("generation defect does not belong to the expected execution attempt")
    change_root = Path(project_root) / "qa" / "changes" / attempt.defect.generation.change_id
    store = TerminalReceiptStore(invocation_activity_receipts_root(change_root, identity.invocation_id))
    try:
        receipts = store.authenticate(identity)
    except (FileNotFoundError, OSError, TerminalReceiptError) as error:
        raise ValueError("generation defect host authority is not authentic") from error
    expected_outcome = TaskOutcome.succeeded(attempt.defect.model_dump(mode="json"))
    if (
        len(receipts) != 1
        or receipts[0].outcome != expected_outcome
        or canonical_digest(receipts[0].model_dump(mode="json")) != attempt.authority_receipt.receipt_digest
    ):
        raise ValueError("generation defect host authority is not authentic")
    try:
        promotion = authenticate_promotion_receipt(
            change_root / ".runtime" / "receipts",
            cycle.execution_provenance.promotion_receipt,
        )
    except (OSError, TaskWorkspaceViolation) as error:
        raise ValueError("generation defect promotion authority is not authentic") from error
    if promotion.identity_digest != identity.workspace_identity_digest:
        raise ValueError("generation defect promotion belongs to another execution workspace")
    payload: JSONValue = attempt.model_dump(mode="json")
    if canonical_digest(payload) != cycle.execution_provenance.output_digest:
        raise ValueError("generation defect payload differs from its Attempt authority")
    return defect


__all__ = ["authenticate_generation_defect_cycle"]
