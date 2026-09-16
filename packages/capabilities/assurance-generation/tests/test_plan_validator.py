from __future__ import annotations

import json
from typing import Any

import pytest

from graph_engine import ENGINE_API_VERSION, RegistryPorts
from graph_engine.plugin_api import (
    CandidateFile,
    CandidateWriteSet,
    ResourceClaims,
    ValidationContext,
    ValidationResult,
)

from assurance_generation.plugin import GenerationPlugin
from assurance_generation.validators.plans import (
    FAMILIES,
    FamilyPlanValidator,
    PlanMechanicalValidator,
    family_validator,
)
from planning_fixtures import (  # pyright: ignore[reportMissingImports]
    VALID_LEAFS,
    family_case_id,
    family_plan_files,
    valid_plan_result,
)

_SHA = "a" * 64


def candidate_with(*paths: str) -> CandidateWriteSet:
    return CandidateWriteSet(
        baseline_tree_id="0" * 64,
        candidate_tree_id="1" * 64,
        files=tuple(CandidateFile(path=path, before_sha256=None, after_sha256=_SHA) for path in paths),
    )


def validation_context() -> ValidationContext:
    return ValidationContext(
        invocation_id="capabilities-test",
        task_id="capabilities-task",
        graph_instance_id="capabilities-graph",
        node_id="capabilities-node",
        resources=ResourceClaims(),
    )


def _plan_bytes(family: str, payload: dict[str, Any] | None = None) -> dict[str, bytes]:
    document = payload if payload is not None else valid_plan_result(family)
    path = f"qa/results/plans/{family}-plan.json"
    return {path: json.dumps(document, separators=(",", ":"), sort_keys=True).encode("utf-8")}


def _validator(family: str, file_bytes: dict[str, bytes]) -> FamilyPlanValidator:
    return family_validator(
        family,
        capability_leafs=frozenset(VALID_LEAFS),
        case_ids=frozenset({family_case_id(family)}),
        file_bytes=file_bytes,
        write_roots=("qa/results/plans/",),
    )


@pytest.mark.parametrize("family", FAMILIES)
def test_family_validator_accepts_own_plan(family: str) -> None:
    files = family_plan_files(family)
    file_bytes = _plan_bytes(family)
    result = _validator(family, file_bytes).validate(
        candidate_with(*files, *file_bytes), validation_context()
    )
    assert result == ValidationResult(accepted=True)


@pytest.mark.parametrize("family", FAMILIES)
def test_family_validator_rejects_wrong_family_entry(family: str) -> None:
    other = "e2e" if family == "api" else "api"
    payload = valid_plan_result(family)
    payload["family"] = other
    files = family_plan_files(family)
    file_bytes = _plan_bytes(family, payload)
    result = _validator(family, file_bytes).validate(
        candidate_with(*files, *file_bytes), validation_context()
    )
    assert result.accepted is False
    assert result.reason is not None
    assert "family" in result.reason


@pytest.mark.parametrize("family", FAMILIES)
def test_family_validator_rejects_unresolved_case_id(family: str) -> None:
    payload = valid_plan_result(family)
    payload["case_ids"] = ["TC_MISSING_001"]
    payload["coverage"][0]["case_id"] = "TC_MISSING_001"
    files = family_plan_files(family)
    file_bytes = _plan_bytes(family, payload)
    result = _validator(family, file_bytes).validate(
        candidate_with(*files, *file_bytes), validation_context()
    )
    assert result.accepted is False
    assert result.reason is not None
    assert "TC_MISSING_001" in result.reason


@pytest.mark.parametrize("family", FAMILIES)
def test_family_validator_rejects_unresolved_leaf(family: str) -> None:
    payload = valid_plan_result(family)
    payload["required_capabilities"] = ["auth.fake"]
    payload["coverage"][0]["required_capabilities"] = ["auth.fake"]
    files = family_plan_files(family)
    file_bytes = _plan_bytes(family, payload)
    result = _validator(family, file_bytes).validate(
        candidate_with(*files, *file_bytes), validation_context()
    )
    assert result.accepted is False
    assert result.reason is not None
    assert "auth.fake" in result.reason


