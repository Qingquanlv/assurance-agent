import pytest

from assurance_agent.artifacts.models.issues import Problem
from assurance_agent.workflow.issues.transitions import (
    HUMAN_TRANSITIONS,
    InvalidTransitionError,
    ResolutionContext,
    StaleVersionError,
    TransitionDecision,
    plan_regression,
    plan_resolution,
    validate_human_transition,
)


def make_problem(**overrides: object) -> Problem:
    doc: dict = {
        "problem_id": "PROB-abc123",
        "fingerprint": {"version": "1", "digest": "sha256:fingerprint"},
        "title": "Empty department name returns HTTP 500",
        "assessment": {
            "classification": "product_bug",
            "severity": "high",
            "authority": "llm_provisional",
        },
        "status": "detected",
        "first_seen": {"change_id": "RET-1", "occurrence_id": "OCC-1"},
        "last_seen": {"change_id": "RET-1", "occurrence_id": "OCC-1"},
        "occurrences": ["OCC-1"],
        "resolution": None,
        "version": 1,
    }
    doc.update(overrides)
    return Problem.model_validate(doc)


def test_human_transition_table_matches_plan() -> None:
    assert HUMAN_TRANSITIONS == {
        "confirm_assessment": {"detected": "triaged"},
        "mark_not_an_issue": {"detected": "not_an_issue", "triaged": "not_an_issue"},
        "accept_risk": {
            "triaged": "accepted_risk",
            "in_progress": "accepted_risk",
            "verification_pending": "accepted_risk",
        },
        "start_work": {"triaged": "in_progress", "accepted_risk": "in_progress"},
        "reopen": {"not_an_issue": "detected"},
    }


@pytest.mark.parametrize(
    ("action", "status", "event_type", "next_status"),
    [
        ("confirm_assessment", "detected", "problem_assessment_confirmed", "triaged"),
        ("mark_not_an_issue", "detected", "problem_marked_not_an_issue", "not_an_issue"),
        ("mark_not_an_issue", "triaged", "problem_marked_not_an_issue", "not_an_issue"),
        ("accept_risk", "triaged", "problem_risk_accepted", "accepted_risk"),
        ("accept_risk", "in_progress", "problem_risk_accepted", "accepted_risk"),
        ("accept_risk", "verification_pending", "problem_risk_accepted", "accepted_risk"),
        ("start_work", "triaged", "problem_work_started", "in_progress"),
        ("start_work", "accepted_risk", "problem_work_started", "in_progress"),
        ("reopen", "not_an_issue", "problem_reopened", "detected"),
    ],
)
def test_validate_human_transition_legal_edges(
    action: str,
    status: str,
    event_type: str,
    next_status: str,
) -> None:
    problem = make_problem(status=status)
    payload = {
        "classification": "product_bug",
        "severity": "high",
    }
    if action == "confirm_assessment":
        payload = {
            "classification": "product_bug",
            "severity": "medium",
        }

    decision = validate_human_transition(
        problem,
        action,
        expected_problem_version=1,
        reason="triaged from execution evidence",
        evidence_refs=["OCC-1"],
        payload=payload,
    )

    assert isinstance(decision, TransitionDecision)
    assert decision.event_type == event_type
    assert decision.problem_id == problem.problem_id
    assert decision.expected_version == 1
    assert decision.payload["next_status"] == next_status
    assert decision.payload["reason"] == "triaged from execution evidence"
    assert decision.payload["evidence_refs"] == ["OCC-1"]


@pytest.mark.parametrize(
    ("action", "status"),
    [
        ("confirm_assessment", "triaged"),
        ("start_work", "detected"),
        ("accept_risk", "detected"),
        ("reopen", "triaged"),
        ("mark_not_an_issue", "in_progress"),
    ],
)
def test_validate_human_transition_rejects_illegal_edges(action: str, status: str) -> None:
    problem = make_problem(status=status)

    with pytest.raises(InvalidTransitionError):
        validate_human_transition(
            problem,
            action,
            expected_problem_version=1,
            reason="should fail",
            evidence_refs=["OCC-1"],
            payload={"classification": "product_bug", "severity": "high"},
        )


def test_validate_human_transition_rejects_detected_to_in_progress_shortcut() -> None:
    problem = make_problem(status="detected")

    with pytest.raises(InvalidTransitionError):
        validate_human_transition(
            problem,
            "start_work",
            expected_problem_version=1,
            reason="skip triage",
            evidence_refs=["OCC-1"],
            payload={"classification": "product_bug", "severity": "high"},
        )


def test_validate_human_transition_rejects_stale_expected_version() -> None:
    problem = make_problem(status="detected", version=2)

    with pytest.raises(StaleVersionError):
        validate_human_transition(
            problem,
            "confirm_assessment",
            expected_problem_version=1,
            reason="stale",
            evidence_refs=["OCC-1"],
            payload={"classification": "product_bug", "severity": "high"},
        )


