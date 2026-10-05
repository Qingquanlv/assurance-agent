from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from assurance_generation.contracts.families import (
    GENERATION_FAMILIES,
    LayerName,
    validate_selected_families,
)


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


__all__ = [
    "GenerationBranchCompletion",
    "GenerationCompletionInput",
    "GenerationCompletionOutput",
    "complete_generation",
]
