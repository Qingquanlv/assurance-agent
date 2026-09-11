from __future__ import annotations

import ast
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from assurance_execution.contracts.workflow import ExecutionCycleResultV1
from assurance_execution.graphs.factory import ExecutionGraphs
from assurance_generation.contracts.workflow import GenerationCycleResultV1
from assurance_generation.graphs.factory import GenerationGraphs
from assurance_healing.contracts.application import AppliedTestRepairV1
from assurance_healing.graphs.factory import HealingGraphs
from assurance_improvement.graphs.factory import ImprovementGraphs
from assurance_intake.contracts.workflow import (
    CaseFlowResultV1,
    EvidenceArtifactRefV1,
    ReviewedCaseV1,
)
from assurance_intake.graphs.factory import IntakeGraphs
from assurance_product.graphs.factory import (
    ProductGraphs,
    build_product_graphs,
    build_thin_entrypoint_graphs,
    invoke_product_root,
)
from assurance_product.graphs.full import route_case_result, route_full_tail
from assurance_product.graphs.revisions import ENTRYPOINT_CONTRACTS, ENTRYPOINT_RECURSION_LIMITS
from assurance_product.graphs.routes import (
    PRODUCT_EXCLUSIVE_ROUTES,
    applied_repair_named_matches,
    execute_named_matches,
    prepare_named_matches,
    quality_named_matches,
    route_applied_repair,
    route_execute,
    route_prepare,
    route_quality,
    route_run,
    run_named_matches,
)
from assurance_product.graphs.tail_contracts import ExecuteTailResultV1
from assurance_product.models import PRODUCT_ENTRYPOINTS, ProductInputV1, ProductPublicOutput
from assurance_quality.contracts.assessment import InspectionOutcomeV1, ReportOutcomeV1
from assurance_quality.graphs.factory import QualityGraphs
from graph_engine.attempts.resolutions import ReceiptRef
from graph_engine.boot.boot import EngineGraphBuildContext
from graph_engine.canonical import canonical_digest
from graph_engine.stategraph.routing import AmbiguousRouteMatch, select_exclusive_route

from tests.architecture.exclusive_route_inventory import ExclusiveRouteRow, EXCLUSIVE_ROUTE_INVENTORY
from tests.product.test_product_input import valid_product_input
from tests.product.test_stategraph_entrypoints import _real_features, _stub_features

_SHA = "a" * 64
_PLAN_DIGEST = "b" * 64
_CASE_DELTA = "qa/cases/system/dept/case.yaml"
_GRAPHS_ROOT = (
    Path(__file__).resolve().parents[2] / "packages/products/assurance-product/assurance_product/graphs"
)
_ROUTES_PATH = _GRAPHS_ROOT / "routes.py"

_NAMED_MATCHES = {
    "prepare": prepare_named_matches,
    "execute": execute_named_matches,
    "run": run_named_matches,
    "quality": quality_named_matches,
    "fix-proposal": applied_repair_named_matches,
}


def _ref(path: str, digest: str = _SHA) -> EvidenceArtifactRefV1:
    return EvidenceArtifactRefV1(path=path, digest=digest)


def _receipt(name: str) -> ReceiptRef:
    return ReceiptRef(receipt_id=name, receipt_digest=_SHA)


def _plan_ref() -> EvidenceArtifactRefV1:
    return _ref(
        f"qa/results/plan/{_PLAN_DIGEST}/resolved-assurance-plan.json",
        "c" * 64,
    )


def _plan_update() -> dict[str, object]:
    return {
        "plan_digest": _PLAN_DIGEST,
        "plan_ref": _plan_ref().model_dump(mode="json"),
        "selected_test_families": ["api"],
    }


def _reviewed(epoch: int = 0) -> ReviewedCaseV1:
    return ReviewedCaseV1(
        change_id="CH-DEMO-001",
        coverage_epoch=epoch,
        plan_digest=_PLAN_DIGEST,
        plan_ref=_plan_ref(),
        preparation_refs=(
            _plan_ref(),
            _ref("qa/results/preparation/context.json"),
        ),
        case_refs=(_ref(_CASE_DELTA),),
        review_ref=_ref("qa/results/review/case-review.json"),
    )


def _case(epoch: int = 0) -> dict[str, object]:
    reviewed = _reviewed(epoch)
    receipt = _receipt(f"case-{epoch}")
    return {
        **CaseFlowResultV1(status="reviewed", reviewed_case=reviewed, receipt=receipt).model_dump(
            mode="json"
        ),
        "case_receipt": receipt.model_dump(mode="json"),
        "decision": "pass",
    }


