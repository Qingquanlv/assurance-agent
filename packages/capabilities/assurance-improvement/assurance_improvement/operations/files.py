"""Stage and read the named files that connect retro and apply steps."""

from __future__ import annotations

from typing import Annotated, TypeVar

from pydantic import BaseModel, Field, TypeAdapter

from agent_runtime_contracts.ops import InputError, PrepareContext
from graph_engine.artifacts import ArtifactReadError, open_artifact, stage_json_artifact
from graph_engine.plugin_api import TaskContext

from assurance_improvement.contracts.agent import RetroAnalysisInputV1, RetroSynthesisInputV1
from assurance_improvement.contracts.retro import (
    EvalEvidenceSlice,
    IssueEvidenceSlice,
    RetroContextV3,
    WorkflowEvidenceSlice,
)

ModelT = TypeVar("ModelT", bound=BaseModel)


def stage_named(context: TaskContext, relative: str, document: BaseModel) -> None:
    stage_json_artifact(context.write_root, relative, document)


def load_named(context: TaskContext, ref: object, model: type[ModelT]) -> ModelT:
    try:
        return open_artifact(context.project_root, ref, model=model)  # type: ignore[arg-type]
    except ArtifactReadError as error:
        raise InputError(str(error)) from error


_SLICES = TypeAdapter(
    Annotated[
        IssueEvidenceSlice | WorkflowEvidenceSlice | EvalEvidenceSlice,
        Field(discriminator="domain"),
    ]
)


def load_analysis_slice(ctx: PrepareContext, business: RetroAnalysisInputV1) -> RetroAnalysisInputV1:
    if business.evidence_slice is not None or business.evidence_slice_ref is None:
        return business
    try:
        raw = open_artifact(ctx.project_root, business.evidence_slice_ref)
    except ArtifactReadError as error:
        raise InputError(str(error)) from error
    return business.model_copy(update={"evidence_slice": _SLICES.validate_json(raw)})


def load_retro_context(ctx: PrepareContext, business: RetroSynthesisInputV1) -> RetroSynthesisInputV1:
    if business.context is not None or business.context_ref is None:
        return business
    try:
        loaded = open_artifact(ctx.project_root, business.context_ref, model=RetroContextV3)
    except ArtifactReadError as error:
        raise InputError(str(error)) from error
    return business.model_copy(update={"context": loaded})
