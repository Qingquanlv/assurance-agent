from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from collections.abc import Mapping
from typing import Any, cast

import pytest
from langgraph.graph import END, START, StateGraph

from assurance_quality.graphs.factory import QualityGraphs
from assurance_quality.graphs.nodes import select_quality
from assurance_quality.graphs.state import QualityState

from assurance_execution.contracts.workflow import VerifiedExecutionCycleResultV1
from graph_engine.attempts import AttemptKey
from graph_engine.attempts.resolutions import ReceiptRef
from assurance_product.graphs.execute import adapt_quality_assess
from assurance_product.graphs.factory import invoke_product_root
from assurance_product.graphs.routes import route_execute, route_run
from assurance_product.graphs.tail_contracts import ExecuteTailResultV1
from tests.product.test_product_stategraph_flow import (
    _analysis_result,
    _diagnostic_report,
    _flow_features,
    _inspection,
    _product_graphs,
    _public_input,
)
from tests.verified_generation_fixture import accepted_verified_execution_input


def _analysis_required() -> dict[str, object]:
    return _inspection(disposition="analysis_required")


def test_assertion_analysis_product_bug_reaches_diagnostic_not_repair() -> None:
    result = invoke_product_root(
        _product_graphs(
            _flow_features(
                assess=_analysis_required(),
                issue_analyze=_analysis_result("product_bug"),
                report=_diagnostic_report(),
            )
        ),
        "execute",
        _public_input("execute"),
    )
    tail = ExecuteTailResultV1.model_validate(result["tail_result"])
    assert tail.status == "diagnostic"
    assert result["terminal"]["reason"] == "not_achieved"
    assert not result.get("repair_result")


def test_assertion_analysis_test_bug_reaches_repair_rerun_and_inspect() -> None:
    result = invoke_product_root(
        _product_graphs(
            _flow_features(
                assess=(_analysis_required(), _inspection()),
                issue_analyze=_analysis_result("test_bug"),
            )
        ),
        "execute",
        _public_input("execute"),
    )
    assert ExecuteTailResultV1.model_validate(result["tail_result"]).status == "reported"
    assert result["repair_result"]["status"] == "applied"
    assert result["execution_result"]["repair_round"] == 1


@pytest.mark.parametrize(
    ("disposition", "kind"),
    [
        ("analysis_required", "unknown"),
        ("analysis_required", "pending"),
        ("analysis_required", "exhausted"),
        ("blocked", "unknown"),
        ("blocked", "pending"),
    ],
)
def test_analysis_requires_human_only_after_analysis_or_budget_exhaustion(
    kind: str, disposition: str
) -> None:
    analysis = _analysis_result("test_bug" if kind == "exhausted" else "unknown")
    if kind == "pending":
        cast(dict, analysis["issue_analysis"])["candidate_digest"] = None
        cast(dict, analysis["issue_analysis"])["agent_result"].update(
            status="pending", candidate_count=0, candidates=[]
        )
    payload = _public_input("execute")
    if kind == "exhausted":
        cast(dict, payload["budgets"])["healing_rounds"] = 0
    result = invoke_product_root(
        _product_graphs(
            _flow_features(
                assess=_inspection(disposition=disposition),
                issue_analyze=analysis,
            )
        ),
        "execute",
        payload,
    )
    tail = ExecuteTailResultV1.model_validate(result["tail_result"])
    assert tail.status == "needs_human"
    assert tail.issue_analysis_ref is not None
    assert not result.get("repair_result")


@pytest.mark.parametrize("invalid", ["empty", "uncovered", "foreign", "stale", "failed"])
def test_invalid_issue_analysis_never_authorizes_repair_or_report(invalid: str) -> None:
    assessment = _analysis_required()
    analysis = _analysis_result("test_bug")
    result_data = cast(dict, analysis["issue_analysis"])["agent_result"]
    if invalid == "empty":
        result_data.update(candidate_count=0, candidates=[])
    elif invalid == "uncovered":
        cast(dict, assessment["assessment_inputs"])["owned_evidence_ids"].append("OBS-DEMO-002")
        cast(list, assessment["owned_evidence_ids"]).append("OBS-DEMO-002")
    elif invalid == "foreign":
        result_data["candidates"][0]["observation_ids"] = ["OBS-OTHER"]
    elif invalid == "stale":
        result_data["batch_id"] = "previous-batch"
    else:
        result_data["status"] = "failed"
        cast(dict, analysis["issue_analysis"])["candidate_digest"] = None
    result = invoke_product_root(
        _product_graphs(
            _flow_features(
                assess=assessment,
                issue_analyze=analysis,
            )
        ),
        "execute",
        _public_input("execute"),
    )
    assert ExecuteTailResultV1.model_validate(result["tail_result"]).status == "blocked"
    assert not result.get("repair_result")
    assert not result.get("report_refs")


