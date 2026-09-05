from collections.abc import Mapping
from types import MappingProxyType

from graph_engine.plugin_api import TaskHandler

from assurance_generation.operations.codegen import (
    FIX_FAMILIES,
    CodegenFinalizeHandler,
    CodegenFixFinalizeHandler,
    CodegenFixPrepareHandler,
    CodegenPrepareHandler,
    codegen_finalize_handler,
    codegen_fix_finalize_handler,
    codegen_fix_prepare_handler,
    codegen_prepare_handler,
)
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
from assurance_generation.operations.workflow_state import (
    GENERATION_COMPLETE_ID,
    REVIEW_ROUND_ADVANCE_ID,
    GenerationCompleteHandler,
    GenerationReviewRoundAdvanceHandler,
)
from assurance_generation.operations.resolve_inputs import ResolveGenerationInputsHandler


def planning_handlers() -> Mapping[str, TaskHandler]:
    handlers: dict[str, TaskHandler] = {}
    for family in FAMILIES:
        handlers[f"assurance.generation.{family}.plan.prepare"] = PlanPrepareHandler(family)
        handlers[f"assurance.generation.{family}.plan.finalize"] = PlanFinalizeHandler(family)
        handlers[f"assurance.generation.{family}.plan-review.prepare"] = PlanReviewPrepareHandler(family)
        handlers[f"assurance.generation.{family}.plan-review.finalize"] = PlanReviewFinalizeHandler(family)
    return MappingProxyType(handlers)


def codegen_handlers() -> Mapping[str, TaskHandler]:
    handlers: dict[str, TaskHandler] = {}
    for family in FAMILIES:
        handlers[f"assurance.generation.{family}.codegen.prepare"] = CodegenPrepareHandler(family)
        handlers[f"assurance.generation.{family}.codegen.finalize"] = CodegenFinalizeHandler(family)
    for family in FIX_FAMILIES:
        handlers[f"assurance.generation.{family}.codegen-fix.prepare"] = CodegenFixPrepareHandler(family)
        handlers[f"assurance.generation.{family}.codegen-fix.finalize"] = CodegenFixFinalizeHandler(family)
    return MappingProxyType(handlers)


def generation_handlers() -> Mapping[str, TaskHandler]:
    return MappingProxyType(
        {
            **planning_handlers(),
            **codegen_handlers(),
            GENERATION_COMPLETE_ID: GenerationCompleteHandler(),
            REVIEW_ROUND_ADVANCE_ID: GenerationReviewRoundAdvanceHandler(),
            "assurance.generation.resolve-inputs.execute": ResolveGenerationInputsHandler(),
        }
    )


__all__ = [
    "FAMILIES",
    "FIX_FAMILIES",
    "GENERATION_COMPLETE_ID",
    "GenerationCompleteHandler",
    "GenerationReviewRoundAdvanceHandler",
    "REVIEW_ROUND_ADVANCE_ID",
    "ResolveGenerationInputsHandler",
    "CodegenFinalizeHandler",
    "CodegenFixFinalizeHandler",
    "CodegenFixPrepareHandler",
    "CodegenPrepareHandler",
    "PlanFinalizeHandler",
    "PlanPrepareHandler",
    "PlanReviewFinalizeHandler",
    "PlanReviewPrepareHandler",
    "codegen_finalize_handler",
    "codegen_fix_finalize_handler",
    "codegen_fix_prepare_handler",
    "codegen_handlers",
    "codegen_prepare_handler",
    "generation_handlers",
    "planning_handler",
    "planning_handlers",
    "review_finalize_handler",
    "review_prepare_handler",
]
