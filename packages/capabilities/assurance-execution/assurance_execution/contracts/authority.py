"""Independent store authentication for repairable pre-dispatch generation defects."""

from __future__ import annotations

from pathlib import Path

from assurance_execution.contracts.workflow import VerifiedGenerationDefectCycleV1
from assurance_generation.contracts.workflow import VerifiedGenerationDefectV1
from graph_engine.attempts.host_receipts import TerminalReceiptError, TerminalReceiptStore
from graph_engine.attempts.production_host import invocation_activity_receipts_root
from graph_engine.attempts.workspace import TaskWorkspaceViolation, authenticate_promotion_receipt
from graph_engine.canonical import JSONValue, canonical_digest
from graph_engine.plugin_api import TaskOutcome


def authenticate_generation_defect_cycle(
    project_root: Path,
    cycle: VerifiedGenerationDefectCycleV1,
) -> VerifiedGenerationDefectV1:
    """Authenticate both host terminal and kernel promotion stores for this defect."""

    attempt = cycle.attempt
    identity = attempt.authority_identity
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
    return attempt.defect


__all__ = ["authenticate_generation_defect_cycle"]
