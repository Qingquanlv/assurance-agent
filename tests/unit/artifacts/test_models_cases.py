import pytest
from pydantic import ValidationError

from assurance_agent.artifacts.models import CaseYaml, CaseYamlAuthoring, QaYaml


def make_case_entry(**overrides: object) -> dict:
    entry: dict = {
        "case_id": "TC_MENU_001",
        "title": "create menu happy path",
        "status": "active",
        "priority": "P1",
        "severity": "major",
        "type": "API",
        "module": "menus",
    }
    entry.update(overrides)
    return entry


def make_case_yaml(**overrides: object) -> dict:
    doc: dict = {
        "schema_version": "1.0",
        "added": [make_case_entry()],
        "modified": [make_case_entry(case_id="TC_MENU_002", status="draft")],
        "removed": [{"case_id": "TC_MENU_003"}],
    }
    doc.update(overrides)
    return doc


def make_qa_yaml(**overrides: object) -> dict:
    doc: dict = {
        "schema_version": "1.0",
        "schema": "qa-yaml/v1",
        "created_at": "2026-07-15T00:00:00Z",
        "change": {
            "change_id": "CH-1",
            "requirement_id": "REQ-1",
            "feature_name": "menu management",
            "status": "in_progress",
        },
        "targets": {
            "cases": [
                {
                    "module": "menus",
                    "change_case_file": "cases/menus/case.yaml",
                    "target_case_file": "qa/cases/menus/case.yaml",
                }
            ]
        },
        "approval": {
            "mode": "autonomous",
            "approved_by": "aa-workflow",
            "approved_approach": "API + E2E + Fuzz + Performance",
            "approved_at": "2026-07-15T00:00:00Z",
        },
    }
    doc.update(overrides)
    return doc


def test_case_yaml_valid_fixture_parses() -> None:
    model = CaseYaml.model_validate(make_case_yaml())
    assert model.added[0].case_id == "TC_MENU_001"
    assert model.modified[0].priority == "P1"
    assert model.removed[0].case_id == "TC_MENU_003"


def _performance_entry(*, scenario: dict[str, object]) -> dict:
    return _authoring_entry(
        case_id="TC_MENU_PERF_001",
        type="Performance",
        automation={
            "required": True,
            "framework": "locust",
            "status": "planned",
            "performance": {"scenario": scenario},
        },
    )


def _authoring_entry(**overrides: object) -> dict:
    entry = make_case_entry(
        requirement_id="REQ-1",
        feature_name="menu-management",
        test_condition_id="COND-1",
        design_technique="use_case",
        objective="verify the menu behavior",
        summary="exercise and assert the menu behavior",
        preconditions=[],
        test_data=[],
        steps=["perform the operation"],
        assertions=["the operation succeeds"],
        postconditions=[],
        edge_cases=[],
        related_cases=[],
        risk={
            "level": "high",
            "likelihood": 3,
            "impact": 4,
            "rationale": "important administration path",
        },
        automation={"required": True, "framework": "pytest", "status": "planned"},
        regression={
            "candidate": True,
            "tier": "smoke",
            "rationale": "protect the administration path",
            "selection_reason": ["critical_user_journey"],
            "maintenance_rule": "keep_until_feature_deprecated",
        },
        trace={},
    )
    entry.update(overrides)
    return entry


def test_case_yaml_keeps_historical_performance_documents_compatible() -> None:
    doc = make_case_yaml(added=[_performance_entry(scenario={"thresholds": {"p95_ms": 500}})])
    assert CaseYaml.model_validate(doc).added[0].case_id == "TC_MENU_PERF_001"


@pytest.mark.parametrize("missing", ["capability", "endpoint"])
def test_case_yaml_authoring_requires_performance_execution_identity(missing: str) -> None:
    scenario = {
        "capability": "menu_list_query",
        "endpoint": "GET /api/v1/menu/list",
        "thresholds": {"p95_ms": 500},
    }
    del scenario[missing]
    doc = make_case_yaml(added=[_performance_entry(scenario=scenario)], modified=[])

    with pytest.raises(ValidationError, match=missing):
        CaseYamlAuthoring.model_validate(doc)


