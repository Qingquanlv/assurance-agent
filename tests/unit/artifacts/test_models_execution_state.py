import pytest
from pydantic import ValidationError

from assurance_agent.artifacts.models import ExecutionManifest, WorkflowState


def make_manifest(**overrides: object) -> dict:
    doc: dict = {
        "schema_version": "1.0",
        "change_id": "CH-1",
        "batch_id": "batch-20260715-001",
        "selected_targets": {"api": True, "e2e": True, "fuzz": False, "performance": False},
        "result_files": {"api": "execution/runs/batch-20260715-001/api-result.json"},
        "final_status": "PASS",
    }
    doc.update(overrides)
    return doc


def test_execution_manifest_valid_fixture_parses() -> None:
    model = ExecutionManifest.model_validate(make_manifest())
    assert model.batch_id == "batch-20260715-001"
    assert model.selected_targets.api is True
    assert model.final_status == "PASS"
    assert model.tests_tree_sha256 is None


def test_execution_manifest_final_status_enum_violation_fails() -> None:
    with pytest.raises(ValidationError):
        ExecutionManifest.model_validate(make_manifest(final_status="GREEN"))


def test_execution_manifest_final_status_optional() -> None:
    doc = make_manifest()
    del doc["final_status"]
    assert ExecutionManifest.model_validate(doc).final_status is None


def test_execution_manifest_missing_batch_id_fails() -> None:
    doc = make_manifest()
    del doc["batch_id"]
    with pytest.raises(ValidationError):
        ExecutionManifest.model_validate(doc)


def test_execution_manifest_wrong_schema_version_fails() -> None:
    with pytest.raises(ValidationError):
        ExecutionManifest.model_validate(make_manifest(schema_version="2.0"))


def test_workflow_state_minimal_doc_parses() -> None:
    model = WorkflowState.model_validate({})
    assert model.schema_version is None
    assert model.phases.healing.attempts_used == 0
    assert model.phases.execution is None


def test_workflow_state_extra_fields_preserved() -> None:
    model = WorkflowState.model_validate(
        {
            "schema_version": "1",
            "params": {"run_mode": "full"},
            "phases": {"explore": {"status": "done"}},
            "gates": {"healing_available": True},
        }
    )
    assert model.params == {"run_mode": "full"}
    assert model.gates.healing_available is True
    assert model.phases.model_extra is not None
    assert model.phases.model_extra["explore"]["status"] == "done"


def test_workflow_state_known_core_fields_are_typed() -> None:
    model = WorkflowState.model_validate({
        "run_context": {"active_scope": "execute"},
        "phases": {
            "execution": {"status": "FAIL", "batch_id": "b1"},
            "inspect": {"inspect_mode": "primary"},
            "healing": {"status": "pending", "attempts_used": 2, "all_fixers_no_op": False},
        },
    })
    assert model.run_context.active_scope == "execute"
    assert model.phases.execution is not None
    assert model.phases.execution.batch_id == "b1"
    assert model.phases.healing.attempts_used == 2


def test_workflow_state_negative_attempts_rejected() -> None:
    with pytest.raises(ValidationError):
        WorkflowState.model_validate({"phases": {"healing": {"attempts_used": -1}}})
