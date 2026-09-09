from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from typing import cast

import pytest
from agent_runtime_contracts import AgentRunResult
from pydantic import ValidationError

from graph_engine.canonical import JSONValue, canonical_digest, canonical_json_bytes
from assurance_quality.contracts.assessment import (
    AssessmentSkillInputV1,
    FinalizedFactBaselineV1,
    FinalizedInspectionV1,
    MaterializeAssessmentInputV1,
)
from assurance_quality.contracts.agent import InspectionResultV1
from assurance_quality.contracts.assessment import FailureClassificationFactsV1
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1
from assurance_quality.graphs.nodes import publish_inspect
from assurance_quality.graphs.routes import route_coverage
from assurance_quality.operations.agent_skills import (
    FactBaselineFinalizeHandler,
    FactBaselinePrepareHandler,
    InspectFinalizeHandler,
)
from assurance_quality.operations.assessment import (
    classify_inspection_disposition,
    materialize_assessment_inputs,
)
from assurance_quality.contracts.metrics import MetricEntry, MetricScope, MetricsDocument
from assurance_quality.contracts.sufficiency import TraceInsufficientCase, TraceSufficiencyFacts
from assurance_execution.contracts.evidence import ExecutionEvidenceV1
from assurance_quality.operations.inspect import build_failure_classification_facts
from test_agent_skills import BINDING  # pyright: ignore[reportMissingImports]
from test_assessment_materialization import (  # pyright: ignore[reportMissingImports]
    BATCH_ID as MATERIALIZED_BATCH_ID,
    CAPABILITY,
    CHANGE_ID as MATERIALIZED_CHANGE_ID,
    EVIDENCE_PATH,
    _case,
    _write_json,
    _workspace_input,
)
from tests.product.test_change_local_output_routing import execute_task
from test_quality_graph_factory import (  # pyright: ignore[reportMissingImports]
    _assessment_output,
    _inspect_output,
    _receipt,
    assess_graph_input,
)


def _publish_state() -> dict[str, object]:
    state = assess_graph_input()
    state["assessment_inputs"] = _assessment_output()
    state["fact_baseline_ref"] = {
        "path": "qa/changes/CH-DEMO-001/facts/fact-baseline.json",
        "digest": "a" * 64,
    }
    return state


def _agent_run(result: object) -> dict[str, object]:
    payload = cast(JSONValue, result)
    return AgentRunResult(
        result_payload=payload,
        result_digest=canonical_digest(payload),
        evidence_digest="f" * 64,
        adapter_id="test.fake",
        adapter_version="1.0.0",
    ).model_dump(mode="json")


def _assessment_business(root: Path) -> AssessmentSkillInputV1:
    request = MaterializeAssessmentInputV1.model_validate(_workspace_input(root))
    assessment = materialize_assessment_inputs(request, project_root=root, write_root=root)
    return AssessmentSkillInputV1(
        change_id=MATERIALIZED_CHANGE_ID,
        coverage_epoch=request.reviewed_case.coverage_epoch,
        batch_id=MATERIALIZED_BATCH_ID,
        plan_digest=request.plan_digest,
        plan_ref=request.plan_ref,
        capability_leafs=("entities.item.constraints.description", "entities.item.constraints.name"),
        artifact_paths=(),
        assessment=assessment,
        reviewed_case=request.reviewed_case,
        mapping_ref=request.generation.mapping_ref,
    )


def _write_stage(stage: Path, relative: str, document: object) -> None:
    path = stage / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json_bytes(cast(JSONValue, document)))