def _generation(epoch: int = 0) -> dict[str, object]:
    result = GenerationCycleResultV1(
        change_id="CH-DEMO-001",
        coverage_epoch=epoch,
        plan_digest=_PLAN_DIGEST,
        plan_ref=_plan_ref(),
        reviewed_case=_reviewed(epoch),
        mapping_ref=_ref(f"qa/results/generation/epochs/{epoch}/mapping.json"),
        source_refs=(_ref(f"qa/results/generated/epochs/{epoch}/tests/test_case.py"),),
        plan_refs=(_ref(f"qa/results/plans/epochs/{epoch}/api.json"),),
    )
    return {"generation_result": result.model_dump(mode="json"), "status": "passed"}


def _execution(epoch: int = 0, *, repair_round: int = 0, status: str = "PASS") -> dict[str, object]:
    generated = GenerationCycleResultV1.model_validate(_generation(epoch)["generation_result"])
    result = ExecutionCycleResultV1(
        change_id="CH-DEMO-001",
        coverage_epoch=epoch,
        plan_digest=_PLAN_DIGEST,
        plan_ref=_plan_ref(),
        repair_round=repair_round,
        batch_id=f"20260905T120{epoch}{repair_round}0Z",
        executed_at=datetime(2026, 9, 5, 12, epoch, repair_round, tzinfo=UTC),
        final_status=status,  # type: ignore[arg-type]
        evidence_ref=_ref(f"qa/results/execution/epochs/{epoch}/rounds/{repair_round}/result.json"),
        mapping_ref=generated.mapping_ref,
        source_refs=generated.source_refs,
        receipt=_receipt(f"execution-{epoch}-{repair_round}"),
    )
    return {"execution_result": result.model_dump(mode="json"), "status": "passed"}


def _inspection(epoch: int = 0, disposition: str = "satisfied") -> dict[str, object]:
    execution = ExecutionCycleResultV1.model_validate(_execution(epoch)["execution_result"])
    gaps = _ref(f"qa/results/inspect/epochs/{epoch}/gaps.json")
    observations = _ref(f"qa/results/inspect/epochs/{epoch}/batches/{execution.batch_id}/observations.json")
    issue_manifest = _ref(
        f"qa/results/inspect/epochs/{epoch}/batches/{execution.batch_id}/issue-evidence-manifest.json"
    )
    trace = _ref(f"qa/results/inspect/epochs/{epoch}/trace.json")
    metrics = _ref(f"qa/results/inspect/epochs/{epoch}/metrics.json")
    sufficiency = _ref(f"qa/results/inspect/epochs/{epoch}/trace-sufficiency.json")
    coverage_state = {
        "satisfied": "satisfied",
        "coverage_insufficient": "repair_required",
    }.get(disposition)
    outcome = InspectionOutcomeV1(
        change_id="CH-DEMO-001",
        coverage_epoch=epoch,
        batch_id=execution.batch_id,
        plan_digest=_PLAN_DIGEST,
        plan_ref=_plan_ref(),
        disposition=disposition,  # type: ignore[arg-type]
        inspection_receipt=_receipt(f"inspect-{epoch}"),
        reviewed_case=_reviewed(epoch),
        mapping_ref=execution.mapping_ref,
        assessment_refs=(gaps,),
        reason_codes=(f"inspection.{disposition}",),
        coverage_state=coverage_state,  # type: ignore[arg-type]
    )
    return {
        "inspection_outcome": outcome.model_dump(mode="json"),
        "assessment_inputs": {
            "change_id": "CH-DEMO-001",
            "coverage_epoch": epoch,
            "batch_id": execution.batch_id,
            "plan_digest": _PLAN_DIGEST,
            "plan_ref": _plan_ref().model_dump(mode="json"),
            "scope": {
                "change_id": "CH-DEMO-001",
                "coverage_epoch": epoch,
                "required_case_ids": ["TC-DEMO-001"],
                "selected_families": ["api"],
                "applicable_goals": ["constraint_coverage"],
                "applicability_refs": [_reviewed(epoch).review_ref.model_dump(mode="json")],
                "risk_tier": "high",
                "policy_digest": _SHA,
            },
            "policy": {
                "coverage_floor_by_tier": {
                    "low": 0.7,
                    "medium": 0.8,
                    "high": 0.9,
                    "critical": 1.0,
                }
            },
            "trace_ref": trace.model_dump(mode="json"),
            "gaps_ref": gaps.model_dump(mode="json"),
            "metrics_ref": metrics.model_dump(mode="json"),
            "sufficiency_ref": sufficiency.model_dump(mode="json"),
            "execution_ref": execution.evidence_ref.model_dump(mode="json"),
            "observations_ref": observations.model_dump(mode="json"),
            "issue_evidence_manifest_ref": issue_manifest.model_dump(mode="json"),
            "owned_evidence_ids": ["OBS-DEMO-001"],
            "evidence_bundle_digest": f"sha256:{_SHA}",
            "healing_ref": None,
            "issue_ref": None,
        },
        "owned_evidence_ids": ["OBS-DEMO-001"],
        "evidence_bundle_digest": f"sha256:{_SHA}",
        "observations_ref": observations.model_dump(mode="json"),
        "issue_evidence_manifest_ref": issue_manifest.model_dump(mode="json"),
        "coverage_state": coverage_state or "",
        "status": "passed",
    }


