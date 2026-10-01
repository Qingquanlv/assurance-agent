from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, cast

from langgraph.graph import END, START
from langgraph.graph.state import CompiledStateGraph

from graph_engine.boot.boot import CapabilityBuildContext
from graph_engine.stategraph import AttemptGraph
from graph_engine.stategraph.routing import select_exclusive_route

from assurance_healing.graphs.nodes import (
    activation_repair,
    admit_passthrough,
    advance_repair_round_node,
    proposal_approval,
    publish_applied_repair,
    publish_proposal,
    select_application,
    select_failure,
    terminal_done,
    terminal_exhausted,
    terminal_failed,
    terminal_needs_review,
    terminal_not_eligible,
)
from assurance_healing.graphs.state import HealingState

_FIX_PROPOSAL_ID = "assurance.healing.agent.fix-proposal.v1"
_APPLICATION_ID = "assurance.healing.agent.apply-test-repair.v1"
_FIX_PROPOSAL_NODE = "healing.fix-proposal"
_APPLICATION_NODE = "healing.apply-test-repair"
_ADMIT_OTHERWISE = "not-eligible"
_ADMIT_ADVANCE = "repair-round-advance"
_ADMIT_EXHAUSTED = "exhausted"
_ADMIT_TARGETS = (_ADMIT_ADVANCE, _ADMIT_EXHAUSTED, _ADMIT_OTHERWISE)
_STATUS_OTHERWISE = "failed"
_DONE = "done"
_NEEDS_REVIEW = "needs-review"
_NOT_ELIGIBLE = "not-eligible"
_FAILURE_STATUS_TARGETS = (_DONE, _NEEDS_REVIEW, _ADMIT_EXHAUSTED, _NOT_ELIGIBLE, _STATUS_OTHERWISE)
_PROPOSAL_STATUS_OTHERWISE = _STATUS_OTHERWISE
_PROPOSAL_APPROVAL_OTHERWISE = _NEEDS_REVIEW
_PROPOSAL_APPROVAL_TARGETS = (_APPLICATION_NODE, _PROPOSAL_APPROVAL_OTHERWISE)
_FAILURE_ELIGIBLE = frozenset({"test", "test-data"})


def _node(fn: object) -> Callable[..., Any]:
    return cast(Callable[..., Any], fn)


def _as_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _exhausted(state: Mapping[str, object]) -> bool:
    used = _as_int(state.get("rounds_used"))
    budget = _as_int(state.get("rounds_budget"))
    if used is None or budget is None:
        return False
    return used >= budget


def _failure_eligible(state: Mapping[str, object]) -> bool:
    used = _as_int(state.get("rounds_used"))
    budget = _as_int(state.get("rounds_budget"))
    if used is None or budget is None:
        return False
    return (
        state.get("classification") in _FAILURE_ELIGIBLE
        and state.get("fix_eligible") is True
        and used < budget
    )


def _proposal_needs_approval(state: Mapping[str, object]) -> bool:
    return not state.get("attempt_failure")


def _proposal_approved(state: Mapping[str, object]) -> bool:
    return state.get("human_action") == "approve" and isinstance(state.get("approval_ref"), Mapping)


_ADMIT_FAILURE_TABLE: dict[str, Callable[[Mapping[str, object]], bool]] = {
    "advance": _failure_eligible,
    "exhausted": _exhausted,
}
_ADMIT_FAILURE_NODES = {
    "advance": _ADMIT_ADVANCE,
    "exhausted": _ADMIT_EXHAUSTED,
}
_PROPOSAL_STATUS_TABLE: dict[str, Callable[[Mapping[str, object]], bool]] = {
    "approval": _proposal_needs_approval,
}
_PROPOSAL_APPROVAL_TABLE: dict[str, Callable[[Mapping[str, object]], bool]] = {
    "apply": _proposal_approved,
    "review": lambda state: not _proposal_approved(state),
}
_PROPOSAL_APPROVAL_NODES = {
    "apply": _APPLICATION_NODE,
    "review": _NEEDS_REVIEW,
}


def admit_failure_named_matches(state: Mapping[str, object]) -> dict[str, str | None]:
    return {
        label: _ADMIT_FAILURE_NODES[label] if predicate(state) else None
        for label, predicate in _ADMIT_FAILURE_TABLE.items()
    }