@pytest.mark.parametrize(
    ("second_kind", "expected_status"),
    [
        ("product_bug", "diagnostic"),
        ("environment_issue", "diagnostic"),
        ("unknown", "needs_human"),
    ],
)
def test_mixed_analysis_does_not_authorize_whole_batch_test_repair(
    second_kind: str,
    expected_status: str,
) -> None:
    assessment = _analysis_required()
    cast(dict, assessment["assessment_inputs"])["owned_evidence_ids"].append("OBS-DEMO-002")
    cast(list, assessment["owned_evidence_ids"]).append("OBS-DEMO-002")
    analysis = _analysis_result("test_bug")
    result_data = cast(dict, analysis["issue_analysis"])["agent_result"]
    other = cast(dict, _analysis_result(second_kind)["issue_analysis"])["agent_result"]["candidates"][0]
    other.update(candidate_id="CAND-2", observation_ids=["OBS-DEMO-002"])
    other["proposed"]["severity"] = "low"
    result_data["candidates"].append(other)
    result_data["candidate_count"] = 2
    result = invoke_product_root(
        _product_graphs(
            _flow_features(
                assess=assessment,
                issue_analyze=analysis,
                report=_diagnostic_report(),
            )
        ),
        "execute",
        _public_input("execute"),
    )
    assert ExecuteTailResultV1.model_validate(result["tail_result"]).status == expected_status
    assert not result.get("repair_result")


def test_pending_analysis_cannot_publish_a_reference_that_was_not_committed() -> None:
    analysis = _analysis_result("unknown")
    finalized = cast(dict, analysis["issue_analysis"])
    finalized["candidate_digest"] = None
    finalized["agent_result"].update(status="pending", candidate_count=0, candidates=[])
    analysis["evidence_refs"] = []
    result = invoke_product_root(
        _product_graphs(
            _flow_features(
                assess=_analysis_required(),
                issue_analyze=analysis,
            )
        ),
        "execute",
        _public_input("execute"),
    )
    assert ExecuteTailResultV1.model_validate(result["tail_result"]).status == "blocked"


def test_passed_execution_reaches_inspect_and_committed_report() -> None:
    result = invoke_product_root(_product_graphs(), "execute", _public_input("execute"))
    tail = ExecuteTailResultV1.model_validate(result["tail_result"])
    assert tail.status == "reported"
    assert tail.inspection is not None
    assert tail.report is not None


def test_coverage_insufficient_returns_without_report() -> None:
    result = invoke_product_root(
        _product_graphs(_flow_features(assess=_inspection(disposition="coverage_insufficient"))),
        "execute",
        _public_input("execute"),
    )
    tail = ExecuteTailResultV1.model_validate(result["tail_result"])
    assert tail.status == "coverage_insufficient"
    assert result["terminal"] == {"status": "stopped", "reason": "coverage_insufficient"}
    assert tail.report is None
    assert tail.report_refs == ()


def test_needs_human_returns_without_test_repair_or_report() -> None:
    result = invoke_product_root(
        _product_graphs(_flow_features(assess=_inspection(disposition="needs_human"))),
        "execute",
        _public_input("execute"),
    )
    tail = ExecuteTailResultV1.model_validate(result["tail_result"])
    assert tail.status == "needs_human"
    assert result["terminal"] == {"status": "stopped", "reason": "needs_human"}
    assert tail.report is None
    assert "repair_result" not in result


def test_invalid_execution_result_blocks_before_inspect() -> None:
    result = invoke_product_root(
        _product_graphs(_flow_features(execute={"status": "failed", "attempt_failure": {"kind": "runtime"}})),
        "execute",
        _public_input("execute"),
    )
    tail = ExecuteTailResultV1.model_validate(result["tail_result"])
    assert tail.status == "blocked"
    assert "inspection_outcome" not in result
    assert result.get("execution_result") in (None, {})


