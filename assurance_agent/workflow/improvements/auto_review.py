"""Deterministic selection, Gate input, and locked Auto Review application."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from assurance_agent.artifacts.canonical import sha256_bytes
from assurance_agent.artifacts.models.improvement_review import (
    ImprovementAutoReviewAssessment,
    ImprovementAutoReviewStatus,
    ImprovementReviewSubject,
)
from assurance_agent.artifacts.models.improvements import (
    DeliveryKind,
    ImprovementKind,
    ImprovementLedgerProjection,
    ImprovementState,
    is_valid_memory_patch_target,
)
from assurance_agent.workflow.graph.project_locks import ProjectResourceLockManager
from assurance_agent.workflow.improvements.events import (
    ImprovementAutoReviewApprovedEvent,
    ImprovementAutoReviewRecordedEvent,
    read_improvement_events,
)
from assurance_agent.workflow.improvements.ledger import ProjectImprovementStore, atomic_write_json
from assurance_agent.workflow.improvements.projection import project_improvements
from assurance_agent.workflow.improvements.reconciler import ImprovementAcceptStatus

_FROZEN = ConfigDict(frozen=True, extra="forbid")
_LOCK_TOKEN = "project:improvement-registry"


class AutoReviewItem(BaseModel):
    model_config = _FROZEN

    improvement_id: str
    subject_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    expected_improvement_version: int = Field(ge=1)
    review_id: str
    policy_version: Literal["1"] = "1"
    attempt: int = Field(default=1, ge=1)


class AutoReviewGateInput(BaseModel):
    model_config = _FROZEN

    schema_version: Literal["1"] = "1"
    review_id: str
    improvement_id: str
    expected_improvement_version: int
    subject_sha256: str
    assessment_sha256: str
    policy_version: Literal["1"] = "1"
    projection_state: str
    projection_subject_matches: bool
    semantic_subject_matches: bool
    kind_allowed: bool
    delivery_allowed: bool
    risk_low: bool
    confidence_high: bool
    reviewer_pass: bool
    human_review_required: bool
    source_refs_resolve: bool
    has_blocking_findings: bool
    verification_ready: bool
    ambiguity_free: bool
    prior_terminal_review: bool
    explicit_retry_allowed: bool

    @property
    def auto_approve(self) -> bool:
        return all(
            (
                self.projection_state == "proposed",
                self.projection_subject_matches,
                self.semantic_subject_matches,
                self.kind_allowed,
                self.delivery_allowed,
                self.risk_low,
                self.confidence_high,
                self.reviewer_pass,
                not self.human_review_required,
                self.source_refs_resolve,
                not self.has_blocking_findings,
                self.verification_ready,
                self.ambiguity_free,
                not self.prior_terminal_review or self.explicit_retry_allowed,
            )
        )


def _review_id(improvement_id: str, subject_sha256: str, policy_version: str, attempt: int) -> str:
    identity = f"{improvement_id}:{subject_sha256}:{policy_version}:{attempt}"
    return "AUTO-" + sha256_bytes(identity.encode()).removeprefix("sha256:")[:24]


def _delivery_allowed(delivery: DeliveryKind, target: str) -> bool:
    if delivery is DeliveryKind.KNOWLEDGE_DELTA:
        return False
    if delivery is DeliveryKind.MEMORY_PATCH:
        return is_valid_memory_patch_target(target)
    return True


def select_auto_review_items(
    projection: ImprovementLedgerProjection,
    accept_statuses: Sequence[ImprovementAcceptStatus],
    *,
    policy_version: str = "1",
) -> tuple[AutoReviewItem, ...]:
    """Select only explicit current-run accepted statuses; never scan a queue."""
    if policy_version != "1":
        return ()
    selected: dict[str, AutoReviewItem] = {}
    for status in accept_statuses:
        if status.result != "accepted":
            continue
        for improvement_id in status.improvement_ids:
            item = projection.improvements.get(improvement_id)
            if (
                item is None
                or item.state is not ImprovementState.PROPOSED
                or item.review_subject_sha256 is None
            ):
                continue
            prior = item.last_auto_review
            if prior is not None and prior.subject_sha256 == item.review_subject_sha256:
                continue
            selected[improvement_id] = AutoReviewItem(
                improvement_id=improvement_id,
                subject_sha256=item.review_subject_sha256,
                expected_improvement_version=item.version,
                review_id=_review_id(improvement_id, item.review_subject_sha256, policy_version, 1),
            )
    return tuple(selected[key] for key in sorted(selected))


def build_auto_review_gate_input(
    project_root: Path,
    *,
    review_id: str,
    improvement_id: str,
    subject_sha256: str,
    expected_version: int,
    policy_version: str,
    attempt: int,
) -> AutoReviewGateInput:
    """Mechanically derive eligibility facts from canonical artifacts."""
    if policy_version != "1":
        raise ValueError(f"unsupported auto review policy version: {policy_version}")
    root = project_root / "qa" / "improvements"
    events = read_improvement_events(root / "events.jsonl")
    projection = project_improvements(events)
    current = projection.improvements[improvement_id]
    subject_path = root / "review-subjects" / f"{subject_sha256}.json"
    subject_bytes = subject_path.read_bytes()
    subject = ImprovementReviewSubject.model_validate_json(subject_bytes)
    assessment_path = root / "reviews" / review_id / "assessment.json"
    assessment_bytes = assessment_path.read_bytes()
    assessment = ImprovementAutoReviewAssessment.model_validate_json(assessment_bytes)
    resolved = subject.source_manifest.resolvable_ids()
    terminal = any(
        getattr(event, "review_id", None) == review_id
        and event.type in {"improvement_auto_review_approved", "improvement_auto_review_recorded"}
        for event in events
    )
    return AutoReviewGateInput(
        review_id=review_id,
        improvement_id=improvement_id,
        expected_improvement_version=expected_version,
        subject_sha256=subject_sha256,
        assessment_sha256=sha256_bytes(assessment_bytes),
        policy_version=policy_version,
        projection_state=current.state.value,
        projection_subject_matches=current.review_subject_sha256 == subject_sha256,
        semantic_subject_matches=(
            sha256_bytes(subject_bytes) == subject_sha256
            and subject.improvement_id == improvement_id
            and assessment.subject_sha256 == subject_sha256
            and assessment.expected_improvement_version == expected_version
        ),
        kind_allowed=subject.kind
        in {
            ImprovementKind.PROMPT,
            ImprovementKind.FIXTURE,
            ImprovementKind.TEST,
            ImprovementKind.WORKFLOW,
        },
        delivery_allowed=_delivery_allowed(subject.delivery, subject.target),
        risk_low=subject.risk == "low",
        confidence_high=subject.confidence == "high",
        reviewer_pass=assessment.decision == "pass",
        human_review_required=assessment.human_review_required,
        source_refs_resolve=set(subject.source_refs.all_ids()).issubset(resolved),
        has_blocking_findings=any(
            finding.severity in {"high", "critical", "blocking"} for finding in assessment.findings
        ),
        verification_ready=bool(subject.verification.suites)
        and bool(subject.verification.success_criteria.strip())
        and assessment.verification_readiness == "ready",
        ambiguity_free=assessment.scope_readiness == "ready",
        prior_terminal_review=terminal,
        explicit_retry_allowed=attempt > 1,
    )


def _write_status(project_root: Path, status: ImprovementAutoReviewStatus) -> None:
    atomic_write_json(
        project_root / "qa" / "improvements" / "reviews" / status.review_id / "status.json",
        status.model_dump(mode="json"),
    )


def apply_auto_review_result(
    project_root: Path,
    item: AutoReviewItem,
    *,
    gate_verdict: Literal["auto_approve", "record", "review_error"],
) -> ImprovementAutoReviewStatus:
    """Lock, reread all bindings, and append at most one terminal review event."""
    root = project_root / "qa" / "improvements"
    assessment_bytes = (root / "reviews" / item.review_id / "assessment.json").read_bytes()
    assessment = ImprovementAutoReviewAssessment.model_validate_json(assessment_bytes)
    assessment_sha = sha256_bytes(assessment_bytes)
    locks = ProjectResourceLockManager(project_root)
    with locks.acquire((_LOCK_TOKEN,), timeout_seconds=30.0):
        events = read_improvement_events(root / "events.jsonl")
        for event in events:
            if getattr(event, "review_id", None) != item.review_id:
                continue
            if isinstance(event, ImprovementAutoReviewApprovedEvent):
                result: Literal["approved", "escalated", "review_error"] = "approved"
            elif isinstance(event, ImprovementAutoReviewRecordedEvent):
                result = "review_error" if event.verdict == "review_error" else "escalated"
            else:
                continue
            status = ImprovementAutoReviewStatus(
                review_id=item.review_id,
                improvement_id=item.improvement_id,
                result=result,
                ledger_event_id=event.event_id,
                replayed=True,
            )
            _write_status(project_root, status)
            return status
        projection = project_improvements(events)
        current = projection.improvements.get(item.improvement_id)
        subject_path = root / "review-subjects" / f"{item.subject_sha256}.json"
        subject_is_valid = (
            subject_path.is_file() and sha256_bytes(subject_path.read_bytes()) == item.subject_sha256
        )
        if (
            current is None
            or current.state is not ImprovementState.PROPOSED
            or current.version != item.expected_improvement_version
            or current.review_subject_sha256 != item.subject_sha256
            or (gate_verdict != "review_error" and not subject_is_valid)
            or assessment.improvement_id != item.improvement_id
            or assessment.review_id != item.review_id
            or assessment.expected_improvement_version != item.expected_improvement_version
            or assessment.subject_sha256 != item.subject_sha256
        ):
            status = ImprovementAutoReviewStatus(
                review_id=item.review_id,
                improvement_id=item.improvement_id,
                result="stale",
                ledger_event_id=None,
            )
            _write_status(project_root, status)
            return status
        key = f"auto-review:{item.review_id}"
        event_id = "IMPEVT-" + sha256_bytes(key.encode()).removeprefix("sha256:")[:16]
        event_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        if gate_verdict == "auto_approve":
            event = ImprovementAutoReviewApprovedEvent(
                schema_version="1.0",
                seq=len(events) + 1,
                event_id=event_id,
                idempotency_key=key,
                ts=event_ts,
                improvement_id=item.improvement_id,
                expected_improvement_version=item.expected_improvement_version,
                type="improvement_auto_review_approved",
                review_id=item.review_id,
                subject_sha256=item.subject_sha256,
                assessment_sha256=assessment_sha,
                policy_version=item.policy_version,
            )
            result: Literal["approved", "escalated", "review_error"] = "approved"
        else:
            verdict = cast(
                Literal["changes_requested", "needs_human_review", "reject_advice", "review_error"],
                {
                    "changes_requested": "changes_requested",
                    "needs_human_review": "needs_human_review",
                    "reject": "reject_advice",
                    "pass": "needs_human_review",
                }[assessment.decision]
                if gate_verdict == "record"
                else "review_error",
            )
            event = ImprovementAutoReviewRecordedEvent(
                schema_version="1.0",
                seq=len(events) + 1,
                event_id=event_id,
                idempotency_key=key,
                ts=event_ts,
                improvement_id=item.improvement_id,
                expected_improvement_version=item.expected_improvement_version,
                type="improvement_auto_review_recorded",
                review_id=item.review_id,
                subject_sha256=item.subject_sha256,
                assessment_sha256=assessment_sha,
                policy_version=item.policy_version,
                verdict=verdict,
                reason_code=f"reviewer_{assessment.decision}",
            )
            result = "escalated" if gate_verdict == "record" else "review_error"
        ProjectImprovementStore(project_root).append_and_rebuild((event,))
        status = ImprovementAutoReviewStatus(
            review_id=item.review_id,
            improvement_id=item.improvement_id,
            result=result,
            ledger_event_id=event.event_id,
        )
        _write_status(project_root, status)
        return status


__all__ = [
    "AutoReviewGateInput",
    "AutoReviewItem",
    "apply_auto_review_result",
    "build_auto_review_gate_input",
    "select_auto_review_items",
]
