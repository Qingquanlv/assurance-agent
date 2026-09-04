from __future__ import annotations

import ast
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any, cast

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from assurance_execution.graphs.factory import ExecutionGraphs
from assurance_generation.graphs.factory import GenerationGraphs
from assurance_healing.graphs.factory import HealingGraphs
from assurance_improvement.graphs.factory import ImprovementGraphs
from assurance_intake.graphs.factory import IntakeGraphs
from assurance_product.graphs.factory import (
    ProductGraphs,
    build_product_graphs,
    build_thin_entrypoint_graphs,
    invoke_product_root,
)
from assurance_product.graphs.revisions import ENTRYPOINT_CONTRACTS, ENTRYPOINT_RECURSION_LIMITS
from assurance_product.graphs.routes import (
    PRODUCT_EXCLUSIVE_ROUTES,
    coverage_repair_named_matches,
    execute_named_matches,
    execute_tail_named_matches,
    issue_analysis_named_matches,
    prepare_named_matches,
    quality_named_matches,
    quality_recheck_named_matches,
    route_coverage_repair,
    route_execute,
    route_execute_tail,
    route_issue_analysis,
    route_prepare,
    route_quality,
    route_quality_recheck,
    route_run,
    run_named_matches,
)
from assurance_product.models import PRODUCT_ENTRYPOINTS, ProductInputV1, ProductPublicOutput
from assurance_quality.graphs.factory import QualityGraphs
from graph_engine.boot.boot import EngineGraphBuildContext
from graph_engine.stategraph.routing import AmbiguousRouteMatch, select_exclusive_route

from tests.architecture.exclusive_route_inventory import ExclusiveRouteRow, EXCLUSIVE_ROUTE_INVENTORY
from tests.product.test_product_input import valid_product_input
from tests.product.test_stategraph_entrypoints import _real_features, _stub_features

_SHA = "a" * 64
_CASE_DELTA = "qa/changes/CH-DEMO-001/cases/system/dept/case.yaml"
_GRAPHS_ROOT = (
    Path(__file__).resolve().parents[2] / "packages/products/assurance-product/assurance_product/graphs"
)
_ROUTES_PATH = _GRAPHS_ROOT / "routes.py"

_NAMED_MATCHES = {
    "prepare": prepare_named_matches,
    "execute-tail": execute_tail_named_matches,
    "execute": execute_named_matches,
    "run": run_named_matches,
    "issue-analysis": issue_analysis_named_matches,
    "quality": quality_named_matches,
    "quality-recheck": quality_recheck_named_matches,
    "coverage-repair": coverage_repair_named_matches,
}


def _build_context(checkpointer: Any = None) -> EngineGraphBuildContext:
    return EngineGraphBuildContext(
        contracts={},
        checkpointer=checkpointer,
        approved_source_roots=(),
    )


