"""Batch-scoped PR-cadence metric evidence artifacts (M1 Task 6).

These land under ``execution/runs/<batch>/`` and stay unregistered until Task 7
promotes authoritative ``inspect/metrics.json``. Each collector owns one shape;
``collection_gaps`` / ``shortboards`` travel with the evidence so aggregation
does not re-derive failure modes.
"""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from assurance_agent.artifacts.models.common import NonEmptyStr
from assurance_agent.artifacts.models.metrics import (
    MetricCollectionGap,
    MetricScope,
    MetricShortboard,
)

_FROZEN = ConfigDict(frozen=True, extra="forbid")

CoverageDiffRel = Literal["coverage-diff.json"]
ConstraintCoverageRel = Literal["constraint-coverage.json"]
AuthMatrixRel = Literal["auth-matrix.json"]
JourneyCoverageRel = Literal["journey-coverage.json"]
PerfSlackRel = Literal["perf-slack.json"]


class CoverageDiffFile(BaseModel):
    model_config = _FROZEN

    path: NonEmptyStr
    changed_lines: tuple[int, ...] = ()
    covered_lines: tuple[int, ...] = ()
    uncovered_lines: tuple[int, ...] = ()


class CoverageDiffEvidence(BaseModel):
    """``execution/runs/<batch>/coverage-diff.json`` (§5-A1)."""

    model_config = _FROZEN

    schema_version: Literal["1"]
    change_id: NonEmptyStr
    batch_id: NonEmptyStr
    total_changed_lines: int = Field(ge=0)
    covered_changed_lines: int = Field(ge=0)
    # None when there are no changed lines (scalar has no vacuous 1.0 claim).
    value: float | None
    files: tuple[CoverageDiffFile, ...] = ()
    source: dict[str, str] = Field(default_factory=dict)
    collection_gaps: tuple[MetricCollectionGap, ...] = ()
    shortboards: tuple[MetricShortboard, ...] = ()

    @model_validator(mode="after")
    def _counts_agree(self) -> Self:
        if self.covered_changed_lines > self.total_changed_lines:
            raise ValueError("covered_changed_lines exceeds total_changed_lines")
        if self.total_changed_lines == 0:
            if self.value is not None:
                raise ValueError("no changed lines means no diff_coverage value")
            return self
        expected = self.covered_changed_lines / self.total_changed_lines
        if self.value is None or abs(self.value - expected) > 1e-9:
            raise ValueError(
                f"value={self.value} contradicts {self.covered_changed_lines}/{self.total_changed_lines}"
            )
        return self


class ConstraintCoverageEvidence(BaseModel):
    """``execution/runs/<batch>/constraint-coverage.json`` (§5-A2)."""

    model_config = _FROZEN

    schema_version: Literal["1"]
    change_id: NonEmptyStr
    batch_id: NonEmptyStr
    declared: MetricScope | None
    touched: MetricScope | None = None
    value: float | None = None
    collection_gaps: tuple[MetricCollectionGap, ...] = ()
    shortboards: tuple[MetricShortboard, ...] = ()


class AuthMatrixCellResult(BaseModel):
    model_config = _FROZEN

    cell_id: NonEmptyStr
    route: NonEmptyStr
    method: NonEmptyStr
    token: NonEmptyStr
    expected: Literal["allow", "deny"]
    allowed_status_codes: tuple[int, ...]
    asserted: bool
    outcome: Literal["passed", "failed", "skipped", "missing", "unasserted"] = "missing"
    actual_status_code: int | None = None
    parameterized_id: str = ""


class AuthMatrixEvidence(BaseModel):
    """``execution/runs/<batch>/auth-matrix.json`` (§5-A3)."""

    model_config = _FROZEN

    schema_version: Literal["1"]
    change_id: NonEmptyStr
    batch_id: NonEmptyStr
    declared: MetricScope | None
    touched: MetricScope | None = None
    value: float | None = None
    cells: tuple[AuthMatrixCellResult, ...] = ()
    collection_gaps: tuple[MetricCollectionGap, ...] = ()
    shortboards: tuple[MetricShortboard, ...] = ()


