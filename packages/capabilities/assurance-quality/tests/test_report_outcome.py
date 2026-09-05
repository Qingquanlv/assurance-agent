from __future__ import annotations

import hashlib
from pathlib import Path
from typing import cast

import pytest
from agent_runtime_contracts import AgentRunResult
from agent_runtime_contracts.schema import canonical_digest
from graph_engine.attempts.resolutions import PermanentTaskFailure, ReceiptRef
from graph_engine.canonical import JSONValue
from graph_engine.testing import GraphHarness, committed
from pydantic import ValidationError

from assurance_product.graphs.tail_contracts import ExecuteTailResultV1, reported_tail_result
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1
from assurance_quality.contracts.assessment import (
    FinalizedReportV1,
    InspectionOutcomeV1,
    ReportOutcomeV1,
    ReportSkillInputV1,
)
from assurance_quality.contracts.attempts import AGENT_JOB_CONTRACTS, TASK_ATTEMPT_CONTRACTS
from assurance_quality.graphs.factory import build_quality_graphs
from assurance_quality.graphs.nodes import publish_report, select_report
from assurance_quality.operations.agent_skills import ReportFinalizeHandler, ReportPrepareHandler
from tests.product.test_change_local_output_routing import dual_roots, execute_task

_CHANGE = "CH-REPORT-1"
_BATCH = "batch-1"
_BYTES = b"authenticated evidence\n"
_DIGEST = hashlib.sha256(_BYTES).hexdigest()
_BINDING: JSONValue = {
    "agent_profile": "aa-reporter",
    "execution": {
        "provider_model": "test-model",
        "worker_profile": "worker",
        "permission_profile_digest": _DIGEST,
        "limits": {"max_seconds": 5},
    },
    "request_policy_digest": _DIGEST,
    "request_config_digest": _DIGEST,
}


def _ref(path: str, digest: str = _DIGEST) -> dict[str, str]:
    return {"path": path, "digest": digest}


def _receipt(name: str) -> ReceiptRef:
    return ReceiptRef(receipt_id=name, receipt_digest=_DIGEST)


def _state(*, batch_id: str = _BATCH) -> dict[str, object]:
    base = f"qa/changes/{_CHANGE}"
    assessment_base = f"{base}/inspect/epochs/0/batches/{batch_id}"
    reviewed = {
        "change_id": _CHANGE,
        "coverage_epoch": 0,
        "preparation_refs": [_ref(f"{base}/preparation/context.json")],
        "case_refs": [_ref(f"{base}/cases/system/case.yaml")],
        "review_ref": _ref(f"{base}/review/case-review.json"),
    }
    mapping_ref = _ref(f"{base}/generation/epochs/0/mapping.json")
    execution_ref = _ref(f"{base}/execution/epochs/0/batches/{batch_id}/result.json")
    assessment = {
        "change_id": _CHANGE,
        "coverage_epoch": 0,
        "batch_id": batch_id,
        "scope": {
            "change_id": _CHANGE,
            "coverage_epoch": 0,
            "required_case_ids": ["CASE-1"],
            "selected_families": ["api"],
            "applicable_goals": ["constraint_coverage"],
            "applicability_refs": reviewed["case_refs"],
            "risk_tier": "high",
            "policy_digest": _DIGEST,
        },
        "policy": {
            "coverage_floor_by_tier": {
                "low": 0.7,
                "medium": 0.8,
                "high": 0.9,
                "critical": 1.0,
            }
        },
        "trace_ref": _ref(f"{assessment_base}/trace.json"),
        "gaps_ref": _ref(f"{assessment_base}/coverage-gaps.json"),
        "metrics_ref": _ref(f"{assessment_base}/metrics.json"),
        "sufficiency_ref": _ref(f"{assessment_base}/trace-sufficiency.json"),
        "execution_ref": execution_ref,
        "healing_ref": None,
        "issue_ref": None,
    }
    fact_ref = _ref(f"{base}/facts/fact-baseline.json")
    assessment_refs = sorted(
        [
            assessment["trace_ref"],
            assessment["gaps_ref"],
            assessment["metrics_ref"],
            assessment["sufficiency_ref"],
            execution_ref,
            fact_ref,
        ],
        key=lambda item: (item["path"], item["digest"]),
    )
    inspection = {
        "change_id": _CHANGE,
        "coverage_epoch": 0,
        "batch_id": batch_id,
        "disposition": "satisfied",
        "inspection_receipt": _receipt(f"inspect-{batch_id}").model_dump(mode="json"),
        "reviewed_case": reviewed,
        "mapping_ref": mapping_ref,
        "assessment_refs": assessment_refs,
        "reason_codes": ["coverage.satisfied"],
        "coverage_state": "satisfied",
    }
    return {
        "change_id": _CHANGE,
        "coverage_epoch": 0,
        "batch_id": batch_id,
        "capability_leafs": ["orders.create"],
        "allowed_artifact_paths": [base],
        "assessment_inputs": assessment,
        "fact_baseline_ref": fact_ref,
        "reviewed_case": reviewed,
        "generation_result": {
            "change_id": _CHANGE,
            "coverage_epoch": 0,
            "reviewed_case": reviewed,
            "mapping_ref": mapping_ref,
            "source_refs": [_ref(f"{base}/generated/api/files/tests/test_orders.py")],
            "plan_refs": [_ref(f"{base}/generation/epochs/0/api/plan.json")],
        },
        "inspection_outcome": inspection,
        "coverage_state": "satisfied",
        "report_refs": [_ref(f"{base}/report/stale.md")],
        "report_receipt": _receipt("stale-report").model_dump(mode="json"),
        "execution_digest": _DIGEST,
        "healing_digest": None,
        "trace_digest": _DIGEST,
        "coverage_digest": _DIGEST,
        "metrics_digest": _DIGEST,
        "case_digest": _DIGEST,
        "plan_digest": _DIGEST,
        "mapping_digest": _DIGEST,
        "issue_digest": None,
    }


