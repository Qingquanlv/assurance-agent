from __future__ import annotations

from pathlib import Path

import pytest
from agent_runtime_contracts import AgentRunRequest
from graph_engine.canonical import canonical_json_bytes
from graph_engine.plugin_api import TaskHandler
from tests.product.test_change_local_output_routing import execute_task

from assurance_improvement.contracts.agent import ArchiveResultV1
from assurance_improvement.operations.agent import ArchiveFinalizeHandler, ArchivePrepareHandler
from assurance_improvement.operations.archive import ProjectArchiveHandler
from assurance_improvement.operations.delivery import (
    ApplyMemoryImprovementHandler,
    EvaluateMemoryImprovementHandler,
    ExportChangeImprovementHandler,
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
    improvement_projection,
    json_value,
    locked_archive_input,
    quality_report_payload,
    skill_input,
    validation_context,
    write_set,
)


def _projection(*, state: str = "approved", delivery: str = "memory_patch") -> dict[str, object]:
    return improvement_projection(state=state, delivery=delivery)


def _eval_receipt(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "eval_run_id": "eval-1",
        "outcome": "passed",
        "report_sha256": "r",
        "staged_sha256": "s",
        "baseline_sha256": None,
    }
    payload.update(overrides)
    return payload


def attempt_apply(*, state: str, evaluation: str = "passed"):
    from assurance_improvement.operations.delivery import attempt_apply as _attempt_apply

    return _attempt_apply(state=state, evaluation=evaluation)


@pytest.mark.parametrize("state", ["proposed", "changes_requested", "rejected", "superseded"])
def test_apply_requires_authenticated_approved_state(state: str) -> None:
    result = attempt_apply(state=state, evaluation="passed")
    assert result.applied is False
    assert result.effect_intents == ()


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


@pytest.mark.parametrize("evaluation", ["failed", "missing", "stale"])
def test_apply_rejects_unsuccessful_or_stale_evaluation(evaluation: str) -> None:
    result = attempt_apply(state="approved", evaluation=evaluation)
    assert result.applied is False
    assert result.effect_intents == ()
    assert result.write_authorization == ()


def test_apply_rejects_forged_approved_proof() -> None:
    from assurance_improvement.operations.delivery import attempt_apply as _attempt_apply

    result = _attempt_apply(state="approved", evaluation="passed", forge_state_digest=True)
    assert result.applied is False
    assert result.effect_intents == ()
    assert result.write_authorization == ()


def test_authenticated_approved_and_current_eval_applies() -> None:
    result = attempt_apply(state="approved", evaluation="passed")
    assert result.applied is True
    assert result.effect_intents != ()


@pytest.mark.asyncio
async def test_apply_memory_requires_passed_eval(tmp_path: Path) -> None:
    eval_receipt = {**_eval_receipt(), "outcome": "regressed"}
    from assurance_improvement.contracts.delivery import artifact_digest
    from assurance_improvement.contracts.improvements import ImprovementProjection

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
async def test_project_archive_uses_quality_report_issue_risk(tmp_path: Path) -> None:
    outcome = await execute_task(
        ProjectArchiveHandler(),
        json_value(
            {
                "change_id": "CH-DEMO-001",
                "invocation_id": "inv-archive-1",
                "archive_digest": HEX_A,
                "report": quality_report_payload(issue_risk="high"),
                "artifact_paths": ["qa/archive/CH-DEMO-001/archive-summary.md"],
                "publish_receipt": _publish_receipt(),
            }
        ),
        tmp_path,
    )
    assert outcome.status == "succeeded"
    payload = as_object(outcome.output)
    assert payload["archive_status"] == "archived_with_warnings"
    assert payload["issue_risk"] == "high"
    assert outcome.effects[0].kind == "assurance.improvement.effect.archive.v1"


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
async def test_project_archive_without_publish_receipt_fails(tmp_path: Path) -> None:
    outcome = await execute_task(
        ProjectArchiveHandler(),
        json_value(
            {
                "change_id": "CH-DEMO-001",
                "invocation_id": "inv-archive-1",
                "archive_digest": HEX_A,
                "report": quality_report_payload(issue_risk="high"),
                "artifact_paths": ["qa/archive/CH-DEMO-001/archive-summary.md"],
            }
        ),
        tmp_path,
    )
    assert outcome.status != "succeeded"
    assert outcome.failure is not None
    assert outcome.failure.kind == "invalid_input"
    assert "publish" in (outcome.failure.message or "").lower()


