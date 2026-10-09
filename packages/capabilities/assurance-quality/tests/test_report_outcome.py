from __future__ import annotations

import hashlib
from pathlib import Path
from typing import cast

import pytest
from agent_runtime_contracts import AgentRunResult
from agent_runtime_contracts.wire.schema import canonical_digest
from graph_engine.attempts.models.resolutions import PermanentTaskFailure, ReceiptRef
from graph_engine.canonical import JSONValue
from graph_engine.plugin_api import TaskHandler
from graph_engine.testing import GraphHarness, committed
from pydantic import ValidationError

from assurance_intake.contracts.workflow import EvidenceArtifactRefV1
from assurance_quality.contracts.assessment import (
    FinalizedReportV1,
    InspectionOutcomeV1,
    ReportOutcomeV1,
    ReportPublishedV1,
    ReportSkillInputV1,
)
from assurance_quality.contracts.attempts import AGENT_JOB_CONTRACTS, TASK_ATTEMPT_CONTRACTS
from assurance_quality.derived import derive_report_input
from assurance_quality.graphs.factory import build_quality_graphs as _build_quality_graphs
from assurance_quality.ops.report.hooks import seal_report
from assurance_quality.ops.report import finalize as report_finalize, prepare as report_prepare
from tests.product.test_change_local_output_routing import dual_roots, execute_task

from graph_engine.testing.feature_bundle import compile_bundle


def build_quality_graphs(*args, **kwargs):
    return compile_bundle(_build_quality_graphs(*args, **kwargs))


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
    results = "qa/results"
    assessment_base = f"{results}/inspect/epochs/0/batches/{batch_id}"
    plan_ref = _ref(f"{results}/plan/{_DIGEST}/resolved-assurance-plan.json")
    reviewed = {
        "change_id": _CHANGE,
        "coverage_epoch": 0,
        "plan_digest": _DIGEST,
        "plan_ref": plan_ref,
        "preparation_refs": sorted(
            [plan_ref, _ref(f"{results}/preparation/context.json")],
            key=lambda item: (item["path"], item["digest"]),
        ),
        "case_refs": [_ref("qa/cases/system/case.yaml")],
        "review_ref": _ref(f"{results}/review/case-review.json"),
        "selection_ref": _ref("qa/results/cases/epochs/0/selection.json"),
    }
    mapping_ref = _ref(f"{results}/generation/epochs/0/mapping.json")
    execution_ref = _ref(f"{results}/execution/epochs/0/batches/{batch_id}/result.json")
    assessment = {
        "change_id": _CHANGE,
        "coverage_epoch": 0,
        "batch_id": batch_id,
        "plan_digest": _DIGEST,
        "plan_ref": plan_ref,
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
        "observations_ref": _ref(f"{assessment_base}/observations.json"),
        "obligation_assessment_ref": _ref(f"{assessment_base}/obligation-assessment.json"),
        "obligation_gate_facts": {
            "required_count": 1,
            "supported_count": 1,
            "refuted_count": 0,
            "inconclusive_count": 0,
            "repairable_gap_count": 0,
            "human_gap_count": 0,
        },
        "issue_evidence_manifest_ref": _ref(f"{assessment_base}/issue-evidence-manifest.json"),
        "owned_evidence_ids": ["OBS-REPORT-1"],
        "evidence_bundle_digest": f"sha256:{_DIGEST}",
        "healing_ref": None,
        "issue_ref": None,
    }
    fact_ref = _ref(f"{results}/facts/fact-baseline.json")
    assessment_refs = sorted(
        [
            assessment["trace_ref"],
            assessment["gaps_ref"],
            assessment["metrics_ref"],
            assessment["sufficiency_ref"],
            assessment["observations_ref"],
            assessment["obligation_assessment_ref"],
            assessment["issue_evidence_manifest_ref"],
            execution_ref,
            fact_ref,
        ],
        key=lambda item: (item["path"], item["digest"]),
    )
    inspection = {
        "change_id": _CHANGE,
        "coverage_epoch": 0,
        "batch_id": batch_id,
        "plan_digest": _DIGEST,
        "plan_ref": plan_ref,
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
        "allowed_artifact_paths": [results],
        "assessment_inputs": assessment,
        "fact_baseline_ref": fact_ref,
        "reviewed_case": reviewed,
        "generation_result": {
            "change_id": _CHANGE,
            "coverage_epoch": 0,
            "plan_digest": _DIGEST,
            "plan_ref": plan_ref,
            "reviewed_case": reviewed,
            "mapping_ref": mapping_ref,
            "source_refs": [_ref("qa/tests/test_orders.py")],
            "plan_refs": [_ref(f"{results}/generation/epochs/0/api/plan.json")],
            "method_plan_ref": _ref(f"{results}/generation/epochs/0/obligation-methods.json"),
        },
        "inspection_outcome": inspection,
        "coverage_state": "satisfied",
        "report_refs": [_ref(f"{results}/report/stale.md")],
        "report_receipt": _receipt("stale-report").model_dump(mode="json"),
        "execution_digest": _DIGEST,
        "healing_digest": None,
        "trace_digest": _DIGEST,
        "coverage_digest": _DIGEST,
        "metrics_digest": _DIGEST,
        "case_digest": _DIGEST,
        "plan_digest": _DIGEST,
        "plan_ref": plan_ref,
        "mapping_digest": _DIGEST,
        "issue_digest": None,
    }