def _report(epoch: int = 0) -> dict[str, object]:
    inspection = InspectionOutcomeV1.model_validate(_inspection(epoch)["inspection_outcome"])
    ref = _ref(f"qa/results/report/epochs/{epoch}/report.json")
    receipt = _receipt(f"report-{epoch}")
    outcome = ReportOutcomeV1(
        change_id="CH-DEMO-001",
        coverage_epoch=epoch,
        batch_id=inspection.batch_id,
        inspection_receipt=inspection.inspection_receipt,
        plan_digest=_PLAN_DIGEST,
        plan_ref=_plan_ref(),
        report_refs=(ref,),
        report_receipt=receipt,
    )
    return {
        "report_outcome": outcome.model_dump(mode="json"),
        "report_refs": [ref.model_dump(mode="json")],
        "report_receipt": receipt.model_dump(mode="json"),
        "status": "reported",
    }


def _diagnostic_report(epoch: int = 0) -> dict[str, object]:
    ref = _ref(f"qa/results/report/epochs/{epoch}/report.json")
    receipt = _receipt(f"diagnostic-report-{epoch}")
    return {
        "report_outcome": {},
        "report_refs": [ref.model_dump(mode="json")],
        "report_receipt": receipt.model_dump(mode="json"),
        "report_purpose": "diagnostic",
        "status": "diagnostic",
    }


def _analysis_result(classification: str) -> dict[str, object]:
    inspection = cast(dict, _inspection()["inspection_outcome"])
    ref = {"path": "qa/results/inspect/issue-analysis.json", "digest": "a" * 64}
    return {
        "issue_analysis": {
            "agent_result": {
                "schema_version": "1.0",
                "change_id": "CH-DEMO-001",
                "batch_id": inspection["batch_id"],
                "evidence_bundle_digest": f"sha256:{'a' * 64}",
                "status": "completed",
                "candidate_count": 1,
                "candidates": [
                    {
                        "candidate_id": "CAND-1",
                        "observation_ids": ["OBS-DEMO-001"],
                        "proposed": {
                            "title": "Failure",
                            "classification": classification,
                            "severity": "high",
                            "root_cause_hypothesis": "Compare the assertion to the frozen contract",
                        },
                        "affected_surface": {"kind": "test", "value": "tests/a.py"},
                        "fingerprint_inputs": {"surface": "tests/a.py", "symptom": "assertion failed"},
                        "possible_problem_ids": [],
                        "confidence": 0.9,
                        "recommended_action": "investigate",
                    }
                ],
            },
            "candidate_digest": f"sha256:{'a' * 64}",
            "issue_analysis_ref": ref,
        },
        "evidence_refs": [ref],
    }


def _applied(epoch: int = 0, repair_round: int = 1) -> dict[str, object]:
    source = _ref(f"qa/results/generated/epochs/{epoch}/tests/test_case.py")
    result = AppliedTestRepairV1(
        change_id="CH-DEMO-001",
        coverage_epoch=epoch,
        repair_round=repair_round,
        plan_digest=_PLAN_DIGEST,
        plan_ref=_plan_ref(),
        status="applied",
        changed_test_refs=(source,),
        mapping_ref=_ref(f"qa/results/generation/epochs/{epoch}/mapping.json"),
        receipt=_receipt(f"repair-{epoch}-{repair_round}"),
    )
    return {
        "change_id": "CH-DEMO-001",
        "coverage_epoch": epoch,
        "repair_result": result.model_dump(mode="json"),
        "rounds_used": repair_round,
        "healing_rounds_used": repair_round,
        "status": "applied",
    }


