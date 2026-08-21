from __future__ import annotations

from pathlib import Path

import yaml

from graph_engine.plugin_api import (
    CandidateFile,
    CandidateWriteSet,
    ResourceClaims,
    ValidationContext,
    ValidationResult,
)

from assurance_intake.validators import CaseCandidateValidator, CaseReferenceValidator

_SHA = "a" * 64
_FIXTURES = Path(__file__).resolve().parent / "fixtures"


def candidate_with(*paths: str) -> CandidateWriteSet:
    return CandidateWriteSet(
        baseline_tree_id="0" * 64,
        candidate_tree_id="1" * 64,
        files=tuple(CandidateFile(path=path, before_sha256=None, after_sha256=_SHA) for path in paths),
    )


def validation_context() -> ValidationContext:
    return ValidationContext(
        invocation_id="phase4-test",
        task_id="phase4-task",
        graph_instance_id="phase4-graph",
        node_id="phase4-node",
        resources=ResourceClaims(),
    )


def test_case_validator_rejects_tree_outside_case_paths() -> None:
    result = CaseCandidateValidator().validate(candidate_with("src/app.py"), validation_context())
    assert result == ValidationResult(
        accepted=False,
        reason="intake candidate may write only change and cases paths",
    )


def test_case_validator_rejects_traversal_and_absolute_paths() -> None:
    context = validation_context()
    validator = CaseCandidateValidator()
    assert validator.validate(candidate_with("../secret.yaml"), context).accepted is False
    assert validator.validate(candidate_with("/tmp/case.yaml"), context).accepted is False


def test_case_validator_accepts_change_case_yaml_with_exact_leaf() -> None:
    path = "qa/changes/CH-DEMO-001/cases/menus/case.yaml"
    payload = (_FIXTURES / "case-authoring-valid.yaml").read_bytes()
    result = CaseCandidateValidator(
        capability_leafs=frozenset({"entities.item.create", "auth.session.create"}),
        file_bytes={path: payload},
    ).validate(candidate_with(path), validation_context())
    assert result == ValidationResult(accepted=True)


def test_case_reference_validator_rejects_missing_related_case() -> None:
    path = "qa/changes/CH-DEMO-001/cases/menus/case.yaml"
    raw = yaml.safe_load((_FIXTURES / "case-authoring-valid.yaml").read_text(encoding="utf-8"))
    raw["added"][0]["related_cases"] = ["qa/cases/menus/missing.yaml"]
    payload = yaml.safe_dump(raw, sort_keys=False).encode("utf-8")
    result = CaseReferenceValidator(file_bytes={path: payload}).validate(
        candidate_with(path),
        validation_context(),
    )
    assert result.accepted is False
    assert result.reason is not None
    assert "qa/cases/menus/missing.yaml" in result.reason