class JourneyCoverageItem(BaseModel):
    model_config = _FROZEN

    journey_key: NonEmptyStr
    case_ids: tuple[str, ...] = ()
    executed_case_ids: tuple[str, ...] = ()
    covered: bool
    quarantined: bool = False
    status: Literal[
        "covered",
        "covered_but_failing",
        "not_executed",
        "missing",
        "quarantined",
        "skipped_by_scope",
        "weak_oracle",
        "oracle_unavailable",
    ]


class JourneyCoverageEvidence(BaseModel):
    """``execution/runs/<batch>/journey-coverage.json`` (§5-A4)."""

    model_config = _FROZEN

    schema_version: Literal["1"]
    change_id: NonEmptyStr
    batch_id: NonEmptyStr
    declared: MetricScope | None
    touched: MetricScope | None = None
    value: float | None = None
    items: tuple[JourneyCoverageItem, ...] = ()
    collection_gaps: tuple[MetricCollectionGap, ...] = ()
    shortboards: tuple[MetricShortboard, ...] = ()


class PerfSlackScenario(BaseModel):
    model_config = _FROZEN

    capability: NonEmptyStr
    endpoint: NonEmptyStr
    threshold_p95_ms: float
    measured_p95_ms: float | None
    # None when measured is missing or non-positive.
    slack: float | None


class PerfSlackEvidence(BaseModel):
    """``execution/runs/<batch>/perf-slack.json`` (§5-B5)."""

    model_config = _FROZEN

    schema_version: Literal["1"]
    change_id: NonEmptyStr
    batch_id: NonEmptyStr
    # Max slack across measurable scenarios; None when none measured.
    value: float | None
    slack_band: float = 10.0
    scenarios: tuple[PerfSlackScenario, ...] = ()
    collection_gaps: tuple[MetricCollectionGap, ...] = ()
    shortboards: tuple[MetricShortboard, ...] = ()


MutationRel = Literal["mutation.json"]
MutationEvidenceStatus = Literal["evaluated", "not_evaluated", "collection_failed"]


class MutationSurvivor(BaseModel):
    """One surviving mutant locator for report / retro (capped at report time)."""

    model_config = _FROZEN

    locator: NonEmptyStr
    module: NonEmptyStr
    line: int = Field(ge=1)
    operator: NonEmptyStr
    mutant_id: NonEmptyStr
    equivalent: bool = False


class MutationEvidence(BaseModel):
    """``execution/runs/<batch>/mutation.json`` (§5-B1 nightly).

    Score and survivors are report-only: no floor fields, and budget overruns
    land as ``mutation_budget_exceeded`` shortboards rather than FAIL.
    """

    model_config = _FROZEN

    schema_version: Literal["1"]
    change_id: NonEmptyStr
    batch_id: NonEmptyStr
    status: MutationEvidenceStatus
    # killed / (killed + survived); None when not evaluated or collection failed.
    # Equivalents are excluded from the denominator.
    value: float | None
    killed: int = Field(ge=0)
    survived: int = Field(ge=0)
    equivalent: int = Field(ge=0)
    tested: int = Field(ge=0)
    selected: int = Field(ge=0)
    budget_seconds: int = Field(ge=0)
    elapsed_seconds: float = Field(ge=0.0)
    budget_exceeded: bool = False
    cache_hit: bool = False
    seed: int = 0
    survivors: tuple[MutationSurvivor, ...] = ()
    survivors_truncated: bool = False
    survivor_report_cap: int = Field(default=50, ge=0)
    collection_gaps: tuple[MetricCollectionGap, ...] = ()
    shortboards: tuple[MetricShortboard, ...] = ()
    source: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _counts_and_status_agree(self) -> Self:
        if self.tested != self.killed + self.survived + self.equivalent:
            raise ValueError("tested must equal killed + survived + equivalent")
        if self.tested > self.selected:
            raise ValueError("tested cannot exceed selected")
        if self.status == "evaluated":
            denom = self.killed + self.survived
            if denom == 0:
                if self.value is not None:
                    raise ValueError("empty evaluated denominator means no mutation_score value")
            else:
                expected = self.killed / denom
                if self.value is None or abs(self.value - expected) > 1e-9:
                    raise ValueError(f"value={self.value} contradicts {self.killed}/{denom}")
        elif self.value is not None:
            raise ValueError(f"{self.status} mutation evidence cannot publish a value")
        if self.budget_exceeded and not any(
            board.code == "mutation_budget_exceeded" for board in self.shortboards
        ):
            raise ValueError("budget_exceeded requires mutation_budget_exceeded shortboard")
        if self.survivors_truncated and self.survivor_report_cap <= 0:
            raise ValueError("truncated survivors require a positive survivor_report_cap")
        if len(self.survivors) > self.survivor_report_cap > 0:
            raise ValueError("survivors exceed survivor_report_cap")
        return self


