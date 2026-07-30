from dataclasses import replace
from typing import cast

import pytest

from assurance_agent.artifacts.models.assurance import KNOWN_PLAN_CHECK_IDS, PLAN_CHECK_IDS, PlanCheckId
from assurance_agent.verification.profiles import (
    _build_profile_registry,
    get_layer_assurance_profile,
    iter_layer_assurance_profiles,
)


def test_profiles_cover_exactly_four_layers_in_stable_order() -> None:
    profiles = iter_layer_assurance_profiles()

    assert tuple(profile.layer for profile in profiles) == (
        "api",
        "e2e",
        "fuzz",
        "performance",
    )
    assert all(profile.capability_contract_enabled for profile in profiles)


def test_profiles_expose_the_approved_artifacts_and_gates() -> None:
    profiles = iter_layer_assurance_profiles()

    assert [
        (
            profile.case_type,
            profile.plan_artifacts,
            profile.review_artifact,
            profile.review_alias,
            profile.checks_artifact,
            profile.gate_id,
        )
        for profile in profiles
    ] == [
        (
            "API",
            (
                "plans/api-plan.md",
                "plans/api-test-data-plan.md",
                "plans/api-codegen-plan.md",
            ),
            "review/api-plan-review.json",
            "api_plan_review",
            "review/api-plan-checks.json",
            "api-plan-review-gate",
        ),
        (
            "E2E",
            (
                "plans/e2e-plan.md",
                "plans/e2e-test-data-plan.md",
                "plans/e2e-codegen-plan.md",
            ),
            "review/plan-review.json",
            "plan_review",
            "review/e2e-plan-checks.json",
            "e2e-plan-review-gate",
        ),
        (
            "Fuzz",
            ("plans/fuzz-plan.md", "plans/fuzz-codegen-plan.md"),
            "review/fuzz-plan-review.json",
            "fuzz_plan_review",
            "review/fuzz-plan-checks.json",
            "fuzz-plan-review-gate",
        ),
        (
            "Performance",
            ("plans/performance-plan.md", "plans/performance-codegen-plan.md"),
            "review/performance-plan-review.json",
            "performance_plan_review",
            "review/performance-plan-checks.json",
            "performance-plan-review-gate",
        ),
    ]


def test_profile_applicable_checks_are_subset_of_shared_catalog() -> None:
    for profile in iter_layer_assurance_profiles():
        assert profile.applicable_check_ids <= KNOWN_PLAN_CHECK_IDS


def test_static_check_applicability_matrix() -> None:
    expected = set(PLAN_CHECK_IDS)

    assert get_layer_assurance_profile("api").applicable_check_ids == expected
    assert get_layer_assurance_profile("e2e").applicable_check_ids == expected
    assert get_layer_assurance_profile("fuzz").applicable_check_ids == expected - {"assert_ideal"}
    assert get_layer_assurance_profile("performance").applicable_check_ids == expected - {"assert_ideal"}


def test_unknown_layer_fails_closed() -> None:
    with pytest.raises(ValueError, match="unknown assurance layer: mobile"):
        get_layer_assurance_profile("mobile")


def test_registry_rejects_duplicate_layer() -> None:
    api = get_layer_assurance_profile("api")

    with pytest.raises(ValueError, match="duplicate layer"):
        _build_profile_registry((api, api), known_check_ids=set(PLAN_CHECK_IDS))


def test_registry_rejects_unknown_check() -> None:
    api = get_layer_assurance_profile("api")

    with pytest.raises(ValueError, match="unknown check"):
        _build_profile_registry(
            (replace(api, applicable_check_ids=frozenset({cast(PlanCheckId, "invented")})),),
            known_check_ids=set(PLAN_CHECK_IDS),
        )


def test_registry_rejects_unconsumed_known_check() -> None:
    api = get_layer_assurance_profile("api")

    with pytest.raises(ValueError, match="unconsumed known check"):
        _build_profile_registry(
            (replace(api, applicable_check_ids=frozenset(PLAN_CHECK_IDS[:-1])),),
            known_check_ids=set(PLAN_CHECK_IDS),
        )


def test_registry_rejects_check_artifact_outside_review() -> None:
    api = get_layer_assurance_profile("api")

    with pytest.raises(ValueError, match="check artifact.*review/"):
        _build_profile_registry(
            (replace(api, checks_artifact="plans/api-plan-checks.json"),),
            known_check_ids=set(api.applicable_check_ids),
        )
