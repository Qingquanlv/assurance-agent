import pytest
from pydantic import StrictBool, StrictInt, ValidationError

from assurance_agent.artifacts.canonical import canonical_json_bytes
from assurance_agent.artifacts.models import (
    ApiCodegenFixApplyIntentV1,
    ApiCodegenFixApplySummaryV1,
    ApiCodegenFixerSafetyCheckV1,
    E2eCodegenFixApplyIntentV1,
    FixerAuthorityPathV1,
    FixerAuthorityTargetV1,
    FixerAuthorityV1,
    FixerProposalApprovalReceiptV1,
    FixerSafetyCheckV1,
    StrictWireModel,
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

    payload["targets"] = ["api", "e2e"]
    payload["paths"] = []
    with pytest.raises(ValidationError):
        FixerProposalApprovalReceiptV1.model_validate(payload)


def test_fixer_authority_and_safety_models_round_trip() -> None:
    authority = FixerAuthorityV1(
        schema_version="1",
        change_id="CH-1",
        targets=[
            FixerAuthorityTargetV1(
                target="api",
                status="ready",
                codegen_attempt_id="cg-1",
                generated_files_sha256="sha256:" + "a" * 64,
                summary_sha256="sha256:" + "b" * 64,
                write_set_id="ws-1",
                execution_batch_id="batch-1",
                paths=[
                    FixerAuthorityPathV1(
                        repo_path="tests/api/test_login.py",
                        disposition="generated",
                        content_sha256="sha256:" + "c" * 64,
                    )
                ],
            )
        ],
    )
    assert canonical_json_bytes(authority) == canonical_json_bytes(authority)
    summary = ApiCodegenFixApplySummaryV1(
        schema_version="1",
        target="api",
        outcome="applied",
        proposal_ids=["FIX_001"],
        claimed_modified_paths=["tests/api/test_login.py"],
        intent_sha256="sha256:" + "d" * 64,
        write_set_id="ws-2",
        applied=True,
    )
    fragment = ApiCodegenFixerSafetyCheckV1(
        schema_version="1",
        target="api",
        passed=True,
        needs_review=False,
        product_code_modified=False,
        skip_or_xfail_added=False,
        unrelated_tests_modified=False,
        assertion_expected_value_changes_detected=False,
        high_risk_proposal_applied=False,
        applied_proposal_count=1,
    )
    aggregate = FixerSafetyCheckV1(
        schema_version="1",
        passed=True,
        needs_review=False,
        active_targets=["api"],
        target_safety_sha256=["sha256:" + "e" * 64],
        product_code_modified=False,
        skip_or_xfail_added=False,
        unrelated_tests_modified=False,
        assertion_expected_value_changes_detected=False,
        high_risk_proposal_applied=False,
    )
    assert summary.target == "api"
    assert fragment.passed is True
    assert aggregate.active_targets == ["api"]
    from assurance_agent.artifacts.models.healing import ApplySummary, SafetyCheck

    ApplySummary.model_validate(summary.model_dump(mode="json"))
    SafetyCheck.model_validate(aggregate.model_dump(mode="json"))


def test_strict_wire_model_rejects_coerced_booleans_and_integers() -> None:
    class Probe(StrictWireModel):
        flag: StrictBool
        count: StrictInt

    Probe.model_validate({"flag": True, "count": 1})

    with pytest.raises(ValidationError):
        Probe.model_validate({"flag": 1, "count": 1})

    with pytest.raises(ValidationError):
        Probe.model_validate({"flag": True, "count": "1"})