def _verified_state(root: Path, completion: str = "collected") -> dict[str, Any]:
    prepared = accepted_verified_execution_input(root, change_id="CH-DEMO-001")
    generation_model = prepared.generation_result
    assert generation_model is not None
    machine_ref = generation_model.case_execution_plan_ref
    assert machine_ref is not None
    execution_id = "12345678-1234-4123-8123-123456789abc"
    process_ref = generation_model.mapping_ref.model_copy(
        update={"path": f"qa/changes/{prepared.change_id}/execution/{execution_id}/process_terminal.json"}
    )
    cycle = VerifiedExecutionCycleResultV1(
        validation_profile="api_db.v1",
        change_id=prepared.change_id,
        case_id="TC_USER_CREATE_001",
        reviewed_case=generation_model.reviewed_case,
        coverage_epoch=prepared.coverage_epoch,
        repair_round=0,
        plan_digest=prepared.plan_digest,
        plan_ref=prepared.plan_ref,
        case_execution_plan_ref=machine_ref,
        case_execution_plan_digest=machine_ref.digest,
        spec_digest=generation_model.reviewed_case.preparation_refs[0].digest,
        execution_id=execution_id,
        attempt_key=AttemptKey(digest="4" * 64),
        batch_id="verified-batch",
        executed_at=datetime(2026, 9, 6, tzinfo=UTC),
        completion_status=cast(Any, completion),
        mapping_ref=generation_model.mapping_ref,
        mapping_digest=generation_model.mapping_ref.digest,
        manifest_ref=generation_model.mapping_ref.model_copy(
            update={"path": f"qa/changes/{prepared.change_id}/execution/{execution_id}/manifest.json"}
        ),
        evidence_ref=generation_model.mapping_ref.model_copy(
            update={"path": f"qa/changes/{prepared.change_id}/execution/{execution_id}/outcome.json"}
        ),
        execution_index_ref=generation_model.mapping_ref.model_copy(
            update={"path": f"qa/changes/{prepared.change_id}/execution/execute-result.json"}
        ),
        execution_authority_ref=generation_model.mapping_ref.model_copy(
            update={
                "path": (f"qa/changes/{prepared.change_id}/execution/{execution_id}/execution_terminal.json")
            }
        ),
        raw_evidence_refs=(process_ref,),
        source_refs=generation_model.source_refs,
        receipt=ReceiptRef(receipt_id="verified-execution", receipt_digest="f" * 64),
    )
    # adapt_quality only needs coherent generation/cycle identities; use the
    # cycle's ReviewedCase and machine-plan bindings in the generation DTO.
    generation = {
        "change_id": cycle.change_id,
        "coverage_epoch": cycle.coverage_epoch,
        "reviewed_case": cycle.reviewed_case.model_dump(mode="json"),
        "plan_digest": cycle.plan_digest,
        "plan_ref": cycle.plan_ref.model_dump(mode="json"),
        "mapping_ref": cycle.mapping_ref.model_dump(mode="json"),
        "source_refs": [item.model_dump(mode="json") for item in cycle.source_refs],
        "plan_refs": [cycle.case_execution_plan_ref.model_dump(mode="json")],
        "case_execution_plan_ref": cycle.case_execution_plan_ref.model_dump(mode="json"),
        "case_execution_plan_digest": cycle.case_execution_plan_digest,
    }
    state = cast(dict[str, Any], _public_input("full"))
    state.update(
        {
            "change_id": cycle.change_id,
            "coverage_epoch": cycle.coverage_epoch,
            "validation_profile": cycle.validation_profile,
            "generation_result": generation,
            "execution_result": cycle.model_dump(mode="json"),
            "reviewed_case": cycle.reviewed_case.model_dump(mode="json"),
        }
    )
    return state


def test_collected_and_incomplete_verified_cycles_both_reach_quality(tmp_path: Path) -> None:
    for completion in ("collected", "incomplete"):
        state = _verified_state(tmp_path / completion, completion)
        assert route_execute(state) == "quality"
        assert route_run(state) == "quality"
        adapted = adapt_quality_assess(cast(Any, state))
        assert adapted["execution_status"] == completion
        feature_input = cast(dict[str, Any], adapted["feature_input"])
        execution_result = cast(dict[str, Any], feature_input["execution_result"])
        assert execution_result["completion_status"] == completion


