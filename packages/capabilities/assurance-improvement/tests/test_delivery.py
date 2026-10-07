from __future__ import annotations

import hashlib
import json
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
async def test_rollback_publishes_receipt_with_memory_rollback(tmp_path: Path) -> None:
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
    assert "qa/results/improvement/memory-rollback.json" in outcome.workspace_bytes


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
    assert "qa/results/improvement/memory-apply.json" not in outcome.workspace_bytes


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
    assert "qa/results/improvement/memory-apply.json" not in outcome.workspace_bytes


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


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ("memory_eval", "memory_apply", "memory_rollback", "change_export"))
async def test_delivery_stages_full_identity_and_nested_receipt(kind: str, tmp_path: Path) -> None:
    from assurance_improvement.contracts.delivery import artifact_digest

    projection = _projection(delivery="change_draft" if kind == "change_export" else "memory_patch")
    payload: dict[str, object] = {"projection": projection, "target_digest": HEX_A}
    handler: TaskHandler
    if kind == "memory_eval":
        handler = EvaluateMemoryImprovementHandler()
        payload.update(eval_run_id="eval-1", outcome="passed", report_sha256="r", staged_sha256="s")
    elif kind == "memory_apply":
        handler = ApplyMemoryImprovementHandler()
        payload.update(
            eval_receipt=_eval_receipt(projection),
            approved_state_digest=artifact_digest(ImprovementProjection.model_validate(projection)),
            approved_version=1,
            before_sha256="b",
            after_sha256="a",
            receipt_sha256="r",
        )
    elif kind == "memory_rollback":
        handler = RollbackMemoryImprovementHandler()
        payload.update(reason="regressed", restored_sha256="x")
    else:
        handler = ExportChangeImprovementHandler()
        payload.update(artifact_path="qa/improvements/drafts/IMP-1.yaml", sha256="x", created=True)
    outcome = await execute_task(handler, json_value(payload), tmp_path)
    assert outcome.status == "succeeded"
    record_path = "qa/results/improvement/" + kind.replace("_", "-") + ".json"
    assert record_path in outcome.workspace_bytes
    record = json.loads(outcome.workspace_bytes[record_path])
    assert record["improvement_id"] == "IMP-1"
    assert record["version"] == 1
    assert record["target_kind"] == kind
    assert record["target_digest"] == HEX_A
    receipt = record["receipt"]
    if kind == "memory_eval":
        assert record["target"] == ".aa/memory/aa-api-plan.md"
        assert receipt == as_object(outcome.output)["memory_eval"]
    elif kind == "memory_rollback":
        assert receipt == {
            "target": ".aa/memory/aa-api-plan.md",
            "restored_sha256": "x",
            "reason": "regressed",
        }
    else:
        assert receipt == as_object(outcome.output)


@pytest.mark.asyncio
@pytest.mark.parametrize("evaluation_outcome", ("passed", "regressed", "awaiting_baseline", "error"))
async def test_apply_reads_and_validates_the_bound_evaluation_record(
    evaluation_outcome: str, tmp_path: Path
) -> None:
    from assurance_improvement.contracts.delivery import artifact_digest

    projection = _projection()
    evaluated = await execute_task(
        EvaluateMemoryImprovementHandler(),
        json_value(
            {
                "projection": projection,
                "eval_run_id": "eval-1",
                "outcome": evaluation_outcome,
                "report_sha256": "r",
                "staged_sha256": "s",
                "target_digest": HEX_A,
            }
        ),
        tmp_path,
        write_root=tmp_path / "evaluate-stage",
    )
    assert evaluated.status == "succeeded"
    assert as_object(evaluated.output)["route"] == ("apply" if evaluation_outcome == "passed" else "failed")
    record_path = "qa/results/improvement/memory-eval.json"
    record_bytes = evaluated.workspace_bytes[record_path]
    committed_path = tmp_path / record_path
    committed_path.parent.mkdir(parents=True, exist_ok=True)
    committed_path.write_bytes(record_bytes)
    applied = await execute_task(
        ApplyMemoryImprovementHandler(),
        json_value(
            {
                "projection": projection,
                "eval_receipt_ref": {"path": record_path, "digest": hashlib.sha256(record_bytes).hexdigest()},
                "approved_state_digest": artifact_digest(ImprovementProjection.model_validate(projection)),
                "approved_version": 1,
                "before_sha256": "b",
                "after_sha256": "a",
                "receipt_sha256": "r",
                "target_digest": HEX_A,
            }
        ),
        tmp_path,
        write_root=tmp_path / "apply-stage",
    )
    apply_path = tmp_path / "apply-stage/qa/results/improvement/memory-apply.json"
    if evaluation_outcome == "passed":
        assert applied.status == "succeeded"
        assert json.loads(apply_path.read_bytes())["receipt"]["after_sha256"] == "a"
    else:
        assert applied.failure is not None
        assert applied.failure.kind == "invalid_input"
        assert not apply_path.exists()