async def _finalize_inspection(
    root: Path,
    business: AssessmentSkillInputV1,
    *,
    status: str,
    classification_performed: bool = True,
):
    baseline_document = {"source": "unavailable", "change_id": MATERIALIZED_CHANGE_ID}
    baseline_path = f"qa/changes/{MATERIALIZED_CHANGE_ID}/facts/fact-baseline.json"
    baseline_ref = EvidenceArtifactRefV1.model_validate(_write_json(root, baseline_path, baseline_document))
    inspect_business = business.model_copy(update={"fact_baseline_ref": baseline_ref})
    assessment = business.assessment
    inspection_document = {
        "schema_version": "1.0",
        "change_id": MATERIALIZED_CHANGE_ID,
        "batch_id": MATERIALIZED_BATCH_ID,
        "inspect_mode": "primary",
        "classification_performed": classification_performed,
        "status": status,
        "execution_digest": assessment.execution_ref.digest,
        "healing_digest": None,
        "trace_digest": assessment.trace_ref.digest,
        "coverage_digest": assessment.gaps_ref.digest,
        "metrics_digest": assessment.metrics_ref.digest,
    }
    stage = root / ".stage"
    _write_stage(
        stage,
        f"qa/changes/{MATERIALIZED_CHANGE_ID}/inspect/inspection.json",
        inspection_document,
    )
    return await execute_task(
        InspectFinalizeHandler(),
        cast(
            JSONValue,
            {
                **inspect_business.model_dump(mode="json"),
                "agent_result": _agent_run(inspection_document),
            },
        ),
        root,
        write_root=stage,
    )


def test_repairable_failure_precedes_a_coverage_shortfall() -> None:
    facts = FailureClassificationFactsV1(
        identity_valid=True,
        blocking_failure=False,
        analysis_required=False,
        needs_human=False,
        repairable_failure=True,
    )
    assert (
        classify_inspection_disposition(facts=facts, coverage_state="repair_required")
        == "repairable_execution_failure"
    )


def test_agent_result_has_no_business_route_field() -> None:
    result = _inspect_output()["agent_result"]
    assert isinstance(result, dict)
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        InspectionResultV1.model_validate({**result, "route": "satisfied"})


def test_publish_inspect_derives_satisfied_without_an_agent_coverage_state() -> None:
    published = publish_inspect(_publish_state(), _inspect_output(), _receipt())
    assert published["coverage_state"] == "satisfied"
    outcome = published["inspection_outcome"]
    assert isinstance(outcome, dict)
    assert outcome["disposition"] == "satisfied"
    assert route_coverage(published) == "satisfied"


def test_publish_inspect_routes_coverage_shortfall_to_case_rework() -> None:
    output = _inspect_output()
    metrics = MetricsDocument.model_validate(output["metrics"])
    metric_map = dict(metrics.metrics)
    scope = MetricScope.of(total=1, covered=0, uncovered=("constraint.missing",))
    metric_map["constraint_coverage"] = MetricEntry(
        layer="api",
        status="evaluated",
        value=0.0,
        declared=scope,
        evidence="trace",
    )
    output["metrics"] = metrics.model_copy(update={"metrics": metric_map, "floor_ratio": 0.0}).model_dump(
        mode="json"
    )
    sufficiency = TraceSufficiencyFacts.model_validate(output["sufficiency"])
    output["sufficiency"] = sufficiency.model_copy(
        update={
            "sufficient": False,
            "insufficient_cases": (TraceInsufficientCase(case_id="TC_ITEM_001", reason_codes=("no_pass",)),),
        }
    ).model_dump(mode="json")

    published = publish_inspect(_publish_state(), output, _receipt())

    outcome = published["inspection_outcome"]
    assert isinstance(outcome, dict)
    assert outcome["coverage_state"] == "repair_required"
    assert outcome["disposition"] == "coverage_insufficient"
    assert route_coverage(published) == "coverage_insufficient"


@pytest.mark.parametrize("stale_field", ["coverage_epoch", "batch_id", "policy_sha256"])
def test_stale_cycle_identity_cannot_publish_a_normal_result(stale_field: str) -> None:
    state = _publish_state()
    state[stale_field] = {
        "coverage_epoch": 1,
        "batch_id": "20260821T000000Z",
        "policy_sha256": "b" * 64,
    }[stale_field]
    with pytest.raises(ValueError, match="identity"):
        publish_inspect(state, _inspect_output(), _receipt())


