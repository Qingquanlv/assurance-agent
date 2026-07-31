"""Canonical Fuzz/Performance contract fixtures — strong review and mechanical round trips."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest
import yaml

from assurance_agent.artifacts.models.review import PlanReview, PlanReviewAuthoring
from assurance_agent.verification.applicability import derive_layer_applicability
from assurance_agent.verification.checks.base import CheckContext
from assurance_agent.verification.checks.registry import run_plan_checks
from assurance_agent.verification.gate_state import plan_assurance_state
from assurance_agent.verification.profiles import get_layer_assurance_profile

REPO_ROOT = Path(__file__).resolve().parents[3]
_CANONICAL_CHANGE_ID = "CH-CANONICAL"


@dataclass(frozen=True)
class ContractFixture:
    root: Path
    review_bytes: bytes
    codegen_plan: str
    plan_texts: dict[str, str]
    cases: list[dict[str, object]]
    data_knowledge: dict[str, object]
    required_capabilities: tuple[str, ...]


def load_contract_fixture(layer: str) -> ContractFixture:
    root = REPO_ROOT / "tests" / "fixtures" / "assurance" / f"{layer}-contract"
    profile = get_layer_assurance_profile(layer)  # type: ignore[arg-type]
    plan_texts = {path: (root / path).read_text(encoding="utf-8") for path in profile.plan_artifacts}
    cases = [
        yaml.safe_load(path.read_text(encoding="utf-8"))
        for path in sorted((root / "cases").glob("**/case.yaml"))
    ]
    data_knowledge = yaml.safe_load((root / ".aa" / "data-knowledge.yaml").read_text(encoding="utf-8"))
    review_path = root / profile.review_artifact
    review_bytes = review_path.read_bytes()
    review = yaml.safe_load(review_bytes.decode("utf-8"))
    required_capabilities = tuple(review["required_capabilities"])
    codegen_plan = plan_texts[profile.plan_artifacts[-1]]
    return ContractFixture(
        root=root,
        review_bytes=review_bytes,
        codegen_plan=codegen_plan,
        plan_texts=plan_texts,
        cases=cases,
        data_knowledge=data_knowledge,
        required_capabilities=required_capabilities,
    )


def _check(document, check_id: str):
    return next(item for item in document.checks if item.check_id == check_id)


def _context(fixture: ContractFixture, layer: str) -> CheckContext:
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
    assert "## Factory Mapping" in fixture.codegen_plan
    assert "| Shared Module | Function | Ownership |" in fixture.codegen_plan


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
        change_id=_CANONICAL_CHANGE_ID,
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
