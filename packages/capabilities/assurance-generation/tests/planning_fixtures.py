from __future__ import annotations

from typing import Any, cast

from agent_runtime_contracts import AgentRunResult
from agent_runtime_contracts.schema import canonical_digest
from graph_engine.canonical import JSONValue
from tests.phase4.agent_harness import FakeAgentAdapter

FAMILIES = ("api", "e2e", "fuzz", "performance")
VALID_LEAFS = ("auth.session.create", "entities.item.create")
_SHA = "a" * 64
PLAN_DIGEST = _SHA
PLAN_REF: dict[str, JSONValue] = {
    "path": f"qa/results/plan/{PLAN_DIGEST}/resolved-assurance-plan.json",
    "digest": _SHA,
}
BINDING: dict[str, JSONValue] = {
    "agent_profile": "aa-doc-author",
    "execution": {
        "provider_model": "test-model",
        "worker_profile": "worker",
        "permission_profile_digest": _SHA,
        "limits": {"max_seconds": 5},
    },
    "request_policy_digest": _SHA,
    "request_config_digest": _SHA,
}
_CASE_TYPE = {"api": "API", "e2e": "E2E", "fuzz": "Fuzz", "performance": "Performance"}
_FRAMEWORK = {
    "api": "pytest",
    "e2e": "pytest-playwright",
    "fuzz": "schemathesis",
    "performance": "locust",
}


def family_case_id(family: str) -> str:
    return f"TC_{family.upper()}_001"


def family_write_root(change_id: str = "CH-DEMO-001") -> str:
    del change_id
    return "qa/results/plans/"


def family_plan_files(family: str, change_id: str = "CH-DEMO-001") -> tuple[str, ...]:
    del change_id
    root = "qa/results/plans"
    names = {
        "api": (
            "api-plan.md",
            "api-test-data-plan.md",
            "api-codegen-plan.md",
            "api-codegen-mapping.json",
            "m3-review-summary.md",
        ),
        "e2e": (
            "e2e-plan.md",
            "e2e-test-data-plan.md",
            "e2e-codegen-plan.md",
            "e2e-codegen-mapping.json",
            "m4-review-summary.md",
        ),
        "fuzz": (
            "fuzz-plan.md",
            "fuzz-codegen-plan.md",
            "fuzz-codegen-mapping.json",
            "fuzz-review-summary.md",
        ),
        "performance": (
            "performance-plan.md",
            "performance-codegen-plan.md",
            "performance-codegen-mapping.json",
            "performance-review-summary.md",
        ),
    }[family]
    return tuple(f"{root}/{name}" for name in names)


def reviewed_case(family: str) -> dict[str, Any]:
    case: dict[str, Any] = {
        "case_id": family_case_id(family),
        "title": f"{family} happy path",
        "status": "active",
        "priority": "P1",
        "severity": "major",
        "type": _CASE_TYPE[family],
        "module": "items",
        "requirement_id": "REQ-1",
        "feature_name": "item-management",
        "test_condition_id": "COND-1",
        "design_technique": "use_case",
        "objective": f"verify the {family} behavior",
        "summary": f"exercise and assert the {family} behavior",
        "preconditions": [],
        "test_data": [],
        "steps": ["perform the operation"],
        "assertions": ["the operation succeeds"],
        "postconditions": [],
        "edge_cases": [],
        "related_cases": ["TC_API_001"] if family == "fuzz" else [],
        "risk": {
            "level": "high",
            "likelihood": 3,
            "impact": 4,
            "rationale": "important administration path",
        },
        "automation": {
            "required": True,
            "framework": _FRAMEWORK[family],
            "status": "planned",
        },
        "regression": {
            "candidate": True,
            "tier": "smoke",
            "rationale": "protect the administration path",
            "selection_reason": ["critical_user_journey"],
            "maintenance_rule": "keep_until_feature_deprecated",
        },
        "trace": {"entities.item.create": {"covered": True}},
    }
    if family == "fuzz":
        case["automation"]["fuzz"] = {
            "endpoints": [{"method": "POST", "path": "/items"}],
            "property": "item.create.payload",
            "expectations": ["never returns 5xx", "valid payloads remain schema-conformant"],
        }
    if family == "performance":
        case["automation"]["performance"] = {
            "scenario": {
                "capability": "entities.item.create",
                "endpoint": "POST /items",
                "load": {
                    "concurrency": 10,
                    "spawn_rate_per_second": 2,
                    "duration_seconds": 60,
                },
                "thresholds": {"p95_ms": 200, "error_rate_max": 0.01},
            }
        }
    return case