def test_failed_attempt_cannot_publish_a_normal_result() -> None:
    state = _publish_state()
    state["attempt_failure"] = {"kind": "invalid_output", "message": "agent failed"}
    with pytest.raises(ValueError, match="failed Inspect attempt"):
        publish_inspect(state, _inspect_output(), _receipt())


def test_stale_reviewed_case_cannot_publish_a_normal_result() -> None:
    state = _publish_state()
    reviewed = deepcopy(state["reviewed_case"])
    assert isinstance(reviewed, dict)
    reviewed["review_ref"] = {
        "path": "qa/changes/CH-DEMO-001/review/case-review.json",
        "digest": "b" * 64,
    }
    state["reviewed_case"] = reviewed
    with pytest.raises(ValueError, match="stale Reviewed Case"):
        publish_inspect(state, _inspect_output(), _receipt())


def test_stale_mapping_cannot_publish_a_normal_result() -> None:
    state = _publish_state()
    generation = deepcopy(state["generation_result"])
    assert isinstance(generation, dict)
    generation["mapping_ref"] = {
        "path": "qa/changes/CH-DEMO-001/codegen/closed-mapping.json",
        "digest": "b" * 64,
    }
    state["generation_result"] = generation
    with pytest.raises(ValueError, match="stale test mapping"):
        publish_inspect(state, _inspect_output(), _receipt())


def test_mixed_execution_failure_and_coverage_gap_selects_one_execution_action() -> None:
    output = _inspect_output()
    facts = output["failure_facts"]
    assert isinstance(facts, dict)
    facts["repairable_failure"] = True
    output["reason_codes"] = ["execution.locator_failure"]
    published = publish_inspect(_publish_state(), output, _receipt())
    outcome = published["inspection_outcome"]
    assert isinstance(outcome, dict)
    assert outcome["disposition"] == "repairable_execution_failure"
    assert outcome["coverage_state"] is None
    assert route_coverage(published) == "repairable_execution_failure"


def test_blocking_failure_precedes_repairable_failure() -> None:
    facts = FailureClassificationFactsV1(
        identity_valid=True,
        blocking_failure=True,
        analysis_required=False,
        needs_human=False,
        repairable_failure=True,
    )
    assert classify_inspection_disposition(facts=facts, coverage_state="repair_required") == "blocked"


def test_adversarial_counterexample_is_a_blocking_failure(tmp_path: Path) -> None:
    business = _assessment_business(tmp_path)
    execution = ExecutionEvidenceV1.model_validate_json(
        (tmp_path / business.assessment.execution_ref.path).read_bytes()
    )
    metrics_data = json.loads((tmp_path / business.assessment.metrics_ref.path).read_bytes())
    metrics_data["metrics"]["adversarial_clean"] = {
        "layer": "cross",
        "status": "evaluated",
        "holds": False,
        "evidence": "adversarial-run",
    }
    metrics = MetricsDocument.model_validate(metrics_data)

    facts, reasons = build_failure_classification_facts(
        execution,
        metrics,
    )

    assert facts.blocking_failure is True
    assert "adversarial.open_counterexample" in reasons


