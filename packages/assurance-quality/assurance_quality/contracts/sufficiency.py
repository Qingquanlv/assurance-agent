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

from assurance_generation.contracts.families import CASE_TYPES, LAYER_NAMES, CaseType, LayerName
from assurance_intake.contracts import NonEmptyStr
from assurance_quality.contracts.trace import TraceGapCodeV2, TraceIntegrity

EvidenceKind = Literal["covered", "execution_recent", "fuzz_run", "perf_run", "pass_status"]

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


"""inspect/trace-sufficiency.json (must_compat): what the trace gate routes on.

The document that `operation:materialize-trace-projection` publishes beside
`inspect/trace-projection.json`, and the only input the independent
`trace-sufficiency-gate` reads. It carries **facts, never a route**: the
producer states what the projection and the policy amount to, and the gate's DSL
maps those facts onto `pass`/`stop`/`needs_human_review`. A `verdict` field here
would put one routing table in the producer and a second in the gate, and the
two would drift — `test_the_document_carries_no_verdict_route_or_action_field`
pins the absence.

Two obligations are enforced by the model rather than left to the gate:

- **Integrity is judged before sufficiency** (spec v3 §13). `incomplete` means an
  input could not be read, so `rows` is not this change's row set and no verdict
  over it means anything — most sharply when the fold read nothing and
  `sufficient` is *vacuously* true. `integrity_blocks_routing` restates that
  ordering as a fact, and the validator below forbids a document whose two
  fields disagree, so a gate cannot be handed a projection that denies its own
  unreadability.
- **An error state judged nothing.** `error_code` means adjudication did not
  happen at all, which is different from concluding that evidence is thin; such
  a document may not also claim `sufficient`, or a gate reading `sufficient`
  alone would pass on it.

The module deliberately does not import `assurance_agent.evidence`: the artifact
layer is the wire contract and must stay loadable by a consumer that has no
evaluator. The two vocabularies that *originate* in the evidence layer
(`SufficiencyReasonCode`, `EvidenceCoverageErrorCode`) are therefore spelled out
here and pinned equal by test, exactly as `evidence/sufficiency.py` already
spells out the projection's `integrity` levels rather than importing them.
"""

# Mirrors `artifacts.models.sufficiency.SufficiencyReasonCode` / the evaluator;
# pinned by `test_the_reason_vocabulary_tracks_the_sufficiency_report`.
TraceSufficiencyReasonCode = Literal[
    "not_in_current_batch",
    "uncovered",
    "never_run",
    "execution_stale",
    "fuzz_run_missing",
    "perf_run_missing",
    "no_pass",
    "pass_stale",
]

# Mirrors `evidence.sufficiency.EvidenceCoverageErrorCode`: the two ways
# adjudication can fail to happen, as opposed to concluding evidence is thin.
TraceSufficiencyErrorCode = Literal["evidence_projection_missing", "policy_error"]


class TraceInsufficientCase(BaseModel):
    """One case that fell short, with the reasons it did.

    The reasons travel with the case rather than as a document-level set: an
    operator acting on this reads "which case needs what", and a flattened set
    cannot answer that.
    """

    model_config = _FROZEN

    case_id: str
    reason_codes: tuple[TraceSufficiencyReasonCode, ...]


class TraceSufficiencyFacts(BaseModel):
    model_config = _FROZEN

    schema_version: Literal["1"]
    change_id: str
    # "" when the projection has no authoritative batch at all, mirroring
    # `TraceProjection.authoritative_batch_id`.
    authoritative_batch_id: str
    # The policy these verdicts were judged under, and the instant they were
    # judged at. Both null exactly when nothing was judged (`error_code` set):
    # a stored verdict without them is uninterpretable, because the same rows
    # are sufficient or not depending on a cutoff nothing else records.
    policy_digest: str | None
    as_of: AwareDatetime | None
    integrity: TraceIntegrity
    integrity_blocks_routing: bool
    sufficient: bool
    # The projection's open-Problem facts, reduced to the one bit that routes.
    # Separate from `sufficient` because an open product bug is a reason to stop
    # regardless of how fresh the evidence is.
    has_open_problems: bool
    error_code: TraceSufficiencyErrorCode | None
    insufficient_cases: tuple[TraceInsufficientCase, ...]
    gap_codes: tuple[TraceGapCodeV2, ...]

    _BLOCKING_INTEGRITY = "incomplete"

    @model_validator(mode="after")
    def _integrity_and_its_consequence_agree(self) -> TraceSufficiencyFacts:
        """`integrity_blocks_routing` must be the `integrity` level, restated.

        Carried as its own field so a gate DSL — which cannot express "which of
        the three levels forbids believing the verdicts" — routes on the
        comparison instead of re-deriving it. Two fields for one fact can
        disagree, and the disagreement that matters is the one that fails open,
        so both directions are refused here.
        """
        blocks = self.integrity == self._BLOCKING_INTEGRITY
        if self.integrity_blocks_routing != blocks:
            raise ValueError(f"integrity_blocks_routing must be {blocks} for integrity={self.integrity!r}")
        return self

    @model_validator(mode="after")
    def _an_error_state_reached_no_verdict(self) -> TraceSufficiencyFacts:
        """A document that judged nothing may not claim a verdict.

        `error_code` means adjudication never happened, which is not the same as
        concluding that evidence is thin. A consumer that read `sufficient`
        without branching on `error_code` first would read a default as a pass,
        so the pairing is refused rather than trusted.
        """
        if self.error_code is None:
            if self.policy_digest is None or self.as_of is None:
                raise ValueError(
                    "a judged document must record the policy digest and the instant it was judged at"
                )
            return self
        if self.sufficient:
            raise ValueError(f"a {self.error_code} document cannot also claim sufficient=True")
        if self.policy_digest is not None or self.as_of is not None:
            raise ValueError(f"a {self.error_code} document judged nothing, so it has no policy or instant")
        return self

    @model_validator(mode="after")
    def _insufficient_cases_belong_to_an_insufficient_verdict(self) -> TraceSufficiencyFacts:
        """The gap list and the verdict are one fact, stated twice.

        A `sufficient` document listing shortfalls contradicts itself, and a
        consumer would have to guess which half to believe.
        """
        if self.sufficient and self.insufficient_cases:
            raise ValueError("a sufficient=True document cannot list insufficient_cases")
        return self