def reviewed_cases(family: str) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "added": [reviewed_case(family)],
        "modified": [],
        "removed": [],
    }


def family_constraints(family: str) -> dict[str, Any]:
    return {
        "write_roots": list(
            {
                "api": ("qa/tests/api/", "qa/tests/testdata/api/"),
                "e2e": ("qa/tests/e2e/", "qa/tests/testdata/e2e/"),
                "fuzz": ("qa/tests/fuzz/", "qa/tests/testdata/fuzz/"),
                "performance": ("qa/tests/perf/", "qa/tests/testdata/perf/"),
            }[family]
        ),
        "operations": ["COND-1"],
        "risks": ["high"],
    }


def plan_input(family: str) -> dict[str, JSONValue]:
    return {
        "change_id": "CH-DEMO-001",
        "plan_digest": PLAN_DIGEST,
        "plan_ref": PLAN_REF,
        "capability_leafs": list(VALID_LEAFS),
        "artifact_paths": list(family_plan_files(family)),
        "reviewed_cases": reviewed_cases(family),
        "family_constraints": family_constraints(family),
    }


def valid_plan_result(family: str) -> dict[str, Any]:
    case_id = family_case_id(family)
    payload: dict[str, Any] = {
        "schema_version": "1",
        "family": family,
        "change_id": "CH-DEMO-001",
        "case_ids": [case_id],
        "required_capabilities": ["entities.item.create"],
        "coverage": [
            {
                "case_id": case_id,
                "operation": "create",
                "risk": "high",
                "required_capabilities": ["entities.item.create"],
            }
        ],
        "output_files": list(family_plan_files(family)),
    }
    if family == "fuzz":
        payload["fuzz_strategy"] = {
            "endpoint": "POST /items",
            "property_name": "item.create.payload",
        }
    if family == "performance":
        payload["performance_scenarios"] = [
            {
                "scenario_id": "item-create-load",
                "capability": "entities.item.create",
                "endpoint": "POST /items",
                "p95_ms": 200,
                "error_rate_max": 0.01,
            }
        ]
    return payload


def review_result(family: str, leaf: str = "entities.item.create") -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "review_type": f"{family}-codegen",
        "change_id": "CH-DEMO-001",
        "route": "codegen",
        "findings": [],
        "finding_ids": [],
        "next_action": "proceed to codegen",
        "risk_level": "medium",
        "required_capabilities": [leaf],
    }


def fake_agent_result(
    structured_result: dict[str, Any],
    *,
    artifact_paths: list[str] | None = None,
    capability_leafs: tuple[str, ...] = VALID_LEAFS,
    allowed_paths: list[str] | None = None,
) -> dict[str, JSONValue]:
    payload = cast(JSONValue, structured_result)
    result = AgentRunResult(
        result_payload=payload,
        result_digest=canonical_digest(payload),
        evidence_digest=FakeAgentAdapter.EVIDENCE_DIGEST,
        adapter_id="test.fake",
        adapter_version="1.0.0",
    )
    document: dict[str, JSONValue] = {
        "agent_result": result.model_dump(mode="json"),
        "plan_digest": PLAN_DIGEST,
        "plan_ref": PLAN_REF,
        "capability_leafs": list(capability_leafs),
        "artifact_paths": list(artifact_paths or []),
        "allowed_paths": list(allowed_paths or []),
    }
    required_capabilities = structured_result.get("required_capabilities")
    if isinstance(required_capabilities, list):
        document["required_capabilities"] = cast(JSONValue, required_capabilities)
    return document