@pytest.mark.asyncio
async def test_project_archive_rejects_mismatched_publish_receipt(tmp_path: Path) -> None:
    outcome = await execute_task(
        ProjectArchiveHandler(),
        json_value(
            {
                "change_id": "CH-DEMO-001",
                "invocation_id": "inv-archive-1",
                "archive_digest": HEX_A,
                "report": quality_report_payload(issue_risk="clear"),
                "artifact_paths": ["qa/archive/CH-DEMO-001/archive-summary.md"],
                "publish_receipt": _publish_receipt("CH-OTHER-001"),
            }
        ),
        tmp_path,
    )
    assert outcome.failure is not None
    assert outcome.failure.kind == "invalid_input"
    assert "receipt" in (outcome.failure.message or "").lower()


@pytest.mark.asyncio
async def test_project_archive_accepts_authenticated_publish_receipt(tmp_path: Path) -> None:
    outcome = await execute_task(
        ProjectArchiveHandler(),
        json_value(
            {
                "change_id": "CH-DEMO-001",
                "invocation_id": "inv-archive-1",
                "archive_digest": HEX_A,
                "report": quality_report_payload(issue_risk="clear"),
                "artifact_paths": ["qa/archive/CH-DEMO-001/archive-summary.md"],
                "publish_receipt": _publish_receipt(),
            }
        ),
        tmp_path,
    )
    assert outcome.status == "succeeded"
    payload = as_object(outcome.output)
    assert payload["archive_status"] == "archived"
    assert payload["change_id"] == "CH-DEMO-001"


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
        locked_archive_input(archive_result(issue_risk="high", archive_status="archived")),
        tmp_path,
    )
    assert outcome.failure is not None
    assert outcome.failure.kind == "invalid_output"
    assert outcome.failure.retryable is True


@pytest.mark.asyncio
async def test_archive_finalize_accepts_warning_status(tmp_path: Path) -> None:
    outcome = await execute_task(
        ArchiveFinalizeHandler(),
        locked_archive_input(archive_result(issue_risk="high", archive_status="archived_with_warnings")),
        tmp_path,
    )
    assert outcome.status == "succeeded"
    document = ArchiveResultV1.model_validate(outcome.output)
    assert document.archive_status == "archived_with_warnings"


def test_archive_result_contract_bytes_equal_typed_model() -> None:
    assert resource_bytes("result-contracts/archive.v1.schema.json") == canonical_json_bytes(
        ArchiveResultV1.model_json_schema()
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
        ArchiveFinalizeHandler(),
        locked_archive_input(
            archive_result(issue_risk="clear", archive_status="archived"),
            quality_report=report,
            quality_report_digest=digest_hex(artifact_digest(QualityReport.model_validate(report))),
        ),
        tmp_path,
    )
    assert outcome.failure is not None
    assert outcome.failure.kind == "invalid_output"


def test_delivery_validator_rejects_proposed_injected_document() -> None:
    import hashlib
    import json

    from assurance_improvement.contracts.delivery import ImprovementDeliveryDocument
    from assurance_improvement.contracts.improvements import ImprovementProjection

    document = ImprovementDeliveryDocument.model_validate(
        {
            "schema_version": "1",
            "improvement_id": IMPROVEMENT_ID,
            "expected_improvement_version": 1,
            "delivery": "memory_patch",
            "memory_eval": _eval_receipt(),
        }
    ).model_dump(mode="json")
    projection = ImprovementProjection.model_validate(_projection(state="proposed"))
    files = {
        "improvements/delivery.json": json.dumps(document, sort_keys=True).encode(),
        "improvements/target-digest": HEX_A.encode(),
    }
    listed = {path: hashlib.sha256(raw).hexdigest() for path, raw in files.items()}
    write = write_set(*files).model_copy(
        update={
            "files": tuple(
                item.model_copy(update={"after_sha256": listed[item.path]})
                for item in write_set(*files).files
            )
        }
    )
    result = DeliveryValidator(
        expected={
            "delivery": listed["improvements/delivery.json"],
            "target": listed["improvements/target-digest"],
        },
        file_bytes=files,
        projection=projection,
    ).validate(write, validation_context())
    assert result.accepted is False
    assert "approved" in (result.reason or "")