@pytest.mark.parametrize("family", FAMILIES)
def test_family_validator_rejects_missing_operation_or_risk(family: str) -> None:
    payload = valid_plan_result(family)
    payload["coverage"] = []
    files = family_plan_files(family)
    file_bytes = _plan_bytes(family, payload)
    result = _validator(family, file_bytes).validate(
        candidate_with(*files, *file_bytes), validation_context()
    )
    assert result.accepted is False
    assert result.reason is not None
    assert "operation" in result.reason or "risk" in result.reason or "coverage" in result.reason


def test_family_validator_rejects_path_traversal_and_outside_write_root() -> None:
    validator = FamilyPlanValidator(
        "api",
        capability_leafs=frozenset(VALID_LEAFS),
        write_roots=("qa/results/plans/",),
    )
    context = validation_context()
    assert validator.validate(candidate_with("../secret.md"), context).accepted is False
    assert validator.validate(candidate_with("/tmp/api-plan.md"), context).accepted is False
    assert validator.validate(candidate_with("src/app.py"), context).accepted is False


def test_fuzz_validator_requires_endpoint_property_strategy() -> None:
    payload = valid_plan_result("fuzz")
    del payload["fuzz_strategy"]
    files = family_plan_files("fuzz")
    file_bytes = _plan_bytes("fuzz", payload)
    result = _validator("fuzz", file_bytes).validate(
        candidate_with(*files, *file_bytes), validation_context()
    )
    assert result.accepted is False
    assert result.reason is not None
    assert "endpoint" in result.reason or "property" in result.reason or "fuzz" in result.reason


def test_performance_validator_requires_scenario_and_thresholds() -> None:
    payload = valid_plan_result("performance")
    del payload["performance_scenarios"]
    files = family_plan_files("performance")
    file_bytes = _plan_bytes("performance", payload)
    result = _validator("performance", file_bytes).validate(
        candidate_with(*files, *file_bytes), validation_context()
    )
    assert result.accepted is False
    assert result.reason is not None
    assert "scenario" in result.reason or "threshold" in result.reason


def test_plan_mechanical_dispatches_closed_family_table() -> None:
    files = family_plan_files("api")
    file_bytes = _plan_bytes("api")
    validator = PlanMechanicalValidator(
        capability_leafs=frozenset(VALID_LEAFS),
        case_ids=frozenset({family_case_id("api")}),
        file_bytes=file_bytes,
        write_roots=("qa/results/plans/",),
    )
    assert validator.validate(candidate_with(*files, *file_bytes), validation_context()) == ValidationResult(
        accepted=True
    )


def test_plan_mechanical_rejects_family_discriminator_mismatch() -> None:
    payload = valid_plan_result("api")
    payload["family"] = "e2e"
    files = family_plan_files("api")
    file_bytes = _plan_bytes("api", payload)
    validator = PlanMechanicalValidator(
        capability_leafs=frozenset(VALID_LEAFS),
        case_ids=frozenset({family_case_id("api")}),
        file_bytes=file_bytes,
        write_roots=("qa/results/plans/",),
    )
    result = validator.validate(candidate_with(*files, *file_bytes), validation_context())
    assert result.accepted is False
    assert result.reason is not None
    assert "family" in result.reason


def test_plugin_contributed_codegen_validators_allowlist_registered_paths() -> None:
    contribution = GenerationPlugin.contribute(RegistryPorts(engine_api=ENGINE_API_VERSION))
    generated = contribution.commit_validators["assurance.generation.validator.generated-files.v1"]
    mapping = contribution.commit_validators["assurance.generation.validator.codegen-mapping.v1"]
    context = validation_context()
    allowed = candidate_with("qa/tests/api/test_users.py")
    assert generated.validate(allowed, context).accepted is True
    assert mapping.validate(allowed, context).accepted is True
    rejected = generated.validate(candidate_with("src/app.py"), context)
    assert rejected.accepted is False
    assert mapping.validate(candidate_with("../secret.md"), context).accepted is False