def _build_context(checkpointer: Any = None) -> EngineGraphBuildContext:
    return EngineGraphBuildContext(contracts={}, checkpointer=checkpointer, approved_source_roots=())


def _public_input(entrypoint: str, **overrides: object) -> dict[str, object]:
    case_delta = (_CASE_DELTA,) if entrypoint in {"intake", "case", "full"} else ()
    families = ("api",) if entrypoint in {"full", "intake"} else ()
    resolved_plan_ref = _plan_ref().model_dump(mode="json") if entrypoint in {"case", "execute"} else None
    values: dict[str, object] = {
        "case_delta_paths": case_delta,
        "candidate_test_families": families,
        "resolved_plan_ref": resolved_plan_ref,
    }
    values.update(overrides)
    payload = valid_product_input(
        **values,
    )
    return ProductInputV1.model_validate(payload).model_dump(mode="json")


def _echo(update: Mapping[str, object]) -> CompiledStateGraph:
    builder = StateGraph(cast(Any, dict))

    def node(state: object) -> dict[str, object]:
        del state
        return dict(update)

    builder.add_node("echo", node)
    builder.add_edge(START, "echo")
    builder.add_edge("echo", END)
    return builder.compile(checkpointer=None)


def _sequenced(updates: tuple[Mapping[str, object], ...]) -> CompiledStateGraph:
    builder = StateGraph(cast(Any, dict))
    calls = {"n": 0}

    def node(state: object) -> dict[str, object]:
        del state
        index = min(calls["n"], len(updates) - 1)
        calls["n"] += 1
        return dict(updates[index])

    builder.add_node("echo", node)
    builder.add_edge(START, "echo")
    builder.add_edge("echo", END)
    return builder.compile(checkpointer=None)


def _graph(value: Mapping[str, object] | tuple[Mapping[str, object], ...]) -> CompiledStateGraph:
    return _sequenced(value) if isinstance(value, tuple) else _echo(value)


def _flow_features(
    *,
    prepare: Mapping[str, object] | None = None,
    case: Mapping[str, object] | tuple[Mapping[str, object], ...] | None = None,
    generation: Mapping[str, object] | tuple[Mapping[str, object], ...] | None = None,
    execute: Mapping[str, object] | tuple[Mapping[str, object], ...] | None = None,
    run: Mapping[str, object] | tuple[Mapping[str, object], ...] | None = None,
    assess: Mapping[str, object] | tuple[Mapping[str, object], ...] | None = None,
    issue_analyze: Mapping[str, object] | None = None,
    repair_failure: Mapping[str, object] | None = None,
    repair_coverage: Mapping[str, object] | None = None,
    report: Mapping[str, object] | tuple[Mapping[str, object], ...] | None = None,
    retro: Mapping[str, object] | None = None,
    apply: Mapping[str, object] | None = None,
) -> dict[str, object]:
    del repair_coverage
    features = _stub_features()
    features["assurance.intake"] = IntakeGraphs(
        prepare=_echo(
            prepare
            or {
                **_plan_update(),
                "status": "prepared",
                "preparation_refs": [_ref("qa/results/preparation/context.json").model_dump(mode="json")],
            }
        ),
        load_plan=_echo({**_plan_update(), "status": "prepared"}),
        case=_graph(case or _case()),
    )
    features["assurance.generation"] = GenerationGraphs(
        generation=_graph(generation or _generation()),
        api=_echo({"status": "passed"}),
        e2e=_echo({"status": "skipped"}),
        fuzz=_echo({"status": "skipped"}),
        performance=_echo({"status": "skipped"}),
    )
    features["assurance.execution"] = ExecutionGraphs(
        execute=_graph(execute or _execution()),
        rerun=_graph(run or _execution(repair_round=1)),
    )
    features["assurance.quality"] = QualityGraphs(
        assess=_graph(assess or _inspection()),
        issue_review=_echo({"classification": "test", "fix_eligible": True}),
        issue_analyze=_echo(issue_analyze or {"classification": "test", "fix_eligible": True}),
        issue_reconcile=_echo({"classification": "test", "fix_eligible": True}),
        report=_graph(report or _report()),
    )
    features["assurance.healing"] = HealingGraphs(
        repair_failure=_echo(repair_failure or _applied()),
        repair_coverage=_echo({"status": "failed", "kind": "coverage"}),
    )
    features["assurance.improvement"] = ImprovementGraphs(
        archive=_echo({"status": "done"}),
        retro=_echo(
            retro
            or {
                "status": "done",
                "receipt_refs": [{"receipt_id": "retro", "receipt_digest": _SHA}],
            }
        ),
        review=_echo({"status": "done"}),
        evaluate=_echo({"status": "done"}),
        export=_echo({"status": "done"}),
        apply=_echo(
            apply
            or {
                "status": "done",
                "receipt_refs": [{"receipt_id": "apply", "receipt_digest": _SHA}],
            }
        ),
        rollback=_echo({"status": "done"}),
    )
    return features


