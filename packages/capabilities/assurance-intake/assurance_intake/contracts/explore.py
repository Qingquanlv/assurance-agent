from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, model_validator

from graph_engine.plugin_api import FrozenModel

from assurance_intake.contracts.common import MrcCategory, MrcLayer
from assurance_intake.contracts.obligations import PreparedObligationV1, SourceKind
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1
from assurance_intake.contracts.impact import (
    INVENTORY_PATH,
    ImpactProjectionV1,
)

EXPLORATION_PATH = "qa/results/explore/exploration.json"
EXPLORATION_DRAFT_PATH = "qa/results/explore/exploration-draft.json"
REQUIREMENT_PATH = "qa/requirement.md"
RUN_SPEC_SNAPSHOT_PATH = "qa/results/intake/sources/run-spec.effective.yaml"
REQUIREMENT_CONTEXT_BUDGET = 65536
EXPLORE_AGENT_OUTPUT_PATHS: tuple[str, ...] = (EXPLORATION_DRAFT_PATH, INVENTORY_PATH)
EXPLORE_OFFICIAL_OUTPUT_PATHS: tuple[str, ...] = (EXPLORATION_PATH, INVENTORY_PATH)
EXPLORE_OUTPUT_PATHS: tuple[str, ...] = EXPLORE_AGENT_OUTPUT_PATHS


class RequirementReadFactsV1(FrozenModel):
    total_bytes: int = Field(ge=0)
    provided_bytes: int = Field(ge=0)
    read_state: Literal["complete", "truncated"]


class SourceCodeEvidenceV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    source: Literal["source_code"]
    type: str = Field(min_length=1)
    description: str = Field(min_length=1)
    parse_confidence_cap: Literal["medium", "low"] = "medium"
    module: str | None = None


class EvidenceInventoryV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    available: list[Any]
    missing: list[Any]
    not_inspected: list[Any]


class CaseDesignGuidanceV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    priority_hints: list[Any]
    suggested_scenarios: list[Any]
    regression_focus: list[Any]


class TestScopeV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    in_scope: list[str]
    out_of_scope: list[str]


class LayerRecommendationV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    layer: Literal["API", "E2E", "Fuzz", "Performance"]
    recommended: bool
    rationale: str = Field(min_length=1)
    evidence_ids: list[str]


class TestStrategyV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: TestScopeV1 | None
    data_focus: list[str]
    depth: Literal["smoke", "core", "exhaustive"]
    layer_recommendation: list[LayerRecommendationV1] = Field(min_length=4, max_length=4)
    approach: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _all_layers_once(self) -> TestStrategyV1:
        layers = tuple(item.layer for item in self.layer_recommendation)
        expected = ("API", "E2E", "Fuzz", "Performance")
        if layers != expected:
            raise ValueError("layer_recommendation must list API, E2E, Fuzz, Performance once")
        return self


class SourceCatalogEntryV1(FrozenModel):
    source_id: str = Field(min_length=1)
    kind: SourceKind
    artifact: EvidenceArtifactRefV1
    quotable: bool


class SourceQuoteV1(FrozenModel):
    source_id: str = Field(min_length=1)
    quote: str = Field(min_length=1)
    context_quote: str | None


class ObservationDraftV1(FrozenModel):
    key: str = Field(min_length=1)
    condition: str = Field(min_length=1)
    proposed_expected_status: int | None
    basis_quotes: tuple[SourceQuoteV1, ...]


class ObligationDraftV1(FrozenModel):
    draft_id: str = Field(min_length=1)
    proposed_key: str | None
    category: MrcCategory
    layer: MrcLayer
    statement: str = Field(min_length=1)
    applicability_conditions: tuple[str, ...]
    impact_row_ids: tuple[str, ...]
    proposed_profile_id: str | None
    prerequisites: tuple[str, ...]
    observation_goals: tuple[ObservationDraftV1, ...]
    basis_quotes: tuple[SourceQuoteV1, ...]
    open_questions: tuple[str, ...]

    @model_validator(mode="after")
    def _quotes_must_hit_catalog(self, info: ValidationInfo) -> ObligationDraftV1:
        catalog = (info.context or {}).get("source_catalog")
        if catalog is None:
            return self
        allowed = {entry.source_id for entry in catalog}
        quotes = [
            *self.basis_quotes,
            *(quote for goal in self.observation_goals for quote in goal.basis_quotes),
        ]
        unknown = sorted({quote.source_id for quote in quotes if quote.source_id not in allowed})
        if unknown:
            raise ValueError(f"source_id is not in the host source catalog: {unknown}")
        return self


class ExploreAdvisoryV1(BaseModel):
    """Current Explore advisory; drafts are authoring, not authenticated obligations."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1"]
    change_id: str = Field(min_length=1)
    context_ref: str = Field(min_length=1)
    generated_at: str = Field(min_length=1)
    executive_summary: str = Field(min_length=1)
    watchlist: list[Any]
    evidence_inventory: EvidenceInventoryV1
    source_code_evidence: list[SourceCodeEvidenceV1]
    case_design_guidance: CaseDesignGuidanceV1
    minimum_required_coverage: tuple[ObligationDraftV1, ...]
    open_questions_for_case_design: list[Any]
    test_strategy: TestStrategyV1

    @model_validator(mode="after")
    def _draft_quotes_use_catalog(self, info: ValidationInfo) -> ExploreAdvisoryV1:
        catalog = (info.context or {}).get("source_catalog")
        if catalog is None:
            return self
        for draft in self.minimum_required_coverage:
            ObligationDraftV1.model_validate(
                draft.model_dump(mode="json"), context={"source_catalog": catalog}
            )
        return self


class PreparedExploreV1(FrozenModel):
    """Finalize-owned Explore read model. MRC is the formal obligation set."""

    schema_version: Literal["1"]
    change_id: str = Field(min_length=1)
    context_ref: str = Field(min_length=1)
    generated_at: str = Field(min_length=1)
    executive_summary: str = Field(min_length=1)
    watchlist: tuple[Any, ...]
    evidence_inventory: EvidenceInventoryV1
    source_code_evidence: tuple[SourceCodeEvidenceV1, ...]
    case_design_guidance: CaseDesignGuidanceV1
    minimum_required_coverage: tuple[PreparedObligationV1, ...]
    open_questions_for_case_design: tuple[Any, ...]
    test_strategy: TestStrategyV1


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


__all__ = [
    "EXPLORATION_DRAFT_PATH",
    "EXPLORATION_PATH",
    "EXPLORE_AGENT_OUTPUT_PATHS",
    "EXPLORE_OFFICIAL_OUTPUT_PATHS",
    "EXPLORE_OUTPUT_PATHS",
    "REQUIREMENT_CONTEXT_BUDGET",
    "REQUIREMENT_PATH",
    "RUN_SPEC_SNAPSHOT_PATH",
    "ExploreAdvisoryV1",
    "ExploreContextV1",
    "ObligationDraftV1",
    "ObservationDraftV1",
    "PreparedExploreV1",
    "RequirementReadFactsV1",
    "SourceCatalogEntryV1",
    "SourceQuoteV1",
]
