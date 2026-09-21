"""Intake-owned quality goal definitions shared with Quality."""

from __future__ import annotations

from collections.abc import Mapping
import re
from typing import Annotated, Any, Literal

from pydantic import Field, field_validator

from graph_engine.identifiers import IdentifierError, validate_qualified_id
from graph_engine.plugin_api import FrozenModel

from assurance_intake.contracts.common import (
    MrcCategory,
    MrcLayer,
    RiskTier,
    TestFamily,
    validate_family_tuple,
)
from assurance_intake.contracts.explore import ObligationDraftV1, SourceQuoteV1
from assurance_intake.contracts.obligations import (
    ExpectedBasisV1,
    PreparedObligationV1,
    RequiredObservationV1,
    SourceRefV1,
    VerificationRequirementV1,
)
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1

_MRC_PREFIX = {
    "api": "API",
    "e2e": "E2E",
    "e2e_if_enabled": "E2E",
    "negative": "NEGATIVE",
    "data_integrity": "DATA-INTEGRITY",
}

FiniteFloor = Annotated[float, Field(ge=0.0, le=1.0, allow_inf_nan=False)]
CoverageGoal = Literal["constraint_coverage", "auth_matrix_coverage", "journey_coverage"]
COVERAGE_GOAL_ORDER: tuple[CoverageGoal, ...] = (
    "constraint_coverage",
    "auth_matrix_coverage",
    "journey_coverage",
)


class CoverageFloorsV1(FrozenModel):
    low: FiniteFloor
    medium: FiniteFloor
    high: FiniteFloor
    critical: FiniteFloor


class CoverageGoalPolicyV1(FrozenModel):
    coverage_floor_by_tier: CoverageFloorsV1

    @classmethod
    def from_product_policy(cls, product_policy: Mapping[str, object]) -> CoverageGoalPolicyV1:
        raw = product_policy.get("coverage_floor_by_tier")
        if raw is None:
            raise ValueError("policy_error: coverage_floor_by_tier is required")
        return cls.model_validate({"coverage_floor_by_tier": raw})

    def floor_for(self, tier: RiskTier) -> float:
        return float(getattr(self.coverage_floor_by_tier, tier))


class SufficiencyPolicyV1(FrozenModel):
    recency_hours: int = Field(gt=0)
    require_current_batch: Literal[True]

    @classmethod
    def from_product_policy(cls, product_policy: Mapping[str, object]) -> SufficiencyPolicyV1:
        raw = product_policy.get("evidence_sufficiency")
        if not isinstance(raw, Mapping):
            raise ValueError("policy_error: evidence_sufficiency is required")
        return cls.model_validate(raw)


def validate_resource_digests(
    value: tuple[tuple[str, str], ...],
) -> tuple[tuple[str, str], ...]:
    if not value:
        raise ValueError("source_resource_digests must not be empty")
    for resource_id, digest in value:
        try:
            validate_qualified_id(resource_id)
        except IdentifierError as error:
            raise ValueError("source resource id must be qualified") from error
        if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise ValueError("source resource digest must be lowercase sha256")
    if value != tuple(sorted(set(value))):
        raise ValueError("source_resource_digests must be sorted and unique")
    ids = tuple(resource_id for resource_id, _digest in value)
    if len(ids) != len(set(ids)):
        raise ValueError("source_resource_digests must bind one digest per resource")
    return value


class PreparedQualityGoalV1(FrozenModel):
    obligations_ref: EvidenceArtifactRefV1
    source_resource_digests: tuple[tuple[str, str], ...]
    required_test_families: tuple[TestFamily, ...]
    metric_catalog: tuple[CoverageGoal, ...]
    coverage_policy: CoverageGoalPolicyV1
    sufficiency_policy: SufficiencyPolicyV1

    @field_validator("source_resource_digests")
    @classmethod
    def _source_resource_digests(cls, value: tuple[tuple[str, str], ...]) -> tuple[tuple[str, str], ...]:
        return validate_resource_digests(value)

    @field_validator("required_test_families")
    @classmethod
    def _required_test_families(cls, value: tuple[TestFamily, ...]) -> tuple[TestFamily, ...]:
        return validate_family_tuple(value)

    @field_validator("metric_catalog")
    @classmethod
    def _metric_catalog(cls, value: tuple[CoverageGoal, ...]) -> tuple[CoverageGoal, ...]:
        if value != COVERAGE_GOAL_ORDER:
            raise ValueError("metric_catalog must contain the canonical supported goals")
        return value


def journey_keys_from_document(document: Mapping[str, object]) -> tuple[str, ...]:
    raw = document.get("journeys", [])
    if not isinstance(raw, list) or any(
        not isinstance(item, str) or not item or item != item.strip() for item in raw
    ):
        raise ValueError("journeys must be canonical non-empty strings")
    value = tuple(raw)
    if value != tuple(sorted(set(value))):
        raise ValueError("journeys must be sorted and unique")
    return value


def _obligation_layers(row: PreparedObligationV1) -> tuple[TestFamily, ...]:
    return ("api", "e2e") if row.layer == "both" else (row.layer,)


