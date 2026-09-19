"""Change impact inventory contracts owned by assurance-intake.

The inventory records what a change touched (seeds), which behavior may be affected,
what must be verified, which assets already exist, and one disposition per row. The
kernel authenticates every reference against the sealed Explore context and requires
every seed to be explained or excluded. It never judges whether a row's analysis is
right; digests prove provenance, not correctness.
"""

from __future__ import annotations

from pathlib import PurePosixPath
import re
from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

from graph_engine.plugin_api import FrozenModel

from assurance_intake.contracts.common import TEST_FAMILY_ORDER, TestFamily

ImpactDisposition = Literal["reuse", "modify", "add", "capability_gap", "pending_confirmation"]
BehaviorKind = Literal["api", "journey", "role", "data_constraint"]
SeedReason = Literal["diff", "requirement_hint"]
ImpactConfidence = Literal["high", "medium", "low"]

INVENTORY_PATH = "qa/results/explore/impact-inventory.json"
CHANGE_EVIDENCE_PATH = "qa/results/explore/change-evidence.json"

_OPEN_DISPOSITIONS: frozenset[str] = frozenset({"capability_gap", "pending_confirmation"})
_ACTIONABLE_DISPOSITIONS: frozenset[str] = frozenset({"add", "modify"})
_KIND_FAMILY: dict[str, TestFamily] = {
    "api": "api",
    "journey": "e2e",
    "role": "api",
    "data_constraint": "api",
}
_SHA256 = r"^[0-9a-f]{64}$"
_CASE_MODULE_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _canonical_relative(value: str, label: str) -> str:
    path = PurePosixPath(value)
    if path.is_absolute() or path.as_posix() != value or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"{label} must be a canonical relative path")
    return value


def _require_unique(values: tuple[str, ...], label: str) -> tuple[str, ...]:
    if len(set(values)) != len(values):
        duplicates = sorted({value for value in values if values.count(value) > 1})
        raise ValueError(f"{label} must be unique: {duplicates}")
    return values


class ChangedFileV1(FrozenModel):
    path: str = Field(min_length=1)
    status: Literal["added", "modified", "deleted", "renamed"]
    symbols: tuple[str, ...] = ()
    digest: str | None = Field(default=None, pattern=_SHA256)

    @field_validator("path")
    @classmethod
    def _path(cls, value: str) -> str:
        return _canonical_relative(value, "changed file path")

    @field_validator("symbols")
    @classmethod
    def _symbols(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item.strip() for item in value):
            raise ValueError("changed file symbols must be non-empty strings")
        return _require_unique(value, "changed file symbols")

    @model_validator(mode="after")
    def _deleted_has_no_digest(self) -> Self:
        if (self.status == "deleted") != (self.digest is None):
            raise ValueError("deleted files carry no digest; every other status requires one")
        return self


class ChangeEvidenceV1(FrozenModel):
    """Product-sealed diff projection, produced outside the attempt and read as content."""

    schema_version: Literal["1"] = "1"
    change_id: str = Field(min_length=1)
    base_ref: str = Field(min_length=1)
    head_ref: str = Field(min_length=1)
    changed_files: tuple[ChangedFileV1, ...]

    @field_validator("changed_files")
    @classmethod
    def _unique_paths(cls, value: tuple[ChangedFileV1, ...]) -> tuple[ChangedFileV1, ...]:
        _require_unique(tuple(item.path for item in value), "changed file paths")
        return value


class ImpactSeedV1(FrozenModel):
    seed_id: str = Field(pattern=r"^CF-\d{3,}$")
    path: str = Field(min_length=1)
    symbol: str | None = Field(default=None, min_length=1)
    reason: SeedReason

    @field_validator("path")
    @classmethod
    def _path(cls, value: str) -> str:
        return _canonical_relative(value, "seed path")


class CandidateCaseV1(FrozenModel):
    evidence_id: str = Field(pattern=r"^CS-\d{3,}$")
    case_id: str = Field(min_length=1)
    module: str = Field(min_length=1)
    path: str = Field(min_length=1)
    title: str = Field(min_length=1)

    @field_validator("path")
    @classmethod
    def _path(cls, value: str) -> str:
        parts = PurePosixPath(_canonical_relative(value, "candidate case path")).parts
        if len(parts) < 4 or parts[:2] != ("qa", "cases") or parts[-1] != "case.yaml":
            raise ValueError("candidate case path must be qa/cases/<module>/case.yaml")
        return value


class HistoricalProblemV1(FrozenModel):
    evidence_id: str = Field(pattern=r"^HI-\d{3,}$")
    problem_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    classification: str | None = None
    status: str | None = None


