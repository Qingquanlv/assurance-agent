from __future__ import annotations

import ast
from pathlib import Path
from typing import cast

import pytest
from pydantic import ValidationError

from graph_engine import ENGINE_API_VERSION, RegistryPorts
from graph_engine.canonical import JSONValue, canonical_json_bytes

from assurance_generation.contracts import (
    CampaignSpec,
    CodegenMapping,
    GeneratedFilesV1,
    PlanCheckDocument,
    PlanReviewAuthoring,
)
from assurance_generation.plugin import GenerationPlugin

_TESTS_ROOT = Path(__file__).resolve().parent
_WHEEL_ROOT = _TESTS_ROOT.parent
VALID_LEAFS = frozenset({"entities.item.create", "auth.session.create", "capabilities.adapters.create"})
_LEGACY_ROOTS = ("assurance_agent", "assurance_kernel")
_NON_CONTRACT_INTAKE = (
    "assurance_intake.plugin",
    "assurance_intake.operations",
    "assurance_intake.validators",
    "assurance_intake.resource_loader",
)


def schema_bytes(schema_id: str) -> bytes:
    contribution = GenerationPlugin.contribute(RegistryPorts(engine_api=ENGINE_API_VERSION))
    for schema in contribution.schemas:
        if schema.schema_id == schema_id:
            return bytes(schema.content)
    raise KeyError(schema_id)


def forbidden_generation_imports() -> set[str]:
    root = _WHEEL_ROOT / "assurance_generation"
    if not root.is_dir():
        raise FileNotFoundError(f"package source is missing: {root}")
    found: set[str] = set()
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        for module_name in _imported_modules(ast.parse(path.read_text(encoding="utf-8"))):
            if any(module_name == item or module_name.startswith(f"{item}.") for item in _LEGACY_ROOTS):
                found.add(module_name)
            if module_name == "assurance_intake" or any(
                module_name == item or module_name.startswith(f"{item}.") for item in _NON_CONTRACT_INTAKE
            ):
                found.add(module_name)
            if module_name.startswith("assurance_intake.") and not (
                module_name == "assurance_intake.contracts"
                or module_name.startswith("assurance_intake.contracts.")
            ):
                found.add(module_name)
    return found


def valid_plan_review(*, required_capabilities: list[str] | None = None) -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "review_type": "api-plan",
        "change_id": "CH-DEMO-001",
        "decision": "pass",
        "findings": [],
        "auto_fix_plan": [],
        "next_action": "proceed to codegen",
        "auto_fix_allowed": True,
        "human_review_required": False,
        "codegen_readiness": "ready",
        "risk_level": "medium",
        "required_capabilities": required_capabilities or ["entities.item.create"],
    }


def _imported_modules(tree: ast.AST) -> tuple[str, ...]:
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            names.append(node.module)
    return tuple(names)


def test_plan_review_rejects_prefix_valid_but_unknown_leaf() -> None:
    raw = valid_plan_review(required_capabilities=["capabilities.adapters.missing"])
    with pytest.raises(ValidationError, match="unknown capability leaf"):
        PlanReviewAuthoring.model_validate(raw, context={"capability_leafs": VALID_LEAFS})


def test_plan_review_rejects_prefix_leaf() -> None:
    raw = valid_plan_review(required_capabilities=["entities.item"])
    with pytest.raises(ValidationError, match="unknown capability leaf|canonical C4 leaf key"):
        PlanReviewAuthoring.model_validate(raw, context={"capability_leafs": VALID_LEAFS})


def test_plan_review_accepts_exact_typed_leaf() -> None:
    raw = valid_plan_review(required_capabilities=["entities.item.create"])
    model = PlanReviewAuthoring.model_validate(raw, context={"capability_leafs": VALID_LEAFS})
    assert model.required_capabilities == ["entities.item.create"]


def test_performance_plan_review_accepts_bounded_automatic_repair() -> None:
    raw = valid_plan_review(required_capabilities=["entities.item.create"])
    raw.update(
        {
            "review_type": "performance-plan",
            "decision": "needs_fix",
            "findings": [
                {
                    "id": "PERF-PLAN-001",
                    "severity": "blocking",
                    "category": "runtime_contract",
                    "message": "Use the source-backed unfiltered tree lookup for descendants.",
                    "locator": {
                        "artifact": "qa/changes/CH-DEMO-001/plans/performance-plan.md",
                        "case_id": "TC_PERFORMANCE_001",
                        "key": "Seed Lifecycle",
                    },
                }
            ],
            "auto_fix_plan": ["PERF-PLAN-001"],
            "next_action": "repair the bounded seed lookup",
            "auto_fix_allowed": True,
            "human_review_required": False,
            "codegen_readiness": "not_ready",
        }
    )

    model = PlanReviewAuthoring.model_validate(raw, context={"capability_leafs": VALID_LEAFS})

    assert model.decision == "needs_fix"
    assert model.auto_fix_plan == ["PERF-PLAN-001"]


def test_generation_contracts_import_only_intake_contracts() -> None:
    assert forbidden_generation_imports() == set()


def test_generation_schema_bytes_equal_model_schema() -> None:
    assert schema_bytes("assurance.generation.schema.plan-check.v1") == canonical_json_bytes(
        cast(JSONValue, PlanCheckDocument.model_json_schema())
    )
    assert schema_bytes("assurance.generation.schema.plan-review.v1") == canonical_json_bytes(
        cast(JSONValue, PlanReviewAuthoring.model_json_schema())
    )
    assert schema_bytes("assurance.generation.schema.generated-files.v1") == canonical_json_bytes(
        cast(JSONValue, GeneratedFilesV1.model_json_schema())
    )
    assert schema_bytes("assurance.generation.schema.codegen-mapping.v1") == canonical_json_bytes(
        cast(JSONValue, CodegenMapping.model_json_schema())
    )
    assert schema_bytes("assurance.generation.schema.discovery-campaign.v1") == canonical_json_bytes(
        cast(JSONValue, CampaignSpec.model_json_schema())
    )
