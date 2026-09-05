from __future__ import annotations

from typing import cast

from assurance_intake.graphs.factory import IntakeGraphs
from assurance_product.graphs.factory import invoke_product_root
from assurance_product.graphs.tail_contracts import ExecuteTailResultV1

from tests.product.test_product_stategraph_flow import (
    _case,
    _execution,
    _flow_features,
    _generation,
    _inspection,
    _product_graphs,
    _public_input,
    _report,
)


def test_coverage_insufficient_reenters_the_shared_case_flow() -> None:
    features = _flow_features(
        case=(_case(0), _case(1)),
        generation=(_generation(0), _generation(1)),
        execute=(_execution(0), _execution(1)),
        assess=(_inspection(0, "coverage_insufficient"), _inspection(1)),
        report=_report(1),
    )
    result = invoke_product_root(_product_graphs(features), "full", _public_input("full"))
    assert result["terminal"] == "achieved"
    assert result["coverage_epoch"] == 1
    assert result["case_rework_context"]["previous_case"]["coverage_epoch"] == 0
    assert result["reviewed_case"]["coverage_epoch"] == 1


def test_exhausted_coverage_budget_stops_without_report_or_retro() -> None:
    result = invoke_product_root(
        _product_graphs(_flow_features(assess=_inspection(0, "coverage_insufficient"))),
        "full",
        _public_input(
            "full",
            budgets={
                "review_rounds": 2,
                "coverage_rounds": 0,
                "healing_rounds": 1,
                "execution_retries": 1,
            },
        ),
    )
    assert result["terminal"] == "not-achieved"
    assert ExecuteTailResultV1.model_validate(result["tail_result"]).status == "coverage_insufficient"
    assert result.get("report_outcome") in (None, {})
    assert "projection" not in result


def test_compiled_product_graph_has_no_coverage_repair_route() -> None:
    graphs = _product_graphs()
    full = graphs.entrypoints["full"]
    tail_node = full.nodes["execute-tail"]
    runnable = getattr(tail_node, "runnable", tail_node)
    nested = getattr(runnable, "bound", runnable)
    nodes = getattr(nested, "nodes", None)
    if nodes is None:
        inner = getattr(runnable, "afunc", None) or getattr(runnable, "func", None)
        nodes = getattr(inner, "nodes", {})
    assert not {
        "coverage-repair",
        "coverage-repair-brief",
        "quality-recheck",
        "coverage-needed",
    }.intersection(nodes)
    assert "advance-coverage" in full.nodes


def test_same_case_graph_object_handles_initial_and_rework_inputs() -> None:
    features = _flow_features()
    intake = cast(IntakeGraphs, features["assurance.intake"])
    graphs = _product_graphs(features)
    assert graphs.entrypoints["full"].nodes["case"] is not None
    assert intake.case is not None
    assert set(graphs.entrypoints["full"].nodes).isdisjoint({"case-rework", "coverage-case"})
