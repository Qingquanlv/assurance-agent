"""Explore op input and the files the explore Agent and finalize own."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from graph_engine.plugin_api import FrozenModel

from assurance_intake.contracts.agent import SkillInputV1
from assurance_intake.contracts.common import (
    SHA256_PATTERN,
    TestFamily,
    require_unique,
    validate_family_tuple,
)
from assurance_intake.contracts.explore import EXPLORATION_PATH
from assurance_intake.contracts.impact import INVENTORY_PATH
from assurance_intake.contracts.obligations import SourceKind
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1

SeedReason = Literal["diff", "requirement_hint"]

CONTEXT_PATH = "qa/results/explore/context.json"
CHANGE_EVIDENCE_PATH = "qa/results/explore/change-evidence.json"
REQUIREMENT_CONTEXT_BUDGET = 65536
EXPLORATION_DRAFT_PATH = "qa/results/explore/exploration-draft.json"
EXPLORE_AGENT_OUTPUT_PATHS: tuple[str, ...] = (EXPLORATION_DRAFT_PATH, INVENTORY_PATH)
EXPLORE_OFFICIAL_OUTPUT_PATHS: tuple[str, ...] = (EXPLORATION_PATH, INVENTORY_PATH)


class ExploreInputV1(SkillInputV1):
    candidate_test_families: tuple[TestFamily, ...] = Field(min_length=1)

    @field_validator("candidate_test_families")
    @classmethod
    def _candidate_test_families(cls, value: tuple[TestFamily, ...]) -> tuple[TestFamily, ...]:
        return validate_family_tuple(value)


def _canonical_relative(value: str, label: str) -> str:
    path = PurePosixPath(value)
    if path.is_absolute() or path.as_posix() != value or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"{label} must be a canonical relative path")
    return value


class ChangedFileV1(FrozenModel):
    path: str = Field(min_length=1)
    status: Literal["added", "modified", "deleted", "renamed"]
    symbols: tuple[str, ...] = ()
    digest: str | None = Field(default=None, pattern=SHA256_PATTERN)

    @field_validator("path")
    @classmethod
    def _path(cls, value: str) -> str:
        return _canonical_relative(value, "changed file path")

    @field_validator("symbols")
    @classmethod
    def _symbols(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item.strip() for item in value):
            raise ValueError("changed file symbols must be non-empty strings")
        return require_unique(value, "changed file symbols")

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
        require_unique(tuple(item.path for item in value), "changed file paths")
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
        require_unique(tuple(item.seed_id for item in self.seeds), "seed_id values")
        require_unique(
            tuple(item.evidence_id for item in self.candidate_cases), "candidate case evidence_id values"
        )
        require_unique(tuple(item.case_id for item in self.candidate_cases), "candidate case_id values")
        require_unique(
            tuple(item.evidence_id for item in self.historical_problems),
            "historical problem evidence_id values",
        )
        require_unique(
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


class RequirementReadFactsV1(FrozenModel):
    total_bytes: int = Field(ge=0)
    provided_bytes: int = Field(ge=0)
    read_state: Literal["complete", "truncated"]


class SourceCatalogEntryV1(FrozenModel):
    source_id: str = Field(min_length=1)
    kind: SourceKind
    artifact: EvidenceArtifactRefV1
    quotable: bool


class ExploreContextV1(BaseModel):
    """Deterministic graph-owned evidence inventory supplied to Explore."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    change_id: str = Field(min_length=1)
    generated_at: Literal["1970-01-01T00:00:00Z"] = "1970-01-01T00:00:00Z"
    requirement_summary: str | None = None
    aggregation_policy: dict[str, Any]
    archive_window: dict[str, Any]
    staleness: dict[str, Any]
    impact: ImpactProjectionV1
    case_signals: list[Any]
    test_health: list[Any]
    historical_issues: list[Any]
    evidence: list[Any]
    source_catalog: tuple[SourceCatalogEntryV1, ...]
    requirement_read_facts: RequirementReadFactsV1
    degraded: bool
    degraded_reasons: list[str]
    no_git: bool
