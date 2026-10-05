from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest
from agent_runtime_contracts import AgentRunRequest
from graph_engine.frozen_json import freeze_json
from graph_engine.plugin_api import TaskHandler
from tests.product.test_change_local_output_routing import execute_task, task_request

from assurance_improvement.contracts.agent import ArchiveResultV1
from assurance_improvement.contracts.improvements import ImprovementProjection
from assurance_improvement.ops.archive import finalize as archive_finalize, prepare as archive_prepare
from assurance_improvement.operations.delivery import (
    ApplyMemoryImprovementHandler,
    EvaluateMemoryImprovementHandler,
    ExportChangeImprovementHandler,
    RollbackMemoryImprovementHandler,
)
from improvement_fixtures import (  # pyright: ignore[reportMissingImports]
    BINDING,
    HEX_A,
    archive_result,
    as_object,
    improvement_projection,
    json_value,
    locked_archive_input,
    quality_report_payload,
    skill_input,
)


def _projection(*, state: str = "approved", delivery: str = "memory_patch") -> dict[str, object]:
    return improvement_projection(state=state, delivery=delivery)


def _eval_receipt(projection: dict[str, object] | None = None, **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "eval_run_id": "eval-1",
        "outcome": "passed",
        "report_sha256": "r",
        "staged_sha256": "s",
        "baseline_sha256": None,
    }
    if projection is not None:
        from assurance_improvement.contracts.delivery import artifact_digest

        model = ImprovementProjection.model_validate(projection)
        payload["approved_state_digest"] = artifact_digest(model)
        payload["approved_version"] = model.version
    payload.update(overrides)
    return payload


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


def test_apply_proof_rejects_missing_and_stale_evaluation_binding() -> None:
    from pydantic import ValidationError

    from assurance_improvement.contracts.delivery import ImprovementApplyProof, MemoryEvalReceipt

    current = "sha256:" + ("a" * 64)
    other = "sha256:" + ("d" * 64)
    bound = MemoryEvalReceipt.model_validate(_eval_receipt(approved_state_digest=current, approved_version=1))
    ImprovementApplyProof(approved_state_digest=current, approved_version=1, evaluation=bound)
    with pytest.raises(ValidationError, match="missing"):
        ImprovementApplyProof(
            approved_state_digest=current,
            approved_version=1,
            evaluation=MemoryEvalReceipt.model_validate(_eval_receipt()),
        )
    with pytest.raises(ValidationError, match="stale"):
        ImprovementApplyProof(
            approved_state_digest=current,
            approved_version=1,
            evaluation=MemoryEvalReceipt.model_validate(
                _eval_receipt(approved_state_digest=other, approved_version=1)
            ),
        )


