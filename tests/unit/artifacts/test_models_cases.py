import pytest
from pydantic import ValidationError

from assurance_agent.artifacts.models import CaseYaml, QaYaml


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
    }
    doc.update(overrides)
    return doc


def test_case_yaml_valid_fixture_parses() -> None:
    model = CaseYaml.model_validate(make_case_yaml())
    assert model.added[0].case_id == "TC_MENU_001"
    assert model.modified[0].priority == "P1"
    assert model.removed[0].case_id == "TC_MENU_003"


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