def _skill_from_state(state: dict[str, object]) -> ReportSkillInputV1:
    payload = dict(state)
    if payload.get("issue_digest") is None:
        payload.pop("issue_digest", None)
    filled = derive_report_input(payload)
    return ReportSkillInputV1.model_validate(
        {
            "change_id": filled.get("change_id"),
            "batch_id": filled.get("batch_id"),
            "capability_leafs": filled.get("capability_leafs", ()),
            "artifact_paths": filled.get("artifact_paths") or filled.get("allowed_artifact_paths", ()),
            "coverage_epoch": filled.get("coverage_epoch"),
            "purpose": filled.get("report_purpose", state.get("report_purpose", "normal")),
            "inspection": filled.get("inspection_outcome"),
            "assessment": filled.get("assessment_inputs"),
            "generation": filled.get("generation_result"),
            "fact_baseline_ref": filled.get("fact_baseline_ref"),
            "issue_analysis_ref": filled.get("issue_analysis_ref"),
            "execution_digest": filled.get("execution_digest") or filled.get("execution_evidence_digest"),
            "healing_digest": filled.get("healing_digest"),
            "trace_digest": filled.get("trace_digest"),
            "coverage_digest": filled.get("coverage_digest"),
            "metrics_digest": filled.get("metrics_digest"),
            "case_digest": filled.get("case_digest"),
            "plan_digest": filled.get("plan_digest"),
            "plan_ref": filled.get("plan_ref"),
            "mapping_digest": filled.get("mapping_digest"),
            "issue_digest": filled.get("issue_digest"),
        }
    )


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
        result["report_files"] = ["qa/results/report/report.md"]
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


