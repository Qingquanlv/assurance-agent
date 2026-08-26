from __future__ import annotations

import ast
from copy import deepcopy
from collections.abc import Mapping
from pathlib import Path
from typing import cast

import pytest
import yaml
from pydantic import ValidationError

from graph_engine import ENGINE_API_VERSION, RegistryPorts
from graph_engine.canonical import JSONValue, canonical_json_bytes

from assurance_intake.contracts import CaseReviewResultV1, CaseYaml, CaseYamlAuthoring, QaYaml
from assurance_intake.plugin import IntakePlugin

_TESTS_ROOT = Path(__file__).resolve().parent
_WHEEL_ROOT = _TESTS_ROOT.parent
VALID_LEAFS = frozenset({"entities.item.create", "auth.session.create"})
_LEGACY_ROOTS = ("assurance_agent", "assurance_kernel")


def load_fixture(name: str) -> object:
    return yaml.safe_load((_TESTS_ROOT / "fixtures" / name).read_text(encoding="utf-8"))


def schema_bytes(schema_id: str) -> bytes:
    contribution = IntakePlugin.contribute(RegistryPorts(engine_api=ENGINE_API_VERSION))
    for schema in contribution.schemas:
        if schema.schema_id == schema_id:
            return bytes(schema.content)
    raise KeyError(schema_id)


def forbidden_imports(package: str, prefix: str | None = None) -> set[str]:
    root = _WHEEL_ROOT / package
    if not root.is_dir():
        raise FileNotFoundError(f"package source is missing: {root}")
    forbidden = {*_LEGACY_ROOTS}
    if prefix is not None:
        forbidden.add(prefix)
    found: set[str] = set()
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        for module_name in _imported_modules(ast.parse(path.read_text(encoding="utf-8"))):
            if any(module_name == item or module_name.startswith(f"{item}.") for item in forbidden):
                found.add(module_name)
    return found


def authoring_payload(*, trace: Mapping[str, object] | None = None) -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "added": [
            {
                "case_id": "TC_MENU_001",
                "title": "create menu happy path",
                "status": "active",
                "priority": "P1",
                "severity": "major",
                "type": "API",
                "module": "menus",
                "requirement_id": "REQ-1",
                "feature_name": "menu-management",
                "test_condition_id": "COND-1",
                "design_technique": "use_case",
                "objective": "verify the menu behavior",
                "summary": "exercise and assert the menu behavior",
                "preconditions": [],
                "test_data": [],
                "steps": ["perform the operation"],
                "assertions": ["the operation succeeds"],
                "postconditions": [],
                "edge_cases": [],
                "related_cases": [],
                "risk": {
                    "level": "high",
                    "likelihood": 3,
                    "impact": 4,
                    "rationale": "important administration path",
                },
                "automation": {
                    "required": True,
                    "framework": "pytest",
                    "status": "planned",
                },
                "regression": {
                    "candidate": True,
                    "tier": "smoke",
                    "rationale": "protect the administration path",
                    "selection_reason": ["critical_user_journey"],
                    "maintenance_rule": "keep_until_feature_deprecated",
                },
                "trace": dict(trace or {}),
            }
        ],
        "modified": [],
        "removed": [],
    }


def _imported_modules(tree: ast.AST) -> tuple[str, ...]:
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            names.append(node.module)
    return tuple(names)


def test_case_authoring_rejects_fake_capability_leaf() -> None:
    raw = load_fixture("case-authoring-invalid-capability.yaml")
    with pytest.raises(ValidationError, match="capability key is not a declared typed leaf"):
        CaseYamlAuthoring.model_validate(raw, context={"capability_leafs": VALID_LEAFS})


def test_case_authoring_rejects_prefix_capability_leaf() -> None:
    raw = authoring_payload(trace={"entities.item": {"covered": True}})
    with pytest.raises(ValidationError, match="capability key is not a declared typed leaf"):
        CaseYamlAuthoring.model_validate(raw, context={"capability_leafs": VALID_LEAFS})


def test_case_authoring_accepts_exact_typed_leaf() -> None:
    raw = authoring_payload(trace={"entities.item.create": {"covered": True}})
    model = CaseYamlAuthoring.model_validate(raw, context={"capability_leafs": VALID_LEAFS})
    assert model.added[0].trace["entities.item.create"].model_dump() == {"covered": True}


