from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Literal, cast

from langgraph.graph import END, START
from langgraph.graph.state import CompiledStateGraph
from pydantic import BaseModel, ConfigDict, Field

from graph_engine.boot.boot import CapabilityBuildContext
from graph_engine.plugin_api import FrozenModel
from graph_engine.stategraph import AttemptGraph, human_gate
from graph_engine.stategraph.routing import select_exclusive_route

from assurance_intake.contracts.workflow import CaseFlowResultV1
from assurance_intake.graphs.calls import (
    activation_review_round,
    publish_case_design,
    publish_case_review,
    select_case_design,
    select_case_repair,
    select_case_review,
)
from assurance_intake.graphs.state import IntakeState, as_int, terminal_failed
from assurance_intake.ops.case_design import op as case_design
from assurance_intake.ops.case_repair import op as case_repair
from assurance_intake.ops.case_review import op as case_review


class ReviewRoundAdvanceInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    rounds_used: int = Field(ge=0)
    rounds_budget: int = Field(ge=1)


class ReviewRoundAdvanceOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    rounds_used: int = Field(ge=1)
    rounds_budget: int = Field(ge=1)


def advance_review_round(data: object) -> ReviewRoundAdvanceOutput:
    payload = ReviewRoundAdvanceInput.model_validate(data)
    if payload.rounds_used >= payload.rounds_budget:
        raise ValueError("rounds_used must be below rounds_budget")
    return ReviewRoundAdvanceOutput(
        rounds_used=payload.rounds_used + 1,
        rounds_budget=payload.rounds_budget,
    )


def terminal_reviewed(state: Mapping[str, object]) -> dict[str, object]:
    result = CaseFlowResultV1.model_validate(
        {
            "status": "reviewed",
            "reviewed_case": state.get("reviewed_case"),
            "receipt": state.get("case_receipt"),
        }
    )
    return {**result.model_dump(mode="json"), "decision": "pass"}


def terminal_rejected(state: Mapping[str, object]) -> dict[str, object]:
    del state
    return {"status": "rejected", "decision": "reject", "reviewed_case": None, "case_receipt": None}


def terminal_exhausted(state: Mapping[str, object]) -> dict[str, object]:
    return {
        "status": "exhausted",
        "decision": "exhausted",
        "rounds_used": state.get("rounds_used", 0),
        "reviewed_case": None,
        "case_receipt": None,
    }


def review_round_advance(state: Mapping[str, object]) -> dict[str, object]:
    return advance_review_round(
        {"rounds_used": state["rounds_used"], "rounds_budget": state["rounds_budget"]}
    ).model_dump(mode="json")


HUMAN_REVIEW_ACTIONS = ("approve", "reject", "request_rework")


class HumanReviewDecision(FrozenModel):
    action: Literal["approve", "reject", "request_rework"]


def _human_review_payload(state: Mapping[str, object]) -> dict[str, object]:
    return {
        "reason": "needs_human_review",
        "actions": list(HUMAN_REVIEW_ACTIONS),
        "rounds_used": as_int(state["rounds_used"], name="rounds_used"),
        "rounds_budget": as_int(state["rounds_budget"], name="rounds_budget"),
    }


human_review = human_gate(_human_review_payload, decision=HumanReviewDecision)


def _has_budget(state: Mapping[str, object]) -> bool:
    return as_int(state["rounds_used"], name="rounds_used") < as_int(
        state["rounds_budget"], name="rounds_budget"
    )


def _is_review_repair(state: Mapping[str, object]) -> bool:
    return (
        state.get("decision") == "needs_fix"
        and state.get("auto_fix_allowed") is True
        and state.get("human_review_required") is not True
    )


def _named_matches(
    state: Mapping[str, object],
    table: Mapping[str, Callable[[Mapping[str, object]], bool]],
    *,
    blocked: bool = False,
) -> dict[str, str | None]:
    if blocked:
        return {target: None for target in table}
    return {target: target if predicate(state) else None for target, predicate in table.items()}


_CASE_REVIEW_OTHERWISE = "exhausted"
_CASE_REVIEW_TABLE: dict[str, Callable[[Mapping[str, object]], bool]] = {
    "done": lambda state: state.get("decision") == "pass" and state.get("human_review_required") is not True,
    "rejected": lambda state: (
        state.get("decision") == "reject" and state.get("human_review_required") is not True
    ),
    "human-review": lambda state: (
        state.get("decision") == "needs_human_review" or state.get("human_review_required") is True
    ),
    "review-round-advance": lambda state: _is_review_repair(state) and _has_budget(state),
}