def _bound_report(project: Path, selected: ReportSkillInputV1) -> dict[str, object]:
    """Stage the producer files and return the ref input prepare validates."""

    from graph_engine.artifacts import stage_json_artifact

    from assurance_execution.contracts.workflow import EXECUTION_CYCLE_PATH, ExecutionCycleDocumentV1
    from assurance_generation.contracts.workflow import GENERATION_CYCLE_PATH
    from assurance_quality.contracts.assessment import (
        ASSESSMENT_INPUTS_PATH,
        INSPECTION_OUTCOME_PATH,
        InspectionDocumentV1,
        ReportBoundInputV1,
    )

    raw = selected.inspection.model_dump(mode="json")
    raw.pop("inspection_receipt", None)
    inspection = stage_json_artifact(
        project, INSPECTION_OUTCOME_PATH, InspectionDocumentV1.model_validate(raw)
    )
    assessment = stage_json_artifact(project, ASSESSMENT_INPUTS_PATH, selected.assessment)
    generation = stage_json_artifact(project, GENERATION_CYCLE_PATH, selected.generation)
    execution = stage_json_artifact(
        project,
        EXECUTION_CYCLE_PATH,
        ExecutionCycleDocumentV1.model_validate(
            {
                "change_id": selected.change_id,
                "plan_digest": selected.plan_digest,
                "plan_ref": selected.plan_ref.model_dump(mode="json"),
                "coverage_epoch": selected.coverage_epoch,
                "repair_round": 0,
                "batch_id": selected.batch_id,
                "executed_at": "2026-08-22T00:00:00Z",
                "final_status": "PASS",
                "evidence_ref": selected.assessment.execution_ref.model_dump(mode="json"),
                "mapping_ref": selected.generation.mapping_ref.model_dump(mode="json"),
                "source_refs": [item.model_dump(mode="json") for item in selected.generation.source_refs],
                "family_outcomes": [{"family": "api", "state": "executed"}],
            }
        ),
    )

    def _plain(value: object) -> object:
        dump = getattr(value, "model_dump", None)
        return dump(mode="json") if callable(dump) else value

    return ReportBoundInputV1.model_validate(
        {
            "change_id": selected.change_id,
            "coverage_epoch": selected.coverage_epoch,
            "purpose": selected.purpose,
            "capability_leafs": list(selected.capability_leafs),
            "fact_baseline_ref": _plain(selected.fact_baseline_ref),
            "issue_analysis_ref": _plain(selected.issue_analysis_ref),
            "artifact_paths": list(selected.artifact_paths),
            "inspection_ref": {"path": inspection.path, "digest": inspection.digest},
            "assessment_ref": {"path": assessment.path, "digest": assessment.digest},
            "generation_ref": {"path": generation.path, "digest": generation.digest},
            "execution_ref": {"path": execution.path, "digest": execution.digest},
            "execution_receipt": {"receipt_id": "execute", "receipt_digest": _DIGEST},
            "inspection_receipt": _plain(selected.inspection.inspection_receipt),
        }
    ).model_dump(mode="json")


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
    if selected.issue_analysis_ref is not None:
        path = project / selected.issue_analysis_ref.path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_BYTES)
    policy = project / ".aa/policy.yaml"
    policy.parent.mkdir(parents=True, exist_ok=True)
    policy.write_bytes(_BYTES)


def test_report_input_does_not_accept_a_coverage_flag_or_stale_refs_alone() -> None:
    state = _state()
    state.pop("inspection_outcome")
    with pytest.raises(ValueError, match="inspection_outcome"):
        derive_report_input(state)


def test_report_prepare_binds_current_satisfied_inspection_chain() -> None:
    filled = derive_report_input(_state())
    selected = _skill_from_state(_state())
    assert filled["coverage_epoch"] == 0
    assert selected.inspection.disposition == "satisfied"
    assert selected.inspection.batch_id == selected.batch_id


def test_diagnostic_report_binds_current_issue_analysis() -> None:
    state = _state()
    inspection = cast(dict[str, object], state["inspection_outcome"])
    inspection["disposition"] = "blocked"
    inspection["coverage_state"] = None
    state["report_purpose"] = "diagnostic"
    issue_ref = _ref("qa/results/inspect/issue-analysis.json")
    state["issue_analysis_ref"] = issue_ref
    state.pop("issue_digest", None)

    filled = derive_report_input(state)
    selected = _skill_from_state(state)

    assert filled["issue_digest"] == issue_ref["digest"]
    assert selected.issue_analysis_ref == EvidenceArtifactRefV1.model_validate(issue_ref)
    assert selected.issue_digest == issue_ref["digest"]


def test_diagnostic_report_rejects_missing_issue_analysis() -> None:
    state = _state()
    inspection = cast(dict[str, object], state["inspection_outcome"])
    inspection["disposition"] = "blocked"
    inspection["coverage_state"] = None
    state["report_purpose"] = "diagnostic"

    with pytest.raises(ValidationError, match="issue analysis"):
        _skill_from_state(state)


