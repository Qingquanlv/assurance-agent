from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, cast

from langgraph.graph import END, START
from langgraph.graph.state import CompiledStateGraph

from graph_engine.boot.boot import CapabilityBuildContext
from graph_engine.stategraph import AttemptGraph
from graph_engine.stategraph.attempt_graph import HasContractId
from graph_engine.stategraph.routing import select_exclusive_route

from assurance_improvement.contracts.attempts import TASK_ATTEMPT_CONTRACTS
from assurance_improvement.graphs.nodes import (
    _public_terminal,
    activation_one_shot,
    apply_human_interrupt,
    publish_apply_memory,
    publish_archive,
    publish_auto_review,
    publish_evaluate,
    publish_export,
    publish_human_review,
    publish_review,
    publish_rollback,
    select_apply_memory,
    select_auto_review,
    select_evaluate,
    select_export,
    select_human_review,
    select_rollback,
    select_skill,
    terminal_done,
    terminal_failed,
)
from assurance_improvement.graphs.state import ImprovementState
from assurance_improvement.ops.archive import op as archive
from assurance_improvement.ops.improvement_review import op as improvement_review

_AUTO_REVIEW = TASK_ATTEMPT_CONTRACTS["assurance.improvement.apply-improvement-auto-review"]
_HUMAN_REVIEW = TASK_ATTEMPT_CONTRACTS["assurance.improvement.apply-improvement-review"]
_EVALUATE = TASK_ATTEMPT_CONTRACTS["assurance.improvement.evaluate-memory-improvement"]
_EXPORT = TASK_ATTEMPT_CONTRACTS["assurance.improvement.export-change-improvement"]
_APPLY = TASK_ATTEMPT_CONTRACTS["assurance.improvement.apply-memory-improvement"]
_ROLLBACK = TASK_ATTEMPT_CONTRACTS["assurance.improvement.rollback-memory-improvement"]
_FAILED = "failed"
_COMMITTED_TARGETS = ("done", _FAILED)
_HUMAN_ACTIONS = frozenset({"approve", "reject", "request_rework", "supersede"})


def _node(fn: object) -> Callable[..., Any]:
    return cast(Callable[..., Any], fn)


def _named_matches(
    state: Mapping[str, object],
    table: Mapping[str, Callable[[Mapping[str, object]], bool]],
) -> dict[str, str | None]:
    return {target: target if predicate(state) else None for target, predicate in table.items()}


def terminal_rejected(state: Mapping[str, object]) -> dict[str, object]:
    return _public_terminal(state, "rejected")


def terminal_rework(state: Mapping[str, object]) -> dict[str, object]:
    return _public_terminal(state, "rework")


def terminal_superseded(state: Mapping[str, object]) -> dict[str, object]:
    return _public_terminal(state, "superseded")


def route_committed(state: Mapping[str, object]) -> str:
    if state.get("attempt_failure"):
        return _FAILED
    return select_exclusive_route({"done": "done"}, otherwise=_FAILED)


_AUTO_REVIEW_OTHERWISE = _FAILED
_AUTO_REVIEW_TABLE: dict[str, Callable[[Mapping[str, object]], bool]] = {
    "improvement.apply-evaluate": lambda state: state.get("lifecycle_state") == "approved",
    "rework": lambda state: state.get("lifecycle_state") == "needs_rework",
    "rejected": lambda state: state.get("lifecycle_state") == "rejected",
    "human-review": lambda state: state.get("lifecycle_state") == "proposed",
}


def route_auto_review(state: Mapping[str, object]) -> str:
    if state.get("attempt_failure"):
        return _AUTO_REVIEW_OTHERWISE
    return select_exclusive_route(
        _named_matches(state, _AUTO_REVIEW_TABLE),
        otherwise=_AUTO_REVIEW_OTHERWISE,
    )


_HUMAN_ACTION_OTHERWISE = _FAILED
_HUMAN_ACTION_TABLE: dict[str, Callable[[Mapping[str, object]], bool]] = {
    "improvement.apply-human-review": lambda state: state.get("human_action") in _HUMAN_ACTIONS,
}


def route_human_action(state: Mapping[str, object]) -> str:
    return select_exclusive_route(
        _named_matches(state, _HUMAN_ACTION_TABLE),
        otherwise=_HUMAN_ACTION_OTHERWISE,
    )


_HUMAN_RESULT_OTHERWISE = _FAILED
_HUMAN_RESULT_TABLE: dict[str, Callable[[Mapping[str, object]], bool]] = {
    "improvement.apply-evaluate": lambda state: state.get("lifecycle_state") == "approved",
    "rejected": lambda state: state.get("lifecycle_state") == "rejected",
    "rework": lambda state: state.get("lifecycle_state") == "needs_rework",
    "superseded": lambda state: state.get("lifecycle_state") == "superseded",
}


def route_human_review_result(state: Mapping[str, object]) -> str:
    if state.get("attempt_failure"):
        return _HUMAN_RESULT_OTHERWISE
    return select_exclusive_route(
        _named_matches(state, _HUMAN_RESULT_TABLE),
        otherwise=_HUMAN_RESULT_OTHERWISE,
    )


_APPLY_EVALUATE_OTHERWISE = _FAILED
_APPLY_EVALUATE_TABLE: dict[str, Callable[[Mapping[str, object]], bool]] = {
    "improvement.apply": lambda state: state.get("outcome") == "passed",
}