def test_delivery_validator_accepts_approved_injected_document() -> None:
    import hashlib
    import json

    from assurance_improvement.contracts.delivery import ImprovementDeliveryDocument
    from assurance_improvement.contracts.improvements import ImprovementProjection

    document = ImprovementDeliveryDocument.model_validate(
        {
            "schema_version": "1",
            "improvement_id": IMPROVEMENT_ID,
            "expected_improvement_version": 1,
            "delivery": "memory_patch",
            "memory_eval": _eval_receipt(),
        }
    ).model_dump(mode="json")
    projection = ImprovementProjection.model_validate(_projection())
    files = {
        "improvements/delivery.json": json.dumps(document, sort_keys=True).encode(),
        "improvements/target-digest": HEX_A.encode(),
    }
    listed = {path: hashlib.sha256(raw).hexdigest() for path, raw in files.items()}
    write = write_set(*files).model_copy(
        update={
            "files": tuple(
                item.model_copy(update={"after_sha256": listed[item.path]})
                for item in write_set(*files).files
            )
        }
    )
    result = DeliveryValidator(
        expected={
            "delivery": listed["improvements/delivery.json"],
            "target": listed["improvements/target-digest"],
        },
        file_bytes=files,
        projection=projection,
    ).validate(write, validation_context())
    assert result.accepted is True


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


def test_delivery_validator_requires_injected_bytes() -> None:
    result = DeliveryValidator(
        expected={"delivery": HEX_A, "target": HEX_A},
    ).validate(
        write_set("improvements/delivery.json", "improvements/target-digest"),
        validation_context(),
    )
    assert result.accepted is False
    assert "candidate bytes" in (result.reason or "")


