from __future__ import annotations

from pathlib import Path

import pytest
from agent_runtime_contracts import AgentRunRequest
from graph_engine.canonical import canonical_json_bytes
from tests.phase4.conformance import execute_task

from assurance_improvement.contracts.agent import ArchiveResultV1
from assurance_improvement.operations.agent import ArchiveFinalizeHandler, ArchivePrepareHandler
from assurance_improvement.operations.archive import ProjectArchiveHandler
from assurance_improvement.operations.delivery import (
    ApplyMemoryImprovementHandler,
    LoadImprovementDeliveryHandler,
    RollbackMemoryImprovementHandler,
)
from assurance_improvement.resource_loader import resource_bytes
from assurance_improvement.validators.archive import ArchiveIntegrityValidator
from assurance_improvement.validators.candidates import CandidatesValidator
from assurance_improvement.validators.delivery import DeliveryValidator
from improvement_fixtures import (  # pyright: ignore[reportMissingImports]
    BINDING,
    HEX_A,
    IMPROVEMENT_ID,
    archive_result,
    as_object,
    fake_agent_result,
    json_value,
    quality_report_payload,
    skill_input,
    validation_context,
    write_set,
)


def _projection(*, state: str = "approved", delivery: str = "memory_patch") -> dict[str, object]:
    return {
        "improvement_id": IMPROVEMENT_ID,
        "fingerprint": "f" * 64,
        "kind": "prompt_improvement" if delivery == "memory_patch" else "workflow_improvement",
        "delivery": delivery,
        "source_refs": {"problem_ids": ["PROB-1"], "occurrence_ids": ["OCC-1"]},
        "target": ".aa/memory/aa-api-plan.md"
        if delivery == "memory_patch"
        else "schemas/workflow-schema.yaml",
        "rationale": "gap",
        "proposed_change": "register adapters",
        "verification": {"suites": [], "required_cases": [], "success_criteria": "review"},
        "risk": "low",
        "confidence": "high",
        "state": state,
        "version": 1,
        "proposed_by_retro_ids": ["RET-1"],
        "last_event_id": "IMPEVT-1",
    }


def _eval_receipt() -> dict[str, object]:
    return {
        "eval_run_id": "eval-1",
        "outcome": "passed",
        "report_sha256": "r",
        "staged_sha256": "s",
        "baseline_sha256": None,
    }


@pytest.mark.asyncio
async def test_load_delivery_requires_approved_state(tmp_path: Path) -> None:
    outcome = await execute_task(
        LoadImprovementDeliveryHandler(),
        json_value(
            {
                "projection": _projection(state="proposed"),
                "stage": "evaluate",
                "memory_eval": _eval_receipt(),
            }
        ),
        tmp_path,
    )
    assert outcome.failure is not None
    assert outcome.failure.kind == "invalid_input"


@pytest.mark.asyncio
async def test_load_delivery_requires_exact_stage_receipts(tmp_path: Path) -> None:
    outcome = await execute_task(
        LoadImprovementDeliveryHandler(),
        json_value(
            {
                "projection": _projection(),
                "stage": "apply",
                "memory_eval": _eval_receipt(),
            }
        ),
        tmp_path,
    )
    assert outcome.failure is not None
    assert "exact receipts" in (outcome.failure.message or "")


@pytest.mark.asyncio
async def test_rollback_emits_delivery_effect_with_memory_rollback(tmp_path: Path) -> None:
    outcome = await execute_task(
        RollbackMemoryImprovementHandler(),
        json_value(
            {
                "projection": _projection(),
                "reason": "regressed",
                "restored_sha256": "x",
                "target_digest": HEX_A,
            }
        ),
        tmp_path,
    )
    assert outcome.status == "succeeded"
    payload = as_object(outcome.output)
    assert payload["reason"] == "regressed"
    assert len(outcome.effects) == 1
    assert outcome.effects[0].kind == "assurance.improvement.effect.delivery.v1"
    assert as_object(outcome.effects[0].payload)["kind"] == "memory_rollback"


@pytest.mark.asyncio
async def test_apply_memory_requires_passed_eval(tmp_path: Path) -> None:
    eval_receipt = {**_eval_receipt(), "outcome": "regressed"}
    outcome = await execute_task(
        ApplyMemoryImprovementHandler(),
        json_value(
            {
                "projection": _projection(),
                "eval_receipt": eval_receipt,
                "before_sha256": "b",
                "after_sha256": "a",
                "receipt_sha256": "r",
                "target_digest": HEX_A,
            }
        ),
        tmp_path,
    )
    assert outcome.failure is not None
    assert outcome.failure.kind == "invalid_input"