@pytest.mark.parametrize("inspection", [None, {}, _inspection(disposition="blocked")["inspection_outcome"]])
def test_failed_inspect_attempt_stops_without_diagnostic_report(inspection) -> None:
    result = invoke_product_root(
        _product_graphs(
            _flow_features(
                assess={
                    "attempt_failure": {"kind": "invalid_output", "message": "Inspect failed"},
                    "inspection_outcome": inspection,
                }
            )
        ),
        "execute",
        _public_input("execute"),
    )
    assert ExecuteTailResultV1.model_validate(result["tail_result"]).status == "blocked"
    assert result["attempt_failure"]["kind"] == "invalid_output"
    assert not result.get("report_refs")
    assert result["terminal"] == {"status": "failed", "reason": "blocked"}


@pytest.mark.parametrize("entrypoint", ["execute", "full"])
@pytest.mark.parametrize("disposition", ["blocked", "analysis_required"])
def test_blocking_inspection_publishes_diagnostic_report_without_achievement(
    entrypoint: str, disposition: str
) -> None:
    from types import SimpleNamespace

    from assurance_execution.graphs.nodes import publish_execution
    from assurance_product.status import _execution_gate_from_snapshot
    from tests.product.test_achieved_terminal import _execution_evidence
    from tests.product.test_product_stategraph_flow import _execution

    issue_inputs: list[dict[str, object]] = []
    report_inputs: list[dict[str, object]] = []
    execution = _execution(status="FAIL")
    cycle = cast(dict, execution["execution_result"])
    execution.update(
        publish_execution(
            {"rounds_budget": 2, "rounds_used": 0},
            _execution_evidence(
                batch_id=cycle["batch_id"],
                status="failed",
                plan_digest=cycle["plan_digest"],
                plan_ref=cycle["plan_ref"],
            ),
            None,
        )
    )

    def recording_graph(seen: list[dict[str, object]], update: Mapping[str, object]) -> Any:
        builder = StateGraph(QualityState)

        def record(state: Mapping[str, object]) -> dict[str, object]:
            if seen is issue_inputs:
                business = select_quality(state)
                assert business.trace_digest == "a" * 64
                assert business.coverage_digest == "a" * 64
                assert business.metrics_digest == "a" * 64
                assert business.case_digest == "a" * 64
                assert business.mapping_digest == "a" * 64
                assert business.execution_digest == "a" * 64
            seen.append(dict(state))
            return dict(update)

        builder.add_node("record", cast(Any, record))
        builder.add_edge(START, "record")
        builder.add_edge("record", END)
        return builder.compile()

    issue_ref = {
        "path": "qa/changes/CH-DEMO-001/inspect/issue-analysis.json",
        "digest": "a" * 64,
    }
    features = _flow_features(
        execute=execution,
        assess=_inspection(disposition=disposition),
        report=_diagnostic_report(),
    )
    quality = cast(QualityGraphs, features["assurance.quality"])
    features["assurance.quality"] = QualityGraphs(
        assess=quality.assess,
        issue_review=quality.issue_review,
        issue_analyze=recording_graph(
            issue_inputs,
            {
                **_analysis_result("product_bug"),
                "classification": "product_bug",
                "fix_eligible": False,
                "evidence_refs": [issue_ref],
                "status": "passed",
            },
        ),
        issue_reconcile=quality.issue_reconcile,
        report=recording_graph(report_inputs, _diagnostic_report()),
    )
    result = invoke_product_root(
        _product_graphs(features),
        entrypoint,
        _public_input(entrypoint),
    )

    tail = ExecuteTailResultV1.model_validate(result["tail_result"])
    assert tail.status == "diagnostic"
    assert tail.inspection is not None
    assert tail.inspection.disposition == disposition
    assert tail.report is None
    assert tail.report_refs
    assert tail.report_receipt is not None
    assert result["terminal"] == {"status": "failed", "reason": "not_achieved"}
    assert len(issue_inputs) == 1
    assert issue_inputs[0]["owned_evidence_ids"] == ["OBS-DEMO-001"]
    assert issue_inputs[0]["evidence_bundle_digest"] == f"sha256:{'a' * 64}"
    assert len(report_inputs) == 1
    assert report_inputs[0]["issue_analysis_ref"] == issue_ref
    gate = _execution_gate_from_snapshot(SimpleNamespace(values=result))
    assert gate is not None
    assert gate.execution_digest == execution["execution_digest"]
