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
    CHANGE_ID as MATERIALIZED_CHANGE_ID,
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


def test_repairable_failure_precedes_a_coverage_shortfall() -> None:
    facts = FailureClassificationFactsV1(
        identity_valid=True,
        blocking_failure=False,
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
        adversarial_required=False,
    )

    assert facts.blocking_failure is True
    assert "adversarial.open_counterexample" in reasons


def test_required_missing_adversarial_evidence_makes_assessment_incomplete(
    tmp_path: Path,
) -> None:
    business = _assessment_business(tmp_path)
    execution = ExecutionEvidenceV1.model_validate_json(
        (tmp_path / business.assessment.execution_ref.path).read_bytes()
    )
    metrics = MetricsDocument.model_validate_json(
        (tmp_path / business.assessment.metrics_ref.path).read_bytes()
    )

    facts, reasons = build_failure_classification_facts(
        execution,
        metrics,
        adversarial_required=True,
    )

    assert facts.identity_valid is False
    assert "adversarial.required_evidence_missing" in reasons


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
