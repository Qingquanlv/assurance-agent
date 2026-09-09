from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import cast

import pytest
from pydantic import ValidationError

from graph_engine import ENGINE_API_VERSION, RegistryPorts
from graph_engine.canonical import JSONValue, canonical_digest, canonical_json_bytes

from assurance_generation.contracts import (
    CampaignSpec,
    CodegenMapping,
    GeneratedFilesV1,
    PlanCheckDocument,
    PlanReview,
    PlanReviewAuthoring,
)
from assurance_generation.contracts.families import GENERATION_FAMILIES, validate_selected_families
from assurance_generation.contracts.reviews import PUBLIC_REVIEW_OUTCOMES, public_review_outcome
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
_CURRENT_GENERATION_SCHEMA_MAPPING: dict[str, tuple[str, str]] = {
    "assurance.generation.schema.codegen-mapping.v1": (
        "1",
        "9d623c3f691070097c9d3f120877db09596ef1dbdd060814e82d16e9876a44d1",
    ),
    "assurance.generation.schema.discovery-campaign.v1": (
        "1",
        "2a7c580ae79697388b2e169c1c3c96d5200f23fbeec7080a24f53b8c500e1e31",
    ),
    "assurance.generation.schema.generated-files.v1": (
        "1",
        "01243f769b92aff0b396bf068ce606008565945e81593bb9940240bd7d497757",
    ),
    "assurance.generation.schema.plan-check.v1": (
        "1",
        "07e34ca9d819ec2679bbdc78906bdf298f364999ccf37c86870b88047ef548cd",
    ),
    "assurance.generation.schema.plan-review.v1": (
        "1",
        "bcb4a32846cfc04bc6c5f52347e05f06b36a96e1babd3c621711bc6fb0054918",
    ),
    "assurance.generation.workflow.generate.input.v1": (
        "1",
        "940e0fc7647aa3fe68bccecaec4af951903fababeac56e1ab6314954167e556d",
    ),
    "assurance.generation.workflow.generate.output.v1": (
        "1",
        "ed44f659a1f3ec7d9cd80b3c0bd2a4461f26a13eb27d09e8703193d8366a455d",
    ),
}


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
            if module_name == "assurance_product" or module_name.startswith("assurance_product."):
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


def _installed_schema_mapping() -> dict[str, tuple[str, str]]:
    contribution = GenerationPlugin.contribute(RegistryPorts(engine_api=ENGINE_API_VERSION))
    return {
        schema.schema_id: (
            "1",
            canonical_digest(cast(JSONValue, json.loads(schema.content))),
        )
        for schema in contribution.schemas
    }


def test_generation_product_lock_schema_mapping_is_current_only() -> None:
    assert _installed_schema_mapping() == _CURRENT_GENERATION_SCHEMA_MAPPING


def test_plan_review_rejects_approved() -> None:
    with pytest.raises(ValidationError):
        PlanReview.model_validate(
            {**valid_plan_review(), "decision": "approved"},
            context={"capability_leafs": VALID_LEAFS},
        )


def test_plan_review_rejects_prefix_valid_but_unknown_leaf() -> None:
    raw = valid_plan_review(required_capabilities=["capabilities.adapters.missing"])
    with pytest.raises(ValidationError, match="unknown capability leaf"):
        PlanReviewAuthoring.model_validate(raw, context={"capability_leafs": VALID_LEAFS})


def test_plan_review_rejects_prefix_leaf() -> None:
    raw = valid_plan_review(required_capabilities=["entities.item"])
    with pytest.raises(ValidationError, match="unknown capability leaf|canonical C4 leaf key"):
        PlanReviewAuthoring.model_validate(raw, context={"capability_leafs": VALID_LEAFS})


@pytest.mark.parametrize(
    ("decision", "auto_fix_allowed", "human_review_required", "expected"),
    [
        ("pass", False, False, "pass"),
        ("needs_fix", True, False, "needs_fix"),
        ("needs_fix", False, True, "needs_human"),
        ("needs_human_review", False, True, "needs_human"),
        ("reject", False, False, "reject"),
    ],
)
def test_plan_review_normalizes_public_outcomes(
    decision: str,
    auto_fix_allowed: bool,
    human_review_required: bool,
    expected: str,
) -> None:
    assert public_review_outcome(decision, auto_fix_allowed, human_review_required) == expected
    assert expected in PUBLIC_REVIEW_OUTCOMES


