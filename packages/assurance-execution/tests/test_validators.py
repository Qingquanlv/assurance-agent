from __future__ import annotations

from graph_engine import ENGINE_API_VERSION, RegistryPorts
from graph_engine.plugin_api import (
    CandidateFile,
    CandidateWriteSet,
    ResourceClaims,
    ValidationContext,
)

from assurance_execution.plugin import ExecutionPlugin
from assurance_execution.validators.evidence import ExecutionEvidenceValidator
from assurance_execution.validators.mapping import ClosedMappingValidator


def candidate_with_result(test: str) -> CandidateWriteSet:
    return CandidateWriteSet(
        baseline_tree_id="0" * 64,
        candidate_tree_id="1" * 64,
        files=(CandidateFile(path=test, before_sha256=None, after_sha256="a" * 64),),
    )


def validation_context() -> ValidationContext:
    return ValidationContext(
        invocation_id="phase4-test",
        task_id="phase4-task",
        graph_instance_id="phase4-graph",
        node_id="phase4-node",
        resources=ResourceClaims(),
    )


def test_evidence_validator_rejects_result_outside_mapping() -> None:
    result = ExecutionEvidenceValidator().validate(
        candidate_with_result("tests/legacy_test.py"), validation_context()
    )
    assert result.accepted is False
    assert result.reason == "execution evidence contains a test outside the closed mapping"


def test_closed_mapping_validator_rejects_unmapped_test() -> None:
    result = ClosedMappingValidator().validate(
        candidate_with_result("tests/legacy_test.py"), validation_context()
    )
    assert result.accepted is False
    assert result.reason == "mapping must equal selected tests"


def test_plugin_contributed_validators_are_path_only() -> None:
    contribution = ExecutionPlugin.contribute(RegistryPorts(engine_api=ENGINE_API_VERSION))
    mapping = contribution.commit_validators["assurance.execution.validator.closed-mapping.v1"]
    evidence = contribution.commit_validators["assurance.execution.validator.evidence.v1"]
    context = validation_context()
    allowed = candidate_with_result("tests/legacy_test.py")
    assert mapping.validate(allowed, context).accepted is True
    assert evidence.validate(allowed, context).accepted is True
    rejected = mapping.validate(candidate_with_result("src/app.py"), context)
    assert rejected.accepted is False
    assert evidence.validate(candidate_with_result("../secret.py"), context).accepted is False
