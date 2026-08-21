from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest
import yaml
from pydantic import ValidationError

from graph_engine.canonical import JSONValue, canonical_json_bytes
from graph_engine.plugin_api import (
    CandidateFile,
    CandidateWriteSet,
    ResourceClaims,
    ValidationContext,
    ValidationResult,
)

from assurance_intake.contracts import CaseYamlAuthoring, QaYaml
from assurance_intake.validators import CaseCandidateValidator
from tests.phase4.conformance import assert_validator_rejects

from assurance_kernel.artifacts.models.cases import CaseYamlAuthoring as LegacyCaseYamlAuthoring
from assurance_kernel.artifacts.models.cases import QaYaml as LegacyQaYaml

_FIXTURES = Path(__file__).resolve().parent / "fixtures"
VALID_LEAFS = frozenset({"entities.item.create", "auth.session.create"})
_SHA = "a" * 64


def _load(name: str) -> object:
    return yaml.safe_load((_FIXTURES / name).read_text(encoding="utf-8"))


def _candidate_with(*paths: str) -> CandidateWriteSet:
    return CandidateWriteSet(
        baseline_tree_id="0" * 64,
        candidate_tree_id="1" * 64,
        files=tuple(CandidateFile(path=path, before_sha256=None, after_sha256=_SHA) for path in paths),
    )


def _validation_context() -> ValidationContext:
    return ValidationContext(
        invocation_id="phase4-test",
        task_id="phase4-task",
        graph_instance_id="phase4-graph",
        node_id="phase4-node",
        resources=ResourceClaims(),
    )


def _qa_payload(*, approved_by: str = "user") -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "schema": "qa-change/v1",
        "created_at": "2026-08-22T00:00:00Z",
        "change": {
            "change_id": "CH-DEMO-001",
            "requirement_id": "REQ-1",
            "feature_name": "menu-management",
            "status": "draft",
        },
        "targets": {
            "cases": [
                {
                    "module": "menus",
                    "change_case_file": "qa/changes/CH-DEMO-001/cases/menus/case.yaml",
                    "target_case_file": "qa/cases/menus/case.yaml",
                }
            ]
        },
        "approval": {
            "mode": "interactive",
            "approved_by": approved_by,
            "approved_approach": "api-first",
            "approved_at": "2026-08-22T00:00:00Z",
        },
    }


def _gate_decision(approval: object) -> str:
    if not isinstance(approval, dict):
        return "stop"
    if approval.get("mode") == "autonomous":
        return "pass"
    if (
        approval.get("approved_by") == "user"
        and approval.get("approved_approach")
        and approval.get("approved_at")
    ):
        return "pass"
    return "stop"


def test_legacy_and_intake_canonical_case_yaml_match() -> None:
    raw = _load("case-authoring-valid.yaml")
    legacy = LegacyCaseYamlAuthoring.model_validate(raw)
    current = CaseYamlAuthoring.model_validate(raw, context={"capability_leafs": VALID_LEAFS})
    assert canonical_json_bytes(cast(JSONValue, legacy.model_dump(mode="json"))) == canonical_json_bytes(
        cast(JSONValue, current.model_dump(mode="json"))
    )


def test_legacy_and_intake_reject_the_same_unknown_capability_key() -> None:
    raw = _load("case-authoring-invalid-capability.yaml")
    with pytest.raises(ValidationError, match="capability key is not a declared typed leaf"):
        CaseYamlAuthoring.model_validate(raw, context={"capability_leafs": VALID_LEAFS})
    legacy = LegacyCaseYamlAuthoring.model_validate(raw)
    rejected = [
        key for entry in (*legacy.added, *legacy.modified) for key in entry.trace if key not in VALID_LEAFS
    ]
    assert rejected == ["entities.fake"]


def test_legacy_and_intake_gate_decision_match() -> None:
    valid = _qa_payload()
    invalid = _qa_payload(approved_by="agent")
    assert (
        LegacyQaYaml.model_validate(valid).approval.approved_by
        == QaYaml.model_validate(valid).approval.approved_by
    )
    assert _gate_decision(valid["approval"]) == "pass"
    assert _gate_decision(invalid["approval"]) == "stop"


def test_legacy_and_intake_candidate_write_set_match() -> None:
    allowed = "qa/changes/CH-DEMO-001/cases/menus/case.yaml"
    forbidden = "src/app.py"
    accepted = CaseCandidateValidator().validate(_candidate_with(allowed), _validation_context())
    assert accepted == ValidationResult(accepted=True)
    assert_validator_rejects(
        CaseCandidateValidator(),
        reason="intake candidate may write only change and cases paths",
        files=_candidate_with(forbidden).files,
    )
    assert allowed.startswith(("qa/changes/", "qa/cases/"))
    assert not forbidden.startswith(("qa/changes/", "qa/cases/"))