AssertionStrengthRel = Literal["assertion-strength.json"]
AssertionStrengthEvidenceStatus = Literal["evaluated", "not_evaluated", "collection_failed"]
AssertionStrengthSurfaceLayer = Literal["api", "e2e"]


class WeakAssertionLocator(BaseModel):
    """One weak test-function locator for report / aggregate shortboards."""

    model_config = _FROZEN

    locator: NonEmptyStr
    surface: AssertionStrengthSurfaceLayer
    file: NonEmptyStr
    function_name: NonEmptyStr
    lineno: int = Field(ge=1)
    reasons: tuple[str, ...] = ()


class AssertionStrengthSurfaceSlice(BaseModel):
    """Per-surface tally from one §5-B2 classifier (api or e2e)."""

    model_config = _FROZEN

    layer: AssertionStrengthSurfaceLayer
    declared: MetricScope
    value: float | None
    strong: int = Field(ge=0)
    weak: int = Field(ge=0)

    @model_validator(mode="after")
    def _counts_agree(self) -> Self:
        if self.strong + self.weak != self.declared.total:
            raise ValueError("strong + weak must equal declared.total")
        if self.strong != self.declared.covered:
            raise ValueError("strong must equal declared.covered")
        if self.declared.total == 0:
            if self.value is not None:
                raise ValueError("empty surface slice has no value")
        elif self.value is None or abs(self.value - self.declared.covered / self.declared.total) > 1e-9:
            raise ValueError(f"value={self.value} contradicts surface tally")
        return self


class AssertionStrengthEvidence(BaseModel):
    """``execution/runs/<batch>/assertion-strength.json`` (§5-B2 nightly).

    Evaluated only when *both* api and e2e populations are measurable; otherwise
    ``not_evaluated`` + ``pending_nightly`` so a single surface cannot stand in
    for the other (``METRIC_SURFACES`` / MetricsDocument rules).
    """

    model_config = _FROZEN

    schema_version: Literal["1"]
    change_id: NonEmptyStr
    batch_id: NonEmptyStr
    status: AssertionStrengthEvidenceStatus
    value: float | None = None
    declared: MetricScope | None = None
    surfaces: tuple[AssertionStrengthSurfaceSlice, ...] = ()
    weak_assertions: tuple[WeakAssertionLocator, ...] = ()
    collection_gaps: tuple[MetricCollectionGap, ...] = ()
    shortboards: tuple[MetricShortboard, ...] = ()
    source: dict[str, str] = Field(default_factory=dict)

    @field_validator("surfaces", mode="after")
    @classmethod
    def _sort_surfaces(
        cls, value: tuple[AssertionStrengthSurfaceSlice, ...]
    ) -> tuple[AssertionStrengthSurfaceSlice, ...]:
        return tuple(sorted(value, key=lambda surface: surface.layer))

    @model_validator(mode="after")
    def _status_and_surfaces_agree(self) -> Self:
        present = {s.layer for s in self.surfaces}
        if self.status == "evaluated":
            if present != {"api", "e2e"}:
                raise ValueError(
                    "evaluated assertion_strength publishes each of ['api', 'e2e'] "
                    f"independently; got {sorted(present)}"
                )
            if any(s.declared.total == 0 for s in self.surfaces):
                raise ValueError("evaluated surfaces require a non-empty declared denominator")
            if self.declared is None or self.value is None:
                raise ValueError("evaluated assertion_strength publishes pooled declared+value")
            total = sum(s.declared.total for s in self.surfaces)
            covered = sum(s.declared.covered for s in self.surfaces)
            if self.declared.total != total or self.declared.covered != covered:
                raise ValueError("declared must equal the pooled sum of surface tallies")
            expected = covered / total
            if abs(self.value - expected) > 1e-9:
                raise ValueError(f"value={self.value} contradicts pooled {covered}/{total}")
            return self
        if self.value is not None or self.declared is not None or self.surfaces:
            raise ValueError(f"{self.status} assertion_strength carries no value, scope or surface")
        if self.status == "not_evaluated" and not any(
            board.code == "pending_nightly" and board.metric == "assertion_strength"
            for board in self.shortboards
        ):
            raise ValueError("not_evaluated assertion_strength requires pending_nightly shortboard")
        return self