class ImpactProjectionV1(FrozenModel):
    """Graph-owned change evidence and reusable assets, with the ids rows may cite."""

    diff_base: Literal["change-evidence", "content-snapshot"]
    seeds: tuple[ImpactSeedV1, ...]
    unobserved_hints: tuple[str, ...] = ()
    candidate_cases: tuple[CandidateCaseV1, ...]
    historical_problems: tuple[HistoricalProblemV1, ...]
    factory_leafs: tuple[str, ...]

    @field_validator("factory_leafs")
    @classmethod
    def _factory_leafs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))):
            raise ValueError("factory_leafs must be sorted and unique")
        return value

    @model_validator(mode="after")
    def _unique_identities(self) -> Self:
        _require_unique(tuple(item.seed_id for item in self.seeds), "seed_id values")
        _require_unique(
            tuple(item.evidence_id for item in self.candidate_cases), "candidate case evidence_id values"
        )
        _require_unique(tuple(item.case_id for item in self.candidate_cases), "candidate case_id values")
        _require_unique(
            tuple(item.evidence_id for item in self.historical_problems),
            "historical problem evidence_id values",
        )
        _require_unique(
            tuple(item.problem_id for item in self.historical_problems), "historical problem_id values"
        )
        return self

    def seed_ids(self) -> frozenset[str]:
        return frozenset(item.seed_id for item in self.seeds)

    def resolvable_ids(self) -> frozenset[str]:
        ids: set[str] = set(self.seed_ids())
        for case in self.candidate_cases:
            ids.update((case.evidence_id, case.case_id))
        for problem in self.historical_problems:
            ids.update((problem.evidence_id, problem.problem_id))
        return frozenset(ids)


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
        return _require_unique(value, "asset ids")


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
        return _require_unique(value, "evidence ids")

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
        _require_unique(tuple(row.row_id for row in self.rows), "row_id values")
        _require_unique(tuple(item.seed_id for item in self.exclusions), "exclusion seed_id values")
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


def validate_inventory_references(
    inventory: ChangeImpactInventoryV1,
    *,
    resolvable: frozenset[str],
    seed_ids: frozenset[str],
    capability_leafs: frozenset[str],
) -> None:
    """Every cited id must resolve to sealed context; every seed must be handled."""

    errors: list[str] = []
    for row in inventory.rows:
        for label, ids in (
            ("change_evidence_ids", row.change_evidence_ids),
            ("expected_basis_ids", row.expected_basis_ids),
            ("assets.case_ids", row.assets.case_ids),
            ("assets.problem_ids", row.assets.problem_ids),
        ):
            unknown = sorted(set(ids) - resolvable)
            if unknown:
                errors.append(f"{row.row_id}: unresolvable {label}: {unknown}")
        unknown_leafs = sorted(set(row.assets.factory_leafs) - capability_leafs)
        if unknown_leafs:
            errors.append(f"{row.row_id}: assets.factory_leafs outside the typed catalog: {unknown_leafs}")
        behavior = row.affected_behavior
        if (
            behavior.kind == "data_constraint"
            and row.disposition != "capability_gap"
            and behavior.key not in capability_leafs
        ):
            errors.append(
                f"{row.row_id}: data_constraint key is not a typed leaf; declare capability_gap: {behavior.key}"
            )
    for exclusion in inventory.exclusions:
        if exclusion.seed_id not in seed_ids:
            errors.append(f"exclusion cites unknown seed: {exclusion.seed_id}")
    handled = {seed_id for row in inventory.rows for seed_id in row.change_evidence_ids}
    handled.update(item.seed_id for item in inventory.exclusions)
    unhandled = sorted(seed_ids - handled)
    if unhandled:
        errors.append(f"seeds without an impact row or exclusion: {unhandled}")
    if errors:
        raise ValueError("; ".join(errors))


def validate_inventory_closed_keys(
    inventory: ChangeImpactInventoryV1,
    *,
    journey_keys: frozenset[str],
) -> None:
    """Journey rows must name an authenticated journey unless they declare a capability gap."""

    errors = [
        f"{row.row_id}: journey key is not an authenticated journey; declare capability_gap: {row.affected_behavior.key}"
        for row in inventory.rows
        if row.affected_behavior.kind == "journey"
        and row.disposition != "capability_gap"
        and row.affected_behavior.key not in journey_keys
    ]
    if errors:
        raise ValueError("; ".join(errors))


def impact_required_families(inventory: ChangeImpactInventoryV1) -> tuple[TestFamily, ...]:
    """Families that closed rows need; open rows never retain a family."""

    families = {
        _KIND_FAMILY[row.affected_behavior.kind]
        for row in inventory.rows
        if row.disposition not in _OPEN_DISPOSITIONS
    }
    return tuple(family for family in TEST_FAMILY_ORDER if family in families)


def impact_row_identity(*, plan_digest: str, inventory_digest: str, row_id: str) -> tuple[str, str, str]:
    """Full impact-row identity. Naked IR-* is not reusable across inventories."""
    if not plan_digest or not inventory_digest or not row_id:
        raise ValueError("impact row identity requires plan digest, inventory digest, and row_id")
    return (plan_digest, inventory_digest, row_id)


__all__ = [
    "AffectedBehaviorV1",
    "BehaviorKind",
    "CHANGE_EVIDENCE_PATH",
    "CandidateCaseV1",
    "ChangeEvidenceV1",
    "ChangeImpactInventoryV1",
    "ChangedFileV1",
    "HistoricalProblemV1",
    "INVENTORY_PATH",
    "ImpactAssetsV1",
    "ImpactConfidence",
    "ImpactDisposition",
    "ImpactExclusionV1",
    "ImpactProjectionV1",
    "ImpactRowV1",
    "ImpactSeedV1",
    "SeedReason",
    "impact_required_families",
    "impact_row_identity",
    "validate_inventory_closed_keys",
    "validate_inventory_references",
]
