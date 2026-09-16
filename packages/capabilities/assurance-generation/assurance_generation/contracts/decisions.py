from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from assurance_generation.contracts.families import (
    GENERATION_FAMILIES,
    LayerName,
    validate_selected_families,
)

ReviewStage = Literal["codegen"]
_STAGES: tuple[ReviewStage, ...] = ("codegen",)


class GenerationBranchCompletion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    value: Literal[True]


class GenerationCompletionInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    completed: tuple[GenerationBranchCompletion, ...]
    selected_families: tuple[LayerName, ...]


class GenerationCompletionOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    families: dict[LayerName, dict[str, bool]]
    selected_families: tuple[LayerName, ...]


class GenerationReviewRoundAdvanceInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    family: LayerName
    stage: ReviewStage
    rounds_used: int = Field(ge=0)
    rounds_budget: int = Field(ge=1)

    @property
    def remaining(self) -> int:
        return self.rounds_budget - self.rounds_used


class GenerationReviewRoundAdvanceOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    family: LayerName
    stage: ReviewStage
    rounds_used: int = Field(ge=1)
    rounds_budget: int = Field(ge=1)


def complete_generation(data: object) -> GenerationCompletionOutput:
    try:
        payload = GenerationCompletionInput.model_validate(data)
    except ValidationError:
        raise
    selected = validate_selected_families(payload.selected_families)
    if len(payload.completed) != len(GENERATION_FAMILIES):
        raise ValueError(f"generation completion requires {len(GENERATION_FAMILIES)} lane tokens")
    return GenerationCompletionOutput(
        families={family: {"completed": True} for family in selected},
        selected_families=selected,
    )


def advance_review_round(data: object) -> GenerationReviewRoundAdvanceOutput:
    try:
        payload = GenerationReviewRoundAdvanceInput.model_validate(data)
    except ValidationError:
        raise
    if payload.family not in GENERATION_FAMILIES:
        raise ValueError(f"unknown generation family: {payload.family}")
    if payload.stage not in _STAGES:
        raise ValueError(f"unknown generation review stage: {payload.stage}")
    if payload.rounds_used >= payload.rounds_budget:
        raise ValueError("rounds_used must be below rounds_budget")
    return GenerationReviewRoundAdvanceOutput(
        family=payload.family,
        stage=payload.stage,
        rounds_used=payload.rounds_used + 1,
        rounds_budget=payload.rounds_budget,
    )


__all__ = [
    "GenerationBranchCompletion",
    "GenerationCompletionInput",
    "GenerationCompletionOutput",
    "GenerationReviewRoundAdvanceInput",
    "GenerationReviewRoundAdvanceOutput",
    "ReviewStage",
    "advance_review_round",
    "complete_generation",
]
