"""Immutable assurance profiles for each supported verification layer."""

from dataclasses import dataclass
from typing import Iterable

from assurance_agent.artifacts.models.assurance import (
    KNOWN_PLAN_CHECK_IDS,
    LAYER_NAMES,
    CaseType,
    LayerName,
    PlanCheckId,
)
from assurance_agent.artifacts.models.review import PlanReview, Review


@dataclass(frozen=True)
class LayerAssuranceProfile:
    layer: LayerName
    case_type: CaseType
    plan_artifacts: tuple[str, ...]
    review_artifact: str
    review_alias: str
    checks_artifact: str
    gate_id: str
    applicable_check_ids: frozenset[PlanCheckId]
    capability_contract_enabled: bool
    review_model: type[Review]


def _build_profile_registry(
    profiles: Iterable[LayerAssuranceProfile], *, known_check_ids: set[PlanCheckId]
) -> tuple[LayerAssuranceProfile, ...]:
    ordered_profiles = tuple(profiles)
    layers = tuple(profile.layer for profile in ordered_profiles)
    if layers != LAYER_NAMES:
        raise ValueError(f"profiles must declare layers in order {LAYER_NAMES}, got {layers}")

    case_types: set[CaseType] = set()
    artifact_paths: set[str] = set()
    review_aliases: set[str] = set()
    gate_ids: set[str] = set()
    consumed_check_ids: set[PlanCheckId] = set()

    for profile in ordered_profiles:
        if profile.case_type in case_types:
            raise ValueError(f"duplicate case type: {profile.case_type}")
        case_types.add(profile.case_type)

        if profile.review_alias in review_aliases:
            raise ValueError(f"duplicate review alias: {profile.review_alias}")
        review_aliases.add(profile.review_alias)

        if profile.gate_id in gate_ids:
            raise ValueError(f"duplicate gate id: {profile.gate_id}")
        gate_ids.add(profile.gate_id)

        if not (isinstance(profile.review_model, type) and issubclass(profile.review_model, Review)):
            raise ValueError(
                f"review_model must be a Review subclass for layer {profile.layer!r}: {profile.review_model!r}"
            )

        for kind, artifact_path in (
            ("review artifact", profile.review_artifact),
            ("check artifact", profile.checks_artifact),
        ):
            if not artifact_path.startswith("review/"):
                raise ValueError(f"{kind} must be under review/: {artifact_path}")
            if artifact_path in artifact_paths:
                raise ValueError(f"duplicate review/check artifact path: {artifact_path}")
            artifact_paths.add(artifact_path)

        unknown_check_ids = profile.applicable_check_ids - known_check_ids
        if unknown_check_ids:
            raise ValueError(f"unknown check: {sorted(unknown_check_ids)[0]}")
        consumed_check_ids.update(profile.applicable_check_ids)

    unconsumed_check_ids = known_check_ids - consumed_check_ids
    if unconsumed_check_ids:
        raise ValueError(f"unconsumed known check: {sorted(unconsumed_check_ids)[0]}")

    return ordered_profiles


_PROFILES = _build_profile_registry(
    (
        LayerAssuranceProfile(
            layer="api",
            case_type="API",
            plan_artifacts=(
                "plans/api-plan.md",
                "plans/api-test-data-plan.md",
                "plans/api-codegen-plan.md",
            ),
            review_artifact="review/api-plan-review.json",
            review_alias="api_plan_review",
            checks_artifact="review/api-plan-checks.json",
            gate_id="api-plan-review-gate",
            applicable_check_ids=KNOWN_PLAN_CHECK_IDS,
            capability_contract_enabled=True,
            review_model=PlanReview,
        ),
        LayerAssuranceProfile(
            layer="e2e",
            case_type="E2E",
            plan_artifacts=(
                "plans/e2e-plan.md",
                "plans/e2e-test-data-plan.md",
                "plans/e2e-codegen-plan.md",
            ),
            review_artifact="review/plan-review.json",
            review_alias="plan_review",
            checks_artifact="review/e2e-plan-checks.json",
            gate_id="e2e-plan-review-gate",
            applicable_check_ids=KNOWN_PLAN_CHECK_IDS,
            capability_contract_enabled=True,
            review_model=PlanReview,
        ),
        LayerAssuranceProfile(
            layer="fuzz",
            case_type="Fuzz",
            plan_artifacts=("plans/fuzz-plan.md", "plans/fuzz-codegen-plan.md"),
            review_artifact="review/fuzz-plan-review.json",
            review_alias="fuzz_plan_review",
            checks_artifact="review/fuzz-plan-checks.json",
            gate_id="fuzz-plan-review-gate",
            applicable_check_ids=KNOWN_PLAN_CHECK_IDS - {"assert_ideal"},
            capability_contract_enabled=True,
            review_model=PlanReview,
        ),
        LayerAssuranceProfile(
            layer="performance",
            case_type="Performance",
            plan_artifacts=("plans/performance-plan.md", "plans/performance-codegen-plan.md"),
            review_artifact="review/performance-plan-review.json",
            review_alias="performance_plan_review",
            checks_artifact="review/performance-plan-checks.json",
            gate_id="performance-plan-review-gate",
            applicable_check_ids=KNOWN_PLAN_CHECK_IDS - {"assert_ideal"},
            capability_contract_enabled=True,
            review_model=PlanReview,
        ),
    ),
    known_check_ids=set(KNOWN_PLAN_CHECK_IDS),
)
_PROFILES_BY_LAYER = {profile.layer: profile for profile in _PROFILES}


def get_layer_assurance_profile(layer: str) -> LayerAssuranceProfile:
    try:
        return _PROFILES_BY_LAYER[layer]
    except KeyError as error:
        raise ValueError(f"unknown assurance layer: {layer}") from error


def iter_layer_assurance_profiles() -> tuple[LayerAssuranceProfile, ...]:
    return _PROFILES
