"""Canonical Fuzz/Performance contract fixtures — strong review and mechanical round trips."""

from __future__ import annotations

from copy import deepcopy
from typing import cast

import pytest

from assurance_agent.artifacts.models.review import PlanReview, PlanReviewAuthoring
from assurance_agent.verification.applicability import derive_layer_applicability
from assurance_agent.verification.checks.base import CheckContext
from assurance_agent.verification.checks.registry import run_plan_checks
from assurance_agent.verification.gate_state import plan_assurance_state
from assurance_agent.verification.profiles import get_layer_assurance_profile
from tests.helpers_assurance_contract import (
    CANONICAL_CHANGE_ID,
    CanonicalAssuranceBundle,
    load_canonical_assurance_bundle,
)


def load_contract_fixture(layer: str) -> CanonicalAssuranceBundle:
    return load_canonical_assurance_bundle(layer)


def _check(document, check_id: str):
    return next(item for item in document.checks if item.check_id == check_id)


def _context(fixture: CanonicalAssuranceBundle, layer: str) -> CheckContext:
    return CheckContext(
        plan_texts=fixture.plan_texts,
        cases=fixture.cases,
        data_knowledge=fixture.data_knowledge,
        layer=layer,  # type: ignore[arg-type]
        required_capabilities=fixture.required_capabilities,
    )


@pytest.mark.parametrize("layer", ["fuzz", "performance"])
def test_fixture_is_a_strong_review_and_canonical_plan(layer: str) -> None:
    fixture = load_contract_fixture(layer)
    authored = PlanReviewAuthoring.model_validate_json(fixture.review_bytes)
    review = PlanReview.model_validate(authored.model_dump(mode="json"))
    assert review.review_type == f"{layer}-plan"
    assert review.required_capabilities
    assert review.auto_fix_allowed is False
    assert review.auto_fix_plan == []
    assert "## Factory Mapping" in fixture.plan_texts[get_layer_assurance_profile(layer).plan_artifacts[-1]]
    assert "| Shared Module | Function | Ownership |" in fixture.plan_texts[
        get_layer_assurance_profile(layer).plan_artifacts[-1]
    ]
    codegen = fixture.plan_texts[get_layer_assurance_profile(layer).plan_artifacts[-1]]
    if layer == "fuzz":
        assert "| Case ID | Test Function | Target File |" in codegen
        assert "## Schema Acquisition" in codegen
    else:
        assert "| Case ID | Task Method | Target File |" in codegen


@pytest.mark.parametrize("layer", ["fuzz", "performance"])
def test_fixture_mechanical_round_trip(layer: str) -> None:
    fixture = load_contract_fixture(layer)
    profile = get_layer_assurance_profile(layer)  # type: ignore[arg-type]
    applicability = derive_layer_applicability(fixture.cases, profile)

    authored = PlanReviewAuthoring.model_validate_json(fixture.review_bytes)
    review = PlanReview.model_validate(authored.model_dump(mode="json"))
    checks = run_plan_checks(_context(fixture, layer), applicability=applicability)
    state = plan_assurance_state(
        checks.model_dump(mode="json"),
        review.model_dump(mode="json"),
        fixture.data_knowledge,
        profile.layer,
        change_id=CANONICAL_CHANGE_ID,
    )

    assert state == "applicable"
    assert [item.status for item in checks.checks] == [
        "pass",
        "pass",
        "not_applicable",
        "pass",
    ]


@pytest.mark.parametrize("layer", ["fuzz", "performance"])
def test_fixture_factory_mapping_header_mutation_fails_shared_factory(layer: str) -> None:
    fixture = load_contract_fixture(layer)
    profile = get_layer_assurance_profile(layer)  # type: ignore[arg-type]
    applicability = derive_layer_applicability(fixture.cases, profile)
    codegen_path = profile.plan_artifacts[-1]
    mutated_plans = deepcopy(fixture.plan_texts)
    mutated_plans[codegen_path] = mutated_plans[codegen_path].replace("## Factory Mapping", "## Factories")

    document = run_plan_checks(
        CheckContext(
            plan_texts=mutated_plans,
            cases=fixture.cases,
            data_knowledge=fixture.data_knowledge,
            layer=layer,  # type: ignore[arg-type]
            required_capabilities=fixture.required_capabilities,
        ),
        applicability=applicability,
    )

    assert _check(document, "shared_factory").status == "fail"


@pytest.mark.parametrize("layer", ["fuzz", "performance"])
def test_fixture_missing_plan_raises(layer: str) -> None:
    fixture = load_contract_fixture(layer)
    profile = get_layer_assurance_profile(layer)  # type: ignore[arg-type]
    applicability = derive_layer_applicability(fixture.cases, profile)
    mutated_plans = {
        path: text for path, text in fixture.plan_texts.items() if path != profile.plan_artifacts[0]
    }

    with pytest.raises(ValueError, match="missing plan artifact"):
        run_plan_checks(
            CheckContext(
                plan_texts=mutated_plans,
                cases=fixture.cases,
                data_knowledge=fixture.data_knowledge,
                layer=layer,  # type: ignore[arg-type]
                required_capabilities=fixture.required_capabilities,
            ),
            applicability=applicability,
        )


@pytest.mark.parametrize("layer", ["fuzz", "performance"])
def test_fixture_unknown_capability_fails_capability_keys(layer: str) -> None:
    fixture = load_contract_fixture(layer)
    profile = get_layer_assurance_profile(layer)  # type: ignore[arg-type]
    applicability = derive_layer_applicability(fixture.cases, profile)
    unknown_caps = (*fixture.required_capabilities, "capabilities.adapters.fuzz.auth.missing_leaf")

    document = run_plan_checks(
        CheckContext(
            plan_texts=fixture.plan_texts,
            cases=fixture.cases,
            data_knowledge=fixture.data_knowledge,
            layer=layer,  # type: ignore[arg-type]
            required_capabilities=unknown_caps,
        ),
        applicability=applicability,
    )

    assert _check(document, "capability_keys").status == "fail"


@pytest.mark.parametrize("layer", ["fuzz", "performance"])
def test_fixture_missing_l1_leaf_fails_capability_keys(layer: str) -> None:
    fixture = load_contract_fixture(layer)
    profile = get_layer_assurance_profile(layer)  # type: ignore[arg-type]
    applicability = derive_layer_applicability(fixture.cases, profile)
    mutated_dk = deepcopy(fixture.data_knowledge)
    capabilities = cast(dict[str, object], mutated_dk["capabilities"])
    domain_factories = cast(dict[str, object], capabilities["domain_factories"])
    del domain_factories["account"]

    document = run_plan_checks(
        CheckContext(
            plan_texts=fixture.plan_texts,
            cases=fixture.cases,
            data_knowledge=mutated_dk,
            layer=layer,  # type: ignore[arg-type]
            required_capabilities=fixture.required_capabilities,
        ),
        applicability=applicability,
    )

    assert _check(document, "capability_keys").status == "fail"
