"""Contracts for applying a bounded repair to existing generated tests."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

from graph_engine.attempts.models.resolutions import ReceiptRef
from graph_engine.plugin_api import FrozenModel

from assurance_intake.contracts.workflow import (
    EvidenceArtifactRefV1,
    ReviewedCaseV1,
    require_same_plan,
)

AppliedTestRepairStatus = Literal["applied", "needs_review", "not_eligible", "exhausted", "failed"]


def _canonical_paths(values: tuple[str, ...], *, required: bool) -> tuple[str, ...]:
    if required and not values:
        raise ValueError("paths must not be empty")
    if values != tuple(sorted(values)) or len(values) != len(set(values)):
        raise ValueError("paths must be sorted and unique")
    for value in values:
        path = PurePosixPath(value)
        if (
            path.is_absolute()
            or "\\" in value
            or path.as_posix() != value
            or any(part in {"", ".", ".."} for part in path.parts)
        ):
            raise ValueError("paths must be canonical and relative")
    return values


def _canonical_refs(
    values: tuple[EvidenceArtifactRefV1, ...], *, required: bool
) -> tuple[EvidenceArtifactRefV1, ...]:
    if required and not values:
        raise ValueError("evidence refs must not be empty")
    ordered = tuple(sorted(values, key=lambda item: (item.path, item.digest)))
    if values != ordered or len({item.path for item in values}) != len(values):
        raise ValueError("evidence refs must be sorted with one digest per path")
    return values


class ApplyTestRepairInputV1(FrozenModel):
    change_id: str = Field(min_length=1)
    plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_ref: EvidenceArtifactRefV1
    coverage_epoch: int = Field(ge=0)
    repair_round: int = Field(ge=1)
    reviewed_case: ReviewedCaseV1
    proposal_ref: EvidenceArtifactRefV1
    execution_ref: EvidenceArtifactRefV1
    mapping_ref: EvidenceArtifactRefV1
    source_refs: tuple[EvidenceArtifactRefV1, ...] = Field(min_length=1)
    allowed_test_paths: tuple[str, ...] = Field(min_length=1)
    validation_error: str | None = Field(default=None, min_length=1, max_length=8192)

    @field_validator("source_refs")
    @classmethod
    def _source_refs(cls, value: tuple[EvidenceArtifactRefV1, ...]) -> tuple[EvidenceArtifactRefV1, ...]:
        return _canonical_refs(value, required=True)

    @field_validator("allowed_test_paths")
    @classmethod
    def _allowed_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _canonical_paths(value, required=True)

    @model_validator(mode="after")
    def _bind_cycle(self) -> Self:
        if self.reviewed_case.change_id != self.change_id:
            raise ValueError("reviewed case change_id does not match repair")
        if self.reviewed_case.coverage_epoch != self.coverage_epoch:
            raise ValueError("reviewed case coverage_epoch does not match repair")
        require_same_plan(
            self.plan_digest,
            self.plan_ref,
            self.reviewed_case.plan_digest,
            self.reviewed_case.plan_ref,
        )
        prefix = "qa/"
        refs = (
            self.proposal_ref,
            self.execution_ref,
            self.mapping_ref,
        )
        if any(not ref.path.startswith(prefix) for ref in refs):
            raise ValueError("repair evidence must belong to the current change")
        if any(
            not (ref.path.startswith(prefix) or ref.path.startswith("qa/tests/")) for ref in self.source_refs
        ):
            raise ValueError("repair evidence must belong to the current change")
        source_paths = {ref.path for ref in self.source_refs}
        if any(path not in source_paths for path in self.allowed_test_paths):
            raise ValueError("allowed test paths must be current generation source refs")
        return self


class TestRepairResultV1(FrozenModel):
    schema_version: Literal["1"] = "1"
    change_id: str = Field(min_length=1)
    output_files: tuple[str, ...] = Field(min_length=1)
    summary: str = Field(min_length=1)

    @field_validator("output_files")
    @classmethod
    def _outputs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _canonical_paths(value, required=True)


VERIFIED_REPAIR_PATH = "qa/results/healing/verified-repair.json"


class VerifiedTestRepairV1(FrozenModel):
    change_id: str = Field(min_length=1)
    plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_ref: EvidenceArtifactRefV1
    coverage_epoch: int = Field(ge=0)
    repair_round: int = Field(ge=1)
    changed_test_refs: tuple[EvidenceArtifactRefV1, ...] = Field(min_length=1)
    mapping_ref: EvidenceArtifactRefV1

    @field_validator("changed_test_refs")
    @classmethod
    def _changed_refs(cls, value: tuple[EvidenceArtifactRefV1, ...]) -> tuple[EvidenceArtifactRefV1, ...]:
        return _canonical_refs(value, required=True)


class AppliedTestRepairV1(FrozenModel):
    change_id: str = Field(min_length=1)
    plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_ref: EvidenceArtifactRefV1
    coverage_epoch: int = Field(ge=0)
    repair_round: int = Field(ge=1)
    status: AppliedTestRepairStatus
    changed_test_refs: tuple[EvidenceArtifactRefV1, ...] = ()
    mapping_ref: EvidenceArtifactRefV1 | None = None
    receipt: ReceiptRef | None = None

    @field_validator("changed_test_refs")
    @classmethod
    def _changed_refs(cls, value: tuple[EvidenceArtifactRefV1, ...]) -> tuple[EvidenceArtifactRefV1, ...]:
        return _canonical_refs(value, required=False)

    @model_validator(mode="after")
    def _applied_requires_committed_bytes(self) -> Self:
        complete = bool(self.changed_test_refs) and self.mapping_ref is not None and self.receipt is not None
        if self.status == "applied" and not complete:
            raise ValueError("applied repair requires changed bytes, mapping, and commit receipt")
        if self.status != "applied" and (
            self.changed_test_refs or self.mapping_ref is not None or self.receipt is not None
        ):
            raise ValueError("non-applied repair cannot publish committed repair evidence")
        return self


__all__ = [
    "AppliedTestRepairStatus",
    "AppliedTestRepairV1",
    "ApplyTestRepairInputV1",
    "TestRepairResultV1",
    "VerifiedTestRepairV1",
]