@pytest.mark.parametrize(
    ("decision", "auto_fix_allowed", "human_review_required"),
    [
        ("pass", True, False),
        ("needs_fix", True, True),
        ("needs_fix", False, False),
        ("needs_human_review", True, True),
        ("reject", True, False),
        ("reject", False, True),
    ],
)
def test_plan_review_rejects_contradictory_public_outcomes(
    decision: str,
    auto_fix_allowed: bool,
    human_review_required: bool,
) -> None:
    with pytest.raises(ValueError):
        public_review_outcome(decision, auto_fix_allowed, human_review_required)


def test_selected_families_reject_empty_duplicate_and_unknown() -> None:
    assert validate_selected_families(("api", "e2e")) == ("api", "e2e")
    assert GENERATION_FAMILIES == ("api", "e2e", "fuzz", "performance")
    with pytest.raises(ValueError, match="empty|non-empty"):
        validate_selected_families(())
    with pytest.raises(ValueError, match="duplicate"):
        validate_selected_families(("api", "api"))
    with pytest.raises(ValueError, match="unknown"):
        validate_selected_families(("api", "mobile"))


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


def test_generation_agent_job_catalog_is_feature_owned() -> None:
    from types import MappingProxyType

    from assurance_generation.contracts.attempts import AGENT_JOB_CONTRACTS, OUTPUT_ROUTE_TEMPLATES

    expected = {
        "api.codegen": (
            "aa-api-codegen",
            "assurance-v1-test-author",
            (
                "qa/changes/{change_id}/codegen/api-codegen-summary.md",
                "qa/changes/{change_id}/codegen/api-generated-files.json",
            ),
        ),
        "api.plan-review": (
            "aa-api-plan-reviewer",
            "assurance-v1-reviewer",
            (
                "qa/changes/{change_id}/plan/api/reviews/epochs/{coverage_epoch}/rounds/{review_round}.json",
                "qa/changes/{change_id}/review/api-plan-review-summary.md",
                "qa/changes/{change_id}/review/api-plan-review.json",
            ),
        ),
        "api.plan": (
            "aa-api-plan",
            "assurance-v1-doc-author",
            (
                "qa/changes/{change_id}/plans/api-codegen-mapping.json",
                "qa/changes/{change_id}/plans/api-codegen-plan.md",
                "qa/changes/{change_id}/plans/api-plan.md",
                "qa/changes/{change_id}/plans/api-test-data-plan.md",
                "qa/changes/{change_id}/plans/m3-review-summary.md",
            ),
        ),
        "e2e.codegen": (
            "aa-e2e-codegen",
            "assurance-v1-test-author",
            (
                "qa/changes/{change_id}/codegen/e2e-codegen-summary.md",
                "qa/changes/{change_id}/codegen/e2e-generated-files.json",
            ),
        ),
        "e2e.plan-review": (
            "aa-e2e-plan-reviewer",
            "assurance-v1-reviewer",
            (
                "qa/changes/{change_id}/plan/e2e/reviews/epochs/{coverage_epoch}/rounds/{review_round}.json",
                "qa/changes/{change_id}/review/e2e-plan-review-summary.md",
                "qa/changes/{change_id}/review/e2e-plan-review.json",
            ),
        ),
        "e2e.plan": (
            "aa-e2e-plan",
            "assurance-v1-doc-author",
            (
                "qa/changes/{change_id}/plans/e2e-codegen-mapping.json",
                "qa/changes/{change_id}/plans/e2e-codegen-plan.md",
                "qa/changes/{change_id}/plans/e2e-plan.md",
                "qa/changes/{change_id}/plans/e2e-test-data-plan.md",
                "qa/changes/{change_id}/plans/m4-review-summary.md",
            ),
        ),
        "fuzz.codegen": (
            "aa-fuzz-codegen",
            "assurance-v1-test-author",
            (
                "qa/changes/{change_id}/codegen/fuzz-codegen-summary.md",
                "qa/changes/{change_id}/codegen/fuzz-generated-files.json",
            ),
        ),
        "fuzz.plan-review": (
            "aa-fuzz-plan-reviewer",
            "assurance-v1-reviewer",
            (
                "qa/changes/{change_id}/plan/fuzz/reviews/epochs/{coverage_epoch}/rounds/{review_round}.json",
                "qa/changes/{change_id}/review/fuzz-plan-review-summary.md",
                "qa/changes/{change_id}/review/fuzz-plan-review.json",
            ),
        ),
        "fuzz.plan": (
            "aa-fuzz-plan",
            "assurance-v1-doc-author",
            (
                "qa/changes/{change_id}/plans/fuzz-codegen-mapping.json",
                "qa/changes/{change_id}/plans/fuzz-codegen-plan.md",
                "qa/changes/{change_id}/plans/fuzz-plan.md",
                "qa/changes/{change_id}/plans/fuzz-review-summary.md",
            ),
        ),
        "performance.codegen": (
            "aa-performance-codegen",
            "assurance-v1-test-author",
            (
                "qa/changes/{change_id}/codegen/performance-codegen-summary.md",
                "qa/changes/{change_id}/codegen/performance-generated-files.json",
            ),
        ),
        "performance.plan-review": (
            "aa-performance-plan-reviewer",
            "assurance-v1-reviewer",
            (
                "qa/changes/{change_id}/plan/performance/reviews/epochs/{coverage_epoch}/rounds/{review_round}.json",
                "qa/changes/{change_id}/review/performance-plan-review-summary.md",
                "qa/changes/{change_id}/review/performance-plan-review.json",
            ),
        ),
        "performance.plan": (
            "aa-performance-plan",
            "assurance-v1-doc-author",
            (
                "qa/changes/{change_id}/plans/performance-codegen-mapping.json",
                "qa/changes/{change_id}/plans/performance-codegen-plan.md",
                "qa/changes/{change_id}/plans/performance-plan.md",
                "qa/changes/{change_id}/plans/performance-review-summary.md",
            ),
        ),
    }
    assert isinstance(AGENT_JOB_CONTRACTS, MappingProxyType)
    assert isinstance(OUTPUT_ROUTE_TEMPLATES, MappingProxyType)
    assert len(AGENT_JOB_CONTRACTS) == 12
    assert tuple(AGENT_JOB_CONTRACTS) == tuple(expected)
    assert tuple(OUTPUT_ROUTE_TEMPLATES) == tuple(expected)
    for base, (skill_id, agent_profile, writes) in expected.items():
        contract = AGENT_JOB_CONTRACTS[base]
        assert contract.contract_id == f"assurance.generation.agent.{base}.v1"
        assert contract.skill_id == skill_id
        assert contract.agent_profile == agent_profile
        family, _, stage = base.partition(".")
        expected_claims = tuple(
            path for path in writes if "{coverage_epoch}" not in path and "{review_round}" not in path
        )
        if stage == "codegen":
            expected_claims = tuple(sorted((*writes, f"qa/changes/{{change_id}}/generated/{family}/files")))
            expected_claims = tuple(
                path
                for path in expected_claims
                if "{coverage_epoch}" not in path and "{review_round}" not in path
            )
        if stage == "plan-review":
            expected_claims = tuple(
                sorted((*expected_claims, f"qa/changes/{{change_id}}/plan/{family}/reviews"))
            )
        assert contract.resources.writes == expected_claims
        assert OUTPUT_ROUTE_TEMPLATES[base] == writes
        dumped = json.dumps(contract.canonical_projection()).lower()
        assert "opencode" not in dumped
        assert "cursor" not in dumped
    found = forbidden_generation_imports()
    assert not any(name == "assurance_product" or name.startswith("assurance_product.") for name in found)


def test_codegen_job_claims_cover_dynamic_change_local_mapping_targets() -> None:
    from graph_engine.plugin_api import ResourceClaimTemplate

    from assurance_generation.contracts.attempts import AGENT_JOB_CONTRACTS, OUTPUT_ROUTE_TEMPLATES

    for family in ("api", "e2e", "fuzz", "performance"):
        base = f"{family}.codegen"
        resources = AGENT_JOB_CONTRACTS[base].resources
        assert isinstance(resources, ResourceClaimTemplate)
        claims = resources.resolve({"change_id": "CH-1"}).writes
        dynamic_root = f"qa/changes/CH-1/generated/{family}/files"
        assert dynamic_root in claims
        assert dynamic_root not in tuple(
            path.replace("{change_id}", "CH-1") for path in OUTPUT_ROUTE_TEMPLATES[base]
        )


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