def test_case_yaml_authoring_accepts_complete_performance_execution_identity() -> None:
    doc = make_case_yaml(
        added=[
            _performance_entry(
                scenario={
                    "capability": "menu_list_query",
                    "endpoint": "GET /api/v1/menu/list",
                    "thresholds": {"p95_ms": 500, "error_rate_max": 0.01},
                }
            )
        ],
        modified=[],
    )
    assert CaseYamlAuthoring.model_validate(doc).added[0].case_id == "TC_MENU_PERF_001"


def test_case_yaml_authoring_requires_automation_contract() -> None:
    entry = _authoring_entry()
    del entry["automation"]

    with pytest.raises(ValidationError, match="automation"):
        CaseYamlAuthoring.model_validate(make_case_yaml(added=[entry], modified=[]))


def test_case_yaml_authoring_framework_must_match_case_type() -> None:
    entry = _authoring_entry(type="E2E")

    with pytest.raises(ValidationError, match="pytest-playwright"):
        CaseYamlAuthoring.model_validate(make_case_yaml(added=[entry], modified=[]))


def test_case_yaml_authoring_rejects_empty_delta() -> None:
    with pytest.raises(ValidationError, match="add or modify at least one case"):
        CaseYamlAuthoring.model_validate(make_case_yaml(added=[], modified=[]))


def test_qa_yaml_requires_gate_approval_metadata() -> None:
    doc = make_qa_yaml()
    del doc["approval"]

    with pytest.raises(ValidationError, match="approval"):
        QaYaml.model_validate(doc)


def test_case_yaml_hyphen_case_id_rejected() -> None:
    doc = make_case_yaml(added=[make_case_entry(case_id="TC-MENU-001")])
    with pytest.raises(ValidationError):
        CaseYaml.model_validate(doc)


def test_case_yaml_bad_priority_enum_rejected() -> None:
    doc = make_case_yaml(added=[make_case_entry(priority="P9")])
    with pytest.raises(ValidationError):
        CaseYaml.model_validate(doc)


def test_case_yaml_missing_required_field_rejected() -> None:
    entry = make_case_entry()
    del entry["title"]
    with pytest.raises(ValidationError):
        CaseYaml.model_validate(make_case_yaml(added=[entry]))


def test_case_entry_risk_block_parses_and_exposes_its_level() -> None:
    """`risk` is what the tier lower bound is compared against (metrics spec §7).

    It reached the model unvalidated until now: real case.yaml documents carry
    the block, `CaseEntry` never declared it, and a P0 case labelled `medium`
    was accepted with no complaint.
    """
    entry = make_case_entry(
        risk={"likelihood": 4, "impact": 5, "level": "critical", "rationale": "auth surface"}
    )
    model = CaseYaml.model_validate(make_case_yaml(added=[entry]))
    assert model.added[0].risk is not None
    assert model.added[0].risk.level == "critical"


def test_case_entry_without_a_risk_block_still_parses() -> None:
    model = CaseYaml.model_validate(make_case_yaml())
    assert model.added[0].risk is None


def test_case_entry_risk_level_outside_the_enum_is_rejected() -> None:
    doc = make_case_yaml(added=[make_case_entry(risk={"level": "catastrophic"})])
    with pytest.raises(ValidationError):
        CaseYaml.model_validate(doc)


def test_case_entry_risk_block_without_a_level_is_rejected() -> None:
    """A block whose only load-bearing field is absent is the zero-validation hole."""
    doc = make_case_yaml(added=[make_case_entry(risk={"likelihood": 4, "impact": 5})])
    with pytest.raises(ValidationError):
        CaseYaml.model_validate(doc)


def test_qa_yaml_valid_fixture_parses() -> None:
    model = QaYaml.model_validate(make_qa_yaml())
    assert model.change.change_id == "CH-1"
    assert model.schema_ == "qa-yaml/v1"
    assert model.targets.cases[0].module == "menus"
    assert model.workflow is None


def test_qa_yaml_missing_change_id_rejected() -> None:
    doc = make_qa_yaml()
    del doc["change"]["change_id"]
    with pytest.raises(ValidationError):
        QaYaml.model_validate(doc)


def test_qa_yaml_workflow_optional_fields_parse() -> None:
    doc = make_qa_yaml(workflow={"current_step": "case-design", "next_step": "case-review"})
    model = QaYaml.model_validate(doc)
    assert model.workflow is not None
    assert model.workflow.next_step == "case-review"
