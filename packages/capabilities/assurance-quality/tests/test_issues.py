from __future__ import annotations


import pytest


from pydantic import ValidationError

from assurance_quality.contracts.issues import (
    IssueReconcileStatusDocument,
    ProblemFingerprint,
)
from quality_fixtures import (  # pyright: ignore[reportMissingImports]
    BATCH_ID,
    CHANGE_ID,
    EVIDENCE_REF,
)


def issue_reconcile_v1_document() -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "change_id": CHANGE_ID,
        "batch_id": BATCH_ID,
        "status": "completed",
        "evidence_bundle_digest": EVIDENCE_REF,
    }


def test_issue_reconcile_accepts_only_v2() -> None:
    with pytest.raises(ValidationError):
        IssueReconcileStatusDocument.model_validate(issue_reconcile_v1_document())


def test_problem_fingerprint_requires_preimage() -> None:
    with pytest.raises(ValidationError):
        ProblemFingerprint.model_validate({"version": "1", "digest": "sha256:" + "a" * 64})


@pytest.mark.parametrize("classification", ["test", "test-data"])
def test_public_issue_analysis_allows_fix_eligible_only_for_test_kinds(classification: str) -> None:
    from assurance_quality.contracts.decisions import IssueAnalysisPublicV1

    accepted = IssueAnalysisPublicV1.model_validate({"classification": classification, "fix_eligible": True})
    assert accepted.fix_eligible is True
    denied = IssueAnalysisPublicV1.model_validate({"classification": classification, "fix_eligible": False})
    assert denied.fix_eligible is False


@pytest.mark.parametrize(
    "classification",
    ["product_bug", "environment_failure", "infrastructure_failure", "unknown", "pending", "failed"],
)
def test_public_issue_analysis_rejects_fix_eligible_for_non_test_kinds(classification: str) -> None:
    from assurance_quality.contracts.decisions import IssueAnalysisPublicV1
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        IssueAnalysisPublicV1.model_validate({"classification": classification, "fix_eligible": True})
    denied = IssueAnalysisPublicV1.model_validate({"classification": classification, "fix_eligible": False})
    assert denied.fix_eligible is False