def test_report_input_rejects_an_inspection_from_a_previous_batch() -> None:
    state = _state()
    state["batch_id"] = "batch-2"
    with pytest.raises(ValidationError, match="current inspection"):
        _skill_from_state(state)


@pytest.mark.asyncio
async def test_report_prepare_authenticates_the_current_inspection_chain(tmp_path: Path) -> None:
    project, write_root = dual_roots(tmp_path, _CHANGE)
    selected = cast(ReportSkillInputV1, _skill_from_state(_state()))
    _write_authenticated_inputs(project, selected)
    prepared = await execute_task(
        cast(TaskHandler, report_prepare),
        cast(JSONValue, _bound_report(project, selected)),
        project,
        write_root=write_root,
        binding_data=_BINDING,
    )
    assert prepared.outcome.status == "succeeded", prepared.outcome.failure


@pytest.mark.asyncio
async def test_report_finalizer_rejects_wrapped_input(tmp_path: Path) -> None:
    selected = cast(ReportSkillInputV1, _skill_from_state(_state()))
    result = await execute_task(
        cast(TaskHandler, report_finalize),
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
    selected = cast(ReportSkillInputV1, _skill_from_state(_state()))
    _write_authenticated_inputs(project, selected)
    bound = _bound_report(project, selected)
    missing_refs = await execute_task(
        cast(TaskHandler, report_finalize),
        cast(
            JSONValue,
            {
                **bound,
                "agent_result": _agent_result(_raw_report(selected, include_files=False)),
            },
        ),
        project,
        write_root=write_root,
    )
    assert missing_refs.outcome.failure is not None
    assert missing_refs.outcome.failure.kind == "invalid_output"

    declared_without_bytes = await execute_task(
        cast(TaskHandler, report_finalize),
        cast(
            JSONValue,
            {
                **bound,
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
    selected = cast(ReportSkillInputV1, _skill_from_state(state))
    _write_authenticated_inputs(project, selected)
    bound = _bound_report(project, selected)
    report_path = "qa/results/report/report.md"
    staged = write_root / report_path
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_bytes(b"# Current report\n")
    finalized_run = await execute_task(
        cast(TaskHandler, report_finalize),
        cast(
            JSONValue,
            {
                **bound,
                "agent_result": _agent_result(_raw_report(selected)),
            },
        ),
        project,
        write_root=write_root,
    )
    assert finalized_run.outcome.status == "succeeded", finalized_run.outcome.failure
    published = ReportPublishedV1.model_validate(finalized_run.outcome.output)
    finalized = published.finalized
    assert finalized is not None
    assert finalized.report_refs[0].digest == hashlib.sha256(staged.read_bytes()).hexdigest()
    assert published.publication == "reported"
    assert published.report_outcome is not None
    assert "report_receipt" not in published.report_outcome


@pytest.mark.asyncio
async def test_failed_report_attempt_clears_stale_report_state(tmp_path: Path) -> None:
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
        input=_bound_report(tmp_path, cast(ReportSkillInputV1, _skill_from_state(_state()))),
        script={"quality.report": [PermanentTaskFailure(kind="invalid_output", message="missing report")]},
    )
    terminal = cast(dict[str, object], result.terminal)
    assert terminal["status"] == "failed"
    assert "report_outcome" not in terminal


@pytest.mark.asyncio
async def test_report_graph_publishes_only_the_current_committed_outcome(tmp_path: Path) -> None:
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
    ref = EvidenceArtifactRefV1.model_validate(_ref("qa/results/report/report.md"))
    finalized = FinalizedReportV1(
        change_id=_CHANGE,
        coverage_epoch=0,
        plan_digest=_DIGEST,
        plan_ref=inspection.plan_ref,
        batch_id=_BATCH,
        purpose="normal",
        inspection_receipt=inspection.inspection_receipt,
        report_refs=(ref,),
    )
    selected = cast(ReportSkillInputV1, _skill_from_state(state))
    receipt = _receipt("report-current")
    sealed = seal_report(selected, finalized)
    result = await harness.run(
        graph,
        input=_bound_report(tmp_path, selected),
        script={"quality.report": [committed(sealed, receipt)]},
    )
    published = result.published_update
    assert published is not None
    assert cast(dict[str, object], result.terminal)["status"] == "reported"
    assert "report_outcome" not in published
    assert sealed.report_outcome is not None
    outcome = ReportOutcomeV1.model_validate(
        {
            **sealed.report_outcome,
            "report_receipt": receipt.model_dump(mode="json"),
        }
    )
    assert outcome.batch_id == inspection.batch_id
    assert outcome.inspection_receipt == inspection.inspection_receipt
    assert outcome.report_receipt == receipt


def test_diagnostic_report_cannot_publish_a_normal_success_outcome() -> None:
    state = _state()
    inspection = InspectionOutcomeV1.model_validate(state["inspection_outcome"]).model_copy(
        update={"disposition": "blocked", "coverage_state": None}
    )
    state["inspection_outcome"] = inspection.model_dump(mode="json")
    state["coverage_state"] = None
    state["report_purpose"] = "diagnostic"
    state["issue_analysis_ref"] = _ref("qa/results/inspect/issue-analysis.json")
    selected = cast(ReportSkillInputV1, _skill_from_state(state))
    ref = EvidenceArtifactRefV1.model_validate(_ref("qa/results/report/report.md"))
    finalized = FinalizedReportV1(
        change_id=_CHANGE,
        coverage_epoch=0,
        plan_digest=_DIGEST,
        plan_ref=inspection.plan_ref,
        batch_id=_BATCH,
        purpose="diagnostic",
        inspection_receipt=inspection.inspection_receipt,
        report_refs=(ref,),
    )
    published = seal_report(selected, finalized)
    assert selected.purpose == "diagnostic"
    assert published.publication == "diagnostic"
    assert published.report_outcome is None


def test_report_input_rejects_a_previous_batch() -> None:
    selected = cast(ReportSkillInputV1, _skill_from_state(_state()))
    payload = selected.model_dump(mode="json")
    payload["batch_id"] = "previous-batch"
    with pytest.raises(ValidationError, match="current inspection"):
        ReportSkillInputV1.model_validate(payload)


def test_normal_report_requires_a_satisfied_inspection() -> None:
    selected = cast(ReportSkillInputV1, _skill_from_state(_state()))
    payload = selected.model_dump(mode="json")
    payload["inspection"]["disposition"] = "blocked"
    payload["inspection"]["coverage_state"] = None
    with pytest.raises(ValidationError, match="satisfied inspection"):
        ReportSkillInputV1.model_validate(payload)


def test_quality_gate_rejects_a_previous_inspection_receipt() -> None:
    from assurance_product.models import QualityGateRefV1

    selected = cast(ReportSkillInputV1, _skill_from_state(_state()))
    inspection = selected.inspection
    ref = EvidenceArtifactRefV1.model_validate(_ref("qa/results/report/report.md"))
    outcome = ReportOutcomeV1(
        change_id=inspection.change_id,
        coverage_epoch=inspection.coverage_epoch,
        batch_id=inspection.batch_id,
        inspection_receipt=inspection.inspection_receipt,
        plan_digest=inspection.plan_digest,
        plan_ref=inspection.plan_ref,
        report_refs=(ref,),
        report_receipt=_receipt("report-current"),
    )
    QualityGateRefV1.model_validate(
        {"inspection": inspection.model_dump(mode="json"), "report": outcome.model_dump(mode="json")}
    )
    stale = outcome.model_copy(update={"inspection_receipt": _receipt("previous-inspect")})
    with pytest.raises(ValidationError, match="current inspection"):
        QualityGateRefV1.model_validate(
            {"inspection": inspection.model_dump(mode="json"), "report": stale.model_dump(mode="json")}
        )