@pytest.mark.parametrize("family", ["api", "e2e", "fuzz", "performance"])
@pytest.mark.parametrize(
    "message",
    [
        "AssertionError: assert 200 == 422",
        "AssertionError: Locator expected to have text 'Saved'",
        "assert 200 == 422",
        'Error: expect(locator).toBeVisible() failed\nLocator: getByRole("button")\nExpected: visible\nReceived: hidden',
        "Error: expect(locator).toHaveCount(expected) failed\nExpected: 1\nReceived: 0",
    ],
)
def test_assertion_failure_requires_analysis_not_human_or_coverage_repair(
    tmp_path: Path,
    family: str,
    message: str,
) -> None:
    business = _assessment_business(tmp_path)
    execution = ExecutionEvidenceV1.model_validate_json(
        (tmp_path / business.assessment.execution_ref.path).read_bytes()
    )
    execution = execution.model_copy(
        update={
            "results": [execution.results[0].model_copy(update={"status": "failed", "message": message})],
            "mapping": execution.mapping.model_copy(
                update={
                    "mappings": [
                        item.model_copy(update={"layer": family}) for item in execution.mapping.mappings
                    ]
                }
            ),
        }
    )
    metrics = MetricsDocument.model_validate_json(
        (tmp_path / business.assessment.metrics_ref.path).read_bytes()
    )
    facts, reasons = build_failure_classification_facts(execution, metrics)
    assert facts.needs_human is False
    assert reasons == ("execution.assertion_failure",)
    assert (
        classify_inspection_disposition(facts=facts, coverage_state="repair_required") == "analysis_required"
    )


@pytest.mark.parametrize(("identity_valid", "expected"), [(True, "analysis_required"), (False, "blocked")])
def test_assertion_batch_analysis_precedes_heuristic_blocking_but_not_invalid_identity(
    identity_valid: bool,
    expected: str,
) -> None:
    facts = FailureClassificationFactsV1(
        identity_valid=identity_valid,
        blocking_failure=True,
        analysis_required=True,
        needs_human=False,
        repairable_failure=False,
    )
    assert classify_inspection_disposition(facts=facts, coverage_state=None) == expected


@pytest.mark.asyncio
async def test_selected_fuzz_without_campaign_evidence_remains_not_evaluated(
    tmp_path: Path,
) -> None:
    business = _assessment_business(tmp_path)
    assessment = business.assessment.model_copy(
        update={"scope": business.assessment.scope.model_copy(update={"selected_families": ("api", "fuzz")})}
    )
    business = business.model_copy(update={"assessment": assessment})

    outcome = await _finalize_inspection(tmp_path, business, status="no_failures")

    assert outcome.status == "succeeded", outcome.failure
    finalized = FinalizedInspectionV1.model_validate(outcome.output)
    assert finalized.failure_facts.identity_valid is True
    assert "adversarial.required_evidence_missing" not in finalized.reason_codes


@pytest.mark.asyncio
async def test_no_failure_execution_rejects_analyzed_agent_status(tmp_path: Path) -> None:
    outcome = await _finalize_inspection(
        tmp_path,
        _assessment_business(tmp_path),
        status="analyzed",
    )

    assert outcome.status == "failed"
    assert outcome.failure is not None
    assert outcome.failure.kind == "invalid_output"


@pytest.mark.asyncio
async def test_prepare_rejects_assessment_evidence_that_changed_after_materialization(
    tmp_path: Path,
) -> None:
    business = _assessment_business(tmp_path)
    metrics = tmp_path / business.assessment.metrics_ref.path
    metrics.write_bytes(metrics.read_bytes() + b"\n")

    result = await execute_task(
        FactBaselinePrepareHandler(),
        cast(JSONValue, business.model_dump(mode="json")),
        tmp_path,
        binding_data=BINDING,
    )

    assert result.failure is not None
    assert result.failure.kind == "invalid_input"
    assert "digest changed" in result.failure.message


@pytest.mark.asyncio
@pytest.mark.parametrize("handler_type", [FactBaselineFinalizeHandler, InspectFinalizeHandler])
async def test_assessment_finalizer_rejects_wrapped_input(tmp_path: Path, handler_type) -> None:
    business = _assessment_business(tmp_path).model_dump(mode="json")
    result = await execute_task(
        handler_type(),
        cast(
            JSONValue,
            {
                "validated_input": business,
                "prepared": business,
                "agent_result": _agent_run({"source": "unavailable", "change_id": MATERIALIZED_CHANGE_ID}),
            },
        ),
        tmp_path,
    )
    assert result.failure is not None
    assert result.failure.kind == "invalid_input"