def _raw_report(selected: ReportSkillInputV1, *, include_files: bool = True) -> dict[str, object]:
    result: dict[str, object] = {
        "schema_version": "1.1",
        "change_id": _CHANGE,
        "batch_id": _BATCH,
        "purpose": "normal",
        "case_digest": selected.case_digest,
        "plan_digest": selected.plan_digest,
        "mapping_digest": selected.mapping_digest,
        "execution_digest": selected.execution_digest,
        "healing_digest": selected.healing_digest,
        "trace_digest": selected.trace_digest,
        "coverage_digest": selected.coverage_digest,
        "issue_digest": selected.issue_digest,
        "metrics_digest": selected.metrics_digest,
    }
    if include_files:
        result["report_files"] = [f"qa/changes/{_CHANGE}/report/report.md"]
    return result


def _agent_result(payload: dict[str, object]) -> dict[str, object]:
    result = AgentRunResult(
        result_payload=cast(JSONValue, payload),
        result_digest=canonical_digest(cast(JSONValue, payload)),
        evidence_digest=_DIGEST,
        adapter_id="test.fake",
        adapter_version="1.0.0",
    )
    return result.model_dump(mode="json")


def _write_authenticated_inputs(project: Path, selected: ReportSkillInputV1) -> None:
    refs = (
        *selected.inspection.reviewed_case.preparation_refs,
        *selected.inspection.reviewed_case.case_refs,
        selected.inspection.reviewed_case.review_ref,
        selected.inspection.mapping_ref,
        *selected.generation.source_refs,
        *selected.generation.plan_refs,
        *selected.inspection.assessment_refs,
    )
    for ref in refs:
        path = project / ref.path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_BYTES)
    policy = project / ".aa/policy.yaml"
    policy.parent.mkdir(parents=True, exist_ok=True)
    policy.write_bytes(_BYTES)


def test_report_input_does_not_accept_a_coverage_flag_or_stale_refs_alone() -> None:
    state = _state()
    state.pop("inspection_outcome")
    with pytest.raises(ValueError):
        select_report(state)


def test_report_selector_binds_current_satisfied_inspection_chain() -> None:
    selected = select_report(_state())
    assert isinstance(selected, ReportSkillInputV1)
    assert selected.coverage_epoch == 0
    assert selected.inspection.disposition == "satisfied"
    assert selected.inspection.batch_id == selected.batch_id


def test_report_selector_rejects_an_inspection_from_a_previous_batch() -> None:
    state = _state()
    state["batch_id"] = "batch-2"
    with pytest.raises(ValidationError, match="current inspection"):
        select_report(state)