def test_validate_human_transition_rejects_forbidden_payload_setters() -> None:
    problem = make_problem(status="detected")

    for forbidden_key in ("status", "version", "authority"):
        with pytest.raises(InvalidTransitionError, match=forbidden_key):
            validate_human_transition(
                problem,
                "confirm_assessment",
                expected_problem_version=1,
                reason="bad payload",
                evidence_refs=["OCC-1"],
                payload={
                    "classification": "product_bug",
                    "severity": "high",
                    forbidden_key: "forged",
                },
            )


def test_validate_human_transition_requires_reason_and_evidence() -> None:
    problem = make_problem(status="detected")

    with pytest.raises(InvalidTransitionError, match="reason"):
        validate_human_transition(
            problem,
            "confirm_assessment",
            expected_problem_version=1,
            reason="   ",
            evidence_refs=["OCC-1"],
            payload={"classification": "product_bug", "severity": "high"},
        )

    with pytest.raises(InvalidTransitionError, match="evidence"):
        validate_human_transition(
            problem,
            "confirm_assessment",
            expected_problem_version=1,
            reason="missing evidence",
            evidence_refs=[],
            payload={"classification": "product_bug", "severity": "high"},
        )


def test_plan_resolution_requires_verification_pending_and_all_checks() -> None:
    problem = make_problem(
        status="verification_pending",
        version=3,
        resolution=None,
    )
    context = ResolutionContext(
        linked_fix_exists=True,
        authoritative_batch_selected_target=True,
        verification_cases_executed=True,
        verification_cases_passed=True,
        fingerprint_absent_in_batch=True,
    )

    decision = plan_resolution(
        problem,
        expected_problem_version=3,
        linked_fix_disposition="healing-apply-001",
        verification_scope=["API-DEPT-NEG-001"],
        verification_evidence_digest="sha256:verify",
        change_id="RET-2",
        batch_id="20260726-001",
        context=context,
    )

    assert decision.event_type == "problem_resolved"
    assert decision.expected_version == 3
    assert decision.payload["next_status"] == "resolved"
    assert decision.payload["verification_scope"] == ["API-DEPT-NEG-001"]


@pytest.mark.parametrize(
    "context_kwargs",
    [
        {"linked_fix_exists": False},
        {"authoritative_batch_selected_target": False},
        {"verification_cases_executed": False},
        {"verification_cases_passed": False},
        {"fingerprint_absent_in_batch": False},
    ],
)
def test_plan_resolution_rejects_incomplete_verification(context_kwargs: dict[str, bool]) -> None:
    problem = make_problem(status="verification_pending", version=2)
    base = {
        "linked_fix_exists": True,
        "authoritative_batch_selected_target": True,
        "verification_cases_executed": True,
        "verification_cases_passed": True,
        "fingerprint_absent_in_batch": True,
    }
    base.update(context_kwargs)
    context = ResolutionContext(**base)

    with pytest.raises(InvalidTransitionError):
        plan_resolution(
            problem,
            expected_problem_version=2,
            linked_fix_disposition="healing-apply-001",
            verification_scope=["API-DEPT-NEG-001"],
            verification_evidence_digest="sha256:verify",
            change_id="RET-2",
            batch_id="20260726-001",
            context=context,
        )


def test_plan_resolution_rejects_non_pending_status() -> None:
    problem = make_problem(status="in_progress", version=1)
    context = ResolutionContext(
        linked_fix_exists=True,
        authoritative_batch_selected_target=True,
        verification_cases_executed=True,
        verification_cases_passed=True,
        fingerprint_absent_in_batch=True,
    )

    with pytest.raises(InvalidTransitionError):
        plan_resolution(
            problem,
            expected_problem_version=1,
            linked_fix_disposition="healing-apply-001",
            verification_scope=["API-DEPT-NEG-001"],
            verification_evidence_digest="sha256:verify",
            change_id="RET-2",
            batch_id="20260726-001",
            context=context,
        )


def test_plan_regression_from_resolved_to_detected() -> None:
    problem = make_problem(
        status="resolved",
        version=4,
        resolution={
            "resolved_at": "2026-07-25T10:00:00Z",
            "change_id": "RET-1",
            "batch_id": "20260725-124844",
            "disposition": "healing-apply-001",
            "verification_scope": ["API-DEPT-NEG-001"],
            "evidence_digest": "sha256:verify",
        },
    )

    decision = plan_regression(
        problem,
        expected_problem_version=4,
        occurrence_id="OCC-regression",
        change_id="RET-2",
    )

    assert decision.event_type == "problem_regressed"
    assert decision.expected_version == 4
    assert decision.payload["next_status"] == "detected"
    assert decision.payload["occurrence_id"] == "OCC-regression"


def test_plan_regression_rejects_non_resolved_status() -> None:
    problem = make_problem(status="triaged", version=1)

    with pytest.raises(InvalidTransitionError):
        plan_regression(
            problem,
            expected_problem_version=1,
            occurrence_id="OCC-regression",
            change_id="RET-2",
        )