def test_candidates_validator_requires_manifest_membership() -> None:
    import hashlib
    import json

    from assurance_improvement.contracts.delivery import artifact_digest
    from assurance_improvement.contracts.retro import (
        EvalEvidenceSlice,
        IssueEvidenceSlice,
        WorkflowEvidenceSlice,
    )
    from assurance_improvement.operations.retro import AssembleRetroInput, assemble_context

    window = {"selection": {"mode": "last", "requested_last": 1}, "change_ids": ["CH-DEMO-001"]}
    source = {
        "kind": "project_problem_ledger",
        "change_id": None,
        "head_event_id": "evt-1",
        "sha256": "abc",
        "evidence_ids": ["PROB-1", "OCC-1"],
    }

    def _slice(domain: str) -> dict[str, object]:
        return {
            "schema_version": "3",
            "retro_id": "RET-1",
            "domain": domain,
            "window": window,
            "sources": [source] if domain == "issue" else [],
            "integrity": {"status": "complete", "reasons": []},
            "deterministic_signals": [],
            "entries": [],
        }

    issue = IssueEvidenceSlice.model_validate(_slice("issue"))
    workflow = WorkflowEvidenceSlice.model_validate(_slice("workflow"))
    evaluation = EvalEvidenceSlice.model_validate(_slice("eval"))
    assembled = assemble_context(
        AssembleRetroInput.model_validate(
            {
                "generated_at": "2026-08-22T00:00:00Z",
                "window": window,
                "issue_slice": issue.model_dump(mode="json"),
                "workflow_slice": workflow.model_dump(mode="json"),
                "eval_slice": evaluation.model_dump(mode="json"),
                "issue_signals": {
                    "schema_version": "3",
                    "retro_id": "RET-1",
                    "domain": "issue",
                    "analysis_status": "ok",
                    "failure_reason": None,
                    "analyzer": "aa-retro-issue-analysis",
                    "signals": [],
                    "slice_sha256": artifact_digest(issue),
                },
                "workflow_signals": {
                    "schema_version": "3",
                    "retro_id": "RET-1",
                    "domain": "workflow",
                    "analysis_status": "ok",
                    "failure_reason": None,
                    "analyzer": "aa-retro-workflow-analysis",
                    "signals": [],
                    "slice_sha256": artifact_digest(workflow),
                },
                "eval_signals": {
                    "schema_version": "3",
                    "retro_id": "RET-1",
                    "domain": "eval",
                    "analysis_status": "ok",
                    "failure_reason": None,
                    "analyzer": "aa-retro-eval-analysis",
                    "signals": [],
                    "slice_sha256": artifact_digest(evaluation),
                },
                "issue_slice_sha256": artifact_digest(issue),
                "workflow_slice_sha256": artifact_digest(workflow),
                "eval_slice_sha256": artifact_digest(evaluation),
            }
        )
    )
    document = {
        "schema_version": "3",
        "retro_id": assembled.retro_id,
        "context_sha256": artifact_digest(assembled),
        "candidates": [],
    }
    files = {
        "retro/proposal-candidates.json": json.dumps(document, sort_keys=True).encode(),
        "retro/context.json": json.dumps(assembled.model_dump(mode="json"), sort_keys=True).encode(),
        "retro/source-manifest.json": json.dumps(
            assembled.source_manifest.model_dump(mode="json"), sort_keys=True
        ).encode(),
    }
    listed = {path: hashlib.sha256(raw).hexdigest() for path, raw in files.items()}
    write = write_set(*files).model_copy(
        update={
            "files": tuple(
                item.model_copy(update={"after_sha256": listed[item.path]})
                for item in write_set(*files).files
            )
        }
    )
    accepted = CandidatesValidator(
        expected={
            "candidates": listed["retro/proposal-candidates.json"],
            "context": listed["retro/context.json"],
            "manifest": listed["retro/source-manifest.json"],
        },
        file_bytes=files,
    ).validate(write, validation_context())
    assert accepted.accepted is True


def test_archive_integrity_requires_summary_in_manifest() -> None:
    import hashlib
    import json

    files = {
        "qa/archive/subject.json": json.dumps({"change_id": "CH-DEMO-001"}).encode(),
        "qa/archive/artifact-manifest.json": json.dumps({"artifact_paths": ["qa/archive/other.md"]}).encode(),
        "qa/archive/archive-summary.md": b"# summary\n",
        "qa/changes/pre-archive-tree.json": json.dumps({"tree": "ok"}).encode(),
    }
    listed = {path: hashlib.sha256(raw).hexdigest() for path, raw in files.items()}
    write = write_set(*files).model_copy(
        update={
            "files": tuple(
                item.model_copy(update={"after_sha256": listed[item.path]})
                for item in write_set(*files).files
            )
        }
    )
    result = ArchiveIntegrityValidator(
        expected={
            "subject": listed["qa/archive/subject.json"],
            "manifest": listed["qa/archive/artifact-manifest.json"],
            "summary": listed["qa/archive/archive-summary.md"],
            "pre_archive": listed["qa/changes/pre-archive-tree.json"],
        },
        file_bytes=files,
    ).validate(write, validation_context())
    assert result.accepted is False
    assert "manifest" in (result.reason or "")


def test_plugin_validators_are_path_only() -> None:
    from graph_engine import ENGINE_API_VERSION, RegistryPorts

    from assurance_improvement.plugin import ImprovementPlugin

    contribution = ImprovementPlugin.contribute(RegistryPorts(engine_api=ENGINE_API_VERSION))
    validator = contribution.commit_validators["assurance.improvement.validator.archive-integrity.v1"]
    result = validator.validate(write_set("qa/archive/subject.json"), validation_context())
    assert result.accepted is True