@pytest.mark.asyncio
async def test_report_prepare_authenticates_the_current_inspection_chain(tmp_path: Path) -> None:
    project, write_root = dual_roots(tmp_path, _CHANGE)
    selected = cast(ReportSkillInputV1, select_report(_state()))
    _write_authenticated_inputs(project, selected)
    prepared = await execute_task(
        ReportPrepareHandler(),
        cast(JSONValue, selected.model_dump(mode="json")),
        project,
        write_root=write_root,
        binding_data=_BINDING,
    )
    assert prepared.outcome.status == "succeeded", prepared.outcome.failure


@pytest.mark.asyncio
async def test_report_finalizer_rejects_wrapped_input(tmp_path: Path) -> None:
    selected = cast(ReportSkillInputV1, select_report(_state()))
    result = await execute_task(
        ReportFinalizeHandler(),
        cast(
            JSONValue,
            {
                "validated_input": selected.model_dump(mode="json"),
                "prepared": None,
                "agent_result": _agent_result(_raw_report(selected)),
            },
        ),
        tmp_path,
    )
    assert result.outcome.failure is not None
    assert result.outcome.failure.kind == "invalid_input"


@pytest.mark.asyncio
async def test_report_finalize_requires_declared_new_report_bytes(tmp_path: Path) -> None:
    project, write_root = dual_roots(tmp_path, _CHANGE)
    selected = cast(ReportSkillInputV1, select_report(_state()))
    _write_authenticated_inputs(project, selected)
    missing_refs = await execute_task(
        ReportFinalizeHandler(),
        cast(
            JSONValue,
            {
                **selected.model_dump(mode="json"),
                "agent_result": _agent_result(_raw_report(selected, include_files=False)),
            },
        ),
        project,
        write_root=write_root,
    )
    assert missing_refs.outcome.failure is not None
    assert missing_refs.outcome.failure.kind == "invalid_output"

    declared_without_bytes = await execute_task(
        ReportFinalizeHandler(),
        cast(
            JSONValue,
            {
                **selected.model_dump(mode="json"),
                "agent_result": _agent_result(_raw_report(selected)),
            },
        ),
        project,
        write_root=write_root,
    )
    assert declared_without_bytes.outcome.failure is not None
    assert declared_without_bytes.outcome.failure.kind == "invalid_output"


@pytest.mark.asyncio
async def test_report_bytes_are_finalized_then_bound_to_commit_receipt(tmp_path: Path) -> None:
    project, write_root = dual_roots(tmp_path, _CHANGE)
    state = _state()
    selected = cast(ReportSkillInputV1, select_report(state))
    _write_authenticated_inputs(project, selected)
    report_path = f"qa/changes/{_CHANGE}/report/report.md"
    staged = write_root / report_path
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_bytes(b"# Current report\n")
    finalized_run = await execute_task(
        ReportFinalizeHandler(),
        cast(
            JSONValue,
            {
                **selected.model_dump(mode="json"),
                "agent_result": _agent_result(_raw_report(selected)),
            },
        ),
        project,
        write_root=write_root,
    )
    assert finalized_run.outcome.status == "succeeded", finalized_run.outcome.failure
    finalized = FinalizedReportV1.model_validate(finalized_run.outcome.output)
    assert finalized.report_refs[0].digest == hashlib.sha256(staged.read_bytes()).hexdigest()

    receipt = _receipt("report-current")
    published = publish_report(state, finalized, receipt)
    outcome = ReportOutcomeV1.model_validate(published["report_outcome"])
    assert outcome.report_receipt == receipt
    assert published["report_refs"] == [ref.model_dump(mode="json") for ref in outcome.report_refs]
    assert published["report_receipt"] == receipt.model_dump(mode="json")


def test_reported_tail_rejects_previous_batch_or_inspection_receipt() -> None:
    inspection = InspectionOutcomeV1.model_validate(_state()["inspection_outcome"])
    report = ReportOutcomeV1(
        change_id=_CHANGE,
        coverage_epoch=0,
        batch_id="previous-batch",
        inspection_receipt=_receipt("inspect-previous"),
        report_refs=(EvidenceArtifactRefV1.model_validate(_ref(f"qa/changes/{_CHANGE}/report/report.md")),),
        report_receipt=_receipt("report-current"),
    )
    with pytest.raises(ValueError, match="current inspection"):
        reported_tail_result(inspection, report)


