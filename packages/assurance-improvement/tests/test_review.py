from __future__ import annotations

from pathlib import Path

import pytest
from agent_runtime_contracts import AgentRunRequest
from graph_engine.canonical import canonical_json_bytes
from tests.phase4.conformance import execute_task

from assurance_improvement.contracts.agent import ImprovementReviewResultV1
from assurance_improvement.resource_loader import resource_bytes
from assurance_improvement.operations.agent import ImprovementReviewPrepareHandler
from assurance_improvement.operations.review import ApplyImprovementReviewHandler, apply_review
from assurance_improvement.validators.review import ReviewValidator
from improvement_fixtures import (  # pyright: ignore[reportMissingImports]
    BINDING,
    IMPROVEMENT_ID,
    as_object,
    fake_agent_result,
    json_value,
    skill_input,
    validation_context,
    write_set,
)

REVIEW_RESULT = {
    "schema_version": "1",
    "review_type": "improvement",
    "decision": "pass",
    "findings": [],
    "evidence_traceability": "complete",
    "scope_readiness": "ready",
    "verification_readiness": "ready",
    "delivery_safety": "ready",
    "human_review_required": False,
}


def _projection(*, state: str = "proposed", version: int = 1) -> dict[str, object]:
    return {
        "improvement_id": IMPROVEMENT_ID,
        "fingerprint": "f" * 64,
        "kind": "workflow_improvement",
        "delivery": "change_draft",
        "source_refs": {"problem_ids": ["PROB-1"], "occurrence_ids": ["OCC-1"]},
        "target": "schemas/workflow-schema.yaml",
        "rationale": "gap",
        "proposed_change": "register adapters",
        "verification": {"suites": [], "required_cases": [], "success_criteria": "review"},
        "risk": "low",
        "confidence": "high",
        "state": state,
        "version": version,
        "proposed_by_retro_ids": ["RET-1"],
        "last_event_id": "IMPEVT-1",
    }


@pytest.mark.asyncio
async def test_review_prepare_locks_improvement_reviewer(tmp_path: Path) -> None:
    outcome = await execute_task(
        ImprovementReviewPrepareHandler(),
        skill_input(),
        tmp_path,
        binding_data=BINDING,
    )
    assert outcome.status == "succeeded"
    request = AgentRunRequest.model_validate(outcome.output)
    assert "Capability-owned improvement-reviewer skill" in (request.instructions[0].text_content or "")
    assert "Improvement reviewer persona" in (request.instructions[1].text_content or "")


@pytest.mark.asyncio
async def test_review_finalize_rejects_pass_without_complete_evidence(tmp_path: Path) -> None:
    from assurance_improvement.operations.agent import ImprovementReviewFinalizeHandler

    structured = {**REVIEW_RESULT, "evidence_traceability": "incomplete"}
    outcome = await execute_task(
        ImprovementReviewFinalizeHandler(),
        fake_agent_result(structured),
        tmp_path,
    )
    assert outcome.failure is not None
    assert outcome.failure.kind == "invalid_output"


@pytest.mark.asyncio
async def test_review_finalize_accepts_complete_pass(tmp_path: Path) -> None:
    from assurance_improvement.operations.agent import ImprovementReviewFinalizeHandler

    outcome = await execute_task(
        ImprovementReviewFinalizeHandler(),
        fake_agent_result(REVIEW_RESULT),
        tmp_path,
    )
    assert outcome.status == "succeeded"
    document = ImprovementReviewResultV1.model_validate(outcome.output)
    assert document.decision == "pass"


@pytest.mark.asyncio
async def test_apply_review_approves_proposed_improvement(tmp_path: Path) -> None:
    outcome = await execute_task(
        ApplyImprovementReviewHandler(),
        json_value(
            {
                "projection": _projection(),
                "action": "approve",
                "review_id": "REV-1",
                "expected_improvement_version": 1,
            }
        ),
        tmp_path,
    )
    assert outcome.status == "succeeded"
    payload = as_object(outcome.output)
    assert payload["state"] == "approved"
    assert payload["version"] == 2
    assert payload["approval_source"] == "human"


@pytest.mark.asyncio
async def test_apply_review_rejects_self_transition(tmp_path: Path) -> None:
    outcome = await execute_task(
        ApplyImprovementReviewHandler(),
        json_value(
            {
                "projection": _projection(state="approved"),
                "action": "approve",
                "review_id": "REV-1",
                "expected_improvement_version": 1,
            }
        ),
        tmp_path,
    )
    assert outcome.failure is not None
    assert outcome.failure.kind == "invalid_input"
    assert outcome.failure.retryable is False


def test_review_validator_default_fails_closed() -> None:
    result = ReviewValidator().validate(
        write_set("improvements/reviews/assessment.json", "improvements/review-subjects/subject.json"),
        validation_context(),
    )
    assert result.accepted is False
    assert "not authenticated" in (result.reason or "")


def test_review_validator_path_only_accepts_review_roots() -> None:
    result = ReviewValidator(path_only=True).validate(
        write_set("improvements/reviews/assessment.json"),
        validation_context(),
    )
    assert result.accepted is True


def test_review_validator_rejects_src_path() -> None:
    result = ReviewValidator(path_only=True).validate(write_set("src/app.py"), validation_context())
    assert result.accepted is False


def test_review_result_contract_bytes_equal_typed_model() -> None:
    assert resource_bytes("result-contracts/improvement-review.v1.schema.json") == canonical_json_bytes(
        ImprovementReviewResultV1.model_json_schema()
    )


def test_apply_review_helper_matches_legacy_transition_graph() -> None:
    from assurance_improvement.contracts.improvements import ImprovementProjection

    current = ImprovementProjection.model_validate(_projection())
    from assurance_improvement.operations.review import ApplyReviewInput

    updated = apply_review(
        ApplyReviewInput(
            projection=current,
            action="approve",
            review_id="REV-1",
            expected_improvement_version=1,
        )
    )
    assert updated.state.value == "approved"
