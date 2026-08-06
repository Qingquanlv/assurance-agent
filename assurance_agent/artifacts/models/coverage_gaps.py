"""inspect/coverage-gaps.json (must_compat): typed dual-source coverage gap signals.

Lane B feedstock for ``build-coverage-gap-signals`` (design §7.1). Pure typed
gaps folded from ``TraceProjection`` + sufficiency — never MetricKey / M4.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Self, get_args

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from assurance_agent.artifacts.models.common import NonEmptyStr

_FROZEN = ConfigDict(frozen=True, extra="forbid")

COVERAGE_GAPS_REL = "inspect/coverage-gaps.json"

CoverageGapsSchemaVersion = Literal["1"]

# Closed vocabulary — unknown kinds fail validation (fail-closed).
CoverageGapKind = Literal[
    "uncovered_required_case",
    "stale_required_case",
    "constraint_without_property",
    "matrix_cell_unasserted",
    "unmapped_test_cluster",
]

# Declaration order is the deterministic sort order for kinds.
COVERAGE_GAP_KIND_ORDER: tuple[CoverageGapKind, ...] = get_args(CoverageGapKind)

CoverageGapLayer = Literal["execution", "declaration"]


class CoverageGapLocator(BaseModel):
    """Mechanical locator: case_id / constraint key / matrix cell / cluster."""

    model_config = _FROZEN

    case_id: NonEmptyStr | None = None
    constraint_key: NonEmptyStr | None = None
    cell: NonEmptyStr | None = None
    cluster_key: NonEmptyStr | None = None

    @model_validator(mode="after")
    def _at_least_one_key(self) -> Self:
        if not any((self.case_id, self.constraint_key, self.cell, self.cluster_key)):
            raise ValueError("locator requires at least one of case_id, constraint_key, cell, cluster_key")
        return self


class CoverageGap(BaseModel):
    """One typed coverage gap with batch + projection digest evidence refs."""

    model_config = _FROZEN

    kind: CoverageGapKind
    locator: CoverageGapLocator
    layer: CoverageGapLayer
    batch_id: NonEmptyStr
    evidence_refs: tuple[NonEmptyStr, ...] = ()


class CoverageGapFeedstock(BaseModel):
    """Optional projection extensions not yet on the TraceProjection wire.

    Phase-1 active fold only reads projection rows / unmapped_tests / sufficiency.
    Extension kinds emit **only** when these lists are non-empty — never invented.
    """

    model_config = _FROZEN

    constraints_without_property: tuple[NonEmptyStr, ...] = ()
    matrix_cells_unasserted: tuple[NonEmptyStr, ...] = ()


class CoverageGapsDocument(BaseModel):
    """Authoritative typed gap list at ``inspect/coverage-gaps.json``."""

    model_config = _FROZEN

    schema_version: CoverageGapsSchemaVersion
    change_id: NonEmptyStr
    batch_id: NonEmptyStr
    projection_digest: NonEmptyStr
    gaps: tuple[CoverageGap, ...] = ()
    computed_at: datetime | None = None

    @field_validator("gaps", mode="after")
    @classmethod
    def _deterministic_gap_order(cls, value: tuple[CoverageGap, ...]) -> tuple[CoverageGap, ...]:
        kind_rank = {kind: index for index, kind in enumerate(COVERAGE_GAP_KIND_ORDER)}
        return tuple(
            sorted(
                value,
                key=lambda gap: (
                    kind_rank[gap.kind],
                    gap.locator.case_id or "",
                    gap.locator.constraint_key or "",
                    gap.locator.cell or "",
                    gap.locator.cluster_key or "",
                    gap.batch_id,
                ),
            )
        )


__all__ = [
    "COVERAGE_GAPS_REL",
    "COVERAGE_GAP_KIND_ORDER",
    "CoverageGap",
    "CoverageGapFeedstock",
    "CoverageGapKind",
    "CoverageGapLayer",
    "CoverageGapLocator",
    "CoverageGapsDocument",
    "CoverageGapsSchemaVersion",
]