@pytest.mark.asyncio
async def test_project_archive_uses_quality_report_issue_risk(tmp_path: Path) -> None:
    outcome = await execute_task(
        ProjectArchiveHandler(),
        {
            "change_id": "CH-DEMO-001",
            "invocation_id": "inv-archive-1",
            "archive_digest": HEX_A,
            "report": quality_report_payload(issue_risk="high"),
            "artifact_paths": ["qa/archive/CH-DEMO-001/archive-summary.md"],
        },
        tmp_path,
    )
    assert outcome.status == "succeeded"
    payload = as_object(outcome.output)
    assert payload["archive_status"] == "archived_with_warnings"
    assert payload["issue_risk"] == "high"
    assert outcome.effects[0].kind == "assurance.improvement.effect.archive.v1"


@pytest.mark.asyncio
async def test_archive_prepare_locks_archiver_persona(tmp_path: Path) -> None:
    outcome = await execute_task(
        ArchivePrepareHandler(),
        skill_input(),
        tmp_path,
        binding_data=BINDING,
    )
    assert outcome.status == "succeeded"
    request = AgentRunRequest.model_validate(outcome.output)
    assert "Capability-owned archive skill" in (request.instructions[0].text_content or "")
    assert "Improvement archiver persona" in (request.instructions[1].text_content or "")


@pytest.mark.asyncio
async def test_archive_finalize_rejects_non_clear_risk_without_warning_status(tmp_path: Path) -> None:
    outcome = await execute_task(
        ArchiveFinalizeHandler(),
        fake_agent_result(archive_result(issue_risk="high", archive_status="archived")),
        tmp_path,
    )
    assert outcome.failure is not None
    assert outcome.failure.kind == "invalid_output"
    assert outcome.failure.retryable is True


@pytest.mark.asyncio
async def test_archive_finalize_accepts_warning_status(tmp_path: Path) -> None:
    outcome = await execute_task(
        ArchiveFinalizeHandler(),
        fake_agent_result(archive_result(issue_risk="high", archive_status="archived_with_warnings")),
        tmp_path,
    )
    assert outcome.status == "succeeded"
    document = ArchiveResultV1.model_validate(outcome.output)
    assert document.archive_status == "archived_with_warnings"


def test_archive_result_contract_bytes_equal_typed_model() -> None:
    assert resource_bytes("result-contracts/archive.v1.schema.json") == canonical_json_bytes(
        ArchiveResultV1.model_json_schema()
    )


def test_delivery_validator_default_fails_closed() -> None:
    result = DeliveryValidator().validate(
        write_set("improvements/delivery.json", "improvements/target-digest"),
        validation_context(),
    )
    assert result.accepted is False
    assert "not authenticated" in (result.reason or "")


def test_candidates_validator_requires_complete_expected_map() -> None:
    result = CandidatesValidator(expected={"candidates": HEX_A}).validate(
        write_set(
            "retro/proposal-candidates.json",
            "retro/context.json",
            "retro/source-manifest.json",
        ),
        validation_context(),
    )
    assert result.accepted is False
    assert "incomplete" in (result.reason or "")


def test_archive_integrity_requires_all_four_authenticated_inputs() -> None:
    result = ArchiveIntegrityValidator().validate(
        write_set(
            "qa/archive/subject.json",
            "qa/archive/artifact-manifest.json",
            "qa/archive/archive-summary.md",
        ),
        validation_context(),
    )
    assert result.accepted is False
    assert "pre_archive" in (result.reason or "")


def test_archive_integrity_path_only_rejects_src() -> None:
    result = ArchiveIntegrityValidator(path_only=True).validate(write_set("src/app.py"), validation_context())
    assert result.accepted is False


def test_plugin_validators_are_path_only() -> None:
    from graph_engine import ENGINE_API_VERSION, RegistryPorts

    from assurance_improvement.plugin import ImprovementPlugin

    contribution = ImprovementPlugin.contribute(RegistryPorts(engine_api=ENGINE_API_VERSION))
    validator = contribution.commit_validators["assurance.improvement.validator.archive-integrity.v1"]
    result = validator.validate(write_set("qa/archive/subject.json"), validation_context())
    assert result.accepted is True
