"""Change impact inventory contracts owned by assurance-intake.

The inventory records what a change touched (seeds), which behavior may be affected,
what must be verified, which assets already exist, and one disposition per row. The
kernel authenticates every reference against the sealed Explore context and requires
every seed to be explained or excluded. It never judges whether a row's analysis is
right; digests prove provenance, not correctness.
"""

from __future__ import annotations

import re
from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

from graph_engine.plugin_api import FrozenModel

from assurance_intake.contracts.common import require_unique

ImpactDisposition = Literal["reuse", "modify", "add", "capability_gap", "pending_confirmation"]
BehaviorKind = Literal["api", "journey", "role", "data_constraint"]
ImpactConfidence = Literal["high", "medium", "low"]

INVENTORY_PATH = "qa/results/explore/impact-inventory.json"

_OPEN_DISPOSITIONS: frozenset[str] = frozenset({"capability_gap", "pending_confirmation"})
_ACTIONABLE_DISPOSITIONS: frozenset[str] = frozenset({"add", "modify"})
_CASE_MODULE_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class AffectedBehaviorV1(FrozenModel):
    kind: BehaviorKind
    key: str = Field(min_length=1)


class ImpactAssetsV1(FrozenModel):
    case_ids: tuple[str, ...] = ()
    factory_leafs: tuple[str, ...] = ()
    problem_ids: tuple[str, ...] = ()

    @field_validator("case_ids", "factory_leafs", "problem_ids")
    @classmethod
    def _unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item.strip() for item in value):
            raise ValueError("asset ids must be non-empty strings")
        return require_unique(value, "asset ids")


class ImpactRowV1(FrozenModel):
    row_id: str = Field(pattern=r"^IR-\d{3,}$")
    change_evidence_ids: tuple[str, ...] = Field(min_length=1)
    affected_behavior: AffectedBehaviorV1
    case_module: str | None = None
    obligation: str = Field(min_length=1)  # analysis draft; formal expected behavior is PreparedObligationV1
    expected_basis_ids: tuple[str, ...] = ()
    assets: ImpactAssetsV1
    disposition: ImpactDisposition
    gap_reason: str | None = None
    confidence: ImpactConfidence

    @field_validator("change_evidence_ids", "expected_basis_ids")
    @classmethod
    def _ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item.strip() for item in value):
            raise ValueError("evidence ids must be non-empty strings")
        return require_unique(value, "evidence ids")

    @field_validator("case_module")
    @classmethod
    def _case_module(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if value != value.strip() or not value:
            raise ValueError("case_module must be a trimmed non-empty slug path")
        parts = value.split("/")
        if any(_CASE_MODULE_PART.fullmatch(part) is None for part in parts):
            raise ValueError("case_module must be slash-separated slug segments")
        return value

    @model_validator(mode="after")
    def _disposition_shape(self) -> Self:
        has_cases = bool(self.assets.case_ids)
        if self.disposition in {"reuse", "modify"} and not has_cases:
            raise ValueError(f"{self.row_id}: {self.disposition} requires assets.case_ids")
        if self.disposition == "add" and has_cases:
            raise ValueError(f"{self.row_id}: add cannot cite existing assets.case_ids; use modify or reuse")
        needs_reason = self.disposition in _OPEN_DISPOSITIONS
        has_reason = self.gap_reason is not None and bool(self.gap_reason.strip())
        if needs_reason and not has_reason:
            raise ValueError(f"{self.row_id}: {self.disposition} requires gap_reason")
        if not needs_reason and self.gap_reason is not None:
            raise ValueError(f"{self.row_id}: only capability_gap or pending_confirmation may set gap_reason")
        return self


class ImpactExclusionV1(FrozenModel):
    seed_id: str = Field(pattern=r"^CF-\d{3,}$")
    reason: str = Field(min_length=1)


class ChangeImpactInventoryV1(FrozenModel):
    """Runtime-authored inventory; the kernel seals it after reference validation."""

    schema_version: Literal["1"]
    change_id: str = Field(min_length=1)
    context_ref: Literal["explore/context.json"]
    rows: tuple[ImpactRowV1, ...]
    exclusions: tuple[ImpactExclusionV1, ...]

    @model_validator(mode="after")
    def _unique_identities(self) -> Self:
        require_unique(tuple(row.row_id for row in self.rows), "row_id values")
        require_unique(tuple(item.seed_id for item in self.exclusions), "exclusion seed_id values")
        cited = {seed_id for row in self.rows for seed_id in row.change_evidence_ids}
        contradictory = sorted(cited & {item.seed_id for item in self.exclusions})
        if contradictory:
            raise ValueError(f"seeds cannot be both cited and excluded: {contradictory}")
        return self

    def cited_ids(self) -> frozenset[str]:
        ids: set[str] = set()
        for row in self.rows:
            ids.update(row.change_evidence_ids)
            ids.update(row.expected_basis_ids)
            ids.update(row.assets.case_ids)
            ids.update(row.assets.problem_ids)
        return frozenset(ids)

    def open_rows(self) -> tuple[ImpactRowV1, ...]:
        return tuple(row for row in self.rows if row.disposition in _OPEN_DISPOSITIONS)

    def actionable_rows(self) -> tuple[ImpactRowV1, ...]:
        return tuple(row for row in self.rows if row.disposition in _ACTIONABLE_DISPOSITIONS)


def impact_row_identity(*, plan_digest: str, inventory_digest: str, row_id: str) -> tuple[str, str, str]:
    """Full impact-row identity. Naked IR-* is not reusable across inventories."""
    if not plan_digest or not inventory_digest or not row_id:
        raise ValueError("impact row identity requires plan digest, inventory digest, and row_id")
    return (plan_digest, inventory_digest, row_id)


__all__ = [
    "AffectedBehaviorV1",
    "BehaviorKind",
    "ChangeImpactInventoryV1",
    "INVENTORY_PATH",
    "ImpactAssetsV1",
    "ImpactConfidence",
    "ImpactDisposition",
    "ImpactExclusionV1",
    "ImpactRowV1",
    "impact_row_identity",
]