def _product_graphs(features: Mapping[str, object] | None = None) -> ProductGraphs:
    return build_product_graphs(context=_build_context(), features=features or _flow_features())


def test_build_product_graphs_merges_twelve_thin_roots_plus_execute_and_full() -> None:
    graphs = build_product_graphs(context=_build_context(), features=_real_features())
    assert isinstance(graphs, ProductGraphs)
    assert set(graphs.entrypoints) == set(PRODUCT_ENTRYPOINTS)
    assert len(graphs.entrypoints) == 14
    assert graphs.contracts is ENTRYPOINT_CONTRACTS
    assert set(graphs.entrypoints) - {"execute", "full"} == set(
        build_thin_entrypoint_graphs(context=_build_context(), features=_real_features()).entrypoints
    )


def test_build_product_graphs_rejects_missing_duplicate_and_extra_before_return() -> None:
    from assurance_product.graphs.factory import _closed_entrypoints

    thin = build_thin_entrypoint_graphs(context=_build_context(), features=_flow_features())
    placeholder = next(iter(thin.entrypoints.values()))
    closed = {**dict(thin.entrypoints), "execute": placeholder, "full": placeholder}
    with pytest.raises(ValueError, match="missing"):
        _closed_entrypoints({name: graph for name, graph in closed.items() if name != "intake"})
    with pytest.raises(ValueError, match="extra"):
        _closed_entrypoints({**closed, "rogue": placeholder})

    class _Duplicate(Mapping[str, Any]):
        def __init__(self, items: tuple[tuple[str, Any], ...]) -> None:
            self._items = items

        def __getitem__(self, key: str) -> Any:
            return next(value for name, value in self._items if name == key)

        def __iter__(self) -> Iterator[str]:
            return (name for name, _ in self._items)

        def __len__(self) -> int:
            return len(self._items)

    with pytest.raises(ValueError, match="duplicate"):
        _closed_entrypoints(_Duplicate(tuple(closed.items()) + (("intake", placeholder),)))


def test_execute_composes_generation_execution_inspect_and_report() -> None:
    result = invoke_product_root(_product_graphs(), "execute", _public_input("execute"))
    assert ProductPublicOutput.model_validate(result["output"]).status == "completed"
    assert result["terminal"] == {"status": "completed", "reason": "done"}
    assert ExecuteTailResultV1.model_validate(result["tail_result"]).status == "reported"


def test_repairable_inspection_requires_applied_repair_before_rerun() -> None:
    features = _flow_features(
        assess=(_inspection(disposition="repairable_execution_failure"), _inspection()),
        repair_failure=_applied(),
    )
    result = invoke_product_root(_product_graphs(features), "execute", _public_input("execute"))
    assert result["terminal"] == {"status": "completed", "reason": "done"}
    assert ExecutionCycleResultV1.model_validate(result["execution_result"]).repair_round == 1


def test_proposal_only_does_not_enter_rerun() -> None:
    result = invoke_product_root(
        _product_graphs(
            _flow_features(
                assess=_inspection(disposition="repairable_execution_failure"),
                repair_failure={"proposal_result": {"change_id": "CH-DEMO-001"}, "status": "passed"},
            )
        ),
        "execute",
        _public_input("execute"),
    )
    assert result["terminal"] == {"status": "failed", "reason": "blocked"}
    assert ExecutionCycleResultV1.model_validate(result["execution_result"]).repair_round == 0


