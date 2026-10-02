from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, cast

from langgraph.graph import END, START
from langgraph.graph.state import CompiledStateGraph

from assurance_generation.graphs.nodes import (
    activation_codegen,
    activation_codegen_review,
    human_review,
    plan_round_join,
    publish_codegen,
    publish_codegen_review,
    review_round_advance,
    select_codegen,
    select_codegen_review,
)
from assurance_generation.graphs.state import FamilyLaneOutput, GenerationState, make_family_lane_result
from assurance_generation.ops.api_codegen import op as api_codegen
from assurance_generation.ops.api_codegen_review import op as api_codegen_review
from assurance_generation.ops.e2e_codegen import op as e2e_codegen
from assurance_generation.ops.e2e_codegen_review import op as e2e_codegen_review
from assurance_generation.ops.fuzz_codegen import op as fuzz_codegen
from assurance_generation.ops.fuzz_codegen_review import op as fuzz_codegen_review
from assurance_generation.ops.performance_codegen import op as performance_codegen
from assurance_generation.ops.performance_codegen_review import op as performance_codegen_review
from graph_engine.boot.boot import CapabilityBuildContext
from graph_engine.stategraph import AttemptGraph
from graph_engine.stategraph.routing import select_exclusive_route


def _as_int(value: object, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an int")
    return value


def _family_result(state: Mapping[str, object], *, status: str, selected: bool) -> dict[str, object]:
    family = state.get("family")
    if not isinstance(family, str) or not family:
        raise ValueError("family lane result requires family")
    result = {
        "family_results": [
            make_family_lane_result(
                coverage_epoch=_as_int(state.get("coverage_epoch", 0), name="coverage_epoch"),
                family=family,
                receipt_id=f"receipt-{family}",
                selected=selected,
                status=status,
            )
        ]
    }
    if selected and status == "passed":
        codegen = state.get("codegen_output")
        receipt = state.get("codegen_receipt")
        if isinstance(codegen, Mapping) and isinstance(receipt, Mapping):
            lane = result["family_results"][0]
            lane["receipt_id"] = str(receipt["receipt_id"])
            lane["generated"] = {
                "family": family,
                "coverage_epoch": state.get("coverage_epoch", 0),
                "plan_files": state.get("plan_files", []),
                "files": codegen.get("files", []),
                "mapping": codegen.get("mapping"),
                "receipt": dict(receipt),
                "method_plans": codegen.get("method_plans") or [],
                "semantic_reviews": state.get("semantic_reviews") or [],
            }
    return {"family_results": result["family_results"]}


def terminal_done(state: Mapping[str, object]) -> dict[str, object]:
    status = "failed" if state.get("attempt_failure") else "passed"
    update: dict[str, object] = {
        "status": status,
        "decision": state.get("decision", "pass") if status == "passed" else "failed",
    }
    if isinstance(state.get("family"), str) and state.get("family"):
        update.update(_family_result(state, status=status, selected=True))
    return update


def terminal_rejected(state: Mapping[str, object]) -> dict[str, object]:
    return {
        "status": "rejected",
        "decision": "reject",
        **_family_result(state, status="rejected", selected=True),
    }


def terminal_exhausted(state: Mapping[str, object]) -> dict[str, object]:
    return {
        "status": "exhausted",
        "decision": "exhausted",
        "rounds_used": state.get("rounds_used", 0),
        **_family_result(state, status="exhausted", selected=True),
    }


def terminal_skipped(state: Mapping[str, object]) -> dict[str, object]:
    return {
        "status": "skipped",
        **_family_result(state, status="skipped", selected=False),
    }


def _has_budget(state: Mapping[str, object]) -> bool:
    used = state.get("rounds_used", 0)
    budget = state.get("rounds_budget", 0)
    return isinstance(used, int) and isinstance(budget, int) and used < budget


def _within_spent_budget(state: Mapping[str, object]) -> bool:
    used = state.get("rounds_used", 0)
    budget = state.get("rounds_budget", 0)
    return isinstance(used, int) and isinstance(budget, int) and used <= budget


def _named_matches(
    state: Mapping[str, object],
    table: Mapping[str, Callable[[Mapping[str, object]], bool]],
) -> dict[str, str | None]:
    return {target: target if predicate(state) else None for target, predicate in table.items()}


_FAMILY_ENTRY_OTHERWISE = "skip"
_FAMILY_ENTRY_TABLE: dict[str, Callable[[Mapping[str, object]], bool]] = {
    "codegen": lambda state: bool(state.get("lane_selected")),
}


def family_entry_named_matches(state: Mapping[str, object]) -> dict[str, str | None]:
    return _named_matches(state, _FAMILY_ENTRY_TABLE)


def route_family_entry(state: Mapping[str, object]) -> str:
    return select_exclusive_route(family_entry_named_matches(state), otherwise=_FAMILY_ENTRY_OTHERWISE)


_PLAN_REVIEW_OTHERWISE = "exhausted"
_PLAN_REVIEW_TABLE: dict[str, Callable[[Mapping[str, object]], bool]] = {
    "done": lambda state: state.get("route") == "codegen",
    "codegen-review-round-advance": lambda state: state.get("route") == "auto_fix" and _has_budget(state),
    "codegen-human-review": lambda state: state.get("route") == "human",
    "rejected": lambda state: state.get("route") == "reject",
}


def plan_review_named_matches(state: Mapping[str, object]) -> dict[str, str | None]:
    return _named_matches(state, _PLAN_REVIEW_TABLE)


def route_plan_review(state: Mapping[str, object]) -> str:
    return select_exclusive_route(plan_review_named_matches(state), otherwise=_PLAN_REVIEW_OTHERWISE)


_PLAN_HUMAN_REVIEW_OTHERWISE = "exhausted"
_PLAN_HUMAN_REVIEW_TABLE: dict[str, Callable[[Mapping[str, object]], bool]] = {
    "done": lambda state: state.get("human_action") == "approve",
    "rejected": lambda state: state.get("human_action") == "reject",
    "codegen-review-round-advance": lambda state: (
        state.get("human_action") == "request_rework" and _has_budget(state)
    ),
}


def plan_human_review_named_matches(state: Mapping[str, object]) -> dict[str, str | None]:
    return _named_matches(state, _PLAN_HUMAN_REVIEW_TABLE)


def route_plan_human_review(state: Mapping[str, object]) -> str:
    return select_exclusive_route(
        plan_human_review_named_matches(state), otherwise=_PLAN_HUMAN_REVIEW_OTHERWISE
    )


_PLAN_ADVANCE_OTHERWISE = "exhausted"
_PLAN_ADVANCE_TABLE: dict[str, Callable[[Mapping[str, object]], bool]] = {
    "codegen-round-join": _within_spent_budget,
}


def plan_advance_named_matches(state: Mapping[str, object]) -> dict[str, str | None]:
    return _named_matches(state, _PLAN_ADVANCE_TABLE)


def route_plan_advance(state: Mapping[str, object]) -> str:
    return select_exclusive_route(plan_advance_named_matches(state), otherwise=_PLAN_ADVANCE_OTHERWISE)


def _node(fn: object) -> Callable[..., Any]:
    return cast(Callable[..., Any], fn)


_FAMILY_STAGE_OPS = {
    ("api", "codegen"): api_codegen,
    ("api", "codegen-review"): api_codegen_review,
    ("e2e", "codegen"): e2e_codegen,
    ("e2e", "codegen-review"): e2e_codegen_review,
    ("fuzz", "codegen"): fuzz_codegen,
    ("fuzz", "codegen-review"): fuzz_codegen_review,
    ("performance", "codegen"): performance_codegen,
    ("performance", "codegen-review"): performance_codegen_review,
}


def _assemble_family_graph(
    context: CapabilityBuildContext,
    *,
    family: str,
    output_schema: type[FamilyLaneOutput] | type[GenerationState] | None,
) -> CompiledStateGraph:
    if output_schema is None:
        builder: AttemptGraph[GenerationState] = AttemptGraph(
            GenerationState,
            context,
            namespace="generation",
        )
    else:
        builder = AttemptGraph(
            GenerationState,
            context,
            namespace="generation",
            output_schema=output_schema,
        )
    builder.add_attempt(
        "codegen",
        _FAMILY_STAGE_OPS[(family, "codegen")],
        select=select_codegen,
        publish=publish_codegen,
        activation=activation_codegen,
        semantic_node_id=f"generation.{family}.codegen",
    )
    builder.add_attempt(
        "codegen-review",
        _FAMILY_STAGE_OPS[(family, "codegen-review")],
        select=select_codegen_review,
        publish=publish_codegen_review,
        activation=activation_codegen_review,
        semantic_node_id=f"generation.{family}.codegen-review",
    )
    builder.add_node("codegen-review-round-advance", _node(review_round_advance))
    builder.add_node("codegen-round-join", _node(plan_round_join))
    builder.add_node("codegen-human-review", _node(human_review))
    builder.add_node("skip", _node(terminal_skipped))
    builder.add_node("done", _node(terminal_done))
    builder.add_node("rejected", _node(terminal_rejected))
    builder.add_node("exhausted", _node(terminal_exhausted))
    builder.add_route(
        START,
        route_family_entry,
        targets=(*_FAMILY_ENTRY_TABLE, _FAMILY_ENTRY_OTHERWISE),
    )
    builder.add_attempt_edge("codegen", "codegen-review", on_failure="done")
    builder.add_route(
        "codegen-review",
        route_plan_review,
        targets=(*_PLAN_REVIEW_TABLE, _PLAN_REVIEW_OTHERWISE),
        on_failure="done",
    )
    builder.add_route(
        "codegen-human-review",
        route_plan_human_review,
        targets=(*_PLAN_HUMAN_REVIEW_TABLE, _PLAN_HUMAN_REVIEW_OTHERWISE),
    )
    builder.add_route(
        "codegen-review-round-advance",
        route_plan_advance,
        targets=(*_PLAN_ADVANCE_TABLE, _PLAN_ADVANCE_OTHERWISE),
    )
    builder.add_edge("codegen-round-join", "codegen")
    builder.add_edge("skip", END)
    builder.add_edge("done", END)
    builder.add_edge("rejected", END)
    builder.add_edge("exhausted", END)
    return builder.compile_subgraph()


def compile_family_pair(
    context: CapabilityBuildContext,
    family: str,
) -> tuple[CompiledStateGraph, CompiledStateGraph]:
    standalone = _assemble_family_graph(context, family=family, output_schema=None)
    lane = _assemble_family_graph(context, family=family, output_schema=FamilyLaneOutput)
    return standalone, lane


def compile_family_graph(
    context: CapabilityBuildContext,
    family: str,
) -> CompiledStateGraph:
    return compile_family_pair(context, family)[0]


def build_api_graph(context: CapabilityBuildContext) -> CompiledStateGraph:
    return compile_family_graph(context, "api")


__all__ = [
    "build_api_graph",
    "compile_family_graph",
    "compile_family_pair",
    "family_entry_named_matches",
    "plan_advance_named_matches",
    "plan_human_review_named_matches",
    "plan_review_named_matches",
    "route_family_entry",
    "route_plan_advance",
    "route_plan_human_review",
    "route_plan_review",
    "terminal_done",
    "terminal_exhausted",
    "terminal_rejected",
    "terminal_skipped",
]
