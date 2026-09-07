"""Contracts for applying an approved repair to existing generated tests."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

from graph_engine.attempts.resolutions import ReceiptRef
from graph_engine.attempts import AttemptKey
from graph_engine.canonical import JSONValue, canonical_digest
from graph_engine.plugin_api import FrozenModel

from assurance_generation.contracts.execution_plan import ValidationProfile
from assurance_generation.contracts.families import LayerName
from assurance_generation.contracts.workflow import GenerationCycleResultV1, VerifiedGenerationDefectV1
from assurance_intake.contracts.workflow import (
    EvidenceArtifactRefV1,
    ReviewedCaseV1,
    require_same_plan,
)

AppliedTestRepairStatus = Literal["applied", "needs_review", "not_eligible", "exhausted", "failed"]


class RepairAuthorizationV1(FrozenModel):
    """Product-owned transition from one committed execution defect into healing."""

    schema_version: Literal["1"] = "1"
    attempt_key: AttemptKey
    invocation_id: str = Field(min_length=1)
    semantic_node_id: Literal["execution.execute"]
    defect: VerifiedGenerationDefectV1
    receipt: ReceiptRef

    @model_validator(mode="after")
    def _bind_attempt(self) -> Self:
        if self.attempt_key != self.defect.attempt_key:
            raise ValueError("repair authorization defect uses another Attempt")
        return self

    @property
    def coverage_epoch(self) -> int:
        return self.defect.generation.coverage_epoch

    @property
    def repair_round(self) -> int:
        return self.defect.repair_round

    @property
    def generation_digest(self) -> str:
        payload: JSONValue = self.defect.generation.model_dump(mode="json")
        return canonical_digest(payload)

    @property
    def case_id(self) -> str:
        return self.defect.case_id

    @property
    def bridge_symbol(self) -> str:
        return self.defect.bridge_symbol

    @property
    def bridge_ref(self) -> EvidenceArtifactRefV1:
        return self.defect.bridge_ref

    @property
    def observed_digest(self) -> str | None:
        return self.defect.observed_digest

    @property
    def expected_digest(self) -> str:
        return self.defect.expected_digest


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
    approval_ref: EvidenceArtifactRefV1 | None
    execution_ref: EvidenceArtifactRefV1 | None = None
    mapping_ref: EvidenceArtifactRefV1
    source_refs: tuple[EvidenceArtifactRefV1, ...] = Field(min_length=1)
    allowed_test_paths: tuple[str, ...] = Field(min_length=1)
    generation: GenerationCycleResultV1 | None = None
    validation_profile: ValidationProfile | None = None
    selected_test_families: tuple[LayerName, ...] = ()
    capability_leafs: tuple[str, ...] = ()
    repair_authorization: RepairAuthorizationV1 | None = None

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
        prefix = f"qa/changes/{self.change_id}/"
        refs = (
            self.proposal_ref,
            self.mapping_ref,
            *self.source_refs,
            *(() if self.execution_ref is None else (self.execution_ref,)),
            *(() if self.approval_ref is None else (self.approval_ref,)),
        )
        if any(not ref.path.startswith(prefix) for ref in refs):
            raise ValueError("repair evidence must belong to the current change")
        source_paths = {ref.path for ref in self.source_refs}
        if any(path not in source_paths for path in self.allowed_test_paths):
            raise ValueError("allowed test paths must be current generation source refs")
        if self.repair_authorization is None:
            if self.execution_ref is None:
                raise ValueError("legacy repair requires execution evidence")
            if (
                any(value is not None for value in (self.generation, self.validation_profile))
                or self.selected_test_families
                or self.capability_leafs
            ):
                raise ValueError("legacy repair cannot carry verified generation inputs")
        else:
            authorization = self.repair_authorization
            if (
                self.execution_ref is not None
                or self.generation is None
                or self.validation_profile is None
                or not self.selected_test_families
                or not self.capability_leafs
            ):
                raise ValueError("verified repair requires its closed generation inputs")
            generation_payload: JSONValue = self.generation.model_dump(mode="json")
            if (
                self.repair_round != authorization.repair_round + 1
                or self.coverage_epoch != authorization.coverage_epoch
                or self.generation.change_id != self.change_id
                or self.generation.coverage_epoch != self.coverage_epoch
                or self.generation.reviewed_case != self.reviewed_case
                or self.generation.mapping_ref != self.mapping_ref
                or self.generation.source_refs != self.source_refs
                or canonical_digest(generation_payload) != authorization.generation_digest
                or authorization.bridge_ref not in self.source_refs
                or self.allowed_test_paths != (authorization.bridge_ref.path,)
            ):
                raise ValueError("verified repair authorization differs from its generation cycle")
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
    "RepairAuthorizationV1",
    "TestRepairResultV1",
    "VerifiedTestRepairV1",
]