def test_quality_adapter_uses_committed_time_for_hashed_execution_batch() -> None:
    from assurance_product.graphs.execute import adapt_quality_assess

    execution = ExecutionCycleResultV1.model_validate(_execution()["execution_result"])
    execution = execution.model_copy(update={"batch_id": "d" * 64})
    state = {
        **_public_input("full"),
        **_generation(),
        "execution_result": execution.model_dump(mode="json"),
        "reviewed_case": _reviewed().model_dump(mode="json"),
    }
    adapted = adapt_quality_assess(cast(Any, state))
    assert adapted["batch_id"] == "d" * 64
    assert adapted["execution_at"] == execution.executed_at.isoformat()
    assert adapted["activation"] == {
        "kind": "trigger",
        "value": f"inspect.0.{canonical_digest('d' * 64)[:16]}.0",
    }


def test_full_reuses_case_subgraph_for_coverage_reentry() -> None:
    calls = {"prepare": 0, "case": 0}

    def counted(name: str, updates: tuple[Mapping[str, object], ...]) -> CompiledStateGraph:
        builder = StateGraph(cast(Any, dict))

        def node(state: object) -> dict[str, object]:
            del state
            index = min(calls[name], len(updates) - 1)
            calls[name] += 1
            return dict(updates[index])

        builder.add_node("echo", node)
        builder.add_edge(START, "echo")
        builder.add_edge("echo", END)
        return builder.compile()

    features = _flow_features(
        case=(_case(0), _case(1)),
        generation=(_generation(0), _generation(1)),
        execute=(_execution(0), _execution(1)),
        assess=(_inspection(0, "coverage_insufficient"), _inspection(1)),
        report=_report(1),
    )
    intake = cast(IntakeGraphs, features["assurance.intake"])
    features["assurance.intake"] = IntakeGraphs(
        prepare=counted(
            "prepare",
            (
                {
                    **_plan_update(),
                    "status": "prepared",
                    "preparation_refs": [_ref("qa/results/preparation/context.json").model_dump(mode="json")],
                },
            ),
        ),
        load_plan=intake.load_plan,
        case=counted("case", (_case(0), _case(1))),
    )
    del intake
    result = invoke_product_root(_product_graphs(features), "full", _public_input("full"))
    assert result["terminal"] == {"status": "completed", "reason": "achieved"}
    assert result["coverage_epoch"] == 1
    assert calls == {"prepare": 1, "case": 2}


@pytest.mark.parametrize("feature", ["retro", "apply"])
def test_full_does_not_invoke_optional_post_report_work(feature: str) -> None:
    update = {"status": "failed", "attempt_failure": {"kind": "invalid_input"}}
    result = invoke_product_root(
        _product_graphs(
            _flow_features(
                retro=update if feature == "retro" else None,
                apply=update if feature == "apply" else None,
            )
        ),
        "full",
        _public_input("full"),
    )
    assert result["terminal"] == {"status": "completed", "reason": "achieved"}


def test_full_case_rejection_does_not_enter_generation() -> None:
    result = invoke_product_root(
        _product_graphs(_flow_features(case={"status": "rejected", "decision": "reject"})),
        "full",
        _public_input("full"),
    )
    assert result["terminal"] == {"status": "failed", "reason": "not_achieved"}
    assert "generation_result" not in result


def test_failed_report_never_enters_retro_or_achieved() -> None:
    result = invoke_product_root(
        _product_graphs(_flow_features(report={"status": "failed", "attempt_failure": {"kind": "runtime"}})),
        "full",
        _public_input("full"),
    )
    assert result["terminal"] == {"status": "failed", "reason": "not_achieved"}
    assert result.get("report_outcome") in (None, {})


def test_full_uses_internal_execute_tail_while_public_execute_wraps_it() -> None:
    graphs = _product_graphs()
    assert {"validate", "adapt-tail", "execute-tail", "publish"} <= set(graphs.entrypoints["execute"].nodes)
    assert {"adapt-case", "advance-coverage", "execute-tail"} <= set(graphs.entrypoints["full"].nodes)
    tail = graphs.entrypoints["full"].nodes["execute-tail"]
    runnable = getattr(tail, "runnable", tail)
    nested = getattr(runnable, "bound", runnable)
    nested_nodes = getattr(nested, "nodes", None)
    if nested_nodes is None:
        inner = getattr(runnable, "afunc", None) or getattr(runnable, "func", None)
        nested_nodes = getattr(inner, "nodes", {})
    forbidden = {"coverage-repair", "coverage-repair-brief", "quality-recheck", "coverage-needed"}
    assert not forbidden.intersection(nested_nodes)
    assert {"generation", "execute", "quality", "fix-proposal", "run", "report"} <= set(nested_nodes)


