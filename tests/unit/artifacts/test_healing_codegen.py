import pytest
from pydantic import ValidationError

from assurance_agent.artifacts.canonical import canonical_json_bytes
from assurance_agent.artifacts.models import (
    ApiCodegenFixApplyIntentV1,
    ApiCodegenFixerSafetyCheckV1,
    E2eCodegenFixApplyIntentV1,
    FixerProposalApprovalReceiptV1,
)


def valid_fix_intent_payload(*, outcome: str = "applied") -> dict:
    return {
        "schema_version": "1",
        "target": "api",
        "outcome": outcome,
        "proposal_ids": ["FIX_001"] if outcome == "applied" else [],
        "reason": None if outcome == "applied" else "No authorized change was needed",
        "claimed_modified_paths": ["tests/api/test_login.py"] if outcome == "applied" else [],
    }


def test_applied_intent_requires_canonical_nonempty_proposals_and_paths() -> None:
    payload = valid_fix_intent_payload(outcome="applied")
    payload["proposal_ids"] = []
    with pytest.raises(ValidationError):
        ApiCodegenFixApplyIntentV1.model_validate(payload)

    payload = valid_fix_intent_payload(outcome="applied")
    payload["claimed_modified_paths"] = []
    with pytest.raises(ValidationError):
        ApiCodegenFixApplyIntentV1.model_validate(payload)


@pytest.mark.parametrize("outcome", ["no_op", "skipped"])
def test_non_applied_intent_requires_nonempty_reason_and_no_claimed_paths(outcome: str) -> None:
    payload = valid_fix_intent_payload(outcome=outcome)
    payload["reason"] = ""
    with pytest.raises(ValidationError):
        ApiCodegenFixApplyIntentV1.model_validate(payload)

    payload = valid_fix_intent_payload(outcome=outcome)
    payload["claimed_modified_paths"] = ["tests/api/test_login.py"]
    with pytest.raises(ValidationError):
        ApiCodegenFixApplyIntentV1.model_validate(payload)


def test_intent_rejects_unsorted_duplicate_and_backslash_paths() -> None:
    payload = valid_fix_intent_payload()
    payload["proposal_ids"] = ["FIX_002", "FIX_001"]
    with pytest.raises(ValidationError):
        ApiCodegenFixApplyIntentV1.model_validate(payload)

    payload = valid_fix_intent_payload()
    payload["claimed_modified_paths"] = ["tests/api/test_login.py", "tests/api/test_login.py"]
    with pytest.raises(ValidationError):
        ApiCodegenFixApplyIntentV1.model_validate(payload)

    payload = valid_fix_intent_payload()
    payload["claimed_modified_paths"] = ["tests\\api\\test_login.py"]
    with pytest.raises(ValidationError):
        ApiCodegenFixApplyIntentV1.model_validate(payload)


def test_api_intent_rejects_e2e_target_and_extra_fields() -> None:
    payload = valid_fix_intent_payload()
    payload["target"] = "e2e"
    with pytest.raises(ValidationError):
        ApiCodegenFixApplyIntentV1.model_validate(payload)

    payload = valid_fix_intent_payload()
    payload["approved"] = True
    with pytest.raises(ValidationError):
        ApiCodegenFixApplyIntentV1.model_validate(payload)


def test_e2e_intent_accepts_e2e_and_serializes_canonically() -> None:
    payload = valid_fix_intent_payload()
    payload["target"] = "e2e"
    intent = E2eCodegenFixApplyIntentV1.model_validate(payload)
    assert canonical_json_bytes(intent) == canonical_json_bytes(intent)


def test_approval_receipt_requires_canonical_nonempty_targets_and_paths() -> None:
    payload = {
        "schema_version": "1",
        "approval_id": "APP-1",
        "root_invocation_id": "root-1",
        "interrupt_task_id": "interrupt-1",
        "source_gate_attempt_id": "attempt-1",
        "source_tree_id": "tree-1",
        "proposal_sha256": "sha256:" + "a" * 64,
        "fixer_authority_sha256": "sha256:" + "b" * 64,
        "entry_baseline_sha256": "sha256:" + "c" * 64,
        "policy_sha256": "sha256:" + "d" * 64,
        "targets": ["api", "e2e"],
        "paths": ["tests/api/test_login.py", "tests/e2e/test_login.py"],
        "action": "approve_and_apply",
    }
    receipt = FixerProposalApprovalReceiptV1.model_validate(payload)
    assert canonical_json_bytes(receipt) == canonical_json_bytes(receipt)

    payload["targets"] = ["e2e", "api"]
    with pytest.raises(ValidationError):
        FixerProposalApprovalReceiptV1.model_validate(payload)


def test_safety_wire_model_rejects_coerced_booleans_and_integers() -> None:
    payload = {
        "schema_version": "1",
        "target": "api",
        "passed": True,
        "needs_review": False,
        "product_code_modified": False,
        "skip_or_xfail_added": False,
        "unrelated_tests_modified": False,
        "assertion_expected_value_changes_detected": False,
        "high_risk_proposal_applied": False,
        "applied_proposal_count": 1,
    }
    ApiCodegenFixerSafetyCheckV1.model_validate(payload)

    payload["passed"] = 1
    with pytest.raises(ValidationError):
        ApiCodegenFixerSafetyCheckV1.model_validate(payload)

    payload["passed"] = True
    payload["applied_proposal_count"] = "1"
    with pytest.raises(ValidationError):
        ApiCodegenFixerSafetyCheckV1.model_validate(payload)