@pytest.mark.asyncio
async def test_finalize_authenticates_baseline_and_builds_deterministic_inspection(
    tmp_path: Path,
) -> None:
    business = _assessment_business(tmp_path)
    prepared = await execute_task(
        FactBaselinePrepareHandler(),
        cast(JSONValue, business.model_dump(mode="json")),
        tmp_path,
        binding_data=BINDING,
    )
    assert prepared.status == "succeeded", prepared.failure

    stage = tmp_path / ".stage"
    baseline_document = {"source": "unavailable", "change_id": MATERIALIZED_CHANGE_ID}
    baseline_path = f"qa/changes/{MATERIALIZED_CHANGE_ID}/facts/fact-baseline.json"
    _write_stage(stage, baseline_path, baseline_document)
    baseline_result = await execute_task(
        FactBaselineFinalizeHandler(),
        cast(
            JSONValue,
            {**business.model_dump(mode="json"), "agent_result": _agent_run(baseline_document)},
        ),
        tmp_path,
        write_root=stage,
    )
    assert baseline_result.status == "succeeded", baseline_result.failure
    finalized_baseline = FinalizedFactBaselineV1.model_validate(baseline_result.output)

    committed_baseline = tmp_path / baseline_path
    committed_baseline.parent.mkdir(parents=True, exist_ok=True)
    committed_baseline.write_bytes((stage / baseline_path).read_bytes())
    inspect_business = business.model_copy(update={"fact_baseline_ref": finalized_baseline.fact_baseline_ref})
    assessment = business.assessment
    inspection_document = {
        "schema_version": "1.0",
        "change_id": MATERIALIZED_CHANGE_ID,
        "batch_id": MATERIALIZED_BATCH_ID,
        "inspect_mode": "primary",
        "classification_performed": True,
        "status": "no_failures",
        "execution_digest": assessment.execution_ref.digest,
        "healing_digest": None,
        "trace_digest": assessment.trace_ref.digest,
        "coverage_digest": assessment.gaps_ref.digest,
        "metrics_digest": assessment.metrics_ref.digest,
    }
    inspection_path = f"qa/changes/{MATERIALIZED_CHANGE_ID}/inspect/inspection.json"
    _write_stage(stage, inspection_path, inspection_document)
    inspection_result = await execute_task(
        InspectFinalizeHandler(),
        cast(
            JSONValue,
            {
                **inspect_business.model_dump(mode="json"),
                "agent_result": _agent_run(inspection_document),
            },
        ),
        tmp_path,
        write_root=stage,
    )

    assert inspection_result.status == "succeeded", inspection_result.failure
    finalized = FinalizedInspectionV1.model_validate(inspection_result.output)
    assert finalized.failure_facts.identity_valid is True
    assert finalized.failure_facts.blocking_failure is False
    assert finalized.failure_facts.repairable_failure is False
    assert finalized.reason_codes == ()


