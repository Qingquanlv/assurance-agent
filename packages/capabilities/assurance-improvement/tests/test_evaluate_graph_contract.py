from __future__ import annotations


import pytest
from pydantic import ValidationError

from assurance_improvement.contracts.attempts import (
    TASK_ATTEMPT_CONTRACTS,
    select_evaluate_memory,
)
from assurance_improvement.contracts.delivery import (
    MemoryEvalReceipt,
    MemoryEvalRecordV1,
    artifact_digest,
)
from assurance_improvement.contracts.improvements import ImprovementProjection
from assurance_improvement.operations.delivery import EvaluateMemoryImprovementHandler, EvaluateMemoryInput
from tests.product.test_change_local_output_routing import execute_task

from improvement_fixtures import (  # pyright: ignore[reportMissingImports]
    HEX_A,
    as_object,
    improvement_projection,
    json_value,
)

_LIFECYCLE_ONLY: dict[str, object] = {
    "change_id": "CH-EVAL-001",
    "capability_leafs": ["entities.item.create"],
    "allowed_artifact_paths": [
        "qa/.qa.yaml",
        "qa/cases",
        "qa/fixtures",
        "qa/proposal.md",
        "qa/requirement.md",
        "qa/results",
        "qa/tests",
    ],
    "evidence_refs": [{"path": "qa/results/report/report.md", "digest": "a" * 64}],
    "lifecycle_state": "approved",
}
_EVALUATE_ID = "assurance.improvement.evaluate-memory-improvement"


def complete_evaluate_payload() -> dict[str, object]:
    return {
        "projection": improvement_projection(),
        "eval_run_id": "eval-1",
        "outcome": "passed",
        "report_sha256": "r",
        "staged_sha256": "s",
        "baseline_sha256": None,
        "target_digest": HEX_A,
    }


def test_lifecycle_only_public_payload_is_rejected_by_evaluate_selector() -> None:
    with pytest.raises((ValidationError, ValueError)):
        select_evaluate_memory(_LIFECYCLE_ONLY)


@pytest.mark.asyncio
async def test_lifecycle_only_public_payload_fails_production_evaluate() -> None:
    outcome = await execute_task(EvaluateMemoryImprovementHandler(), json_value(_LIFECYCLE_ONLY))
    assert outcome.status == "failed"
    assert outcome.failure is not None
    assert outcome.failure.kind == "invalid_input"


@pytest.mark.asyncio
async def test_complete_typed_selector_returns_receipt_and_delivery_record() -> None:
    selected = select_evaluate_memory(complete_evaluate_payload())
    assert isinstance(selected, EvaluateMemoryInput)
    assert selected.eval_run_id == "eval-1"
    assert selected.outcome == "passed"
    assert selected.target_digest == HEX_A
    outcome = await execute_task(
        EvaluateMemoryImprovementHandler(),
        json_value(selected.model_dump(mode="json")),
    )
    assert outcome.status == "succeeded"
    receipt = MemoryEvalReceipt.model_validate(as_object(outcome.output)["memory_eval"])
    projection = ImprovementProjection.model_validate(improvement_projection())
    assert receipt.approved_state_digest == artifact_digest(projection)
    assert receipt.approved_version == projection.version
    record = MemoryEvalRecordV1.model_validate_json(
        outcome.workspace_bytes["qa/results/improvement/memory-eval.json"]
    )
    assert record.improvement_id == "IMP-1"
    assert record.target_kind == "memory_eval"
    assert record.target_digest == HEX_A
    assert record.receipt == receipt


def test_offline_benchmark_eval_comparator_is_not_this_handler() -> None:
    contract = TASK_ATTEMPT_CONTRACTS[_EVALUATE_ID]
    assert contract.handler_id == _EVALUATE_ID
    assert contract.input_model is EvaluateMemoryInput
    assert isinstance(EvaluateMemoryImprovementHandler(), EvaluateMemoryImprovementHandler)
    assert "assurance.improvement.graph.benchmark-eval" not in TASK_ATTEMPT_CONTRACTS
    assert "assurance.improvement.evaluate-benchmark" not in TASK_ATTEMPT_CONTRACTS


def test_eval_output_contract_rejects_undeclared_fields() -> None:
    with pytest.raises(ValidationError):
        TASK_ATTEMPT_CONTRACTS[_EVALUATE_ID].output_model.model_validate(
            {
                "eval_run_id": "eval-1",
                "outcome": "passed",
                "report_sha256": "r",
                "staged_sha256": "s",
                "unexpected": "junk",
            },
        )
