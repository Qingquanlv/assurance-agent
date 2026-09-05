from __future__ import annotations

import ast
import json
from copy import deepcopy
from collections.abc import Mapping
from pathlib import Path
from typing import cast

import pytest
import yaml
from pydantic import ValidationError

from graph_engine import ENGINE_API_VERSION, RegistryPorts
from graph_engine.attempts.resolutions import ReceiptRef
from graph_engine.canonical import JSONValue, canonical_digest, canonical_json_bytes

from assurance_intake.contracts import CaseReviewResultV1, CaseYaml, CaseYamlAuthoring, QaYaml
from assurance_intake.contracts.explore import ExploreAdvisoryV1
from assurance_intake.contracts.review import normalized_auto_fix_edits
from assurance_intake.contracts.workflow import (
    CaseFlowResultV1,
    CaseReworkContextV1,
    EvidenceArtifactRefV1,
    ReviewedCaseV1,
)
from assurance_intake.plugin import IntakePlugin

_CURRENT_INTAKE_SCHEMA_MAPPING: dict[str, tuple[str, str]] = {
    "assurance.intake.schema.case-authoring.v1": (
        "1",
        "93b83ab51c63145bc1d7a90f6be6c79ccef64e106aabcb5eee4d9ef75bbcbe2f",
    ),
    "assurance.intake.schema.case-review.v1": (
        "1",
        "f39c10af39534ae25c7daa7be0044c5082d82ab8a4108ad92725219fececefea",
    ),
    "assurance.intake.schema.case.v1": (
        "1",
        "93b83ab51c63145bc1d7a90f6be6c79ccef64e106aabcb5eee4d9ef75bbcbe2f",
    ),
    "assurance.intake.schema.qa-change.v1": (
        "1",
        "c0053b53d8d5358e7818694c5313e7632a62e379113c161c959f2ed46e2ea1f5",
    ),
    "assurance.intake.workflow.case.input.v1": (
        "1",
        "a092fe28e54f550001e021c726350a082bb5797c8868abd13730baae4c54585a",
    ),
    "assurance.intake.workflow.case.output.v1": (
        "1",
        "acdb88e9650b96d7305d3919cb0a9280af3224495e3bd341af2deb5d2cf2075e",
    ),
    "assurance.intake.workflow.prepare.input.v1": (
        "1",
        "9784c26ac8e68916b6961943bd38c2b26082c1ff0b19d525a427844bcd70ca46",
    ),
    "assurance.intake.workflow.prepare.output.v1": (
        "1",
        "acdb88e9650b96d7305d3919cb0a9280af3224495e3bd341af2deb5d2cf2075e",
    ),
}

_TESTS_ROOT = Path(__file__).resolve().parent
_WHEEL_ROOT = _TESTS_ROOT.parent
VALID_LEAFS = frozenset({"entities.item.create", "auth.session.create"})
_LEGACY_ROOTS = ("assurance_agent", "assurance_kernel")
_SHA = "a" * 64


def _reviewed_case() -> ReviewedCaseV1:
    return ReviewedCaseV1(
        change_id="CH-DEMO-001",
        coverage_epoch=0,
        preparation_refs=(EvidenceArtifactRefV1(path="qa/changes/CH-DEMO-001/requirement.md", digest=_SHA),),
        case_refs=(EvidenceArtifactRefV1(path="qa/changes/CH-DEMO-001/cases/menus/case.yaml", digest=_SHA),),
        review_ref=EvidenceArtifactRefV1(path="qa/changes/CH-DEMO-001/review/case-review.json", digest=_SHA),
    )


def test_reviewed_result_requires_review_evidence() -> None:
    with pytest.raises(ValidationError):
        CaseFlowResultV1(status="reviewed", reviewed_case=None, receipt=None)


def test_unsuccessful_case_result_cannot_carry_success_evidence() -> None:
    with pytest.raises(ValidationError):
        CaseFlowResultV1(
            status="rejected",
            reviewed_case=_reviewed_case(),
            receipt=ReceiptRef(receipt_id="review", receipt_digest=_SHA),
        )


@pytest.mark.parametrize("field", ["preparation_refs", "case_refs"])
def test_reviewed_case_requires_all_version_refs(field: str) -> None:
    payload = _reviewed_case().model_dump(mode="json")
    payload[field] = []
    with pytest.raises(ValidationError):
        ReviewedCaseV1.model_validate(payload)