def route_apply_evaluate(state: Mapping[str, object]) -> str:
    if state.get("attempt_failure"):
        return _APPLY_EVALUATE_OTHERWISE
    return select_exclusive_route(
        _named_matches(state, _APPLY_EVALUATE_TABLE),
        otherwise=_APPLY_EVALUATE_OTHERWISE,
    )


def _add_shared_terminals(builder: AttemptGraph[ImprovementState]) -> None:
    builder.add_node("done", _node(terminal_done))
    builder.add_node("failed", _node(terminal_failed))
    builder.add_edge("done", END)
    builder.add_edge("failed", END)


def _build_one_shot(
    context: CapabilityBuildContext,
    *,
    contract_id: str | HasContractId,
    semantic_node_id: str,
    select: object,
    publish: object,
) -> CompiledStateGraph:
    builder: AttemptGraph[ImprovementState] = AttemptGraph(
        ImprovementState,
        context,
        namespace="improvement",
        activation=activation_one_shot,
    )
    builder.add_attempt(
        semantic_node_id,
        contract_id,
        select=select,
        publish=publish,
        semantic_node_id=semantic_node_id,
    )
    _add_shared_terminals(builder)
    builder.add_edge(START, semantic_node_id)
    builder.add_route(semantic_node_id, route_committed, targets=_COMMITTED_TARGETS)
    return builder.compile_subgraph()


def build_archive_graph(context: CapabilityBuildContext) -> CompiledStateGraph:
    return _build_one_shot(
        context,
        contract_id=archive,
        semantic_node_id="improvement.archive",
        select=select_skill,
        publish=publish_archive,
    )


def build_review_graph(context: CapabilityBuildContext) -> CompiledStateGraph:
    return _build_one_shot(
        context,
        contract_id=improvement_review,
        semantic_node_id="improvement.review",
        select=select_skill,
        publish=publish_review,
    )


def build_evaluate_graph(context: CapabilityBuildContext) -> CompiledStateGraph:
    return _build_one_shot(
        context,
        contract_id=_EVALUATE,
        semantic_node_id="improvement.evaluate",
        select=select_evaluate,
        publish=publish_evaluate,
    )


def build_export_graph(context: CapabilityBuildContext) -> CompiledStateGraph:
    return _build_one_shot(
        context,
        contract_id=_EXPORT,
        semantic_node_id="improvement.export",
        select=select_export,
        publish=publish_export,
    )


def build_rollback_graph(context: CapabilityBuildContext) -> CompiledStateGraph:
    return _build_one_shot(
        context,
        contract_id=_ROLLBACK,
        semantic_node_id="improvement.rollback",
        select=select_rollback,
        publish=publish_rollback,
    )


def build_apply_graph(context: CapabilityBuildContext) -> CompiledStateGraph:
    builder: AttemptGraph[ImprovementState] = AttemptGraph(
        ImprovementState,
        context,
        namespace="improvement",
        activation=activation_one_shot,
    )
    builder.add_attempt(
        "improvement.apply-auto-review",
        _AUTO_REVIEW,
        select=select_auto_review,
        publish=publish_auto_review,
        semantic_node_id="improvement.apply-auto-review",
    )
    builder.add_node("human-review", _node(apply_human_interrupt))
    builder.add_attempt(
        "improvement.apply-human-review",
        _HUMAN_REVIEW,
        select=select_human_review,
        publish=publish_human_review,
        semantic_node_id="improvement.apply-human-review",
    )
    builder.add_attempt(
        "improvement.apply-evaluate",
        _EVALUATE,
        select=select_evaluate,
        publish=publish_evaluate,
        semantic_node_id="improvement.apply-evaluate",
    )
    builder.add_attempt(
        "improvement.apply",
        _APPLY,
        select=select_apply_memory,
        publish=publish_apply_memory,
        semantic_node_id="improvement.apply",
    )
    builder.add_node("rejected", _node(terminal_rejected))
    builder.add_node("rework", _node(terminal_rework))
    builder.add_node("superseded", _node(terminal_superseded))
    _add_shared_terminals(builder)
    builder.add_edge(START, "improvement.apply-auto-review")
    builder.add_route(
        "improvement.apply-auto-review",
        route_auto_review,
        targets=(*_AUTO_REVIEW_TABLE, _AUTO_REVIEW_OTHERWISE),
    )
    builder.add_route(
        "human-review",
        route_human_action,
        targets=(*_HUMAN_ACTION_TABLE, _HUMAN_ACTION_OTHERWISE),
    )
    builder.add_route(
        "improvement.apply-human-review",
        route_human_review_result,
        targets=(*_HUMAN_RESULT_TABLE, _HUMAN_RESULT_OTHERWISE),
    )
    builder.add_route(
        "improvement.apply-evaluate",
        route_apply_evaluate,
        targets=(*_APPLY_EVALUATE_TABLE, _APPLY_EVALUATE_OTHERWISE),
    )
    builder.add_route("improvement.apply", route_committed, targets=_COMMITTED_TARGETS)
    builder.add_edge("rejected", END)
    builder.add_edge("rework", END)
    builder.add_edge("superseded", END)
    return builder.compile_subgraph()


__all__ = [
    "build_apply_graph",
    "build_archive_graph",
    "build_evaluate_graph",
    "build_export_graph",
    "build_review_graph",
    "build_rollback_graph",
    "route_apply_evaluate",
    "route_auto_review",
    "route_committed",
    "route_human_action",
    "route_human_review_result",
    "terminal_rejected",
    "terminal_rework",
    "terminal_superseded",
]
