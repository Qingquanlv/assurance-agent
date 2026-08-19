"""Bound evidence sufficiency V2 wire DTOs and layer-join summary models."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    field_validator,
    model_validator,
)
from pydantic.types import AwareDatetime

from assurance_kernel.artifacts.models.assurance import CASE_TYPES, LAYER_NAMES, CaseType, LayerName
from assurance_kernel.artifacts.models.common import NonEmptyStr
from assurance_kernel.artifacts.models.policy import EvidenceKind

_FROZEN = ConfigDict(frozen=True, extra="forbid")

ExecutionState = Literal["never_run", "stale", "fresh"]
EXECUTION_STATES: tuple[ExecutionState, ...] = ("never_run", "stale", "fresh")

SufficiencyReasonCode = Literal[
    "not_in_current_batch",
    "uncovered",
    "never_run",
    "execution_stale",
    "fuzz_run_missing",
    "perf_run_missing",
    "no_pass",
    "pass_stale",
]
SUFFICIENCY_REASON_CODES: tuple[SufficiencyReasonCode, ...] = (
    "not_in_current_batch",
    "uncovered",
    "never_run",
    "execution_stale",
    "fuzz_run_missing",
    "perf_run_missing",
    "no_pass",
    "pass_stale",
)

StrictNonNegativeInt = Annotated[int, Field(strict=True, ge=0)]
StrictPositiveInt = Annotated[int, Field(strict=True, gt=0)]

_ALLOWED_KIND_REASONS: dict[EvidenceKind, frozenset[SufficiencyReasonCode]] = {
    "covered": frozenset({"uncovered"}),
    "execution_recent": frozenset({"not_in_current_batch", "never_run", "execution_stale"}),
    "fuzz_run": frozenset({"not_in_current_batch", "fuzz_run_missing"}),
    "perf_run": frozenset({"not_in_current_batch", "perf_run_missing"}),
    "pass_status": frozenset({"not_in_current_batch", "no_pass", "pass_stale"}),
}


class SufficiencyBindingError(ValueError):
    """A typed sufficiency report cannot be joined to the claimed facts/policy."""


class SufficiencyRowVerdictV2(BaseModel):
    model_config = _FROZEN

    case_id: NonEmptyStr
    sufficient: StrictBool
    missing_kinds: tuple[EvidenceKind, ...]
    reason_codes: tuple[SufficiencyReasonCode, ...]
    execution_state: ExecutionState

    @model_validator(mode="after")
    def _validate_row(self) -> Self:
        if len(self.missing_kinds) != len(self.reason_codes):
            raise ValueError("missing_kinds and reason_codes must have equal length")
        if len(set(self.missing_kinds)) != len(self.missing_kinds):
            raise ValueError("missing_kinds must be unique")
        empty = not self.missing_kinds and not self.reason_codes
        if self.sufficient != empty:
            raise ValueError("sufficient is true iff missing_kinds and reason_codes are empty")
        for kind, reason in zip(self.missing_kinds, self.reason_codes, strict=True):
            allowed = _ALLOWED_KIND_REASONS[kind]
            if reason not in allowed:
                raise ValueError(f"invalid kind/reason pair: {kind}->{reason}")
            if kind == "execution_recent" and reason == "never_run" and self.execution_state != "never_run":
                raise ValueError("execution_recent/never_run requires execution_state=never_run")
            if kind == "execution_recent" and reason == "execution_stale" and self.execution_state != "stale":
                raise ValueError("execution_recent/execution_stale requires execution_state=stale")
        return self


class SufficiencyReportV2(BaseModel):
    model_config = _FROZEN

    schema_version: Literal["2.0"] = "2.0"
    source_projection_digest: NonEmptyStr
    source_policy_digest: NonEmptyStr
    semantics: Literal["evidence_sufficiency/v2"] = "evidence_sufficiency/v2"
    require_current_batch: StrictBool
    as_of: AwareDatetime
    recency_hours: StrictPositiveInt
    verdicts: tuple[SufficiencyRowVerdictV2, ...]

    @model_validator(mode="after")
    def _unique_case_ids(self) -> Self:
        case_ids = [verdict.case_id for verdict in self.verdicts]
        if len(set(case_ids)) != len(case_ids):
            raise ValueError("verdict case_id values must be unique")
        return self

    @property
    def all_sufficient(self) -> bool:
        return all(verdict.sufficient for verdict in self.verdicts)


class LayerSufficiencyCounts(BaseModel):
    model_config = _FROZEN

    layer: LayerName
    case_type: CaseType
    sufficient: StrictNonNegativeInt
    insufficient: StrictNonNegativeInt
    reason_counts: dict[SufficiencyReasonCode, StrictPositiveInt]
    execution_state_counts: dict[ExecutionState, StrictNonNegativeInt]

    @field_validator("reason_counts")
    @classmethod
    def _lexical_reason_keys(
        cls, value: dict[SufficiencyReasonCode, int]
    ) -> dict[SufficiencyReasonCode, int]:
        if tuple(value) != tuple(sorted(value)):
            raise ValueError("reason_counts keys must be in lexical order")
        return value

    @field_validator("execution_state_counts")
    @classmethod
    def _exact_execution_state_keys(cls, value: dict[ExecutionState, int]) -> dict[ExecutionState, int]:
        if tuple(value) != EXECUTION_STATES:
            raise ValueError("execution_state_counts must use never_run, stale, fresh order")
        return value

    @model_validator(mode="after")
    def _conservation(self) -> Self:
        total = self.sufficient + self.insufficient
        if total != sum(self.execution_state_counts.values()):
            raise ValueError("sufficient + insufficient must equal execution_state_counts sum")
        return self


class TraceLayerSufficiencySummary(BaseModel):
    model_config = _FROZEN

    schema_version: Literal["1"] = "1"
    source_projection_digest: NonEmptyStr
    source_policy_digest: NonEmptyStr
    semantics: Literal["evidence_sufficiency/v2"]
    require_current_batch: Literal[True]
    as_of: AwareDatetime
    recency_hours: StrictPositiveInt
    layers: tuple[LayerSufficiencyCounts, ...]

    @model_validator(mode="after")
    def _validate_layers(self) -> Self:
        if len(self.layers) != len(LAYER_NAMES):
            raise ValueError("layers must contain exactly four layer rows")
        if tuple(layer.layer for layer in self.layers) != LAYER_NAMES:
            raise ValueError("layers must follow LAYER_NAMES order")
        if tuple(layer.case_type for layer in self.layers) != CASE_TYPES:
            raise ValueError("layers must follow CASE_TYPES order")
        return self
