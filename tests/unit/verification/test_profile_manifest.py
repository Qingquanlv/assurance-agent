"""Stable assurance-profile manifest and digest invariants."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from assurance_agent.artifacts.models.assurance import LAYER_NAMES, PLAN_CHECK_IDS
from assurance_agent.artifacts.models.review import PlanReview, Review
from assurance_agent.verification.checks.registry import CHECKS_BY_ID
from assurance_agent.verification.profiles import iter_layer_assurance_profiles
from assurance_agent.verification.profile_manifest import (
    assurance_profile_bytes,
    assurance_profile_digest,
    normalized_assurance_profile_manifest,
)


def _manifest() -> dict[str, Any]:
    return normalized_assurance_profile_manifest()


def test_manifest_profiles_follow_layer_names_order() -> None:
    manifest = _manifest()
    assert [profile["layer"] for profile in manifest["profiles"]] == list(LAYER_NAMES)


def test_manifest_applicable_check_ids_follow_plan_check_ids_order() -> None:
    manifest = _manifest()
    for profile in manifest["profiles"]:
        assert profile["applicable_check_ids"] == [
            check_id for check_id in PLAN_CHECK_IDS if check_id in set(profile["applicable_check_ids"])
        ]


def test_manifest_check_catalog_follows_plan_check_ids_order() -> None:
    manifest = _manifest()
    assert [entry["check_id"] for entry in manifest["check_catalog"]] == list(PLAN_CHECK_IDS)
    for entry in manifest["check_catalog"]:
        fn = CHECKS_BY_ID[entry["check_id"]]
        assert entry["callable"] == f"{fn.__module__}.{fn.__qualname__}"


def test_manifest_records_review_models_by_qualified_name() -> None:
    manifest = _manifest()
    expected = {
        profile.layer: f"{profile.review_model.__module__}.{profile.review_model.__name__}"
        for profile in iter_layer_assurance_profiles()
    }
    actual = {profile["layer"]: profile["review_model"] for profile in manifest["profiles"]}
    assert actual == expected
    assert actual["api"] == f"{PlanReview.__module__}.{PlanReview.__name__}"
    assert actual["fuzz"] == f"{Review.__module__}.{Review.__name__}"


def test_repeated_construction_is_byte_identical() -> None:
    first = assurance_profile_bytes()
    second = assurance_profile_bytes()
    assert first == second
    assert assurance_profile_digest() == assurance_profile_digest()


@pytest.mark.parametrize(
    ("path", "mutator"),
    [
        ("profiles[0].case_type", lambda m: m["profiles"][0].update({"case_type": "MUTATED"})),
        ("profiles[0].plan_artifacts", lambda m: m["profiles"][0].update({"plan_artifacts": ["mutated"]})),
        (
            "profiles[0].review_artifact",
            lambda m: m["profiles"][0].update({"review_artifact": "review/mutated.json"}),
        ),
        ("profiles[0].review_alias", lambda m: m["profiles"][0].update({"review_alias": "mutated_review"})),
        (
            "profiles[0].checks_artifact",
            lambda m: m["profiles"][0].update({"checks_artifact": "review/mutated-checks.json"}),
        ),
        ("profiles[0].gate_id", lambda m: m["profiles"][0].update({"gate_id": "mutated-gate"})),
        (
            "profiles[0].capability_contract_enabled",
            lambda m: m["profiles"][0].update(
                {"capability_contract_enabled": not m["profiles"][0]["capability_contract_enabled"]}
            ),
        ),
        (
            "profiles[0].applicable_check_ids",
            lambda m: m["profiles"][0].update(
                {"applicable_check_ids": list(reversed(m["profiles"][0]["applicable_check_ids"]))}
            ),
        ),
        (
            "profiles[0].review_model",
            lambda m: m["profiles"][0].update({"review_model": "mutated.ReviewModel"}),
        ),
        (
            "check_catalog order",
            lambda m: m["check_catalog"].sort(key=lambda entry: entry["check_id"], reverse=True),
        ),
    ],
)
def test_profile_manifest_mutation_changes_digest(path: str, mutator: Any) -> None:
    baseline = assurance_profile_digest()
    mutated = copy.deepcopy(_manifest())
    mutator(mutated)
    assert assurance_profile_digest(mutated) != baseline, path