@pytest.mark.asyncio
async def test_apply_memory_requires_passed_eval(tmp_path: Path) -> None:
    eval_receipt = {**_eval_receipt(), "outcome": "regressed"}
    from assurance_improvement.contracts.delivery import artifact_digest

    projection = _projection()
    outcome = await execute_task(
        ApplyMemoryImprovementHandler(),
        json_value(
            {
                "projection": projection,
                "eval_receipt": eval_receipt,
                "approved_state_digest": artifact_digest(ImprovementProjection.model_validate(projection)),
                "approved_version": 1,
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
async def test_evaluate_stamps_current_approved_state(tmp_path: Path) -> None:
    from assurance_improvement.contracts.delivery import artifact_digest

    projection = _projection()
    outcome = await execute_task(
        EvaluateMemoryImprovementHandler(),
        json_value(
            {
                "projection": projection,
                "eval_run_id": "eval-1",
                "outcome": "passed",
                "report_sha256": "r",
                "staged_sha256": "s",
                "target_digest": HEX_A,
            }
        ),
        tmp_path,
    )
    assert outcome.status == "succeeded"
    payload = as_object(as_object(outcome.output)["memory_eval"])
    model = ImprovementProjection.model_validate(projection)
    assert payload["approved_state_digest"] == artifact_digest(model)
    assert payload["approved_version"] == model.version


@pytest.mark.asyncio
async def test_apply_rejects_eval_bound_to_other_approved_state(tmp_path: Path) -> None:
    from assurance_improvement.contracts.delivery import artifact_digest

    projection = _projection()
    outcome = await execute_task(
        ApplyMemoryImprovementHandler(),
        json_value(
            {
                "projection": projection,
                "eval_receipt": _eval_receipt(
                    approved_state_digest="sha256:" + ("d" * 64),
                    approved_version=1,
                ),
                "approved_state_digest": artifact_digest(ImprovementProjection.model_validate(projection)),
                "approved_version": 1,
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
    assert outcome.effects == ()


@pytest.mark.asyncio
async def test_apply_rejects_eval_missing_approved_binding(tmp_path: Path) -> None:
    from assurance_improvement.contracts.delivery import artifact_digest

    projection = _projection()
    outcome = await execute_task(
        ApplyMemoryImprovementHandler(),
        json_value(
            {
                "projection": projection,
                "eval_receipt": _eval_receipt(),
                "approved_state_digest": artifact_digest(ImprovementProjection.model_validate(projection)),
                "approved_version": 1,
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
    assert outcome.effects == ()


def _publish_receipt(change_id: str = "CH-DEMO-001") -> dict[str, object]:
    return {
        "schema_version": "1",
        "change_id": change_id,
        "manifest_digest": HEX_A,
        "source_digest": HEX_A,
        "target_baseline": HEX_A,
        "final_digest": HEX_A,
    }


@pytest.mark.asyncio
async def test_archive_prepare_locks_skill(tmp_path: Path) -> None:
    outcome = await execute_task(
        cast(TaskHandler, archive_prepare),
        skill_input(),
        tmp_path,
        binding_data=BINDING,
    )
    assert outcome.status == "succeeded"
    request = AgentRunRequest.model_validate(outcome.output)
    assert "Capability-owned archive skill" in (request.instructions[0].text_content or "")
    assert "Do not run product CLI commands" in (request.instructions[0].text_content or "")


@pytest.mark.asyncio
async def test_archive_finalize_rejects_non_clear_risk_without_warning_status(tmp_path: Path) -> None:
    outcome = await execute_task(
        cast(TaskHandler, archive_finalize),
        locked_archive_input(archive_result(issue_risk="high", archive_status="archived")),
        tmp_path,
    )
    assert outcome.failure is not None
    assert outcome.failure.kind == "invalid_output"
    assert outcome.failure.retryable is True


@pytest.mark.asyncio
async def test_archive_finalize_accepts_warning_status(tmp_path: Path) -> None:
    outcome = await execute_task(
        cast(TaskHandler, archive_finalize),
        locked_archive_input(archive_result(issue_risk="high", archive_status="archived_with_warnings")),
        tmp_path,
    )
    assert outcome.status == "succeeded"
    from assurance_improvement.contracts.agent import ArchivePublishedV1

    document = ArchivePublishedV1.model_validate(outcome.output)
    assert document.archive_status == "archived_with_warnings"
    assert document.result.archive_status == "archived_with_warnings"


@pytest.mark.asyncio
async def test_archive_finalize_normalizes_frozen_wire_input(tmp_path: Path) -> None:
    payload = locked_archive_input(archive_result(issue_risk="high", archive_status="archived_with_warnings"))
    request = task_request(payload).model_copy(update={"input": freeze_json(payload)})
    outcome = await execute_task(cast(TaskHandler, archive_finalize), request, tmp_path)
    assert outcome.status == "succeeded"


def test_archive_result_contract_is_the_typed_model() -> None:
    from assurance_improvement.ops import router

    result = router.agent_ops()["archive"].agent.result
    assert (result.__module__, result.__qualname__) == (
        ArchiveResultV1.__module__,
        ArchiveResultV1.__qualname__,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler", "payload"),
    (
        (
            EvaluateMemoryImprovementHandler(),
            {
                "projection": _projection(state="proposed"),
                "eval_run_id": "eval-1",
                "outcome": "passed",
                "report_sha256": "r",
                "staged_sha256": "s",
                "target_digest": HEX_A,
            },
        ),
        (
            ApplyMemoryImprovementHandler(),
            {
                "projection": _projection(state="proposed"),
                "eval_receipt": _eval_receipt(),
                "approved_state_digest": f"sha256:{HEX_A}",
                "approved_version": 1,
                "before_sha256": "b",
                "after_sha256": "a",
                "receipt_sha256": "r",
                "target_digest": HEX_A,
            },
        ),
        (
            RollbackMemoryImprovementHandler(),
            {
                "projection": _projection(state="proposed"),
                "reason": "regressed",
                "restored_sha256": "x",
                "target_digest": HEX_A,
            },
        ),
        (
            ExportChangeImprovementHandler(),
            {
                "projection": _projection(state="proposed", delivery="change_draft"),
                "artifact_path": "qa/improvements/drafts/IMP-1.yaml",
                "sha256": "x",
                "created": True,
                "target_digest": HEX_A,
            },
        ),
    ),
)
async def test_delivery_mutations_require_approved_state(
    handler: TaskHandler, payload: dict[str, object], tmp_path: Path
) -> None:
    outcome = await execute_task(handler, json_value(payload), tmp_path)
    assert outcome.failure is not None
    assert outcome.failure.kind == "invalid_input"
    assert "approved" in (outcome.failure.message or "")


@pytest.mark.asyncio
async def test_archive_finalize_rejects_risk_mismatch(tmp_path: Path) -> None:
    from assurance_improvement.contracts.delivery import artifact_digest, digest_hex
    from assurance_quality.contracts.report import QualityReport

    report = quality_report_payload(issue_risk="high")
    outcome = await execute_task(
        cast(TaskHandler, archive_finalize),
        locked_archive_input(
            archive_result(issue_risk="clear", archive_status="archived"),
            quality_report=report,
            quality_report_digest=digest_hex(artifact_digest(QualityReport.model_validate(report))),
        ),
        tmp_path,
    )
    assert outcome.failure is not None
    assert outcome.failure.kind == "invalid_output"
