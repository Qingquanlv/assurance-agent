"""Execution target selection and the shared closed test mapping."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from assurance_generation.contracts.mapping import (
    ClosedMappingEntryV1,
    ClosedMappingV1,
    selected_test_file,
)


class SelectedTargets(BaseModel):
    """Which of the four execution layers were selected for a batch."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    api: bool
    e2e: bool
    fuzz: bool
    performance: bool


__all__ = ["ClosedMappingEntryV1", "ClosedMappingV1", "SelectedTargets", "selected_test_file"]
