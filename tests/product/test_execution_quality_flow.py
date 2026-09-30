from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

import pytest
from langgraph.graph import END, START, StateGraph

from assurance_product.graphs.factory import invoke_product_root
from assurance_product.graphs.tail_contracts import ExecuteTailResultV1
from assurance_quality.graphs.factory import QualityGraphs
from assurance_quality.graphs.nodes import select_quality
from assurance_quality.graphs.state import QualityState

from tests.product.test_product_stategraph_flow import (
    _analysis_result,
    _diagnostic_report,
    _flow_features,
    _inspection,
    _product_graphs,
    _public_input,
)


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
        "full",
        _public_input("full"),
    )
    tail = ExecuteTailResultV1.model_validate(result["tail_result"])
    assert tail.status == "diagnostic"
    assert result["terminal"]["reason"] == "not_achieved"
    assert not result.get("repair_result")
    assert str(result.get("retro_id") or "").startswith("retro-")


def test_reported_inspect_does_not_enter_retro() -> None:
    result = invoke_product_root(_product_graphs(), "full", _public_input("full"))
    tail = ExecuteTailResultV1.model_validate(result["tail_result"])
    assert tail.status == "reported"
    assert not result.get("retro_id")


def test_assertion_analysis_test_bug_reaches_repair_rerun_and_inspect() -> None:
    result = invoke_product_root(
        _product_graphs(
            _flow_features(
                assess=(_analysis_required(), _inspection()),
                issue_analyze=_analysis_result("test_bug"),
            )
        ),
        "full",
        _public_input("full"),
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
    payload = _public_input("full")
    if kind == "exhausted":
        cast(dict, payload["budgets"])["healing_rounds"] = 0
    result = invoke_product_root(
        _product_graphs(
            _flow_features(
                assess=_inspection(disposition=disposition),
                issue_analyze=analysis,
            )
        ),
        "full",
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
        "full",
        _public_input("full"),
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
        "full",
        _public_input("full"),
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
        "full",
        _public_input("full"),
    )
    assert ExecuteTailResultV1.model_validate(result["tail_result"]).status == "blocked"


def test_passed_execution_reaches_inspect_and_committed_report() -> None:
    result = invoke_product_root(_product_graphs(), "full", _public_input("full"))
    tail = ExecuteTailResultV1.model_validate(result["tail_result"])
    assert tail.status == "reported"
    assert tail.inspection is not None
    assert tail.report is not None


def test_coverage_insufficient_returns_without_report() -> None:
    result = invoke_product_root(
        _product_graphs(_flow_features(assess=_inspection(disposition="coverage_insufficient"))),
        "full",
        _public_input(
            "full",
            budgets={"review_rounds": 1, "coverage_rounds": 0, "healing_rounds": 1, "execution_retries": 1},
        ),
    )
    tail = ExecuteTailResultV1.model_validate(result["tail_result"])
    assert tail.status == "coverage_insufficient"
    assert result["terminal"] == {"status": "failed", "reason": "not_achieved"}
    assert tail.report is None
    assert tail.report_refs == ()


def test_needs_human_returns_without_test_repair_or_report() -> None:
    result = invoke_product_root(
        _product_graphs(_flow_features(assess=_inspection(disposition="needs_human"))),
        "full",
        _public_input("full"),
    )
    tail = ExecuteTailResultV1.model_validate(result["tail_result"])
    assert tail.status == "needs_human"
    assert result["terminal"] == {"status": "failed", "reason": "not_achieved"}
    assert tail.report is None
    assert "repair_result" not in result


def test_invalid_execution_result_blocks_before_inspect() -> None:
    result = invoke_product_root(
        _product_graphs(_flow_features(execute={"status": "failed", "attempt_failure": {"kind": "runtime"}})),
        "full",
        _public_input("full"),
    )
    tail = ExecuteTailResultV1.model_validate(result["tail_result"])
    assert tail.status == "blocked"
    assert "inspection_outcome" not in result
    assert result.get("execution_result") in (None, {})


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
        "full",
        _public_input("full"),
    )
    assert ExecuteTailResultV1.model_validate(result["tail_result"]).status == "blocked"
    assert result["attempt_failure"]["kind"] == "invalid_output"
    assert not result.get("report_refs")
    assert result["terminal"] == {"status": "failed", "reason": "not_achieved"}


@pytest.mark.parametrize("disposition", ["blocked", "analysis_required"])
def test_blocking_inspection_publishes_diagnostic_report_without_achievement(disposition: str) -> None:
    from types import SimpleNamespace

    from assurance_execution.graphs.nodes import publish_execution
    from assurance_product.status import _execution_gate_from_snapshot
    from tests.product.test_achieved_terminal import _execution_evidence
    from tests.product.test_product_stategraph_flow import _execution

    issue_inputs: list[dict[str, object]] = []
    reconcile_inputs: list[dict[str, object]] = []
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
        "path": "qa/results/inspect/issue-analysis.json",
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
        fact_baseline=quality.fact_baseline,
        surface_baseline=quality.surface_baseline,
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
        issue_reconcile=recording_graph(
            reconcile_inputs,
            {
                "issue_snapshot_ref": {
                    "path": "qa/results/issues/snapshot.json",
                    "digest": "a" * 64,
                }
            },
        ),
        report=recording_graph(report_inputs, _diagnostic_report()),
    )
    result = invoke_product_root(
        _product_graphs(features),
        "full",
        _public_input("full"),
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
    assert len(reconcile_inputs) == 1
    assert reconcile_inputs[0]["issue_analysis"] is not None
    assert len(report_inputs) == 1
    assert report_inputs[0]["issue_analysis_ref"] == issue_ref
    gate = _execution_gate_from_snapshot(SimpleNamespace(values=result))
    assert gate is not None
    assert gate.execution_digest == execution["execution_digest"]