def case_review_named_matches(state: Mapping[str, object]) -> dict[str, str | None]:
    return _named_matches(state, _CASE_REVIEW_TABLE, blocked=bool(state.get("attempt_failure")))


def route_case_review(state: Mapping[str, object]) -> str:
    return select_exclusive_route(case_review_named_matches(state), otherwise=_CASE_REVIEW_OTHERWISE)


_HUMAN_REVIEW_OTHERWISE = "exhausted"
_HUMAN_REVIEW_TABLE: dict[str, Callable[[Mapping[str, object]], bool]] = {
    "done": lambda state: state.get("human_action") == "approve",
    "rejected": lambda state: state.get("human_action") == "reject",
    "review-round-advance": lambda state: (
        state.get("human_action") == "request_rework" and _has_budget(state)
    ),
}


def human_review_named_matches(state: Mapping[str, object]) -> dict[str, str | None]:
    return _named_matches(state, _HUMAN_REVIEW_TABLE)


def route_human_review(state: Mapping[str, object]) -> str:
    return select_exclusive_route(human_review_named_matches(state), otherwise=_HUMAN_REVIEW_OTHERWISE)


_REVIEW_ROUND_OTHERWISE = "case-design"
_REVIEW_ROUND_TABLE: dict[str, Callable[[Mapping[str, object]], bool]] = {
    "case-repair": _is_review_repair,
}


def route_review_round(state: Mapping[str, object]) -> str:
    # The advance has already spent this round's budget; only the latest review outcome decides.
    return select_exclusive_route(
        _named_matches(state, _REVIEW_ROUND_TABLE),
        otherwise=_REVIEW_ROUND_OTHERWISE,
    )


def _node(fn: object) -> Callable[..., Any]:
    return cast(Callable[..., Any], fn)


def build_case_graph(context: CapabilityBuildContext) -> CompiledStateGraph:
    builder: AttemptGraph[IntakeState] = AttemptGraph(
        IntakeState,
        context,
        namespace="intake",
        activation=activation_review_round,
    )
    builder.add_attempt(
        "case-design",
        case_design,
        select=select_case_design,
        publish=publish_case_design,
    )
    builder.add_attempt(
        "case-review",
        case_review,
        select=select_case_review,
        publish=publish_case_review,
    )
    builder.add_attempt(
        "case-repair",
        case_repair,
        select=select_case_repair,
        publish=publish_case_design,
    )
    builder.add_node("review-round-advance", _node(review_round_advance))
    builder.add_node("human-review", _node(human_review))
    builder.add_node("done", _node(terminal_reviewed))
    builder.add_node("rejected", _node(terminal_rejected))
    builder.add_node("exhausted", _node(terminal_exhausted))
    builder.add_node("failed", _node(terminal_failed))
    builder.add_edge(START, "case-design")
    builder.add_attempt_edge("case-design", "case-review", on_failure="failed")
    builder.add_route(
        "case-review",
        route_case_review,
        targets=(*_CASE_REVIEW_TABLE, _CASE_REVIEW_OTHERWISE),
    )
    builder.add_route(
        "human-review",
        route_human_review,
        targets=(*_HUMAN_REVIEW_TABLE, _HUMAN_REVIEW_OTHERWISE),
    )
    builder.add_route(
        "review-round-advance",
        route_review_round,
        targets=(*_REVIEW_ROUND_TABLE, _REVIEW_ROUND_OTHERWISE),
    )
    builder.add_attempt_edge("case-repair", "case-review", on_failure="exhausted")
    for terminal in ("done", "rejected", "exhausted", "failed"):
        builder.add_edge(terminal, END)
    return builder.compile_subgraph()


__all__ = [
    "HUMAN_REVIEW_ACTIONS",
    "HumanReviewDecision",
    "ReviewRoundAdvanceInput",
    "ReviewRoundAdvanceOutput",
    "advance_review_round",
    "build_case_graph",
    "case_review_named_matches",
    "human_review",
    "human_review_named_matches",
    "review_round_advance",
    "route_case_review",
    "route_human_review",
    "route_review_round",
    "terminal_exhausted",
    "terminal_rejected",
    "terminal_reviewed",
]