BaselineDriftRel = Literal["baseline-drift.json"]
BaselineDriftEvidenceStatus = Literal["evaluated", "not_evaluated", "collection_failed"]

AdversarialYieldRel = Literal["adversarial-yield.json"]
AdversarialYieldEvidenceStatus = Literal["evaluated", "not_evaluated", "collection_failed"]
AdversarialYieldLayer = Literal["api", "e2e", "fuzz"]


class BaselineDriftScenario(BaseModel):
    """One scenario's current vs baseline regression magnitude."""

    model_config = _FROZEN

    capability: NonEmptyStr
    endpoint: NonEmptyStr
    current_p95_ms: float
    baseline_p95_ms: float
    current_error_rate: float
    baseline_error_rate: float
    # Max relative regression across p95 and error_rate for this scenario.
    drift: float = Field(ge=0.0)
    sample_count: int = Field(ge=0)


class BaselineDriftEvidence(BaseModel):
    """``execution/runs/<batch>/baseline-drift.json`` (§5-A5 nightly).

    Relative regression against recent verified baseline samples. Out-of-band
    drift is a shortboard only; absolute-threshold inconclusive stays a
    deterministic stop elsewhere and is not invented here as a hard gate.
    """

    model_config = _FROZEN

    schema_version: Literal["1"]
    change_id: NonEmptyStr
    batch_id: NonEmptyStr
    status: BaselineDriftEvidenceStatus
    # Max scenario drift when evaluated; None when not evaluated / failed.
    value: float | None = None
    drift_band: float = Field(gt=0.0)
    window: int = Field(ge=1)
    scenarios: tuple[BaselineDriftScenario, ...] = ()
    collection_gaps: tuple[MetricCollectionGap, ...] = ()
    shortboards: tuple[MetricShortboard, ...] = ()
    source: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _status_agrees(self) -> Self:
        if self.status == "evaluated":
            if self.value is None:
                raise ValueError("evaluated baseline_drift requires value")
            if not self.scenarios:
                raise ValueError("evaluated baseline_drift requires scenario rows")
            return self
        if self.value is not None or self.scenarios:
            raise ValueError(f"{self.status} baseline_drift carries no value or scenarios")
        if self.status == "not_evaluated" and not any(
            board.code == "sample_insufficient" and board.metric == "baseline_drift"
            for board in self.shortboards
        ):
            raise ValueError("not_evaluated baseline_drift requires sample_insufficient shortboard")
        return self


