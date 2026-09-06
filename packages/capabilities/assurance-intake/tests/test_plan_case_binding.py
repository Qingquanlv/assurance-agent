import pytest
from pydantic import ValidationError

from assurance_intake.contracts.agent import CaseDesignInputV1, CaseFinalizeInputV1
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1, require_same_plan


PLAN_DIGEST = "a" * 64
PLAN_REF = EvidenceArtifactRefV1(
    path=f"qa/changes/CH-1/plan/{PLAN_DIGEST}/resolved-assurance-plan.json",
    digest="b" * 64,
)


def test_same_ref_does_not_allow_another_plan_digest() -> None:
    with pytest.raises(ValueError, match="plan"):
        require_same_plan(PLAN_DIGEST, PLAN_REF, "c" * 64, PLAN_REF)


def test_case_design_requires_plan_binding() -> None:
    payload = {
        "change_id": "CH-1",
        "capability_leafs": (),
        "artifact_paths": (),
        "selected_test_families": ("api",),
        "case_delta_paths": ("qa/changes/CH-1/cases/core/case.yaml",),
    }
    with pytest.raises(ValidationError, match="plan_digest|plan_ref"):
        CaseDesignInputV1.model_validate(payload)


def test_case_finalize_requires_plan_binding() -> None:
    assert CaseFinalizeInputV1.model_fields["plan_digest"].is_required()
    assert CaseFinalizeInputV1.model_fields["plan_ref"].is_required()
