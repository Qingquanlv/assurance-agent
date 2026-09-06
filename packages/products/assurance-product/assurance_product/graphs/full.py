from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Literal, cast

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from assurance_intake.contracts.workflow import CaseFlowResultV1
from assurance_product.graphs.entrypoints import (
    adapt_case,
    adapt_prepare,
    publish_public_output,
    validate_public_input,
)
from assurance_product.graphs.execute import adapt_execute_tail_input
from assurance_product.graphs.loop_state import advance_coverage, can_reenter_case
from assurance_product.graphs.routes import route_prepare
from assurance_product.graphs.state import ProductState
from assurance_product.graphs.tail_contracts import ExecuteTailResultV1
from assurance_product.models import BusinessBudgetsV1
from graph_engine.boot.boot import GraphBuildContext


def adapt_execute_tail(state: ProductState) -> dict[str, object]:
    return adapt_execute_tail_input(state, standalone=False)


def route_case_result(state: Mapping[str, object]) -> Literal["reviewed", "failed"]:
    try:
        result = CaseFlowResultV1.model_validate(
            {
                "status": state.get("status"),
                "reviewed_case": state.get("reviewed_case"),
                "receipt": state.get("case_receipt", state.get("receipt")),
            }
        )
    except (TypeError, ValueError):
        return "failed"
    if (
        result.status == "reviewed"
        and result.reviewed_case is not None
        and result.reviewed_case.change_id == state.get("change_id")
        and result.reviewed_case.coverage_epoch == state.get("coverage_epoch", 0)
    ):
        return "reviewed"
    return "failed"


def route_full_tail(
    state: Mapping[str, object],
) -> Literal["achieved", "advance-coverage", "not-achieved"]:
    try:
        result = ExecuteTailResultV1.model_validate(state.get("tail_result"))
    except (TypeError, ValueError):
        return "not-achieved"
    if result.status == "reported":
        return "achieved"
    if result.status != "coverage_insufficient" or result.inspection is None:
        return "not-achieved"
    try:
        budgets = BusinessBudgetsV1.model_validate(state.get("budgets"))
        epoch = state.get("coverage_epoch", 0)
        if isinstance(epoch, bool) or not isinstance(epoch, int):
            return "not-achieved"
        return (
            "advance-coverage"
            if result.inspection.coverage_epoch == epoch
            and can_reenter_case(coverage_epoch=epoch, budgets=budgets)
            else "not-achieved"
        )
    except (TypeError, ValueError):
        return "not-achieved"


def _terminal_achieved(state: ProductState) -> dict[str, object]:
    published = publish_public_output(cast(ProductState, {**dict(state), "status": "completed"}))
    return {**published, "terminal": {"status": "completed", "reason": "achieved"}, "status": "completed"}


def _terminal_not_achieved(state: ProductState) -> dict[str, object]:
    published = publish_public_output(cast(ProductState, {**dict(state), "status": "failed"}))
    return {**published, "terminal": {"status": "failed", "reason": "not_achieved"}, "status": "failed"}


def build_full_graph(bundles: object, execute: CompiledStateGraph) -> StateGraph[ProductState]:
    typed = cast(Any, bundles)
    builder: StateGraph[ProductState] = StateGraph(ProductState)
    builder.add_node("validate", validate_public_input("full"))
    builder.add_node("adapt-prepare", cast(Any, adapt_prepare))
    builder.add_node("prepare", typed.intake.prepare)
    builder.add_node("adapt-case", cast(Any, adapt_case))
    builder.add_node("case", typed.intake.case)
    builder.add_node("advance-coverage", cast(Any, advance_coverage))
    builder.add_node("adapt-execute-tail", cast(Any, adapt_execute_tail))
    builder.add_node("execute-tail", execute)
    builder.add_node("achieved", cast(Any, _terminal_achieved))
    builder.add_node("not-achieved", cast(Any, _terminal_not_achieved))
    builder.add_edge(START, "validate")
    builder.add_edge("validate", "adapt-prepare")
    builder.add_edge("adapt-prepare", "prepare")
    builder.add_conditional_edges(
        "prepare",
        cast(Callable[..., Any], route_prepare),
        {"prepared": "adapt-case", "failed": "not-achieved"},
    )
    builder.add_edge("adapt-case", "case")
    builder.add_edge("advance-coverage", "adapt-case")
    builder.add_conditional_edges(
        "case",
        cast(Callable[..., Any], route_case_result),
        {"reviewed": "adapt-execute-tail", "failed": "not-achieved"},
    )
    builder.add_edge("adapt-execute-tail", "execute-tail")
    builder.add_conditional_edges(
        "execute-tail",
        cast(Callable[..., Any], route_full_tail),
        {
            "achieved": "achieved",
            "advance-coverage": "advance-coverage",
            "not-achieved": "not-achieved",
        },
    )
    builder.add_edge("achieved", END)
    builder.add_edge("not-achieved", END)
    return builder


def build_full_root(
    context: GraphBuildContext,
    bundles: object,
    execute: CompiledStateGraph,
) -> CompiledStateGraph:
    return context.compile_root(build_full_graph(bundles, execute))


__all__ = [
    "adapt_execute_tail",
    "build_full_graph",
    "build_full_root",
    "route_case_result",
    "route_full_tail",
]