class AdversarialYieldEvidence(BaseModel):
    """``execution/runs/<batch>/adversarial-yield.json`` (§5-B3 nightly).

    Scalar yield is the count of unique confirmed counterexamples for the search
    layer (Phase 1: ``api``). ``sample_count`` is the number of selections
    actually executed by the identified campaign, not its counterexample count.

    ``unclosed_count`` (Phase 1, discovery-only): confirmed CEs that do not yet
    resolve through the issue snapshot's Observation → Occurrence → Problem
    projection (with the historical setup link retained as compatibility input).
    Task 2's ``adversarial_clean`` hard rule consumes this count.

    Optional C3 summary (M3 Task 3): ``seed_replay_*`` fields are attached only
    when immutable attempt receipts exist under discovery. Absence → all three
    remain ``None`` (not_evaluated); never invent ``0/0 = 1.0``. These fields
    are telemetry for report / expansion gate — not a ``MetricKey`` floor.
    """

    model_config = _FROZEN

    schema_version: Literal["1"]
    change_id: NonEmptyStr
    batch_id: NonEmptyStr
    property: NonEmptyStr
    layer: AdversarialYieldLayer
    sample_count: int = Field(ge=0)
    counterexample_ids: tuple[str, ...] = ()
    unclosed_count: int = Field(ge=0)
    seed: int = 0
    status: AdversarialYieldEvidenceStatus
    # Unique confirmed CE count when evaluated; None otherwise.
    value: float | None = None
    # C3 summary from discovery replay receipts (optional; None = not_evaluated).
    seed_replay_success: int | None = None
    seed_replay_attempts: int | None = None
    seed_replay_rate: float | None = None
    collection_gaps: tuple[MetricCollectionGap, ...] = ()
    shortboards: tuple[MetricShortboard, ...] = ()
    source: dict[str, str] = Field(default_factory=dict)

    @field_validator("counterexample_ids", mode="after")
    @classmethod
    def _sort_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted(value))

    @model_validator(mode="after")
    def _status_and_counts_agree(self) -> Self:
        confirmed = len(self.counterexample_ids)
        if self.unclosed_count > confirmed:
            raise ValueError("unclosed_count cannot exceed confirmed counterexample_ids")
        replay_fields = (
            self.seed_replay_success,
            self.seed_replay_attempts,
            self.seed_replay_rate,
        )
        if any(v is not None for v in replay_fields) and any(v is None for v in replay_fields[:2]):
            raise ValueError("seed_replay_success and seed_replay_attempts must be set together")
        if self.seed_replay_attempts is not None:
            if self.seed_replay_attempts < 1:
                raise ValueError("seed_replay_attempts must be >= 1 when set (0 → leave unset)")
            if self.seed_replay_success is None or self.seed_replay_success > self.seed_replay_attempts:
                raise ValueError("seed_replay_success must be within seed_replay_attempts")
            expected_rate = self.seed_replay_success / self.seed_replay_attempts
            if self.seed_replay_rate is None or abs(self.seed_replay_rate - expected_rate) > 1e-9:
                raise ValueError(
                    f"seed_replay_rate={self.seed_replay_rate} contradicts "
                    f"{self.seed_replay_success}/{self.seed_replay_attempts}"
                )
        elif self.seed_replay_rate is not None:
            raise ValueError("seed_replay_rate requires seed_replay_attempts")
        if self.status == "evaluated":
            if self.value is None or abs(self.value - float(confirmed)) > 1e-9:
                raise ValueError(f"value={self.value} contradicts confirmed count {confirmed}")
            return self
        if self.value is not None:
            raise ValueError(f"{self.status} adversarial_yield cannot publish a value")
        if self.status == "not_evaluated" and not any(
            board.code == "pending_nightly" and board.metric == "adversarial_yield"
            for board in self.shortboards
        ):
            raise ValueError("not_evaluated adversarial_yield requires pending_nightly shortboard")
        return self


__all__ = [
    "AdversarialYieldEvidence",
    "AdversarialYieldEvidenceStatus",
    "AdversarialYieldLayer",
    "AdversarialYieldRel",
    "AssertionStrengthEvidence",
    "AssertionStrengthEvidenceStatus",
    "AssertionStrengthRel",
    "AssertionStrengthSurfaceLayer",
    "AssertionStrengthSurfaceSlice",
    "AuthMatrixCellResult",
    "AuthMatrixEvidence",
    "AuthMatrixRel",
    "BaselineDriftEvidence",
    "BaselineDriftEvidenceStatus",
    "BaselineDriftRel",
    "BaselineDriftScenario",
    "ConstraintCoverageEvidence",
    "ConstraintCoverageRel",
    "CoverageDiffEvidence",
    "CoverageDiffFile",
    "CoverageDiffRel",
    "JourneyCoverageEvidence",
    "JourneyCoverageItem",
    "JourneyCoverageRel",
    "MutationEvidence",
    "MutationEvidenceStatus",
    "MutationRel",
    "MutationSurvivor",
    "PerfSlackEvidence",
    "PerfSlackRel",
    "PerfSlackScenario",
    "WeakAssertionLocator",
]
