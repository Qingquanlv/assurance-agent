from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from typing import cast

import pytest
from agent_runtime_contracts import AgentRunResult
from pydantic import ValidationError

from graph_engine.canonical import JSONValue, canonical_digest, canonical_json_bytes
from graph_engine.plugin_api import TaskHandler
from assurance_quality.contracts.assessment import (
    AssessmentInputsV1,
    AssessmentSkillInputV1,
    FactBaselineBoundInputV1,
    FinalizedFactBaselineV1,
    FinalizedInspectionV1,
    InspectPublishedV1,
    MaterializeAssessmentInputV1,
)
from assurance_quality.contracts.agent import InspectionResultV1
from assurance_quality.contracts.assessment import FailureClassificationFactsV1
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1
from assurance_quality.ops.inspect.hooks import seal_inspection
from assurance_quality.ops.fact_baseline import (
    finalize as fact_baseline_finalize,
    prepare as fact_baseline_prepare,
)
from assurance_quality.ops.inspect import finalize as inspect_finalize, prepare as inspect_prepare
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
    assess_graph_input,
)


def _facts(business: AssessmentSkillInputV1) -> AssessmentInputsV1:
    assessment = business.assessment
    assert assessment is not None
    return assessment


def _seal(state: dict[str, object], output: object) -> dict[str, object]:
    from assurance_generation.contracts.workflow import GenerationCycleResultV1
    from assurance_intake.contracts.workflow import ReviewedCaseV1
    from assurance_quality.contracts.assessment import AssessmentInputsV1

    assessment = AssessmentInputsV1.model_validate(state.get("assessment_inputs"))
    reviewed = ReviewedCaseV1.model_validate(state.get("reviewed_case"))
    generation = GenerationCycleResultV1.model_validate(state.get("generation_result"))
    paths = state.get("artifact_paths") or state.get("allowed_artifact_paths") or ()
    business = AssessmentSkillInputV1.model_validate(
        {
            "change_id": state.get("change_id"),
            "coverage_epoch": state.get("coverage_epoch"),
            "repair_round": state.get("repair_round", 0),
            "batch_id": state.get("batch_id"),
            "plan_digest": state.get("plan_digest"),
            "plan_ref": state.get("plan_ref"),
            "capability_leafs": state.get("capability_leafs", ()),
            "artifact_paths": paths,
            "policy_sha256": state.get("policy_sha256"),
            "assessment": assessment.model_dump(mode="json"),
            "reviewed_case": reviewed.model_dump(mode="json"),
            "mapping_ref": generation.mapping_ref.model_dump(mode="json"),
            "fact_baseline_ref": state.get("fact_baseline_ref"),
        }
    )
    sealed = seal_inspection(business, FinalizedInspectionV1.model_validate(output))
    return sealed.model_dump(mode="json")


