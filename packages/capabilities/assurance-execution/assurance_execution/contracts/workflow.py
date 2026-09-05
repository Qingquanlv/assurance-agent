"""Versioned output for one completed execution or rerun batch."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, model_validator

from graph_engine.attempts.resolutions import ReceiptRef
from graph_engine.plugin_api import FrozenModel

from assurance_generation.contracts.workflow import GenerationCycleResultV1
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1


class ExecutionCycleResultV1(FrozenModel):
    change_id: str = Field(min_length=1)
    coverage_epoch: int = Field(ge=0)
    repair_round: int = Field(ge=0)
    batch_id: str = Field(min_length=1)
    final_status: Literal["PASS", "FAIL"]
    evidence_ref: EvidenceArtifactRefV1
    mapping_ref: EvidenceArtifactRefV1
    source_refs: tuple[EvidenceArtifactRefV1, ...] = Field(min_length=1)
    receipt: ReceiptRef

    @model_validator(mode="after")
    def _paths_match_change(self) -> Self:
        prefix = f"qa/changes/{self.change_id}/"
        if not self.evidence_ref.path.startswith(prefix):
            raise ValueError("execution evidence must belong to the current change")
        if not self.mapping_ref.path.startswith(prefix):
            raise ValueError("execution mapping must belong to the current change")
        if any(not item.path.startswith(prefix) for item in self.source_refs):
            raise ValueError("execution sources must belong to the current change")
        return self


class ExecutionCycleInputV1(FrozenModel):
    generation: GenerationCycleResultV1
    repair_round: int = Field(default=0, ge=0)


__all__ = ["ExecutionCycleInputV1", "ExecutionCycleResultV1"]