def _resolved_refs(
    quotes: tuple[SourceQuoteV1, ...],
    resolved_quotes: Mapping[tuple[str, str], SourceRefV1],
) -> tuple[SourceRefV1, ...]:
    refs: list[SourceRefV1] = []
    seen: set[tuple[str, str]] = set()
    for quote in quotes:
        key = (quote.source_id, quote.quote)
        ref = resolved_quotes.get(key)
        if ref is None or key in seen:
            continue
        seen.add(key)
        refs.append(ref)
    return tuple(refs)


def normalize_obligation_drafts(
    drafts: tuple[ObligationDraftV1, ...],
    *,
    resolved_quotes: Mapping[tuple[str, str], SourceRefV1],
) -> tuple[PreparedObligationV1, ...]:
    rows: list[PreparedObligationV1] = []
    for sequence, draft in enumerate(drafts, start=1):
        mrc_id = (
            draft.draft_id
            if draft.draft_id.startswith("MRC-")
            else f"MRC-{_MRC_PREFIX[draft.category]}-{sequence:03d}"
        )
        observations = tuple(
            RequiredObservationV1(
                observation_key=goal.key,
                condition=goal.condition,
                predicate="status_code_eq",
                expected=goal.proposed_expected_status,
                basis_refs=_resolved_refs(goal.basis_quotes, resolved_quotes),
            )
            for goal in draft.observation_goals
        )
        if draft.proposed_profile_id is None and not observations:
            requirements: tuple[VerificationRequirementV1, ...] = ()
        else:
            requirements = (
                VerificationRequirementV1(
                    requirement_id=f"{mrc_id}-R001",
                    profile_id=draft.proposed_profile_id or "method_unspecified",
                    prerequisites=draft.prerequisites,
                    observations=observations,
                    semantic_review_required=True,
                    subject_binding_required=True,
                ),
            )
        rows.append(
            PreparedObligationV1(
                mrc_id=mrc_id,
                key=None,
                proposed_key=draft.proposed_key,
                category=draft.category,
                layer=draft.layer,
                statement=draft.statement,
                applicability_conditions=draft.applicability_conditions,
                expected_basis_refs=tuple(
                    ExpectedBasisV1(source=ref, source_status="pending")
                    for ref in _resolved_refs(draft.basis_quotes, resolved_quotes)
                ),
                impact_row_ids=draft.impact_row_ids,
                required=True,
                scope_disposition="included",
                exclusion_basis=None,
                open_questions=draft.open_questions,
                verification_requirements=requirements,
            )
        )
    ids = [row.mrc_id for row in rows]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate MRC id")
    return tuple(sorted(rows, key=lambda row: (row.mrc_id, row.proposed_key or "", row.key or "")))


def normalize_goal_obligations(
    advisory: Any,
    *,
    capability_leafs: frozenset[str],
    journey_keys: frozenset[str],
    admissible_families: frozenset[TestFamily] | None = None,
) -> tuple[PreparedObligationV1, ...]:
    del admissible_families
    source = getattr(advisory, "minimum_required_coverage", None)
    if not source:
        raise ValueError("minimum_required_coverage must be a non-empty mapping")
    first = source[0]
    if getattr(first, "mrc_id", None):
        prepared_rows = tuple(source)
    else:
        if any(draft.category == "e2e_if_enabled" for draft in source):
            raise ValueError("e2e_if_enabled applicability is unresolved; resolve obligations under e2e")
        prepared_rows = normalize_obligation_drafts(tuple(source), resolved_quotes={})
    if any(row.category == "e2e_if_enabled" for row in prepared_rows):
        raise ValueError("e2e_if_enabled applicability is unresolved; resolve obligations under e2e")
    rows: list[PreparedObligationV1] = []
    for row in prepared_rows:
        proposed = row.proposed_key
        if row.category == "api" and proposed:
            key: str | None = proposed
        elif proposed and proposed in capability_leafs | journey_keys:
            key = proposed
        else:
            key = None
        if (
            row.category in {"negative", "data_integrity"}
            and proposed is not None
            and key not in capability_leafs
        ):
            raise ValueError(f"unknown closed MRC key: {key}")
        if row.category == "e2e" and key not in journey_keys:
            raise ValueError(f"unknown journey MRC key: {key}")
        rows.append(row.model_copy(update={"key": key}))
    ids = [row.mrc_id for row in rows]
    keys = [row.key for row in rows if row.key is not None]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate MRC id")
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate MRC key")
    return tuple(sorted(rows, key=lambda row: (row.mrc_id, row.key or "")))


def required_goal_families(
    obligations: tuple[PreparedObligationV1, ...],
    *,
    admissible_families: frozenset[TestFamily] | None = None,
) -> tuple[TestFamily, ...]:
    families: set[TestFamily] = set()
    for row in obligations:
        if row.required:
            families.update(_obligation_layers(row))
    return tuple(family for family in ("api", "e2e", "fuzz", "performance") if family in families)


__all__ = [
    "COVERAGE_GOAL_ORDER",
    "CoverageFloorsV1",
    "CoverageGoal",
    "CoverageGoalPolicyV1",
    "FiniteFloor",
    "MrcCategory",
    "MrcLayer",
    "PreparedObligationV1",
    "PreparedQualityGoalV1",
    "SufficiencyPolicyV1",
    "validate_resource_digests",
    "journey_keys_from_document",
    "normalize_goal_obligations",
    "normalize_obligation_drafts",
    "required_goal_families",
]
