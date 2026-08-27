"""inspect/trace-projection.json — fact-only case↔execution↔issue projection.

Projection carries no policy judgment and no wall-clock freshness; consumers
apply ``evaluate_sufficiency(projection, policy, *, as_of)`` at the use site.

Wire boundary: V1 remains the legacy reader; V2 adds recovery gap codes and
semantic validators. Registry-facing dispatch is ``TraceProjectionDocument``.

Union notes (merge of assemble enrichments + V6 variants):
- ``TraceRow.problem_facts`` carries the §9.4/§9.5 join facts next to
  ``open_problem_ids`` (assemble/OURS).
- ``TraceTestRef`` stores ``test_name`` (assemble/OURS tree scan) and accepts
  the V6 ``function`` alias on input.
- ``TraceIntegrity`` accepts both ``complete_with_gaps`` (assemble) and
  ``degraded`` (V6) spellings.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    ValidationInfo,
    model_validator,
)

from assurance_generation.contracts.families import CASE_TYPES, LAYER_NAMES, CaseType, LayerName
from assurance_intake.contracts import NonEmptyStr
from assurance_quality.contracts.common import frozen_catalog, require_catalog_members

_FROZEN = ConfigDict(frozen=True, extra="forbid")

StrictNonNegativeInt = Annotated[int, Field(strict=True, ge=0)]
StrictPositiveInt = Annotated[int, Field(strict=True, gt=0)]

TraceTarget = Literal["api", "e2e", "fuzz", "performance"]
TraceCaseType = Literal["API", "E2E", "Fuzz", "Performance"]

TraceGapCodeV1 = Literal[
    "result_missing",
    "result_corrupt",
    "batch_id_unparseable",
    "manifest_missing",
    "case_unreadable",
    "failure_analysis_missing",
    "issues_snapshot_missing",
    "problems_snapshot_missing",
    "mapped_test_missing_from_tree",
    "tests_tree_digest_mismatch",
    "result_identity_mismatch",
    "problem_alias_invalid",
]

TraceGapCodeV2 = (
    TraceGapCodeV1
    | Literal[
        "failure_analysis_identity_mismatch",
        "issues_snapshot_identity_mismatch",
        "issue_analysis_failed",
        "project_sync_pending",
        "issue_reconcile_failed",
        "issue_reconciliation_unavailable",
    ]
)
TraceSummaryGapCode = TraceGapCodeV1 | TraceGapCodeV2

# Legacy aliases — V1 only; never repoint to V2.
TraceGapCode = TraceGapCodeV1

# Accept both assemble (complete_with_gaps) and V6 (degraded) vocabularies.
TraceIntegrity = Literal["complete", "complete_with_gaps", "degraded", "incomplete"]

_CASE_TYPE_TO_TARGET: dict[str, str] = {
    "API": "api",
    "E2E": "e2e",
    "Fuzz": "fuzz",
    "Performance": "performance",
}
_ALLOWED_GAP_TARGETS = frozenset(LAYER_NAMES)


class TraceExecution(BaseModel):
    model_config = _FROZEN

    batch_id: str
    target: TraceTarget
    status: Literal["passed", "failed", "skipped"]
    ts: datetime
    ts_source: Literal["executed_at", "batch_id_legacy_utc"]


class TraceFailure(BaseModel):
    model_config = _FROZEN

    category: str
    severity: str


class TraceProblemFact(BaseModel):
    """One canonical Problem a case reaches through the §9 two-hop join.

    Populated only when phase=reconciled. ``problem_id`` is the *canonical*
    problem — the end of the ``merged_into`` alias chain — and
    ``source_problem_ids`` lists every problem_id the change's occurrences
    actually referenced to get there.
    """

    model_config = _FROZEN

    problem_id: str
    source_problem_ids: tuple[str, ...]
    fingerprint: str
    status: str
    classification: str
    open_product_bug: bool


class TraceGapV1(BaseModel):
    model_config = _FROZEN

    code: TraceGapCodeV1
    source: str
    batch_id: str | None = None
    target: str | None = None
    detail: str = ""


class TraceGapV2(BaseModel):
    model_config = _FROZEN

    code: TraceGapCodeV2
    source: str
    batch_id: str | None = None
    target: str | None = None
    detail: str = ""


TraceGap = TraceGapV1


class TraceTestRef(BaseModel):
    """One current-tree test function mapped to a case_id (spec §7 scan hit)."""

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    file: str
    test_name: str = Field(validation_alias=AliasChoices("test_name", "function"))

    @property
    def function(self) -> str:
        """V6 alias for ``test_name``."""
        return self.test_name


class UnmappedTest(BaseModel):
    model_config = _FROZEN

    file: str
    test_name: str


class TraceRow(BaseModel):
    model_config = _FROZEN

    case_id: str
    module: str
    case_type: TraceCaseType
    automation_required: bool
    assertions: tuple[str, ...] = ()
    covering_tests: tuple[TraceTestRef, ...] = ()
    coverage_state: Literal["covered", "uncovered", "not_required"]
    latest_execution: TraceExecution | None = None
    freshest_pass: TraceExecution | None = None
    presence_in_current_batch: Literal["executed", "not_in_current_batch", "target_not_selected"]
    atemporal_kinds_present: tuple[str, ...] = ()
    failures: tuple[TraceFailure, ...] = ()
    # Routing subset: problem_facts with open_product_bug, fingerprint-deduped.
    open_problem_ids: tuple[str, ...] = ()
    problem_facts: tuple[TraceProblemFact, ...] = ()
    capability: str | None = None
    plan_id: str | None = None
    schema_id: str | None = None
    issue_ids: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()

    @property
    def test_path(self) -> str:
        if not self.covering_tests:
            return ""
        return self.covering_tests[0].file


class TraceSource(BaseModel):
    model_config = _FROZEN

    path: str
    exists: bool
    sha256: str | None = None


def _gap_identity(gap: TraceGapV1 | TraceGapV2) -> tuple[str, str, str, str, str]:
    return (
        gap.code,
        gap.source,
        gap.batch_id or "",
        gap.target or "",
        gap.detail,
    )


def validate_unique_case_ids(rows: tuple[TraceRow, ...]) -> None:
    seen: set[str] = set()
    for row in rows:
        if row.case_id in seen:
            raise ValueError(f"duplicate case_id: {row.case_id}")
        seen.add(row.case_id)


def validate_unique_source_paths(sources: tuple[TraceSource, ...]) -> None:
    seen: set[str] = set()
    for source in sources:
        if source.path in seen:
            raise ValueError(f"duplicate TraceSource path: {source.path}")
        seen.add(source.path)


def validate_unique_gaps(gaps: tuple[TraceGapV1, ...] | tuple[TraceGapV2, ...]) -> None:
    seen: set[tuple[str, str, str, str, str]] = set()
    for gap in gaps:
        key = _gap_identity(gap)
        if key in seen:
            raise ValueError(f"duplicate gap: {key}")
        seen.add(key)


def validate_row_semantics(phase: Literal["execution", "reconciled"], rows: tuple[TraceRow, ...]) -> None:
    for row in rows:
        if row.automation_required:
            if row.coverage_state == "not_required":
                raise ValueError(f"row {row.case_id}: automation_required requires covered/uncovered")
        elif row.coverage_state != "not_required":
            raise ValueError(f"row {row.case_id}: non-automated row must use coverage_state=not_required")

        if phase == "execution" and (row.failures or row.open_problem_ids or row.problem_facts):
            raise ValueError(f"row {row.case_id}: execution phase must not carry failure/problem enrichment")

        expected_target = _CASE_TYPE_TO_TARGET[row.case_type]
        for execution in (row.latest_execution, row.freshest_pass):
            if execution is not None and execution.target != expected_target:
                raise ValueError(
                    f"row {row.case_id}: execution target {execution.target!r} "
                    f"does not match case_type {row.case_type!r}"
                )


def validate_gap_targets(gaps: tuple[TraceGapV1, ...] | tuple[TraceGapV2, ...]) -> None:
    for gap in gaps:
        if gap.target is not None and gap.target not in _ALLOWED_GAP_TARGETS:
            raise ValueError(f"gap target must be empty or one of {LAYER_NAMES}: {gap.target!r}")


class TraceProjectionV1(BaseModel):
    model_config = _FROZEN

    schema_version: Literal["1"] = "1"
    change_id: str
    phase: Literal["execution", "reconciled"]
    authoritative_batch_id: str
    sources: tuple[TraceSource, ...] = ()
    rows: tuple[TraceRow, ...] = ()
    unmapped_tests: tuple[UnmappedTest, ...] = ()
    gaps: tuple[TraceGapV1, ...] = ()
    integrity: TraceIntegrity


class TraceProjectionV2(BaseModel):
    model_config = _FROZEN

    schema_version: Literal["2"] = "2"
    change_id: str
    phase: Literal["execution", "reconciled"]
    authoritative_batch_id: str
    sources: tuple[TraceSource, ...] = ()
    rows: tuple[TraceRow, ...] = ()
    unmapped_tests: tuple[UnmappedTest, ...] = ()
    gaps: tuple[TraceGapV2, ...] = ()
    integrity: TraceIntegrity

    @model_validator(mode="after")
    def _validate_semantics(self, info: ValidationInfo) -> Self:
        validate_unique_case_ids(self.rows)
        validate_unique_source_paths(self.sources)
        validate_unique_gaps(self.gaps)
        validate_row_semantics(self.phase, self.rows)
        validate_gap_targets(self.gaps)
        leafs = frozen_catalog(info, "capability_leafs", required=True)
        require_catalog_members(
            leafs,
            (row.capability for row in self.rows),
            error="trace capability is not a frozen typed leaf",
        )
        require_catalog_members(
            frozen_catalog(info, "case_ids"),
            (row.case_id for row in self.rows),
            error="case is not a frozen catalog member",
        )
        require_catalog_members(
            frozen_catalog(info, "plan_ids"),
            (row.plan_id for row in self.rows),
            error="plan is not a frozen catalog member",
        )
        test_ids = (f"{ref.file}::{ref.test_name}" for row in self.rows for ref in row.covering_tests)
        require_catalog_members(
            frozen_catalog(info, "test_ids"),
            test_ids,
            error="test is not a frozen catalog member",
        )
        require_catalog_members(
            frozen_catalog(info, "schema_ids"),
            (row.schema_id for row in self.rows),
            error="schema is not a frozen catalog member",
        )
        require_catalog_members(
            frozen_catalog(info, "issue_ids"),
            (issue_id for row in self.rows for issue_id in row.issue_ids),
            error="issue is not a frozen catalog member",
        )
        require_catalog_members(
            frozen_catalog(info, "evidence_refs"),
            (ref for row in self.rows for ref in row.evidence_refs),
            error="evidence is not a frozen catalog member",
        )
        return self


TraceProjection = TraceProjectionV1

TraceProjectionLike = TraceProjectionV1 | TraceProjectionV2

TraceProjectionVariant = Annotated[
    TraceProjectionV1 | TraceProjectionV2,
    Field(discriminator="schema_version"),
]


class TraceProjectionDocument(RootModel[TraceProjectionVariant]):
    @model_validator(mode="before")
    @classmethod
    def _legacy_missing_version(cls, raw: object) -> object:
        if isinstance(raw, dict) and "schema_version" not in raw:
            return {**raw, "schema_version": "1"}
        return raw


def load_trace_projection_document(raw: object) -> TraceProjectionLike:
    return TraceProjectionDocument.model_validate(raw).root


class TraceGapAggregate(BaseModel):
    model_config = _FROZEN

    total: StrictNonNegativeInt
    by_code: dict[TraceSummaryGapCode, StrictPositiveInt]

    @model_validator(mode="after")
    def _total_matches_breakdown(self) -> Self:
        if self.total != sum(self.by_code.values()):
            raise ValueError("gap total must equal by_code sum")
        return self


class TraceLayerFacts(BaseModel):
    model_config = _FROZEN

    layer: LayerName
    case_type: CaseType
    total: StrictNonNegativeInt
    automated: StrictNonNegativeInt
    covered: StrictNonNegativeInt
    uncovered: StrictNonNegativeInt
    not_required: StrictNonNegativeInt
    current_executed: StrictNonNegativeInt
    current_not_present: StrictNonNegativeInt
    target_not_selected: StrictNonNegativeInt
    latest_passed: StrictNonNegativeInt
    latest_failed: StrictNonNegativeInt
    latest_skipped: StrictNonNegativeInt
    never_run: StrictNonNegativeInt
    failure_rows: StrictNonNegativeInt
    failure_links: StrictNonNegativeInt
    open_problem_rows: StrictNonNegativeInt
    open_problem_links: StrictNonNegativeInt
    unique_open_problems: StrictNonNegativeInt
    gaps: TraceGapAggregate

    @model_validator(mode="after")
    def _validate_partitions(self) -> Self:
        if self.automated != self.covered + self.uncovered:
            raise ValueError("automated must equal covered + uncovered")
        if self.total != self.covered + self.uncovered + self.not_required:
            raise ValueError("total must equal covered + uncovered + not_required")
        if self.total != (self.current_executed + self.current_not_present + self.target_not_selected):
            raise ValueError("total must equal current presence partition")
        if self.total != (self.latest_passed + self.latest_failed + self.latest_skipped + self.never_run):
            raise ValueError("total must equal latest status partition")
        if self.failure_rows > self.total:
            raise ValueError("failure_rows cannot exceed total")
        if self.failure_links < self.failure_rows:
            raise ValueError("failure_links cannot be less than failure_rows")
        if self.open_problem_rows > self.total:
            raise ValueError("open_problem_rows cannot exceed total")
        if self.open_problem_links < self.open_problem_rows:
            raise ValueError("open_problem_links cannot be less than open_problem_rows")
        if self.unique_open_problems > self.open_problem_links:
            raise ValueError("unique_open_problems cannot exceed open_problem_links")
        if self.gaps.total != sum(self.gaps.by_code.values()):
            raise ValueError("gap total must equal by_code sum")
        return self


class TraceLayerFactSummary(BaseModel):
    model_config = _FROZEN

    schema_version: Literal["1"] = "1"
    change_id: NonEmptyStr
    phase: Literal["execution", "reconciled"]
    # Matches TraceProjection: fold may emit "" when the manifest is absent.
    authoritative_batch_id: str
    source_projection_digest: NonEmptyStr
    projection_integrity: TraceIntegrity
    layers: tuple[TraceLayerFacts, ...]
    global_gaps: TraceGapAggregate

    @model_validator(mode="after")
    def _validate_layers(self) -> Self:
        if len(self.layers) != len(LAYER_NAMES):
            raise ValueError("layers must contain exactly four layer rows")
        if tuple(layer.layer for layer in self.layers) != LAYER_NAMES:
            raise ValueError("layers must follow LAYER_NAMES order")
        if tuple(layer.case_type for layer in self.layers) != CASE_TYPES:
            raise ValueError("layers must follow CASE_TYPES order")
        if self.phase == "execution":
            for layer in self.layers:
                if (
                    layer.failure_rows
                    or layer.failure_links
                    or layer.open_problem_rows
                    or layer.open_problem_links
                    or layer.unique_open_problems
                ):
                    raise ValueError("execution phase failure/problem counts must be zero")
        return self


__all__ = [
    "TraceCaseType",
    "TraceExecution",
    "TraceFailure",
    "TraceGap",
    "TraceGapAggregate",
    "TraceGapCode",
    "TraceGapCodeV1",
    "TraceGapCodeV2",
    "TraceGapV1",
    "TraceGapV2",
    "TraceIntegrity",
    "TraceLayerFactSummary",
    "TraceLayerFacts",
    "TraceProblemFact",
    "TraceProjection",
    "TraceProjectionDocument",
    "TraceProjectionLike",
    "TraceProjectionV1",
    "TraceProjectionV2",
    "TraceProjectionVariant",
    "TraceRow",
    "TraceSource",
    "TraceSummaryGapCode",
    "TraceTarget",
    "TraceTestRef",
    "UnmappedTest",
    "load_trace_projection_document",
    "validate_gap_targets",
    "validate_row_semantics",
    "validate_unique_case_ids",
    "validate_unique_gaps",
    "validate_unique_source_paths",
]