def _publish_state() -> dict[str, object]:
    state = assess_graph_input()
    state["assessment_inputs"] = _assessment_output()
    state["fact_baseline_ref"] = {
        "path": "qa/results/facts/fact-baseline.json",
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


def _inspect_bound(
    root: Path,
    business: AssessmentSkillInputV1,
    *,
    generation: object | None = None,
    execution: object | None = None,
) -> dict[str, object]:
    """Stage the producer files and return the ref input Inspect validates."""

    from graph_engine.artifacts import stage_json_artifact
    from graph_engine.attempts.models.resolutions import ReceiptRef

    from assurance_execution.contracts.workflow import (
        EXECUTION_CYCLE_PATH,
        ExecutionCycleDocumentV1,
    )
    from assurance_generation.contracts.workflow import GENERATION_CYCLE_PATH, GenerationCycleResultV1
    from assurance_quality.contracts.assessment import ASSESSMENT_INPUTS_PATH, InspectBoundInputV1

    def _plain(value: object) -> object:
        dump = getattr(value, "model_dump", None)
        return dump(mode="json") if callable(dump) else value

    if generation is None or execution is None:
        request = MaterializeAssessmentInputV1.model_validate(_workspace_input(root))
        generation = generation or request.generation
        execution = execution or request.execution
    execution_raw = _plain(execution)
    assert isinstance(execution_raw, dict)
    cycle = dict(execution_raw)
    receipt = cycle.pop("receipt")

    def stage(relative: str, document: object):
        path = root / relative
        if path.exists():
            path.unlink()
        return stage_json_artifact(root, relative, document)  # type: ignore[arg-type]

    reviewed = stage("qa/cases/reviewed-case.json", business.reviewed_case)
    generation_raw = _plain(generation)
    staged_generation = stage(GENERATION_CYCLE_PATH, GenerationCycleResultV1.model_validate(generation_raw))
    document = stage(EXECUTION_CYCLE_PATH, ExecutionCycleDocumentV1.model_validate(cycle))
    assessment = business.assessment
    assert assessment is not None
    staged_assessment = stage(ASSESSMENT_INPUTS_PATH, assessment)

    return InspectBoundInputV1.model_validate(
        {
            "change_id": business.change_id,
            "coverage_epoch": business.coverage_epoch,
            "plan_digest": business.plan_digest,
            "plan_ref": _plain(business.plan_ref),
            "capability_leafs": list(business.capability_leafs),
            "artifact_paths": list(business.artifact_paths),
            "product_policy": {
                "resource_id": "assurance.product.configuration.product-policy",
                "sha256": business.policy_sha256,
            },
            "assessment_ref": {"path": staged_assessment.path, "digest": staged_assessment.digest},
            "reviewed_case_ref": {"path": reviewed.path, "digest": reviewed.digest},
            "generation_ref": {"path": staged_generation.path, "digest": staged_generation.digest},
            "execution_ref": {"path": document.path, "digest": document.digest},
            "execution_receipt": _plain(ReceiptRef.model_validate(receipt)),
            "fact_baseline_ref": _plain(business.fact_baseline_ref),
            "repair_round": business.repair_round,
            "validation_error": business.validation_error,
        }
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
        artifact_paths=("qa/results",),
        assessment=assessment,
        reviewed_case=request.reviewed_case,
        mapping_ref=request.generation.mapping_ref,
        policy_sha256=assessment.scope.policy_digest,
        repair_round=0,
    )


def _fact_baseline_business(business: AssessmentSkillInputV1, root: Path) -> FactBaselineBoundInputV1:
    reviewed = _write_json(
        root, "qa/cases/reviewed-case.json", business.reviewed_case.model_dump(mode="json")
    )
    return FactBaselineBoundInputV1.model_validate(
        {
            "change_id": business.change_id,
            "coverage_epoch": business.coverage_epoch,
            "plan_digest": business.plan_digest,
            "plan_ref": business.plan_ref.model_dump(mode="json"),
            "capability_leafs": list(business.capability_leafs),
            "artifact_paths": list(business.artifact_paths),
            "reviewed_case_ref": reviewed,
        }
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
    baseline_path = "qa/results/facts/fact-baseline.json"
    baseline_ref = EvidenceArtifactRefV1.model_validate(_write_json(root, baseline_path, baseline_document))
    inspect_business = business.model_copy(update={"fact_baseline_ref": baseline_ref})
    assessment = _facts(business)
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
        "qa/results/inspect/inspection.json",
        inspection_document,
    )
    return await execute_task(
        cast(TaskHandler, inspect_finalize),
        cast(
            JSONValue,
            {
                **_inspect_bound(root, inspect_business),
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


@pytest.mark.parametrize(
    ("original", "expected"),
    [
        ("blocked", ("blocked", "blocked", "blocked", "blocked")),
        ("analysis_required", ("analysis_required",) * 4),
        ("needs_human", ("needs_human", "needs_human", "needs_human", "blocked")),
        (
            "repairable_execution_failure",
            ("repairable_execution_failure", "repairable_execution_failure", "needs_human", "blocked"),
        ),
        (
            "coverage_insufficient",
            ("coverage_insufficient", "coverage_insufficient", "needs_human", "blocked"),
        ),
        ("satisfied", ("satisfied", "coverage_insufficient", "needs_human", "blocked")),
    ],
)
def test_obligation_merge_preserves_stop_conditions(original, expected) -> None:
    from assurance_quality.contracts.decisions import merge_obligation_disposition

    decisions = ("satisfied", "repair_required", "needs_human", "blocked")
    assert tuple(merge_obligation_disposition(original, item) for item in decisions) == expected


def test_agent_result_has_no_business_route_field() -> None:
    result = _inspect_output()["agent_result"]
    assert isinstance(result, dict)
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        InspectionResultV1.model_validate({**result, "route": "satisfied"})


def test_publish_inspect_derives_satisfied_without_an_agent_coverage_state() -> None:
    published = _seal(_publish_state(), _inspect_output())
    assert published["coverage_state"] == "satisfied"
    outcome = published["inspection_outcome"]
    assert isinstance(outcome, dict)
    assert outcome["disposition"] == "satisfied"
    assert published["disposition"] == "satisfied"


@pytest.mark.parametrize("gate", ["satisfied", "needs_human", "blocked"])
def test_publish_inspect_routes_coverage_shortfall_to_case_rework(gate: str) -> None:
    output = _inspect_output()
    state = _publish_state()
    if gate != "satisfied":
        assessment = cast(dict, state["assessment_inputs"])
        assessment["obligation_gate_facts"] = {
            "required_count": 1,
            "supported_count": 0,
            "refuted_count": int(gate == "blocked"),
            "inconclusive_count": int(gate == "needs_human"),
            "repairable_gap_count": 0,
            "human_gap_count": int(gate == "needs_human"),
        }
        output["assessment"] = assessment
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

    published = _seal(state, output)

    outcome = published["inspection_outcome"]
    assert isinstance(outcome, dict)
    expected = "coverage_insufficient" if gate == "satisfied" else gate
    assert outcome["coverage_state"] == ("repair_required" if gate == "satisfied" else None)
    assert outcome["disposition"] == expected
    assert published["disposition"] == expected


@pytest.mark.parametrize(
    ("stale_field", "match"),
    [
        ("coverage_epoch", "epoch"),
        ("batch_id", "batch_id"),
        ("policy_sha256", "identity"),
    ],
)
def test_stale_cycle_identity_cannot_publish_a_normal_result(stale_field: str, match: str) -> None:
    state = _publish_state()
    state[stale_field] = {
        "coverage_epoch": 1,
        "batch_id": "20260821T000000Z",
        "policy_sha256": "b" * 64,
    }[stale_field]
    with pytest.raises(ValidationError, match=match):
        _seal(state, _inspect_output())


def test_stale_reviewed_case_cannot_publish_a_normal_result() -> None:
    state = _publish_state()
    reviewed = deepcopy(state["reviewed_case"])
    assert isinstance(reviewed, dict)
    reviewed["review_ref"] = {
        "path": "qa/results/review/case-review.json",
        "digest": "b" * 64,
    }
    state["reviewed_case"] = reviewed
    with pytest.raises(ValueError, match="stale Reviewed Case"):
        _seal(state, _inspect_output())


def test_stale_mapping_cannot_publish_a_normal_result() -> None:
    state = _publish_state()
    generation = deepcopy(state["generation_result"])
    assert isinstance(generation, dict)
    generation["mapping_ref"] = {
        "path": "qa/results/codegen/closed-mapping.json",
        "digest": "b" * 64,
    }
    state["generation_result"] = generation
    with pytest.raises(ValueError, match="stale test mapping"):
        _seal(state, _inspect_output())


def test_mixed_execution_failure_and_coverage_gap_selects_one_execution_action() -> None:
    output = _inspect_output()
    facts = output["failure_facts"]
    assert isinstance(facts, dict)
    facts["repairable_failure"] = True
    output["reason_codes"] = ["execution.locator_failure"]
    published = _seal(_publish_state(), output)
    outcome = published["inspection_outcome"]
    assert isinstance(outcome, dict)
    assert outcome["disposition"] == "repairable_execution_failure"
    assert outcome["coverage_state"] is None
    assert published["disposition"] == "repairable_execution_failure"


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
        (tmp_path / _facts(business).execution_ref.path).read_bytes()
    )
    metrics_data = json.loads((tmp_path / _facts(business).metrics_ref.path).read_bytes())
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
        (tmp_path / _facts(business).execution_ref.path).read_bytes()
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
    metrics = MetricsDocument.model_validate_json((tmp_path / _facts(business).metrics_ref.path).read_bytes())
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
    assessment = _facts(business).model_copy(
        update={"scope": _facts(business).scope.model_copy(update={"selected_families": ("api", "fuzz")})}
    )
    business = business.model_copy(update={"assessment": assessment})

    outcome = await _finalize_inspection(tmp_path, business, status="no_failures")

    assert outcome.status == "succeeded", outcome.failure
    finalized = InspectPublishedV1.model_validate(outcome.output).finalized
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
    metrics = tmp_path / _facts(business).metrics_ref.path
    metrics.write_bytes(metrics.read_bytes() + b"\n")

    result = await execute_task(
        cast(TaskHandler, inspect_prepare),
        cast(JSONValue, _inspect_bound(tmp_path, business)),
        tmp_path,
        binding_data=BINDING,
    )

    assert result.failure is not None
    assert result.failure.kind == "invalid_input"
    assert "digest changed" in result.failure.message


@pytest.mark.asyncio
async def test_prepare_rejects_a_tampered_producer_file(tmp_path: Path) -> None:
    business = _assessment_business(tmp_path)
    bound = _inspect_bound(tmp_path, business)
    produced = tmp_path / "qa/results/inspect/assessment-inputs.json"
    produced.write_bytes(produced.read_bytes() + b"\n")

    result = await execute_task(
        cast(TaskHandler, inspect_prepare),
        cast(JSONValue, bound),
        tmp_path,
        binding_data=BINDING,
    )

    assert result.failure is not None
    assert result.failure.kind == "invalid_input"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "handler",
    [fact_baseline_finalize, inspect_finalize],
)
async def test_assessment_finalizer_rejects_wrapped_input(tmp_path: Path, handler: object) -> None:
    business = _assessment_business(tmp_path).model_dump(mode="json")
    result = await execute_task(
        cast(TaskHandler, handler),
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
        cast(TaskHandler, fact_baseline_prepare),
        cast(JSONValue, _fact_baseline_business(business, tmp_path).model_dump(mode="json")),
        tmp_path,
        binding_data=BINDING,
    )
    assert prepared.status == "succeeded", prepared.failure

    stage = tmp_path / ".stage"
    baseline_document = {"source": "unavailable", "change_id": MATERIALIZED_CHANGE_ID}
    baseline_path = "qa/results/facts/fact-baseline.json"
    _write_stage(stage, baseline_path, baseline_document)
    baseline_result = await execute_task(
        cast(TaskHandler, fact_baseline_finalize),
        cast(
            JSONValue,
            {
                **_fact_baseline_business(business, tmp_path).model_dump(mode="json"),
                "agent_result": _agent_run(baseline_document),
            },
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
    assessment = _facts(business)
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
    inspection_path = "qa/results/inspect/inspection.json"
    _write_stage(stage, inspection_path, inspection_document)
    inspection_result = await execute_task(
        cast(TaskHandler, inspect_finalize),
        cast(
            JSONValue,
            {
                **_inspect_bound(tmp_path, inspect_business),
                "agent_result": _agent_run(inspection_document),
            },
        ),
        tmp_path,
        write_root=stage,
    )

    assert inspection_result.status == "succeeded", inspection_result.failure
    finalized = InspectPublishedV1.model_validate(inspection_result.output).finalized
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
        artifact_paths=("qa/results",),
        assessment=assessment,
        reviewed_case=materialized.reviewed_case,
        mapping_ref=materialized.generation.mapping_ref,
        policy_sha256=assessment.scope.policy_digest,
        repair_round=0,
    )
    baseline_path = "qa/results/facts/fact-baseline.json"
    stage = tmp_path / ".stage"
    baseline_document = {"source": "unavailable", "change_id": MATERIALIZED_CHANGE_ID}
    _write_stage(stage, baseline_path, baseline_document)
    baseline_result = await execute_task(
        cast(TaskHandler, fact_baseline_finalize),
        cast(
            JSONValue,
            {
                **_fact_baseline_business(business, tmp_path).model_dump(mode="json"),
                "agent_result": _agent_run(baseline_document),
            },
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
        "qa/results/inspect/inspection.json",
        inspection_document,
    )

    outcome = await execute_task(
        cast(TaskHandler, inspect_finalize),
        cast(
            JSONValue,
            {
                **_inspect_bound(
                    tmp_path,
                    inspect_business,
                    generation=materialized.generation,
                    execution=materialized.execution,
                ),
                "agent_result": _agent_run(inspection_document),
            },
        ),
        tmp_path,
        write_root=stage,
    )

    assert outcome.status == "succeeded", outcome.failure
    finalized = InspectPublishedV1.model_validate(outcome.output).finalized
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
        "qa/results/inspect/inspection.json",
        inspection_document,
    )
    rejected = await execute_task(
        cast(TaskHandler, inspect_finalize),
        cast(
            JSONValue,
            {
                **_inspect_bound(
                    tmp_path,
                    inspect_business,
                    generation=materialized.generation,
                    execution=materialized.execution,
                ),
                "agent_result": _agent_run(inspection_document),
            },
        ),
        tmp_path,
        write_root=stage,
    )
    assert rejected.status == "failed"
    assert rejected.failure is not None
    assert rejected.failure.kind == "invalid_output"