def test_case_authoring_rejects_scalar_trace_coverage() -> None:
    raw = authoring_payload(trace={"entities.item.create": True})
    with pytest.raises(ValidationError, match="valid dictionary or instance of CaseTraceCoverage"):
        CaseYamlAuthoring.model_validate(raw, context={"capability_leafs": VALID_LEAFS})


def test_case_authoring_rejects_empty_capability_trace() -> None:
    raw = authoring_payload()
    with pytest.raises(ValidationError, match="Dictionary should have at least 1 item"):
        CaseYamlAuthoring.model_validate(raw, context={"capability_leafs": VALID_LEAFS})


def test_fuzz_case_requires_typed_endpoint_property_and_expectations() -> None:
    raw = deepcopy(authoring_payload(trace={"entities.item.create": {"covered": True}}))
    case = cast(dict[str, object], cast(list[object], raw["added"])[0])
    case["type"] = "Fuzz"
    case["related_cases"] = ["TC_API_001"]
    case["automation"] = {
        "required": True,
        "framework": "schemathesis",
        "status": "planned",
        "fuzz": {
            "endpoints": [{"method": "POST", "path": "/items"}],
            "property": "item.create.payload",
        },
    }

    with pytest.raises(ValidationError, match="expectations"):
        CaseYamlAuthoring.model_validate(raw, context={"capability_leafs": VALID_LEAFS})


def test_performance_case_requires_explicit_load_definition() -> None:
    raw = deepcopy(authoring_payload(trace={"entities.item.create": {"covered": True}}))
    case = cast(dict[str, object], cast(list[object], raw["added"])[0])
    case["type"] = "Performance"
    case["automation"] = {
        "required": True,
        "framework": "locust",
        "status": "planned",
        "performance": {
            "scenario": {
                "capability": "entities.item.create",
                "endpoint": "POST /items",
                "thresholds": {"p95_ms": 200, "error_rate_max": 0.01},
            }
        },
    }

    with pytest.raises(ValidationError, match="load"):
        CaseYamlAuthoring.model_validate(raw, context={"capability_leafs": VALID_LEAFS})


def test_performance_case_requires_concrete_http_endpoint() -> None:
    raw = deepcopy(authoring_payload(trace={"entities.item.create": {"covered": True}}))
    case = cast(dict[str, object], cast(list[object], raw["added"])[0])
    case["type"] = "Performance"
    case["automation"] = {
        "required": True,
        "framework": "locust",
        "status": "planned",
        "performance": {
            "scenario": {
                "capability": "entities.item.create",
                "endpoint": "item listing",
                "load": {
                    "concurrency": 10,
                    "spawn_rate_per_second": 2,
                    "duration_seconds": 60,
                },
                "thresholds": {"p95_ms": 200, "error_rate_max": 0.01},
            }
        },
    }

    with pytest.raises(ValidationError, match="endpoint"):
        CaseYamlAuthoring.model_validate(raw, context={"capability_leafs": VALID_LEAFS})


def test_intake_schema_bytes_equal_model_schema() -> None:
    assert schema_bytes("assurance.intake.schema.case-authoring.v1") == canonical_json_bytes(
        cast(JSONValue, CaseYamlAuthoring.model_json_schema())
    )
    assert schema_bytes("assurance.intake.schema.case.v1") == canonical_json_bytes(
        cast(JSONValue, CaseYaml.model_json_schema())
    )
    assert schema_bytes("assurance.intake.schema.qa-change.v1") == canonical_json_bytes(
        cast(JSONValue, QaYaml.model_json_schema())
    )
    assert schema_bytes("assurance.intake.schema.case-review.v1") == canonical_json_bytes(
        cast(JSONValue, CaseReviewResultV1.model_json_schema())
    )


def test_case_review_result_schema_exposes_typed_finding_locators() -> None:
    schema = CaseReviewResultV1.model_json_schema()

    assert schema["properties"]["findings"]["items"] == {
        "$ref": "#/$defs/CaseReviewFindingV1"
    }


def test_intake_imports_no_legacy_package() -> None:
    assert forbidden_imports("assurance_intake") == set()