def test_case_rework_targets_stay_inside_the_current_change() -> None:
    gap = EvidenceArtifactRefV1(path="qa/changes/CH-DEMO-001/inspect/coverage-gaps.json", digest=_SHA)
    with pytest.raises(ValidationError):
        CaseReworkContextV1(
            previous_case=_reviewed_case(),
            inspect_receipt=ReceiptRef(receipt_id="inspect", receipt_digest=_SHA),
            assessment_refs=(gap,),
            gaps_ref=gap,
            target_case_paths=("qa/changes/OTHER/cases/menus/case.yaml",),
        )


def test_case_rework_can_describe_a_missing_requirement_through_gap_evidence() -> None:
    gap = EvidenceArtifactRefV1(path="qa/changes/CH-DEMO-001/inspect/coverage-gaps.json", digest=_SHA)
    context = CaseReworkContextV1(
        previous_case=_reviewed_case(),
        inspect_receipt=ReceiptRef(receipt_id="inspect", receipt_digest=_SHA),
        assessment_refs=(gap,),
        gaps_ref=gap,
        target_case_paths=("qa/changes/CH-DEMO-001/cases/menus/case.yaml",),
    )
    assert context.gaps_ref == gap


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


def _installed_schema_mapping() -> dict[str, tuple[str, str]]:
    contribution = IntakePlugin.contribute(RegistryPorts(engine_api=ENGINE_API_VERSION))
    return {
        schema.schema_id: (
            "1",
            canonical_digest(cast(JSONValue, json.loads(schema.content))),
        )
        for schema in contribution.schemas
    }


def test_intake_product_lock_schema_mapping_is_current_only() -> None:
    assert _installed_schema_mapping() == _CURRENT_INTAKE_SCHEMA_MAPPING


def valid_case_review() -> dict[str, object]:
    return _review_payload(decision="pass", auto_fix_allowed=False, human_review_required=False)


@pytest.mark.parametrize("decision", ["approved", "changes_requested"])
def test_case_review_rejects_old_decisions(decision: str) -> None:
    payload = {
        **valid_case_review(),
        "decision": decision,
        **(
            {"auto_fix_allowed": True, "auto_fix_plan": [{"fix": "tighten assertion"}]}
            if decision == "changes_requested"
            else {}
        ),
    }
    with pytest.raises(ValidationError):
        CaseReviewResultV1.model_validate(payload)


@pytest.mark.parametrize("old_key", ["action", "instructions"])
def test_auto_fix_requires_edits(old_key: str) -> None:
    with pytest.raises(ValueError, match="edits"):
        normalized_auto_fix_edits({old_key: ["replace value"]})


def test_case_yaml_read_requires_authoring_fields() -> None:
    with pytest.raises(ValidationError):
        CaseYaml.model_validate(
            {
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
                    }
                ],
                "modified": [],
                "removed": [],
            },
            context={"capability_leafs": VALID_LEAFS},
        )


def test_explore_rejects_wrong_schema_version() -> None:
    with pytest.raises(ValidationError):
        ExploreAdvisoryV1.model_validate(
            {
                "schema_version": "0.9",
                "change_id": "CH-DEMO-001",
                "context_ref": "explore/context.json",
                "generated_at": "2026-08-23T00:00:00Z",
                "executive_summary": "Department API source was inspected.",
                "watchlist": [],
                "evidence_inventory": {"available": [], "missing": [], "not_inspected": []},
                "source_code_evidence": [],
                "case_design_guidance": {
                    "priority_hints": [],
                    "suggested_scenarios": [],
                    "regression_focus": [],
                },
                "minimum_required_coverage": {"api": []},
                "open_questions_for_case_design": [],
                "test_strategy": {"layer_recommendation": [{"layer": "API", "recommended": True}]},
            }
        )


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

    assert schema["properties"]["findings"]["items"] == {"$ref": "#/$defs/CaseReviewFindingV1"}
    assert any("comma-separated dotted field paths" in note for note in schema["prompt_notes"])


def _review_payload(
    *,
    decision: str,
    auto_fix_allowed: bool,
    human_review_required: bool,
    auto_fix_plan: list[object] | None = None,
) -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "review_type": "case",
        "change_id": "CH-DEMO-001",
        "decision": decision,
        "findings": [],
        "auto_fix_plan": [] if auto_fix_plan is None else auto_fix_plan,
        "next_action": "continue",
        "auto_fix_allowed": auto_fix_allowed,
        "human_review_required": human_review_required,
        "risk_level": "low",
        "minimum_coverage": {
            "total_required": 0,
            "covered": 0,
            "skipped_by_scope": 0,
            "missing": [],
        },
        "source_verification": {
            "independent": True,
            "reviewed_source_files": ["src/app.py"],
            "verified_claims": [{"claim": "create item persists", "evidence_files": ["src/app.py"]}],
        },
    }


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
def test_raw_review_decisions_normalize_to_public_outcomes(
    decision: str,
    auto_fix_allowed: bool,
    human_review_required: bool,
    expected: str,
) -> None:
    from assurance_intake.contracts.review import PUBLIC_REVIEW_OUTCOMES, public_review_outcome

    model = CaseReviewResultV1.model_validate(
        _review_payload(
            decision=decision,
            auto_fix_allowed=auto_fix_allowed,
            human_review_required=human_review_required,
            auto_fix_plan=[{"fix": "tighten assertion"}] if expected == "needs_fix" else [],
        )
    )
    assert public_review_outcome(decision, auto_fix_allowed, human_review_required) == expected
    assert model.public_outcome == expected
    assert public_review_outcome(model) == expected
    assert expected in PUBLIC_REVIEW_OUTCOMES