def route_admit_failure(state: Mapping[str, object]) -> str:
    return select_exclusive_route(admit_failure_named_matches(state), otherwise=_ADMIT_OTHERWISE)


def route_proposal_status(state: Mapping[str, object]) -> str:
    return select_exclusive_route(
        {
            target: target if predicate(state) else None
            for target, predicate in _PROPOSAL_STATUS_TABLE.items()
        },
        otherwise=_PROPOSAL_STATUS_OTHERWISE,
    )


def route_proposal_approval(state: Mapping[str, object]) -> str:
    return select_exclusive_route(
        {
            label: _PROPOSAL_APPROVAL_NODES[label] if predicate(state) else None
            for label, predicate in _PROPOSAL_APPROVAL_TABLE.items()
        },
        otherwise=_PROPOSAL_APPROVAL_OTHERWISE,
    )


def failure_status_named_matches(state: Mapping[str, object]) -> dict[str, str | None]:
    current = state.get("status")
    return {
        "applied": _DONE if current == "applied" else None,
        "needs_review": _NEEDS_REVIEW if current == "needs_review" else None,
        "not_eligible": _NOT_ELIGIBLE if current == "not_eligible" else None,
        "exhausted": _ADMIT_EXHAUSTED if current == "exhausted" else None,
        "failed": _STATUS_OTHERWISE if current == "failed" else None,
    }


def route_failure_status(state: Mapping[str, object]) -> str:
    if state.get("attempt_failure"):
        failure = state.get("attempt_failure")
        if isinstance(failure, Mapping) and failure.get("kind") == "invalid_output":
            return _NEEDS_REVIEW
        return _STATUS_OTHERWISE
    return select_exclusive_route(failure_status_named_matches(state), otherwise=_STATUS_OTHERWISE)


def _add_shared_terminals(builder: AttemptGraph[HealingState]) -> None:
    builder.add_node("admit", _node(admit_passthrough))
    builder.add_node("repair-round-advance", _node(advance_repair_round_node))
    builder.add_node("done", _node(terminal_done))
    builder.add_node("exhausted", _node(terminal_exhausted))
    builder.add_node("not-eligible", _node(terminal_not_eligible))
    builder.add_node("failed", _node(terminal_failed))
    builder.add_edge(START, "admit")
    builder.add_edge("done", END)
    builder.add_edge("exhausted", END)
    builder.add_edge("not-eligible", END)
    builder.add_edge("failed", END)


def build_repair_failure_graph(context: CapabilityBuildContext) -> CompiledStateGraph:
    builder: AttemptGraph[HealingState] = AttemptGraph(
        HealingState,
        context,
        namespace="healing",
        activation=activation_repair,
    )
    _add_shared_terminals(builder)
    builder.add_node("needs-review", _node(terminal_needs_review))
    builder.add_node("approval", _node(proposal_approval))
    builder.add_edge("needs-review", END)
    builder.add_attempt(
        _FIX_PROPOSAL_NODE,
        _FIX_PROPOSAL_ID,
        select=select_failure,
        publish=publish_proposal,
        activation=activation_repair,
        semantic_node_id=_FIX_PROPOSAL_NODE,
    )
    builder.add_attempt(
        _APPLICATION_NODE,
        _APPLICATION_ID,
        select=select_application,
        publish=publish_applied_repair,
        activation=activation_repair,
        semantic_node_id=_APPLICATION_NODE,
    )
    builder.add_route("admit", route_admit_failure, targets=_ADMIT_TARGETS)
    builder.add_edge("repair-round-advance", _FIX_PROPOSAL_NODE)
    builder.add_route(
        _FIX_PROPOSAL_NODE,
        route_proposal_status,
        targets=(*_PROPOSAL_STATUS_TABLE, _PROPOSAL_STATUS_OTHERWISE),
    )
    builder.add_route("approval", route_proposal_approval, targets=_PROPOSAL_APPROVAL_TARGETS)
    builder.add_route(_APPLICATION_NODE, route_failure_status, targets=_FAILURE_STATUS_TARGETS)
    return builder.compile_subgraph()


__all__ = [
    "admit_failure_named_matches",
    "build_repair_failure_graph",
    "failure_status_named_matches",
    "route_admit_failure",
    "route_failure_status",
    "route_proposal_approval",
    "route_proposal_status",
]
