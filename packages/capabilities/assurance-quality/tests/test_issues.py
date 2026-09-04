from __future__ import annotations

import json

import pytest
from graph_engine.canonical import canonical_digest
from graph_engine.plugin_api import CandidateWriteSet

from tests.phase4.conformance import execute_task

from pydantic import ValidationError

from assurance_quality.contracts.issues import IssueReconcileStatusDocument, ProblemFingerprint
from assurance_quality.operations.issues import (
    ApplyProblemReviewHandler,
    CollectObservationsHandler,
    LoadProblemReviewContextHandler,
    ReconcileIssuesHandler,
    RecordEmptyIssueAnalysisHandler,
    RecordIssueAnalysisFailureHandler,
    RecordProjectSyncPendingHandler,
)
from quality_fixtures import (  # pyright: ignore[reportMissingImports]
    BATCH_ID,
    CHANGE_ID,
    EVIDENCE_REF,
    HEX_A,
    as_object,
    issue_candidate_document,
    issue_input,
    occurrence_detected_event,
    validation_context,
    write_set,
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


@pytest.mark.asyncio
async def test_issue_candidate_digest_changes_with_canonical_evidence() -> None:
    first = await execute_task(CollectObservationsHandler(), issue_input(message="a"))
    second = await execute_task(CollectObservationsHandler(), issue_input(message="b"))
    assert first.status == "succeeded"
    assert second.status == "succeeded"
    assert canonical_digest(first.output) != canonical_digest(second.output)


@pytest.mark.asyncio
async def test_empty_and_failure_status_are_digest_bound() -> None:
    empty = await execute_task(
        RecordEmptyIssueAnalysisHandler(),
        {"change_id": CHANGE_ID, "batch_id": BATCH_ID, "evidence_bundle_digest": EVIDENCE_REF},
    )
    failed = await execute_task(
        RecordIssueAnalysisFailureHandler(),
        {
            "change_id": CHANGE_ID,
            "batch_id": BATCH_ID,
            "evidence_bundle_digest": EVIDENCE_REF,
            "error_kind": "timeout",
        },
    )
    pending = await execute_task(
        RecordProjectSyncPendingHandler(),
        {
            "change_id": CHANGE_ID,
            "batch_id": BATCH_ID,
            "evidence_bundle_digest": EVIDENCE_REF,
            "candidate_digest": f"sha256:{HEX_A}",
        },
    )
    assert empty.status == "succeeded"
    assert failed.status == "succeeded"
    assert pending.status == "succeeded"
    assert as_object(empty.output)["status"] == "completed"
    assert as_object(failed.output)["status"] == "failed"
    assert as_object(failed.output)["reason"] == "timeout"
    assert as_object(pending.output)["project_sync_status"] == "pending"


@pytest.mark.asyncio
async def test_reconcile_and_review_use_canonical_fingerprint() -> None:
    observations = await execute_task(CollectObservationsHandler(), issue_input(message="boom"))
    assert observations.status == "succeeded"
    reconcile = await execute_task(
        ReconcileIssuesHandler(),
        {
            "change_id": CHANGE_ID,
            "batch_id": BATCH_ID,
            "observations": as_object(observations.output)["observations"],
            "candidates": [
                {
                    "candidate_id": "CAND-1",
                    "observation_ids": [as_object(observations.output)["observations"][0]["observation_id"]],
                    "proposed": {
                        "title": "pretty title",
                        "classification": "product_bug",
                        "severity": "high",
                        "root_cause_hypothesis": "formatted prose",
                    },
                    "affected_surface": {"kind": "module", "value": "Menus Service"},
                    "fingerprint_inputs": {"surface": "menus", "symptom": "boom"},
                    "possible_problem_ids": [],
                    "confidence": 0.8,
                    "recommended_action": "triage",
                }
            ],
            "evidence_bundle_digest": EVIDENCE_REF,
        },
    )
    assert reconcile.status == "succeeded"
    snapshot = as_object(reconcile.output)
    first_problem = snapshot["problems"][0]
    rewritten = await execute_task(
        ReconcileIssuesHandler(),
        {
            "change_id": CHANGE_ID,
            "batch_id": BATCH_ID,
            "observations": as_object(observations.output)["observations"],
            "candidates": [
                {
                    "candidate_id": "CAND-1",
                    "observation_ids": [as_object(observations.output)["observations"][0]["observation_id"]],
                    "proposed": {
                        "title": "DIFFERENT TITLE",
                        "classification": "product_bug",
                        "severity": "high",
                        "root_cause_hypothesis": "other prose",
                    },
                    "affected_surface": {"kind": "module", "value": "Menus Service"},
                    "fingerprint_inputs": {"surface": "menus", "symptom": "boom"},
                    "possible_problem_ids": [],
                    "confidence": 0.1,
                    "recommended_action": "ignore formatting",
                }
            ],
            "evidence_bundle_digest": EVIDENCE_REF,
        },
    )
    assert rewritten.status == "succeeded"
    assert as_object(rewritten.output)["problems"][0]["problem_id"] == first_problem["problem_id"]
    assert (
        as_object(rewritten.output)["problems"][0]["fingerprint"]["digest"]
        == first_problem["fingerprint"]["digest"]
    )

    context = await execute_task(
        LoadProblemReviewContextHandler(),
        {
            "change_id": CHANGE_ID,
            "problem": first_problem,
            "occurrence_id": snapshot["occurrences"][0]["occurrence_id"],
        },
    )
    assert context.status == "succeeded"
    applied = await execute_task(
        ApplyProblemReviewHandler(),
        {
            "change_id": CHANGE_ID,
            "problem": first_problem,
            "review_id": as_object(context.output)["review_id"],
            "action": "confirm_assessment",
            "evidence_digest": EVIDENCE_REF,
        },
    )
    assert applied.status == "succeeded"
    assert as_object(applied.output)["problem_id"] == first_problem["problem_id"]
    assert as_object(applied.output)["action"] == "confirm_assessment"


def test_issue_validator_rejects_unknown_evidence() -> None:
    from assurance_quality.validators.issues import IssueValidator

    validator = IssueValidator(evidence_refs=frozenset({EVIDENCE_REF}))
    result = validator.validate(
        write_set("issues/snapshot.json"),
        validation_context(),
    )
    assert result.accepted is False


def test_issue_validator_rejects_forged_fingerprint_digest() -> None:
    from assurance_quality.validators.issues import IssueValidator

    payload = issue_candidate_document(possible_problem_ids=["sha256:" + "f" * 64])
    validator = IssueValidator(
        evidence_refs=frozenset({EVIDENCE_REF}),
        file_bytes={"issues/snapshot.json": json.dumps(payload).encode("utf-8")},
    )
    result = validator.validate(write_set("issues/snapshot.json"), validation_context())
    assert result.accepted is False
    assert result.reason == "canonical fingerprint does not match evidence"


def test_issue_validator_rejects_occurrence_without_predecessor() -> None:
    from assurance_quality.validators.issues import IssueValidator

    payload = occurrence_detected_event()
    validator = IssueValidator(
        evidence_refs=frozenset({EVIDENCE_REF}),
        file_bytes={"issues/event.json": json.dumps(payload).encode("utf-8")},
    )
    result = validator.validate(write_set("issues/event.json"), validation_context())
    assert result.accepted is False
    assert result.reason == "issue event transition is not allowed"


def test_problem_apply_validator_rejects_forged_review_id() -> None:
    from assurance_quality.validators.issues import ProblemApplyValidator

    validator = ProblemApplyValidator(
        receipt={
            "problem_id": "PROB-1",
            "review_id": "REV-FORGED",
            "action": "confirm_assessment",
            "expected_problem_version": 1,
        },
        context={
            "problem_id": "PROB-1",
            "review_id": "REV-FORGED",
            "expected_problem_version": 1,
        },
    )
    result = validator.validate(
        write_set("issue-review/rev-1/apply-receipt.json"),
        validation_context(),
    )
    assert result.accepted is False
    assert result.reason


def test_problem_apply_validator_rejects_missing_context_review_id() -> None:
    from assurance_quality.operations.identity import review_id
    from assurance_quality.validators.issues import ProblemApplyValidator

    validator = ProblemApplyValidator(
        receipt={
            "problem_id": "PROB-1",
            "review_id": review_id("PROB-1", 1),
            "action": "confirm_assessment",
            "expected_problem_version": 1,
        },
        context={"problem_id": "PROB-1", "expected_problem_version": 1},
    )
    result = validator.validate(
        write_set("issue-review/rev-1/apply-receipt.json"),
        validation_context(),
    )
    assert result.accepted is False
    assert result.reason


def test_problem_apply_validator_default_fails_closed() -> None:
    from assurance_quality.validators.issues import ProblemApplyValidator

    validator = ProblemApplyValidator()
    result = validator.validate(
        write_set("issue-review/rev-1/apply-receipt.json"),
        validation_context(),
    )
    assert result.accepted is False
    assert result.reason


def test_plugin_problem_apply_validator_is_path_only() -> None:
    from graph_engine import ENGINE_API_VERSION, RegistryPorts

    from assurance_quality.plugin import QualityPlugin

    contribution = QualityPlugin.contribute(RegistryPorts(engine_api=ENGINE_API_VERSION))
    validator = contribution.commit_validators["assurance.quality.validator.problem-apply.v1"]
    accepted = validator.validate(
        write_set("issue-review/rev-1/apply-receipt.json"),
        validation_context(),
    )
    rejected = validator.validate(write_set("src/app.py"), validation_context())
    assert accepted.accepted is True
    assert rejected.accepted is False
    empty = CandidateWriteSet(baseline_tree_id=HEX_A, candidate_tree_id="1" * 64, files=())
    assert validator.validate(empty, validation_context()).accepted is True


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