@pytest.mark.parametrize(
    ("decision", "auto_fix_allowed", "human_review_required"),
    [
        ("pass", True, False),
        ("needs_fix", True, True),
        ("needs_fix", False, False),
        ("needs_human_review", True, True),
        ("needs_human_review", False, False),
        ("reject", True, False),
        ("reject", False, True),
    ],
)
def test_contradictory_review_combinations_fail_validation(
    decision: str,
    auto_fix_allowed: bool,
    human_review_required: bool,
) -> None:
    with pytest.raises(ValidationError, match="review"):
        CaseReviewResultV1.model_validate(
            _review_payload(
                decision=decision,
                auto_fix_allowed=auto_fix_allowed,
                human_review_required=human_review_required,
            )
        )


def test_intake_imports_no_legacy_package() -> None:
    assert forbidden_imports("assurance_intake") == set()


def test_intake_agent_job_catalog_is_feature_owned() -> None:
    from types import MappingProxyType

    from assurance_intake.contracts.attempts import AGENT_JOB_CONTRACTS, OUTPUT_ROUTE_TEMPLATES

    expected = {
        "case-design": (
            "aa-case-design",
            "assurance-v1-doc-author",
            (
                "qa/changes/{change_id}/.qa.yaml",
                "qa/changes/{change_id}/cases",
                "qa/changes/{change_id}/proposal.md",
                "qa/changes/{change_id}/trace/minimum-coverage-matrix.json",
            ),
            (
                "qa/changes/{change_id}/.qa.yaml",
                "qa/changes/{change_id}/proposal.md",
                "qa/changes/{change_id}/trace/minimum-coverage-matrix.json",
            ),
        ),
        "case-review": (
            "aa-case-reviewer",
            "assurance-v1-reviewer",
            (
                "qa/changes/{change_id}/cases/reviewed-case.json",
                "qa/changes/{change_id}/cases/reviews",
                "qa/changes/{change_id}/review/case-review-summary.md",
                "qa/changes/{change_id}/review/case-review.json",
            ),
            (
                "qa/changes/{change_id}/cases/reviews/epochs/{coverage_epoch}/rounds/{review_round}.json",
                "qa/changes/{change_id}/review/case-review-summary.md",
                "qa/changes/{change_id}/review/case-review.json",
            ),
        ),
        "explore": (
            "aa-explore",
            "assurance-v1-explorer",
            (
                "qa/changes/{change_id}/explore/context.json",
                "qa/changes/{change_id}/explore/exploration.json",
            ),
            ("qa/changes/{change_id}/explore/exploration.json",),
        ),
        "intake": (
            "aa-intake",
            "assurance-v1-doc-author",
            ("qa/changes/{change_id}/.qa.yaml", "qa/changes/{change_id}/requirement.md"),
            ("qa/changes/{change_id}/.qa.yaml", "qa/changes/{change_id}/requirement.md"),
        ),
    }
    assert isinstance(AGENT_JOB_CONTRACTS, MappingProxyType)
    assert isinstance(OUTPUT_ROUTE_TEMPLATES, MappingProxyType)
    assert len(AGENT_JOB_CONTRACTS) == 4
    assert tuple(AGENT_JOB_CONTRACTS) == tuple(expected)
    assert tuple(OUTPUT_ROUTE_TEMPLATES) == tuple(expected)
    for base, (skill_id, agent_profile, writes, routes) in expected.items():
        contract = AGENT_JOB_CONTRACTS[base]
        assert contract.contract_id == f"assurance.intake.agent.{base}.v1"
        assert contract.skill_id == skill_id
        assert contract.agent_profile == agent_profile
        assert contract.resources.writes == writes
        assert OUTPUT_ROUTE_TEMPLATES[base] == routes
        dumped = json.dumps(contract.canonical_projection()).lower()
        assert "opencode" not in dumped
        assert "cursor" not in dumped
    assert forbidden_imports("assurance_intake", "assurance_product") == set()
