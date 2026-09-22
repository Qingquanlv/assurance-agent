from __future__ import annotations

import pytest

from assurance_generation.contracts.plans import ObservationBindingV1
from assurance_generation.contracts.obligation_methods import (
    expectation_ready,
    required_observation_keys,
    validate_observation_binding,
)
from assurance_intake.contracts.obligations import PreparedObligationV1, VerificationRequirementV1


def test_wrong_password_only_is_not_a_lockout_method() -> None:
    req = VerificationRequirementV1.model_validate(
        {
            "requirement_id": "R-LOCK",
            "profile_id": "api.state-sequence.v1",
            "prerequisites": ["isolated_account", "initial_state_verified"],
            "observations": [
                {
                    "observation_key": "locked_valid_password",
                    "condition": "连续5次失败后，用正确密码登录",
                    "predicate": "status_code_eq",
                    "expected": 423,
                    "basis_refs": [],
                }
            ],
            "semantic_review_required": True,
            "subject_binding_required": True,
        }
    )
    binding = ObservationBindingV1(
        observation_id="OBS-1",
        observation_key="wrong_password",
        step_id="1",
        test_nodeid="qa/tests/api/test_login.py::test_lock",
        assertion_id="A-401",
    )
    with pytest.raises(ValueError, match="observation"):
        validate_observation_binding(req, (binding,))


def _requirement(**overrides: object) -> VerificationRequirementV1:
    payload: dict[str, object] = {
        "requirement_id": "R-LOCK",
        "profile_id": "api.state-sequence.v1",
        "prerequisites": ["isolated_account"],
        "observations": [
            {
                "observation_key": "locked_valid_password",
                "condition": "连续5次失败后，用正确密码登录",
                "predicate": "status_code_eq",
                "expected": 423,
                "basis_refs": [
                    {
                        "kind": "requirement",
                        "artifact": {"path": "qa/requirement.md", "digest": "a" * 64},
                        "locator": "bytes:0-12",
                    }
                ],
            }
        ],
        "semantic_review_required": True,
        "subject_binding_required": True,
    }
    payload.update(overrides)
    return VerificationRequirementV1.model_validate(payload)


def test_required_keys_come_from_frozen_requirement() -> None:
    assert required_observation_keys(_requirement()) == frozenset({"locked_valid_password"})


def test_expectation_ready_requires_authenticated_basis_and_pass_review() -> None:
    from assurance_generation.contracts.reviews import ObligationSemanticReviewV1

    req = _requirement()
    obligation = PreparedObligationV1.model_validate(
        {
            "mrc_id": "MRC-LOCK",
            "key": None,
            "proposed_key": "auth.lockout",
            "category": "api",
            "layer": "api",
            "statement": "锁定后拒绝正确密码",
            "applicability_conditions": [],
            "expected_basis_refs": [
                {
                    "source": req.observations[0].basis_refs[0].model_dump(mode="json"),
                    "source_status": "authenticated",
                }
            ],
            "impact_row_ids": [],
            "required": True,
            "scope_disposition": "included",
            "exclusion_basis": None,
            "open_questions": [],
            "verification_requirements": [req.model_dump(mode="json")],
        }
    )
    review = ObligationSemanticReviewV1.model_validate(
        {
            "frozen_plan_digest": "b" * 64,
            "mrc_id": "MRC-LOCK",
            "requirement_id": "R-LOCK",
            "plan_ref": {
                "path": "qa/results/plan/" + ("b" * 64) + "/resolved-assurance-plan.json",
                "digest": "c" * 64,
            },
            "status": "fail",
            "reason": "source says 200, not 423",
            "source_refs": [item.model_dump(mode="json") for item in req.observations[0].basis_refs],
            "expectation_reviews": [
                {
                    "observation_key": "locked_valid_password",
                    "status": "fail",
                    "reason": "quoted clause is the success path",
                    "basis_refs": [req.observations[0].basis_refs[0].model_dump(mode="json")],
                }
            ],
        }
    )
    assert (
        expectation_ready(
            obligation,
            req,
            "locked_valid_password",
            review,
        )
        is False
    )
    passed = review.model_copy(
        update={
            "status": "pass",
            "reason": "lockout clause supports 423",
            "expectation_reviews": (
                review.expectation_reviews[0].model_copy(
                    update={"status": "pass", "reason": "matches lockout clause"}
                ),
            ),
        }
    )
    assert expectation_ready(obligation, req, "locked_valid_password", passed) is True
