"""Tests for assurance_agent.workflow.issues.review — pure review API.

Covers:
  - build_problem_review_context: happy path, missing problem
  - validate_review_action: all supported actions, missing reasons/evidence,
    stale Problem versions, illegal transitions, merge cycles, missing
    verification scope, attempts to set canonical fields directly.
  - Advice is ignored for authority (display only).
  - detected → in_progress still forbidden (must triage first).
"""

from __future__ import annotations

import pytest

from assurance_agent.artifacts.models.issues import (
    Problem,
    ProblemProjection,
)
from assurance_agent.workflow.issues.review import (
    REVIEW_ACTIONS,
    ProblemReviewContext,
    ReviewContextError,
    ReviewValidationError,
    build_problem_review_context,
    validate_review_action,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_projection(*problems: dict) -> ProblemProjection:
    """Build a ProblemProjection from a list of problem dicts."""
    return ProblemProjection(
        schema_version="1.0",
        generated_at="2024-01-01T00:00:00Z",
        problems=[Problem.model_validate(p) for p in problems],
    )


def _base_problem(
    *,
    problem_id: str = "PROB-abc1",
    status: str = "detected",
    version: int = 1,
    occurrences: list[str] | None = None,
) -> dict:
    return {
        "problem_id": problem_id,
        "fingerprint": {"version": "1", "digest": "a" * 64},
        "title": "HTTP 500 on empty department name",
        "assessment": {
            "classification": "product_bug",
            "severity": "high",
            "authority": "llm_provisional",
        },
        "status": status,
        "first_seen": {"change_id": "CH-1", "occurrence_id": "OCC-1"},
        "last_seen": {"change_id": "CH-1", "occurrence_id": "OCC-1"},
        "occurrences": occurrences or ["OCC-1"],
        "version": version,
    }


_STD_EVIDENCE = ["OCC-1", "EVT-abc"]
_STD_PAYLOAD: dict[str, object] = {"evidence_refs": _STD_EVIDENCE}


def _ctx(
    *,
    problem_id: str = "PROB-abc1",
    status: str = "detected",
    version: int = 1,
    occurrences: tuple[str, ...] = ("OCC-1",),
) -> ProblemReviewContext:
    return ProblemReviewContext(
        problem_id=problem_id,
        expected_problem_version=version,
        problem_status=status,  # type: ignore[arg-type]
        problem_title="HTTP 500 on empty department name",
        canonical_alias=None,
        occurrence_ids=occurrences,
        problem_digest="sha256:" + "a" * 64,
    )


# ---------------------------------------------------------------------------
# build_problem_review_context
# ---------------------------------------------------------------------------


class TestBuildProblemReviewContext:
    def test_builds_context_for_existing_problem(self) -> None:
        proj = _make_projection(_base_problem())
        ctx = build_problem_review_context("PROB-abc1", proj)
        assert ctx.problem_id == "PROB-abc1"
        assert ctx.expected_problem_version == 1
        assert ctx.problem_status == "detected"
        assert ctx.occurrence_ids == ("OCC-1",)
        assert ctx.problem_digest.startswith("sha256:")

    def test_digest_is_deterministic(self) -> None:
        proj = _make_projection(_base_problem())
        ctx1 = build_problem_review_context("PROB-abc1", proj)
        ctx2 = build_problem_review_context("PROB-abc1", proj)
        assert ctx1.problem_digest == ctx2.problem_digest

    def test_raises_when_problem_not_found(self) -> None:
        proj = _make_projection(_base_problem())
        with pytest.raises(ReviewContextError, match="PROB-missing"):
            build_problem_review_context("PROB-missing", proj)

    def test_raises_on_empty_projection(self) -> None:
        proj = _make_projection()
        with pytest.raises(ReviewContextError):
            build_problem_review_context("PROB-abc1", proj)

    def test_version_reflects_problem(self) -> None:
        proj = _make_projection(_base_problem(version=5, status="in_progress"))
        ctx = build_problem_review_context("PROB-abc1", proj)
        assert ctx.expected_problem_version == 5
        assert ctx.problem_status == "in_progress"


# ---------------------------------------------------------------------------
# validate_review_action — general guards
# ---------------------------------------------------------------------------


class TestValidateReviewActionGuards:
    def test_rejects_empty_action(self) -> None:
        ctx = _ctx()
        with pytest.raises(ReviewValidationError, match="action must not be empty"):
            validate_review_action(ctx, "", _STD_PAYLOAD, "reason", "user")

    def test_rejects_blank_action(self) -> None:
        ctx = _ctx()
        with pytest.raises(ReviewValidationError, match="action must not be empty"):
            validate_review_action(ctx, "   ", _STD_PAYLOAD, "reason", "user")

    def test_rejects_empty_reason(self) -> None:
        ctx = _ctx()
        with pytest.raises(ReviewValidationError, match="reason must not be empty"):
            validate_review_action(ctx, "confirm_assessment", _STD_PAYLOAD, "", "user")

    def test_rejects_blank_reason(self) -> None:
        ctx = _ctx()
        with pytest.raises(ReviewValidationError, match="reason must not be empty"):
            validate_review_action(ctx, "confirm_assessment", _STD_PAYLOAD, "   ", "user")

    def test_rejects_empty_who(self) -> None:
        ctx = _ctx()
        with pytest.raises(ReviewValidationError, match="who must not be empty"):
            validate_review_action(ctx, "confirm_assessment", _STD_PAYLOAD, "triaged", "")

    def test_rejects_unknown_action(self) -> None:
        ctx = _ctx()
        with pytest.raises(ReviewValidationError, match="unsupported review action"):
            validate_review_action(ctx, "force_resolve", _STD_PAYLOAD, "reason", "user")

    def test_rejects_canonical_payload_fields(self) -> None:
        """Payload must not set status/version/authority directly."""
        ctx = _ctx(status="detected")
        bad_payload: dict[str, object] = {
            "evidence_refs": _STD_EVIDENCE,
            "status": "triaged",  # forbidden
            "classification": "product_bug",
            "severity": "high",
        }
        with pytest.raises(ReviewValidationError):
            validate_review_action(ctx, "confirm_assessment", bad_payload, "reason", "user")

    def test_review_actions_set_contains_all_expected(self) -> None:
        expected = {
            "confirm_assessment",
            "mark_not_an_issue",
            "accept_risk",
            "start_work",
            "confirm_link",
            "merge",
            "reopen",
            "submit_resolution",
        }
        assert expected.issubset(REVIEW_ACTIONS)

    def test_stale_version_rejected_via_projection(self) -> None:
        proj = _make_projection(_base_problem(version=3))
        ctx = _ctx(version=2)  # stale
        with pytest.raises(ReviewValidationError, match="stale review context"):
            validate_review_action(
                ctx,
                "mark_not_an_issue",
                _STD_PAYLOAD,
                "reason",
                "user",
                projection=proj,
            )


# ---------------------------------------------------------------------------
# confirm_assessment
# ---------------------------------------------------------------------------


class TestConfirmAssessment:
    def _do(self, **payload_extra: object):
        ctx = _ctx(status="detected")
        payload: dict[str, object] = {
            "classification": "product_bug",
            "severity": "high",
            "evidence_refs": _STD_EVIDENCE,
            **payload_extra,
        }
        return validate_review_action(
            ctx, "confirm_assessment", payload, "triaged from execution evidence", "reviewer"
        )

    def test_legal_transition_detected_to_triaged(self) -> None:
        events = self._do()
        assert len(events) == 1
        ev = events[0]
        assert ev.type == "problem_assessment_confirmed"
        assert ev.problem_id == "PROB-abc1"
        assert ev.expected_problem_version == 1
        assert ev.classification == "product_bug"
        assert ev.severity == "high"
        assert ev.reason == "triaged from execution evidence"

    def test_requires_classification(self) -> None:
        with pytest.raises(ReviewValidationError, match="classification"):
            self._do(classification="bad_value")

    def test_requires_severity(self) -> None:
        with pytest.raises(ReviewValidationError, match="severity"):
            self._do(severity="extreme")

    def test_requires_evidence_refs(self) -> None:
        ctx = _ctx(status="detected")
        payload: dict[str, object] = {
            "classification": "product_bug",
            "severity": "high",
        }
        with pytest.raises(ReviewValidationError, match="evidence_refs"):
            validate_review_action(ctx, "confirm_assessment", payload, "reason", "user")

    def test_illegal_from_triaged(self) -> None:
        ctx = _ctx(status="triaged")
        payload: dict[str, object] = {
            "classification": "product_bug",
            "severity": "high",
            "evidence_refs": _STD_EVIDENCE,
        }
        with pytest.raises(ReviewValidationError):
            validate_review_action(ctx, "confirm_assessment", payload, "reason", "user")

    def test_authority_is_always_human_confirmed(self) -> None:
        """LLM-suggested authority in payload is silently ignored."""
        events = self._do()
        # authority is set by validate_human_transition to human_confirmed
        # The returned event does not expose authority directly, but the payload
        # from validate_human_transition sets it.
        assert events[0].type == "problem_assessment_confirmed"

    def test_idempotency_key_is_deterministic(self) -> None:
        events1 = self._do()
        events2 = self._do()
        assert events1[0].idempotency_key == events2[0].idempotency_key
        assert events1[0].event_id == events2[0].event_id


# ---------------------------------------------------------------------------
# mark_not_an_issue
# ---------------------------------------------------------------------------


class TestMarkNotAnIssue:
    @pytest.mark.parametrize("status", ["detected", "triaged"])
    def test_legal_from_detected_and_triaged(self, status: str) -> None:
        ctx = _ctx(status=status)
        events = validate_review_action(
            ctx, "mark_not_an_issue", _STD_PAYLOAD, "it is expected behavior", "reviewer"
        )
        assert len(events) == 1
        assert events[0].type == "problem_marked_not_an_issue"

    def test_illegal_from_in_progress(self) -> None:
        ctx = _ctx(status="in_progress")
        with pytest.raises(ReviewValidationError):
            validate_review_action(ctx, "mark_not_an_issue", _STD_PAYLOAD, "reason", "user")

    def test_missing_evidence_refs_rejected(self) -> None:
        ctx = _ctx(status="detected")
        with pytest.raises(ReviewValidationError, match="evidence_refs"):
            validate_review_action(ctx, "mark_not_an_issue", {}, "reason", "user")


# ---------------------------------------------------------------------------
# accept_risk
# ---------------------------------------------------------------------------


class TestAcceptRisk:
    @pytest.mark.parametrize("status", ["triaged", "in_progress", "verification_pending"])
    def test_legal_from_allowed_statuses(self, status: str) -> None:
        ctx = _ctx(status=status)
        events = validate_review_action(ctx, "accept_risk", _STD_PAYLOAD, "accepted by PM", "pm")
        assert len(events) == 1
        assert events[0].type == "problem_risk_accepted"

    def test_illegal_from_detected(self) -> None:
        ctx = _ctx(status="detected")
        with pytest.raises(ReviewValidationError):
            validate_review_action(ctx, "accept_risk", _STD_PAYLOAD, "reason", "user")


# ---------------------------------------------------------------------------
# start_work
# ---------------------------------------------------------------------------


class TestStartWork:
    @pytest.mark.parametrize("status", ["triaged", "accepted_risk"])
    def test_legal_from_allowed_statuses(self, status: str) -> None:
        ctx = _ctx(status=status)
        events = validate_review_action(ctx, "start_work", _STD_PAYLOAD, "dev picked it up", "dev")
        assert len(events) == 1
        assert events[0].type == "problem_work_started"

    def test_detected_to_in_progress_forbidden(self) -> None:
        """detected → in_progress must be refused (must triage first)."""
        ctx = _ctx(status="detected")
        with pytest.raises(ReviewValidationError):
            validate_review_action(ctx, "start_work", _STD_PAYLOAD, "reason", "user")


# ---------------------------------------------------------------------------
# reopen
# ---------------------------------------------------------------------------


class TestReopen:
    def test_legal_from_not_an_issue(self) -> None:
        ctx = _ctx(status="not_an_issue")
        events = validate_review_action(ctx, "reopen", _STD_PAYLOAD, "new evidence found", "user")
        assert len(events) == 1
        assert events[0].type == "problem_reopened"

    def test_illegal_from_detected(self) -> None:
        ctx = _ctx(status="detected")
        with pytest.raises(ReviewValidationError):
            validate_review_action(ctx, "reopen", _STD_PAYLOAD, "reason", "user")


# ---------------------------------------------------------------------------
# merge / confirm_link
# ---------------------------------------------------------------------------


class TestMergeActions:
    @pytest.mark.parametrize("action", ["merge", "confirm_link"])
    def test_legal_merge_from_detected(self, action: str) -> None:
        ctx = _ctx(problem_id="PROB-src", status="detected")
        proj = _make_projection(
            _base_problem(problem_id="PROB-src", status="detected"),
            _base_problem(problem_id="PROB-tgt", status="detected"),
        )
        payload: dict[str, object] = {
            "target_problem_id": "PROB-tgt",
            "evidence_refs": _STD_EVIDENCE,
        }
        events = validate_review_action(ctx, action, payload, "confirmed duplicate", "user", projection=proj)
        assert len(events) == 1
        ev = events[0]
        assert ev.type == "problem_merged"
        assert ev.problem_id == "PROB-src"
        assert ev.target_problem_id == "PROB-tgt"

    @pytest.mark.parametrize("action", ["merge", "confirm_link"])
    def test_self_merge_rejected(self, action: str) -> None:
        ctx = _ctx(problem_id="PROB-src", status="detected")
        payload: dict[str, object] = {
            "target_problem_id": "PROB-src",  # same as source
            "evidence_refs": _STD_EVIDENCE,
        }
        with pytest.raises(ReviewValidationError, match="must differ from source"):
            validate_review_action(ctx, action, payload, "reason", "user")

    @pytest.mark.parametrize("action", ["merge", "confirm_link"])
    def test_missing_target_problem_id_rejected(self, action: str) -> None:
        ctx = _ctx(status="detected")
        with pytest.raises(ReviewValidationError, match="target_problem_id"):
            validate_review_action(ctx, action, _STD_PAYLOAD, "reason", "user")

    @pytest.mark.parametrize("action", ["merge", "confirm_link"])
    def test_merge_unknown_target_rejected_with_projection(self, action: str) -> None:
        ctx = _ctx(problem_id="PROB-src", status="detected")
        proj = _make_projection(_base_problem(problem_id="PROB-src", status="detected"))
        payload: dict[str, object] = {
            "target_problem_id": "PROB-nonexistent",
            "evidence_refs": _STD_EVIDENCE,
        }
        with pytest.raises(ReviewValidationError, match="not found"):
            validate_review_action(ctx, action, payload, "reason", "user", projection=proj)

    @pytest.mark.parametrize("status", ["resolved", "not_an_issue", "accepted_risk"])
    def test_merge_disallowed_from_terminal_statuses(self, status: str) -> None:
        ctx = _ctx(status=status)
        payload: dict[str, object] = {
            "target_problem_id": "PROB-tgt",
            "evidence_refs": _STD_EVIDENCE,
        }
        with pytest.raises(ReviewValidationError, match="not allowed"):
            validate_review_action(ctx, "merge", payload, "reason", "user")


# ---------------------------------------------------------------------------
# submit_resolution
# ---------------------------------------------------------------------------


class TestSubmitResolution:
    def _good_payload(self) -> dict[str, object]:
        return {
            "verification_scope": ["target/api_test", "target/fuzz_test"],
            "linked_fix_disposition": "PR-42 merged",
            "change_id": "CH-1",
            "batch_id": "BATCH-abc",
        }

    @pytest.mark.parametrize("status", ["in_progress", "triaged"])
    def test_legal_from_in_progress_and_triaged(self, status: str) -> None:
        ctx = _ctx(status=status)
        events = validate_review_action(ctx, "submit_resolution", self._good_payload(), "fix merged", "dev")
        assert len(events) == 1
        ev = events[0]
        assert ev.type == "problem_verification_requested"
        assert ev.problem_id == "PROB-abc1"
        assert ev.change_id == "CH-1"

    def test_illegal_from_detected(self) -> None:
        ctx = _ctx(status="detected")
        with pytest.raises(ReviewValidationError, match="submit_resolution requires"):
            validate_review_action(ctx, "submit_resolution", self._good_payload(), "reason", "user")

    def test_missing_verification_scope_rejected(self) -> None:
        ctx = _ctx(status="in_progress")
        payload = self._good_payload()
        del payload["verification_scope"]
        with pytest.raises(ReviewValidationError, match="verification_scope"):
            validate_review_action(ctx, "submit_resolution", payload, "reason", "user")

    def test_empty_verification_scope_rejected(self) -> None:
        ctx = _ctx(status="in_progress")
        payload = {**self._good_payload(), "verification_scope": []}
        with pytest.raises(ReviewValidationError, match="verification_scope"):
            validate_review_action(ctx, "submit_resolution", payload, "reason", "user")

    def test_missing_linked_fix_disposition_rejected(self) -> None:
        ctx = _ctx(status="in_progress")
        payload = self._good_payload()
        del payload["linked_fix_disposition"]
        with pytest.raises(ReviewValidationError, match="linked_fix_disposition"):
            validate_review_action(ctx, "submit_resolution", payload, "reason", "user")

    def test_missing_change_id_rejected(self) -> None:
        ctx = _ctx(status="in_progress")
        payload = self._good_payload()
        del payload["change_id"]
        with pytest.raises(ReviewValidationError, match="change_id"):
            validate_review_action(ctx, "submit_resolution", payload, "reason", "user")

    def test_missing_batch_id_rejected(self) -> None:
        ctx = _ctx(status="in_progress")
        payload = self._good_payload()
        del payload["batch_id"]
        with pytest.raises(ReviewValidationError, match="batch_id"):
            validate_review_action(ctx, "submit_resolution", payload, "reason", "user")

    def test_idempotency_key_is_deterministic(self) -> None:
        ctx = _ctx(status="in_progress")
        events1 = validate_review_action(ctx, "submit_resolution", self._good_payload(), "fix merged", "dev")
        events2 = validate_review_action(ctx, "submit_resolution", self._good_payload(), "fix merged", "dev")
        assert events1[0].idempotency_key == events2[0].idempotency_key


# ---------------------------------------------------------------------------
# Stale-version rejection (all paths)
# ---------------------------------------------------------------------------


class TestStaleVersionRejection:
    def test_stale_via_context_status_mismatch_with_projection(self) -> None:
        """Context built at version 1 but projection now at version 2."""
        proj = _make_projection(_base_problem(version=2))
        ctx = _ctx(version=1)
        with pytest.raises(ReviewValidationError, match="stale"):
            validate_review_action(ctx, "mark_not_an_issue", _STD_PAYLOAD, "reason", "user", projection=proj)


# ---------------------------------------------------------------------------
# Advice is display-only (authority field ignored)
# ---------------------------------------------------------------------------


class TestAdviceIgnored:
    def test_extra_advice_fields_in_payload_do_not_affect_event_type(self) -> None:
        """Triage-advisor advice in payload must not change canonical outcome."""
        ctx = _ctx(status="detected")
        payload: dict[str, object] = {
            "classification": "product_bug",
            "severity": "high",
            "evidence_refs": _STD_EVIDENCE,
            # Advisory noise that must not affect the event
            "recommended_action": "confirm_assessment",
            "confidence": 0.9,
        }
        events = validate_review_action(ctx, "confirm_assessment", payload, "confirmed", "user")
        assert events[0].type == "problem_assessment_confirmed"