@pytest.mark.asyncio
async def test_failing_execution_requires_analyzed_agent_status(
    tmp_path: Path,
) -> None:
    request = _workspace_input(
        tmp_path,
        capability_leafs=(CAPABILITY,),
        minimum_required_coverage={"api": [CAPABILITY]},
        case_entries=[_case("TC_ITEM_001", CAPABILITY)],
        matrix_rows=[
            {
                "mrc_id": "MRC-API-001",
                "key": CAPABILITY,
                "category": "api",
                "required": True,
                "layer": "api",
                "covered_by_cases": ["TC_ITEM_001"],
                "status": "covered",
            }
        ],
    )
    evidence_path = tmp_path / EVIDENCE_PATH
    evidence = json.loads(evidence_path.read_bytes())
    evidence["status"] = "failed"
    evidence["results"][0].update(
        {
            "status": "failed",
            "message": "NameError: generated helper is not defined",
        }
    )
    evidence["receipt"]["commands"][0].update({"exit_code": 1, "passed": 0, "failed": 1})
    evidence_ref = _write_json(tmp_path, EVIDENCE_PATH, evidence)
    request["execution"]["final_status"] = "FAIL"
    request["execution"]["evidence_ref"] = evidence_ref

    materialized = MaterializeAssessmentInputV1.model_validate(request)
    assessment = materialize_assessment_inputs(
        materialized,
        project_root=tmp_path,
        write_root=tmp_path,
    )
    business = AssessmentSkillInputV1(
        change_id=MATERIALIZED_CHANGE_ID,
        coverage_epoch=materialized.reviewed_case.coverage_epoch,
        batch_id=MATERIALIZED_BATCH_ID,
        plan_digest=materialized.plan_digest,
        plan_ref=materialized.plan_ref,
        capability_leafs=(CAPABILITY,),
        artifact_paths=(),
        assessment=assessment,
        reviewed_case=materialized.reviewed_case,
        mapping_ref=materialized.generation.mapping_ref,
    )
    baseline_path = f"qa/changes/{MATERIALIZED_CHANGE_ID}/facts/fact-baseline.json"
    stage = tmp_path / ".stage"
    baseline_document = {"source": "unavailable", "change_id": MATERIALIZED_CHANGE_ID}
    _write_stage(stage, baseline_path, baseline_document)
    baseline_result = await execute_task(
        FactBaselineFinalizeHandler(),
        cast(
            JSONValue,
            {**business.model_dump(mode="json"), "agent_result": _agent_run(baseline_document)},
        ),
        tmp_path,
        write_root=stage,
    )
    assert baseline_result.status == "succeeded", baseline_result.failure
    finalized_baseline = FinalizedFactBaselineV1.model_validate(baseline_result.output)
    committed_baseline = tmp_path / baseline_path
    committed_baseline.parent.mkdir(parents=True, exist_ok=True)
    committed_baseline.write_bytes((stage / baseline_path).read_bytes())

    inspect_business = business.model_copy(update={"fact_baseline_ref": finalized_baseline.fact_baseline_ref})
    inspection_document = {
        "schema_version": "1.0",
        "change_id": MATERIALIZED_CHANGE_ID,
        "batch_id": MATERIALIZED_BATCH_ID,
        "inspect_mode": "primary",
        "classification_performed": True,
        "status": "analyzed",
        "execution_digest": assessment.execution_ref.digest,
        "healing_digest": None,
        "trace_digest": assessment.trace_ref.digest,
        "coverage_digest": assessment.gaps_ref.digest,
        "metrics_digest": assessment.metrics_ref.digest,
    }
    _write_stage(
        stage,
        f"qa/changes/{MATERIALIZED_CHANGE_ID}/inspect/inspection.json",
        inspection_document,
    )

    outcome = await execute_task(
        InspectFinalizeHandler(),
        cast(
            JSONValue,
            {
                **inspect_business.model_dump(mode="json"),
                "agent_result": _agent_run(inspection_document),
            },
        ),
        tmp_path,
        write_root=stage,
    )

    assert outcome.status == "succeeded", outcome.failure
    finalized = FinalizedInspectionV1.model_validate(outcome.output)
    assert finalized.failure_facts == FailureClassificationFactsV1(
        identity_valid=True,
        blocking_failure=False,
        analysis_required=False,
        needs_human=False,
        repairable_failure=True,
    )

    inspection_document["status"] = "failed"
    _write_stage(
        stage,
        f"qa/changes/{MATERIALIZED_CHANGE_ID}/inspect/inspection.json",
        inspection_document,
    )
    rejected = await execute_task(
        InspectFinalizeHandler(),
        cast(
            JSONValue,
            {
                **inspect_business.model_dump(mode="json"),
                "agent_result": _agent_run(inspection_document),
            },
        ),
        tmp_path,
        write_root=stage,
    )
    assert rejected.status == "failed"
    assert rejected.failure is not None
    assert rejected.failure.kind == "invalid_output"