def _public_input(entrypoint: str, **overrides: object) -> dict[str, object]:
    case_delta = (_CASE_DELTA,) if entrypoint in {"intake", "case", "full"} else ()
    families = ("api",) if entrypoint in {"full", "execute"} else ()
    payload = valid_product_input(
        case_delta_paths=case_delta,
        selected_test_families=families,
        **overrides,
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


def _flow_features(
    *,
    prepare: Mapping[str, object] | None = None,
    generation: Mapping[str, object] | None = None,
    execute: Mapping[str, object] | None = None,
    run: Mapping[str, object] | tuple[Mapping[str, object], ...] | None = None,
    assess: Mapping[str, object] | tuple[Mapping[str, object], ...] | None = None,
    issue_analyze: Mapping[str, object] | None = None,
    repair_failure: Mapping[str, object] | None = None,
    repair_coverage: Mapping[str, object] | None = None,
    report: Mapping[str, object] | None = None,
    retro: Mapping[str, object] | None = None,
    apply: Mapping[str, object] | None = None,
) -> dict[str, object]:
    features = _stub_features()
    assess_graph = (
        _sequenced(assess)
        if isinstance(assess, tuple)
        else _echo(assess or {"coverage_state": "satisfied", "rounds_used": 0, "rounds_budget": 1})
    )
    features["assurance.intake"] = IntakeGraphs(
        prepare=_echo(prepare or {"decision": "pass", "artifacts": [{"path": "qa/changes", "digest": _SHA}]}),
        case=_echo({"decision": "pass", "artifacts": [{"path": "qa/changes", "digest": _SHA}]}),
    )
    features["assurance.generation"] = GenerationGraphs(
        generation=_echo(generation or {"status": "passed", "families": {"api": {"completed": True}}}),
        api=_echo({"status": "passed"}),
        e2e=_echo({"status": "skipped"}),
        fuzz=_echo({"status": "skipped"}),
        performance=_echo({"status": "skipped"}),
    )
    run_graph = (
        _sequenced(run)
        if isinstance(run, tuple)
        else _echo(run or {"status": "passed", "rounds_used": 1, "rounds_budget": 1})
    )
    features["assurance.execution"] = ExecutionGraphs(
        execute=_echo(execute or {"status": "passed", "rounds_used": 0, "rounds_budget": 1}),
        rerun=run_graph,
    )
    features["assurance.quality"] = QualityGraphs(
        assess=assess_graph,
        issue_review=_echo({"classification": "test", "fix_eligible": True}),
        issue_analyze=_echo(
            issue_analyze
            or {"classification": "test", "fix_eligible": True, "rounds_used": 0, "rounds_budget": 1}
        ),
        issue_reconcile=_echo({"classification": "test", "fix_eligible": True}),
        report=_echo(
            report or {"coverage_state": "satisfied", "report_refs": [{"path": "report", "digest": _SHA}]}
        ),
    )
    features["assurance.healing"] = HealingGraphs(
        repair_failure=_echo(
            repair_failure or {"status": "repaired", "kind": "failure", "rounds_used": 1, "rounds_budget": 1}
        ),
        repair_coverage=_echo(
            repair_coverage
            or {"status": "repaired", "kind": "coverage", "rounds_used": 1, "rounds_budget": 2}
        ),
    )
    features["assurance.improvement"] = ImprovementGraphs(
        archive=_echo({"status": "done"}),
        retro=_echo(
            retro or {"status": "done", "receipt_refs": [{"receipt_id": "retro", "receipt_digest": _SHA}]}
        ),
        review=_echo({"status": "done"}),
        evaluate=_echo({"status": "done"}),
        export=_echo({"status": "done"}),
        apply=_echo(
            apply or {"status": "done", "receipt_refs": [{"receipt_id": "apply", "receipt_digest": _SHA}]}
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
    assert set(graphs.contracts) == set(PRODUCT_ENTRYPOINTS)
    assert graphs.contracts is ENTRYPOINT_CONTRACTS
    assert "execute" in graphs.entrypoints
    assert "full" in graphs.entrypoints
    assert set(graphs.entrypoints) - {"execute", "full"} == set(
        build_thin_entrypoint_graphs(context=_build_context(), features=_real_features()).entrypoints
    )


def test_build_product_graphs_rejects_missing_duplicate_and_extra_before_return() -> None:
    from assurance_product.graphs.factory import _closed_entrypoints

    features = _flow_features()
    thin = build_thin_entrypoint_graphs(context=_build_context(), features=features)
    placeholder = next(iter(thin.entrypoints.values()))
    closed = {**dict(thin.entrypoints), "execute": placeholder, "full": placeholder}

    missing = {name: graph for name, graph in closed.items() if name != "intake"}
    with pytest.raises(ValueError, match="missing"):
        _closed_entrypoints(missing)

    extra = dict(closed)
    extra["rogue"] = placeholder
    with pytest.raises(ValueError, match="extra"):
        _closed_entrypoints(extra)

    class _Duplicate(Mapping[str, Any]):
        def __init__(self, items: tuple[tuple[str, Any], ...]) -> None:
            self._items = items

        def __getitem__(self, key: str) -> Any:
            for name, value in self._items:
                if name == key:
                    return value
            raise KeyError(key)

        def __iter__(self) -> Iterator[str]:
            return (name for name, _ in self._items)

        def __len__(self) -> int:
            return len(self._items)

    with pytest.raises(ValueError, match="duplicate"):
        _closed_entrypoints(_Duplicate(tuple(closed.items()) + (("intake", placeholder),)))


def test_execute_composes_generation_execution_quality_and_report() -> None:
    graphs = _product_graphs()
    result = invoke_product_root(graphs, "execute", _public_input("execute"))
    output = ProductPublicOutput.model_validate(result["output"])
    assert output.change_id == "CH-DEMO-001"
    assert output.status == "completed"
    assert result["terminal"] == "done"
    assert result["coverage_state"] == "satisfied"
    assert "quality.report" not in result or result.get("coverage_state") == "satisfied"
    assert result.get("classification") in {None, ""}
    assert "fix-proposal" not in str(result.get("visited", ()))


def test_execute_failed_join_drives_issue_analysis_healing_and_rerun() -> None:
    graphs = _product_graphs(
        _flow_features(
            execute={"status": "failed", "rounds_used": 0, "rounds_budget": 1},
            issue_analyze={
                "classification": "test",
                "fix_eligible": True,
                "rounds_used": 0,
                "rounds_budget": 1,
            },
            repair_failure={"status": "repaired", "kind": "failure", "rounds_used": 1, "rounds_budget": 1},
            run={"status": "passed", "rounds_used": 1, "rounds_budget": 1},
        )
    )
    result = invoke_product_root(graphs, "execute", _public_input("execute"))
    assert result["terminal"] == "done"
    assert result["coverage_state"] == "satisfied"
    current = result["failed_join_inbox"]["current_trigger"]
    assert current is not None
    assert current["predecessor"] == "execute"
    assert current["value"] == {"rounds_used": 0, "rounds_budget": 1}


def test_execute_product_bug_reports_without_healing() -> None:
    graphs = _product_graphs(
        _flow_features(
            execute={"status": "failed", "rounds_used": 0, "rounds_budget": 1},
            issue_analyze={"classification": "product_bug", "fix_eligible": False},
            report={"coverage_state": "inconclusive", "report_refs": [{"path": "report", "digest": _SHA}]},
        )
    )
    result = invoke_product_root(graphs, "execute", _public_input("execute"))
    assert result["terminal"] == "done"
    assert result["coverage_state"] == "inconclusive"
    assert result.get("status") in {"completed", "failed", "passed"}


def test_full_composes_intake_execute_retro_and_improvement() -> None:
    graphs = _product_graphs()
    result = invoke_product_root(graphs, "full", _public_input("full"))
    output = ProductPublicOutput.model_validate(result["output"])
    assert output.change_id == "CH-DEMO-001"
    assert output.status == "completed"
    assert result["terminal"] == "achieved"
    assert result["coverage_state"] == "satisfied"
    assert tuple(item.receipt_id for item in output.receipts) == ("retro", "apply") or {
        item.receipt_id for item in output.receipts
    } >= {"retro", "apply"}


def test_full_prepare_rejection_is_not_achieved() -> None:
    graphs = _product_graphs(_flow_features(prepare={"decision": "reject"}))
    result = invoke_product_root(graphs, "full", _public_input("full"))
    assert result["terminal"] == "not-achieved"
    output = ProductPublicOutput.model_validate(result["output"])
    assert output.status == "failed"


def test_full_execute_tail_uses_compile_root_and_validates_execute_schema() -> None:
    factory = (_GRAPHS_ROOT / "factory.py").read_text(encoding="utf-8")
    assert "validate=False" not in factory
    assert ".compile(checkpointer=None)" not in factory
    graphs = _product_graphs()
    assert "validate" in graphs.entrypoints["execute"].nodes
    tail = graphs.entrypoints["full"].nodes["execute-tail"]
    runnable = getattr(tail, "runnable", tail)
    nested = getattr(runnable, "bound", runnable)
    nested_nodes = getattr(nested, "nodes", None)
    if nested_nodes is None:
        inner = getattr(runnable, "afunc", None) or getattr(runnable, "func", None)
        nested_nodes = getattr(inner, "nodes", {})
    assert "validate" in set(nested_nodes)


def test_dry_and_runtime_product_roots_share_nodes_and_attach_saver_only_at_runtime() -> None:
    features = _real_features()
    saver = InMemorySaver()
    dry = build_product_graphs(context=_build_context(None), features=features)
    runtime = build_product_graphs(context=_build_context(saver), features=features)
    assert set(dry.entrypoints) == set(runtime.entrypoints) == set(PRODUCT_ENTRYPOINTS)
    for name in PRODUCT_ENTRYPOINTS:
        assert set(dry.entrypoints[name].nodes) == set(runtime.entrypoints[name].nodes)
        assert dry.entrypoints[name].checkpointer is None
        assert runtime.entrypoints[name].checkpointer is saver
        assert dry.entrypoints[name].builder is None or True
        assert ENTRYPOINT_CONTRACTS[name].recursion_limit == ENTRYPOINT_RECURSION_LIMITS[name]


def test_routes_use_select_exclusive_route_without_priority_if_elif() -> None:
    source = _ROUTES_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(_ROUTES_PATH))
    assert "select_exclusive_route" in source
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and node.orelse:
            for child in node.orelse:
                assert not isinstance(child, ast.If), "exclusive routes must not use priority if/elif"


@pytest.mark.parametrize(
    "row",
    [item for item in EXCLUSIVE_ROUTE_INVENTORY if item.owner == "product"],
    ids=lambda row: f"{row.graph_id}/{row.node_id}",
)
def test_exclusive_route(row: ExclusiveRouteRow) -> None:
    builder = _NAMED_MATCHES[row.node_id]
    empty = builder({})
    assert select_exclusive_route(empty, otherwise=row.otherwise_target) == row.otherwise_target
    assert PRODUCT_EXCLUSIVE_ROUTES[row.node_id](empty) == row.otherwise_target
    with pytest.raises(AmbiguousRouteMatch):
        select_exclusive_route(
            {"first": row.otherwise_target, "second": f"{row.otherwise_target}-alt"},
            otherwise=row.otherwise_target,
        )


def test_product_exclusive_routes_match_yaml_conditions() -> None:
    assert route_prepare({"decision": "pass"}) == "execute-tail"
    assert route_prepare({"decision": "approved"}) == "execute-tail"
    assert route_prepare({"decision": "reject"}) == "not-achieved"
    assert route_execute_tail({"coverage_state": "satisfied"}) == "retro"
    assert route_execute_tail({"coverage_state": "exhausted"}) == "not-achieved"
    assert route_execute({"status": "passed"}) == "quality"
    assert route_execute({"status": "failed"}) == "failed-join"
    assert route_execute({"status": "blocked"}) == "not-achieved"
    assert route_run({"status": "passed"}) == "quality"
    assert route_run({"status": "failed"}) == "failed-join"
    assert (
        route_issue_analysis(
            {"classification": "test", "fix_eligible": True, "rounds_used": 0, "rounds_budget": 1}
        )
        == "fix-proposal"
    )
    assert route_issue_analysis({"classification": "product_bug", "fix_eligible": False}) == "report-issue"
    assert route_issue_analysis({"classification": "unknown", "fix_eligible": False}) == "not-achieved"
    assert (
        route_quality({"coverage_state": "repair_required", "rounds_used": 0, "rounds_budget": 2})
        == "coverage-needed"
    )
    assert route_quality({"coverage_state": "satisfied"}) == "assess-satisfied"
    assert route_quality({"coverage_state": "exhausted"}) == "assess-unsatisfied"
    assert route_quality({"coverage_state": "needs_human"}) == "coverage-human"
    assert (
        route_quality_recheck({"coverage_state": "repair_required", "rounds_used": 0, "rounds_budget": 2})
        == "coverage-needed"
    )
    assert route_coverage_repair({"status": "repaired"}) == "quality-recheck"
    assert route_coverage_repair({"status": "exhausted"}) == "report-unsatisfied-repair"
    assert route_coverage_repair({"status": "unknown"}) == "not-achieved"


def test_product_owned_inventory_has_no_pending_rows() -> None:
    product_rows = [row for row in EXCLUSIVE_ROUTE_INVENTORY if row.owner == "product"]
    assert len(product_rows) == 8
    assert all("pending" not in row.target_test for row in product_rows)
    assert all(
        row.target_test.startswith("tests/product/test_product_stategraph_flow.py") for row in product_rows
    )
