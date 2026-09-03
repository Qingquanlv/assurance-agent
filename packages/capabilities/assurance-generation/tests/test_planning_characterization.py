from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from graph_engine.plugin_api import CandidateFile, CandidateWriteSet, ResourceClaims, ValidationContext

from assurance_generation.contracts import PlanReviewAuthoring
from assurance_generation.validators.plans import family_validator
from planning_fixtures import (  # pyright: ignore[reportMissingImports]
    FAMILIES,
    VALID_LEAFS,
    family_case_id,
    family_plan_files,
    review_result,
)

_FIXTURES = Path(__file__).resolve().parent / "fixtures"
_SHA = "a" * 64


def _load(name: str) -> dict[str, Any]:
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))


def _candidate_with(*paths: str) -> CandidateWriteSet:
    return CandidateWriteSet(
        baseline_tree_id="0" * 64,
        candidate_tree_id="1" * 64,
        files=tuple(CandidateFile(path=path, before_sha256=None, after_sha256=_SHA) for path in paths),
    )


def _context() -> ValidationContext:
    return ValidationContext(
        invocation_id="phase4-test",
        task_id="phase4-task",
        graph_instance_id="phase4-graph",
        node_id="phase4-node",
        resources=ResourceClaims(),
    )


@pytest.mark.parametrize("family", FAMILIES)
def test_generation_rejects_unknown_leaf(family: str) -> None:
    raw = review_result(family, leaf="auth.fake")
    with pytest.raises(ValidationError, match="unknown capability leaf"):
        PlanReviewAuthoring.model_validate(raw, context={"capability_leafs": frozenset(VALID_LEAFS)})


@pytest.mark.parametrize("family", FAMILIES)
def test_generation_rejects_invalid_review_decision(family: str) -> None:
    raw = review_result(family)
    raw["decision"] = "approved"
    with pytest.raises(ValidationError):
        PlanReviewAuthoring.model_validate(raw, context={"capability_leafs": frozenset(VALID_LEAFS)})


@pytest.mark.parametrize("family", FAMILIES)
def test_family_plan_characterization_accepts_valid_and_rejects_two_invalids(family: str) -> None:
    valid = _load(f"{family}-plan-valid.json")
    unknown_case = _load(f"{family}-plan-invalid-case.json")
    wrong_family = _load(f"{family}-plan-invalid-family.json")
    files = family_plan_files(family)
    validator = family_validator(
        family,
        capability_leafs=frozenset(VALID_LEAFS),
        case_ids=frozenset({family_case_id(family)}),
        write_roots=("qa/changes/CH-DEMO-001/plans/",),
    )
    valid_path = f"qa/changes/CH-DEMO-001/plans/{family}-plan.json"
    accepted = family_validator(
        family,
        capability_leafs=frozenset(VALID_LEAFS),
        case_ids=frozenset({family_case_id(family)}),
        file_bytes={valid_path: json.dumps(valid, separators=(",", ":"), sort_keys=True).encode("utf-8")},
        write_roots=("qa/changes/CH-DEMO-001/plans/",),
    ).validate(_candidate_with(*files, valid_path), _context())
    assert accepted.accepted is True
    for payload in (unknown_case, wrong_family):
        rejected = family_validator(
            family,
            capability_leafs=frozenset(VALID_LEAFS),
            case_ids=frozenset({family_case_id(family)}),
            file_bytes={
                valid_path: json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
            },
            write_roots=("qa/changes/CH-DEMO-001/plans/",),
        ).validate(_candidate_with(*files, valid_path), _context())
        assert rejected.accepted is False
    assert validator.validate(_candidate_with("src/app.py"), _context()).accepted is False