def test_reported_tail_requires_a_satisfied_inspection() -> None:
    inspection = InspectionOutcomeV1.model_validate(_state()["inspection_outcome"]).model_copy(
        update={"disposition": "blocked", "coverage_state": None}
    )
    matching = ReportOutcomeV1(
        change_id=_CHANGE,
        coverage_epoch=0,
        batch_id=_BATCH,
        inspection_receipt=inspection.inspection_receipt,
        report_refs=(EvidenceArtifactRefV1.model_validate(_ref(f"qa/changes/{_CHANGE}/report/report.md")),),
        report_receipt=_receipt("report-current"),
    )
    with pytest.raises(ValueError, match="satisfied inspection"):
        reported_tail_result(inspection, matching)


@pytest.mark.asyncio
async def test_failed_report_attempt_clears_stale_report_state() -> None:
    contracts = {
        contract.contract_id: contract.to_task_contract() for contract in AGENT_JOB_CONTRACTS.values()
    }
    contracts.update({contract.contract_id: contract for contract in TASK_ATTEMPT_CONTRACTS.values()})
    harness = GraphHarness()
    graph = build_quality_graphs(
        harness.recording_context(owner_id="assurance.quality", contracts=contracts)
    ).report
    result = await harness.run(
        graph,
        input=_state(),
        script={"quality.report": [PermanentTaskFailure(kind="invalid_output", message="missing report")]},
    )
    terminal = cast(dict[str, object], result.terminal)
    assert terminal["report_refs"] == []
    assert terminal["report_receipt"] is None
    assert not terminal.get("report_outcome")
    with pytest.raises(ValidationError):
        ExecuteTailResultV1(
            status="reported",
            inspection=InspectionOutcomeV1.model_validate(_state()["inspection_outcome"]),
            report_refs=(),
            report_receipt=None,
        )


@pytest.mark.asyncio
async def test_report_graph_publishes_only_the_current_committed_outcome() -> None:
    contracts = {
        contract.contract_id: contract.to_task_contract() for contract in AGENT_JOB_CONTRACTS.values()
    }
    contracts.update({contract.contract_id: contract for contract in TASK_ATTEMPT_CONTRACTS.values()})
    harness = GraphHarness()
    graph = build_quality_graphs(
        harness.recording_context(owner_id="assurance.quality", contracts=contracts)
    ).report
    state = _state()
    inspection = InspectionOutcomeV1.model_validate(state["inspection_outcome"])
    ref = EvidenceArtifactRefV1.model_validate(_ref(f"qa/changes/{_CHANGE}/report/report.md"))
    finalized = FinalizedReportV1(
        change_id=_CHANGE,
        coverage_epoch=0,
        batch_id=_BATCH,
        purpose="normal",
        inspection_receipt=inspection.inspection_receipt,
        report_refs=(ref,),
    )
    result = await harness.run(
        graph,
        input=state,
        script={"quality.report": [committed(finalized, _receipt("report-current"))]},
    )
    published = result.published_update
    assert published is not None
    assert published["status"] == "reported"
    outcome = ReportOutcomeV1.model_validate(published["report_outcome"])
    assert outcome.batch_id == inspection.batch_id
    assert outcome.inspection_receipt == inspection.inspection_receipt


def test_diagnostic_report_cannot_publish_a_normal_success_outcome() -> None:
    state = _state()
    inspection = InspectionOutcomeV1.model_validate(state["inspection_outcome"]).model_copy(
        update={"disposition": "blocked", "coverage_state": None}
    )
    state["inspection_outcome"] = inspection.model_dump(mode="json")
    state["coverage_state"] = None
    state["report_purpose"] = "diagnostic"
    selected = cast(ReportSkillInputV1, select_report(state))
    ref = EvidenceArtifactRefV1.model_validate(_ref(f"qa/changes/{_CHANGE}/report/report.md"))
    finalized = FinalizedReportV1(
        change_id=_CHANGE,
        coverage_epoch=0,
        batch_id=_BATCH,
        purpose="diagnostic",
        inspection_receipt=inspection.inspection_receipt,
        report_refs=(ref,),
    )
    published = publish_report(state, finalized, _receipt("diagnostic-report"))
    assert selected.purpose == "diagnostic"
    assert published["status"] == "diagnostic"
    assert published["report_outcome"] is None