def test_dry_and_runtime_product_roots_share_nodes_and_attach_saver_only_at_runtime() -> None:
    features = _real_features()
    dry = build_product_graphs(context=_build_context(None), features=features)
    runtime = build_product_graphs(context=_build_context(InMemorySaver()), features=features)
    for name in PRODUCT_ENTRYPOINTS:
        assert set(dry.entrypoints[name].nodes) == set(runtime.entrypoints[name].nodes)
        assert dry.entrypoints[name].checkpointer is None
        assert runtime.entrypoints[name].checkpointer is not None
        assert ENTRYPOINT_CONTRACTS[name].recursion_limit == ENTRYPOINT_RECURSION_LIMITS[name]


def test_routes_use_select_exclusive_route_without_priority_if_elif() -> None:
    source = _ROUTES_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(_ROUTES_PATH))
    assert "select_exclusive_route" in source
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and node.orelse:
            assert not any(isinstance(child, ast.If) for child in node.orelse)


@pytest.mark.parametrize(
    "row",
    [item for item in EXCLUSIVE_ROUTE_INVENTORY if item.owner == "product"],
    ids=lambda row: f"{row.graph_id}/{row.node_id}",
)
def test_exclusive_route(row: ExclusiveRouteRow) -> None:
    builder = _NAMED_MATCHES[row.node_id]
    empty = builder({})
    assert select_exclusive_route(empty, otherwise=row.otherwise_target) == row.otherwise_target
    assert PRODUCT_EXCLUSIVE_ROUTES[row.node_id]({}) == row.otherwise_target
    with pytest.raises(AmbiguousRouteMatch):
        select_exclusive_route(
            {"first": row.otherwise_target, "second": f"{row.otherwise_target}-alt"},
            otherwise=row.otherwise_target,
        )


def test_product_exclusive_routes_have_fixed_evidence_driven_targets() -> None:
    execution = _execution()["execution_result"]
    assert route_prepare({"status": "prepared"}) == "prepared"
    assert (
        route_execute({"change_id": "CH-DEMO-001", "coverage_epoch": 0, "execution_result": execution})
        == "quality"
    )
    assert (
        route_run({"change_id": "CH-DEMO-001", "coverage_epoch": 0, "execution_result": execution})
        == "quality"
    )
    for disposition, target in {
        "satisfied": "quality-report",
        "coverage_insufficient": "coverage-insufficient",
        "repairable_execution_failure": "fix-proposal",
        "needs_human": "needs-human",
        "blocked": "diagnostic",
    }.items():
        assert route_quality(_inspection(disposition=disposition)) == target
    assert route_applied_repair(_applied()) == "rerun"
    assert route_applied_repair({"proposal_result": {"change_id": "CH-DEMO-001"}}) == "blocked"


def test_full_routes_require_current_typed_case_and_tail_results() -> None:
    case = _case()
    assert route_case_result({"change_id": "CH-DEMO-001", "coverage_epoch": 0, **case}) == "reviewed"
    assert route_case_result({"status": "passed", "decision": "pass"}) == "failed"
    insufficient = ExecuteTailResultV1(
        status="coverage_insufficient",
        plan_digest=_PLAN_DIGEST,
        plan_ref=_plan_ref(),
        inspection=InspectionOutcomeV1.model_validate(
            _inspection(disposition="coverage_insufficient")["inspection_outcome"]
        ),
    )
    state = {
        "coverage_epoch": 0,
        "budgets": {"review_rounds": 1, "coverage_rounds": 1, "healing_rounds": 1, "execution_retries": 1},
        "tail_result": insufficient.model_dump(mode="json"),
    }
    assert route_full_tail(state) == "advance-coverage"
    state["budgets"] = {**cast(dict[str, int], state["budgets"]), "coverage_rounds": 0}
    assert route_full_tail(state) == "not-achieved"


def test_product_owned_inventory_has_no_pending_rows() -> None:
    product_rows = [row for row in EXCLUSIVE_ROUTE_INVENTORY if row.owner == "product"]
    assert len(product_rows) == 5
    assert all("pending" not in row.target_test for row in product_rows)
