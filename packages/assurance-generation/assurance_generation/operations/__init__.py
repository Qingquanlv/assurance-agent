from collections.abc import Mapping
from types import MappingProxyType

from graph_engine.plugin_api import TaskHandler

from assurance_generation.operations.planning import (
    FAMILIES,
    PlanFinalizeHandler,
    PlanPrepareHandler,
    planning_handler,
)
from assurance_generation.operations.review import (
    PlanReviewFinalizeHandler,
    PlanReviewPrepareHandler,
    review_finalize_handler,
    review_prepare_handler,
)


def planning_handlers() -> Mapping[str, TaskHandler]:
    handlers: dict[str, TaskHandler] = {}
    for family in FAMILIES:
        handlers[f"assurance.generation.{family}.plan.prepare"] = PlanPrepareHandler(family)
        handlers[f"assurance.generation.{family}.plan.finalize"] = PlanFinalizeHandler(family)
        handlers[f"assurance.generation.{family}.plan-review.prepare"] = PlanReviewPrepareHandler(family)
        handlers[f"assurance.generation.{family}.plan-review.finalize"] = PlanReviewFinalizeHandler(family)
    return MappingProxyType(handlers)


__all__ = [
    "FAMILIES",
    "PlanFinalizeHandler",
    "PlanPrepareHandler",
    "PlanReviewFinalizeHandler",
    "PlanReviewPrepareHandler",
    "planning_handler",
    "planning_handlers",
    "review_finalize_handler",
    "review_prepare_handler",
]
