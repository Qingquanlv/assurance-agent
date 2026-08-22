"""Normalized execution evidence bound to a closed mapping."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, model_validator

from assurance_execution.contracts.execution import ExecutionReceiptV1, RawTestResultV1
from assurance_execution.contracts.selection import ClosedMappingV1, SelectedTargets
from assurance_intake.contracts import NonEmptyStr

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class ExecutionEvidenceV1(BaseModel):
    """Normalized results plus the closed mapping and receipt digests."""

    model_config = _FROZEN

    schema_version: Literal["1"] = "1"
    change_id: NonEmptyStr
    batch_id: NonEmptyStr
    selected_targets: SelectedTargets
    mapping: ClosedMappingV1
    mapping_digest: NonEmptyStr
    baseline_tree_id: NonEmptyStr
    runner_profile_digest: NonEmptyStr
    receipt_digest: NonEmptyStr
    receipt: ExecutionReceiptV1
    results: tuple[RawTestResultV1, ...]

    @model_validator(mode="after")
    def _results_must_be_selected(self) -> Self:
        allowed = frozenset(self.mapping.selected)
        seen: list[str] = []
        for result in self.results:
            if result.test not in allowed:
                raise ValueError("execution evidence contains a test outside the closed mapping")
            seen.append(result.test)
        if len(seen) != len(set(seen)) or set(seen) != set(allowed):
            raise ValueError("execution evidence must uniquely cover the closed mapping")
        return self
