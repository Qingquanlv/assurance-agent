"""Pure reconciliation: validate a candidate batch, then derive Occurrence/Problem events.

A candidate batch is semantically validated as a complete unit before any
events are created.  A single invalid candidate rejects the entire batch
(all-or-nothing).

Semantic validation rejects a batch when any candidate has:
- an unknown Observation ID (not in the supplied observations document)
- a duplicate candidate_id within the batch
- a duplicate deterministic occurrence_id within the batch
- incomplete / unnormalisable fingerprint inputs
- a possible_problem_id that does not exist in the current projection

Event derivation rules (applied only after full validation passes):
- ``issue_analysis_completed`` is the FIRST Change event before any Occurrence events.
- Exact fingerprint match (existing problem, not resolved) → ``occurrence_linked``
  + ``problem_occurrence_linked``.
- Exact fingerprint match (existing problem, resolved) → ``occurrence_detected``
  + ``problem_regressed``.
- No fingerprint match → ``occurrence_detected`` + ``problem_detected``.
- Possible problem IDs listed by the LLM but NOT matched exactly → one
  ``problem_merge_suggested`` per possible_problem_id, for human review only.

Public API:
    ReconciliationValidationError
    ReconciliationPlan
    plan_reconciliation(candidates, observations, change_snapshot, problems)
        -> ReconciliationPlan
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone

from assurance_agent.artifacts.models.issues import (
    IssueAnalysisStatus,
    IssueCandidateDocument,
    IssueOccurrence,
    OccurrenceAnalysis,
    ObservationDocument,
    ChangeIssueSnapshot,
    Problem,
    ProblemFingerprint,
    ProblemProjection,
    ProvisionalAssessment,
)
from assurance_agent.workflow.issues.events import (
    ChangeIssueEvent,
    IssueAnalysisCompletedEvent,
    OccurrenceDetectedEvent,
    OccurrenceLinkedEvent,
    ProblemDetectedEvent,
    ProblemEvent,
    ProblemMergeSuggestedEvent,
    ProblemOccurrenceLinkedEvent,
    ProblemRegressedEvent,
)
from assurance_agent.workflow.issues.identity import (
    occurrence_id as compute_occurrence_id,
    problem_fingerprint,
    problem_id as compute_problem_id,
)


# ---------------------------------------------------------------------------
# Public exceptions and data types
# ---------------------------------------------------------------------------


class ReconciliationValidationError(Exception):
    """Raised when the candidate batch fails semantic validation.

    The entire batch is rejected; callers must write a failed reconcile-status
    document and must NOT append any Occurrence or Problem events.
    """

    def __init__(self, errors: list[str]) -> None:
        joined = "; ".join(errors)
        super().__init__(f"candidate batch validation failed: {joined}")
        self.errors = errors


@dataclass(frozen=True)
class ReconciliationPlan:
    """Immutable set of events derived from a valid, validated candidate batch.

    ``change_events`` contains (in order):
        issue_analysis_completed, then one occurrence_detected/linked per candidate.

    ``problem_events`` contains (in order):
        problem_detected events, then problem_occurrence_linked / problem_regressed,
        then problem_merge_suggested events.

    ``candidate_digest`` is the batch-level SHA-256 digest of the serialised
    IssueCandidateDocument (suitable for IssueReconcileStatus).

    ``occurrence_count`` equals len(candidates_doc.candidates).
    """

    change_events: tuple[ChangeIssueEvent, ...]
    problem_events: tuple[ProblemEvent, ...]
    candidate_digest: str
    occurrence_count: int


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _utc_now() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _event_id(idempotency_key: str) -> str:
    """Deterministic event ID derived from the idempotency key (16 hex chars)."""
    return "EVT-" + hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:16]


def _per_candidate_digest(candidate_data: dict) -> str:
    """Return a per-candidate SHA-256 digest from its canonical JSON representation."""
    canonical = (
        json.dumps(candidate_data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\n"
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _batch_candidate_digest(candidates_doc: IssueCandidateDocument) -> str:
    """Return a batch-level SHA-256 digest from the canonical IssueCandidateDocument."""
    data = candidates_doc.model_dump(mode="json")
    canonical = (
        json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _make_occurrence(
    *,
    occ_id: str,
    change_id: str,
    batch_id: str,
    candidate_data: dict,
    problem_id: str,
    evidence_bundle_digest: str,
    per_candidate_digest: str,
) -> IssueOccurrence:
    """Build an IssueOccurrence from a candidate dict and reconciliation metadata."""
    proposed = candidate_data["proposed"]
    return IssueOccurrence(
        occurrence_id=occ_id,
        change_id=change_id,
        batch_id=batch_id,
        observation_ids=list(candidate_data["observation_ids"]),
        problem_id=problem_id,
        provisional_assessment=ProvisionalAssessment(
            classification=proposed["classification"],
            severity=proposed["severity"],
            authority="llm_provisional",
            root_cause_hypothesis=proposed["root_cause_hypothesis"],
        ),
        analysis=OccurrenceAnalysis(
            evidence_bundle_digest=evidence_bundle_digest,
            analyzer="aa-issue-analyzer",
            prompt_version="1.0",
            candidate_digest=per_candidate_digest,
        ),
    )


# ---------------------------------------------------------------------------
# Semantic validation
# ---------------------------------------------------------------------------


def _validate_candidate_batch(
    candidates_doc: IssueCandidateDocument,
    observations: ObservationDocument,
    problems: ProblemProjection,
) -> None:
    """Validate the complete candidate batch; raises ``ReconciliationValidationError``
    if any semantic constraint is violated.

    Does NOT modify any mutable state (pure function).
    """
    errors: list[str] = []

    known_obs_ids: set[str] = {obs.observation_id for obs in observations.observations}
    existing_problem_ids: set[str] = {p.problem_id for p in problems.problems}

    seen_candidate_ids: set[str] = set()
    seen_occurrence_ids: set[str] = set()
    seen_fingerprint_digests: dict[str, str] = {}  # digest → candidate_id (for new-in-batch)

    for candidate in candidates_doc.candidates:
        cid = candidate.candidate_id

        # 1. Duplicate candidate_id
        if cid in seen_candidate_ids:
            errors.append(f"duplicate candidate_id: {cid!r}")
        seen_candidate_ids.add(cid)

        # 2. All observation_ids must exist
        for obs_id in candidate.observation_ids:
            if obs_id not in known_obs_ids:
                errors.append(
                    f"candidate {cid!r}: unknown observation_id {obs_id!r}"
                )

        # 3. Fingerprint inputs must be non-empty and normalisable
        try:
            fp = problem_fingerprint(
                affected_surface=candidate.affected_surface,
                fingerprint_inputs=candidate.fingerprint_inputs,
            )
        except (ValueError, Exception) as exc:
            errors.append(f"candidate {cid!r}: invalid fingerprint inputs: {exc}")
            fp = None

        # 4. Compute per-candidate digest and check duplicate occurrence_id
        candidate_data = candidate.model_dump(mode="json")
        per_digest = _per_candidate_digest(candidate_data)
        change_id = candidates_doc.change_id
        batch_id = candidates_doc.batch_id

        occ_id = compute_occurrence_id(change_id, batch_id, per_digest)
        if occ_id in seen_occurrence_ids:
            errors.append(
                f"candidate {cid!r}: duplicate deterministic occurrence_id {occ_id!r}"
            )
        seen_occurrence_ids.add(occ_id)

        # 5. Check for within-batch fingerprint collisions producing same occurrence_id
        if fp is not None:
            if fp.digest in seen_fingerprint_digests and per_digest == _per_candidate_digest(
                candidate_data
            ):
                pass  # same candidate content produces same occ_id caught above

        # 6. possible_problem_ids must reference existing problems
        for possible_pid in candidate.possible_problem_ids:
            if possible_pid not in existing_problem_ids:
                errors.append(
                    f"candidate {cid!r}: possible_problem_id {possible_pid!r} not in projection"
                )

    if errors:
        raise ReconciliationValidationError(errors)


# ---------------------------------------------------------------------------
# Event derivation
# ---------------------------------------------------------------------------


def _derive_reconciliation_events(
    candidates_doc: IssueCandidateDocument,
    problems: ProblemProjection,
    ts: str,
    batch_candidate_digest: str,
) -> tuple[list[ChangeIssueEvent], list[ProblemEvent]]:
    """Derive the full set of change and problem events for a validated batch.

    Assumes ``_validate_candidate_batch`` has already been called and succeeded.

    Returns (change_events, problem_events) in the required emission order.
    """
    change_id = candidates_doc.change_id
    batch_id = candidates_doc.batch_id
    evidence_bundle_digest = candidates_doc.evidence_bundle_digest

    # Running state for version tracking and fingerprint lookup
    fingerprint_digest_to_pid: dict[str, str] = {
        p.fingerprint.digest: p.problem_id for p in problems.problems
    }
    problem_versions: dict[str, int] = {p.problem_id: p.version for p in problems.problems}
    problem_statuses: dict[str, str] = {p.problem_id: p.status for p in problems.problems}
    original_problem_ids: set[str] = {p.problem_id for p in problems.problems}

    change_events: list[ChangeIssueEvent] = []
    problem_events: list[ProblemEvent] = []

    # First Change event: issue_analysis_completed
    completed_idem = (
        f"issue_analysis_completed:{change_id}:{batch_id}:{batch_candidate_digest}"
    )
    analysis_status = IssueAnalysisStatus(
        schema_version="1.0",
        change_id=change_id,
        batch_id=batch_id,
        status="completed",
        evidence_bundle_digest=evidence_bundle_digest,
        candidate_count=len(candidates_doc.candidates),
        candidate_digest=batch_candidate_digest,
    )
    change_events.append(
        IssueAnalysisCompletedEvent(
            schema_version="1.0",
            seq=1,
            event_id=_event_id(completed_idem),
            idempotency_key=completed_idem,
            ts=ts,
            evidence_digest=evidence_bundle_digest,
            change_id=change_id,
            batch_id=batch_id,
            type="issue_analysis_completed",
            analysis_status=analysis_status,
        )
    )

    # Per-candidate events (occurrence + problem)
    merge_suggestions: list[ProblemEvent] = []

    for candidate in candidates_doc.candidates:
        candidate_data = candidate.model_dump(mode="json")
        per_digest = _per_candidate_digest(candidate_data)
        occ_id = compute_occurrence_id(change_id, batch_id, per_digest)

        fp = problem_fingerprint(
            affected_surface=candidate.affected_surface,
            fingerprint_inputs=candidate.fingerprint_inputs,
        )
        pid = compute_problem_id(fp)

        occurrence = _make_occurrence(
            occ_id=occ_id,
            change_id=change_id,
            batch_id=batch_id,
            candidate_data=candidate_data,
            problem_id=pid,
            evidence_bundle_digest=evidence_bundle_digest,
            per_candidate_digest=per_digest,
        )

        if fp.digest in fingerprint_digest_to_pid:
            # Exact fingerprint match — link or regress
            existing_pid = fingerprint_digest_to_pid[fp.digest]
            current_version = problem_versions[existing_pid]
            current_status = problem_statuses[existing_pid]

            if current_status == "resolved":
                # Regression: previously resolved, now seen again
                occ_idem = f"occurrence_detected:{change_id}:{batch_id}:{per_digest}"
                change_events.append(
                    OccurrenceDetectedEvent(
                        schema_version="1.0",
                        seq=1,
                        event_id=_event_id(occ_idem),
                        idempotency_key=occ_idem,
                        ts=ts,
                        evidence_digest=evidence_bundle_digest,
                        change_id=change_id,
                        batch_id=batch_id,
                        type="occurrence_detected",
                        occurrence=occurrence,
                    )
                )
                reg_idem = f"problem_regressed:{existing_pid}:{change_id}:{batch_id}:{per_digest}"
                problem_events.append(
                    ProblemRegressedEvent(
                        schema_version="1.0",
                        seq=1,
                        event_id=_event_id(reg_idem),
                        idempotency_key=reg_idem,
                        ts=ts,
                        evidence_digest=evidence_bundle_digest,
                        problem_id=existing_pid,
                        expected_problem_version=current_version,
                        type="problem_regressed",
                        occurrence_id=occ_id,
                        change_id=change_id,
                    )
                )
                problem_versions[existing_pid] += 1
                problem_statuses[existing_pid] = "detected"

            else:
                # Link: existing non-resolved problem
                occ_idem = f"occurrence_linked:{change_id}:{batch_id}:{per_digest}"
                change_events.append(
                    OccurrenceLinkedEvent(
                        schema_version="1.0",
                        seq=1,
                        event_id=_event_id(occ_idem),
                        idempotency_key=occ_idem,
                        ts=ts,
                        evidence_digest=evidence_bundle_digest,
                        change_id=change_id,
                        batch_id=batch_id,
                        type="occurrence_linked",
                        occurrence=occurrence,
                    )
                )
                link_idem = (
                    f"problem_occurrence_linked:{existing_pid}:{change_id}:{batch_id}:{per_digest}"
                )
                problem_events.append(
                    ProblemOccurrenceLinkedEvent(
                        schema_version="1.0",
                        seq=1,
                        event_id=_event_id(link_idem),
                        idempotency_key=link_idem,
                        ts=ts,
                        evidence_digest=evidence_bundle_digest,
                        problem_id=existing_pid,
                        expected_problem_version=current_version,
                        type="problem_occurrence_linked",
                        occurrence_id=occ_id,
                        change_id=change_id,
                        batch_id=batch_id,
                    )
                )
                problem_versions[existing_pid] += 1

        else:
            # No fingerprint match → new Problem
            occ_idem = f"occurrence_detected:{change_id}:{batch_id}:{per_digest}"
            change_events.append(
                OccurrenceDetectedEvent(
                    schema_version="1.0",
                    seq=1,
                    event_id=_event_id(occ_idem),
                    idempotency_key=occ_idem,
                    ts=ts,
                    evidence_digest=evidence_bundle_digest,
                    change_id=change_id,
                    batch_id=batch_id,
                    type="occurrence_detected",
                    occurrence=occurrence,
                )
            )
            det_idem = (
                f"problem_detected:{pid}:{change_id}:{batch_id}:{per_digest}"
            )
            problem_events.append(
                ProblemDetectedEvent(
                    schema_version="1.0",
                    seq=1,
                    event_id=_event_id(det_idem),
                    idempotency_key=det_idem,
                    ts=ts,
                    evidence_digest=evidence_bundle_digest,
                    problem_id=pid,
                    expected_problem_version=0,
                    type="problem_detected",
                    occurrence_id=occ_id,
                    change_id=change_id,
                    batch_id=batch_id,
                    fingerprint=fp,
                    title=candidate.proposed.title,
                    classification=candidate.proposed.classification,
                    severity=candidate.proposed.severity,
                    root_cause_hypothesis=candidate.proposed.root_cause_hypothesis,
                )
            )
            # Register in running state so subsequent candidates with the same
            # fingerprint in this batch correctly link to this new problem
            fingerprint_digest_to_pid[fp.digest] = pid
            problem_versions[pid] = 1
            problem_statuses[pid] = "detected"

            # Semantic merge suggestions for LLM-proposed possible matches
            for possible_pid in candidate.possible_problem_ids:
                if possible_pid in original_problem_ids:
                    # Only suggest against original-projection problems, not newly created ones
                    merge_idem = (
                        f"problem_merge_suggested:{pid}:{possible_pid}:{change_id}:{batch_id}:{per_digest}"
                    )
                    current_new_version = problem_versions[pid]
                    merge_suggestions.append(
                        ProblemMergeSuggestedEvent(
                            schema_version="1.0",
                            seq=1,
                            event_id=_event_id(merge_idem),
                            idempotency_key=merge_idem,
                            ts=ts,
                            evidence_digest=evidence_bundle_digest,
                            problem_id=pid,
                            expected_problem_version=current_new_version,
                            type="problem_merge_suggested",
                            source_occurrence_id=occ_id,
                            source_change_id=change_id,
                            target_problem_id=possible_pid,
                            candidate_id=candidate.candidate_id,
                            reason=(
                                f"LLM suggested semantic similarity between candidate "
                                f"{candidate.candidate_id!r} and existing problem {possible_pid!r}"
                            ),
                        )
                    )

    # Append merge suggestions after all detection/link events
    problem_events.extend(merge_suggestions)

    return change_events, problem_events


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def plan_reconciliation(
    candidates: IssueCandidateDocument,
    observations: ObservationDocument,
    change_snapshot: ChangeIssueSnapshot,
    problems: ProblemProjection,
) -> ReconciliationPlan:
    """Validate the candidate batch then derive immutable Occurrence/Problem events.

    Raises:
        ReconciliationValidationError: if any candidate fails semantic validation.
            The caller must write only a failed reconcile-status and return
            TaskResult success.  No Occurrence/Problem events may be appended.

    On success, returns a ``ReconciliationPlan`` with all events in the correct
    emission order.  The caller is responsible for appending to both stores in
    the same task write-set.
    """
    _validate_candidate_batch(candidates, observations, problems)

    ts = _utc_now()
    batch_digest = _batch_candidate_digest(candidates)

    change_events, problem_events = _derive_reconciliation_events(
        candidates_doc=candidates,
        problems=problems,
        ts=ts,
        batch_candidate_digest=batch_digest,
    )

    return ReconciliationPlan(
        change_events=tuple(change_events),
        problem_events=tuple(problem_events),
        candidate_digest=batch_digest,
        occurrence_count=len(candidates.candidates),
    )
