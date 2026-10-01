"""Human decision nodes built on LangGraph interrupt."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TypeVar

from langgraph.types import interrupt
from pydantic import BaseModel

DecisionT = TypeVar("DecisionT", bound=BaseModel)
HUMAN_ACTION_KEY = "human_action"


def coerce_action(decision: type[DecisionT], raw: object) -> DecisionT:
    if isinstance(raw, str):
        return decision.model_validate({"action": raw})
    if isinstance(raw, Mapping):
        return decision.model_validate({"action": raw.get("action", raw.get("decision"))})
    return decision.model_validate(raw)


def human_gate(
    payload: Callable[[Mapping[str, object]], Mapping[str, object]],
    *,
    decision: type[BaseModel],
) -> Callable[[Mapping[str, object]], dict[str, object]]:
    def gate(state: Mapping[str, object]) -> dict[str, object]:
        raw = interrupt(dict(payload(state)))
        return {HUMAN_ACTION_KEY: getattr(coerce_action(decision, raw), "action")}

    return gate
