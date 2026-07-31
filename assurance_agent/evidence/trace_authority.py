"""Failure and issue-recovery authority evaluation for reconciled Trace V2.

TraceProjection remains fact-only. This module classifies current-batch authority
and emits at most one failure gap and at most one issue-authority gap. Completed
project/history membership is deferred to later tasks; ``fold_trace`` does not
call this module until Task 12.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import ValidationError

from assurance_agent.artifacts.models.inspect import FailureAnalysis
from assurance_agent.artifacts.models.issue_events import (
    ChangeIssueEvent,
    IssueAnalysisFailedEvent,
    ProjectSyncPendingEvent,
)
from assurance_agent.artifacts.models.issues import (
    ChangeIssueSnapshot,
    IssueCandidateDocument,
    IssueEvidenceManifest,
    IssueEvidenceManifestEntry,
    IssueReconcileStatusV1,
    IssueReconcileStatusV2,
    ObservationDocument,
    load_issue_reconcile_status_document,
)
from assurance_agent.artifacts.models.trace import TraceFailure, TraceGapV2, TraceSource
from assurance_agent.evidence.digests import (
    EvidenceEntryPathError,
    TraceSourceRecorder,
    evidence_bundle_digest_v1,
    normalize_evidence_entry_path,
    read_evidence_entry_v1,
)
from assurance_agent.evidence.issue_identity import candidate_document_digest
from assurance_agent.evidence.issue_replay import (
    IssueLedgerIntegrityError,
    IssueLedgerMissingError,
    dump_projection,
    project_change_issues,
    read_change_issue_events_from_bytes,
)

AuthoritySource = Literal[
    "inspect/failure-analysis.json",
    "inspect/issue-evidence-manifest.json",
    "inspect/issue-candidates.json",
    "inspect/observations.json",
    "issues/events.jsonl",
    "issues/snapshot.json",
    "inspect/issue-reconcile-status.json",
    "qa/issues/events.jsonl",
    "qa/issues/problems.json",
]

AuthorityReason = Literal[
    "missing",
    "malformed",
    "change_id_mismatch",
    "batch_id_mismatch",
    "source_batch_id_mismatch",
    "entry_path_invalid",
    "entry_duplicate",
    "execution_anchor_missing",
    "entry_digest_mismatch",
    "bundle_digest_mismatch",
    "evidence_digest_mismatch",
    "candidate_digest_mismatch",
    "observations_replay_mismatch",
    "ledger_missing",
    "ledger_malformed",
    "event_identity_mismatch",
    "projection_replay_mismatch",
    "recovery_event_missing",
    "analysis_pending",
    "candidate_count_mismatch",
    "occurrence_count_mismatch",
    "occurrence_set_mismatch",
    "occurrence_identity_mismatch",
    "observation_reference_invalid",
    "problem_occurrence_mismatch",
    "status_inconsistent",
]

FAILURE_SOURCE: AuthoritySource = "inspect/failure-analysis.json"
MANIFEST_SOURCE: AuthoritySource = "inspect/issue-evidence-manifest.json"
CANDIDATES_SOURCE: AuthoritySource = "inspect/issue-candidates.json"
OBSERVATIONS_SOURCE: AuthoritySource = "inspect/observations.json"
LEDGER_SOURCE: AuthoritySource = "issues/events.jsonl"
SNAPSHOT_SOURCE: AuthoritySource = "issues/snapshot.json"
RECONCILE_SOURCE: AuthoritySource = "inspect/issue-reconcile-status.json"
PROJECT_LEDGER_SOURCE: AuthoritySource = "qa/issues/events.jsonl"
PROJECT_PROBLEMS_SOURCE: AuthoritySource = "qa/issues/problems.json"

_EXECUTION_ANCHOR = "execution/execution-manifest.yaml"

_ISSUE_SOURCES: tuple[AuthoritySource, ...] = (
    MANIFEST_SOURCE,
    CANDIDATES_SOURCE,
    OBSERVATIONS_SOURCE,
    LEDGER_SOURCE,
    SNAPSHOT_SOURCE,
    RECONCILE_SOURCE,
    PROJECT_LEDGER_SOURCE,
    PROJECT_PROBLEMS_SOURCE,
)


class AuthorityValidationError(ValueError):
    source: AuthoritySource
    reason: AuthorityReason

    def __init__(self, source: AuthoritySource, reason: AuthorityReason) -> None:
        super().__init__(f"{source}:{reason}")
        self.source = source
        self.reason = reason


@dataclass(frozen=True, slots=True)
class FailureAuthorityResult:
    sources: tuple[TraceSource, ...]
    gaps: tuple[TraceGapV2, ...]
    failures_by_case: Mapping[str, tuple[TraceFailure, ...]]


@dataclass(frozen=True, slots=True)
class ValidatedAuthorityPrefix:
    manifest_digest: str
    candidate_digest: str
    manifest: IssueEvidenceManifest
    candidates: IssueCandidateDocument
    observations: ObservationDocument
    change_events: tuple[ChangeIssueEvent, ...]
    replayed_snapshot: ChangeIssueSnapshot
    reconcile_status: IssueReconcileStatusV2


@dataclass(frozen=True, slots=True)
class AuthorityPrefixResult:
    state: Literal[
        "completed",
        "analysis_failed",
        "reconcile_failed",
        "project_sync_pending",
        "unavailable",
    ]
    sources: tuple[TraceSource, ...]
    gaps: tuple[TraceGapV2, ...]
    validated: ValidatedAuthorityPrefix | None


def authority_gap(
    code: str,
    source: AuthoritySource,
    batch_id: str,
    reason: AuthorityReason,
) -> TraceGapV2:
    return TraceGapV2(
        code=code,  # type: ignore[arg-type]
        source=source,
        batch_id=batch_id,
        detail=f"reason={reason}",
    )


def _sha256_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _record_path(
    recorder: TraceSourceRecorder,
    rel: str,
    path: Path,
) -> None:
    recorder.add(
        TraceSource(
            path=rel,
            exists=path.is_file(),
            sha256=_sha256_file(path),
        )
    )


def _project_root(change_dir: Path) -> Path:
    return change_dir.parents[2]


def _index_failures(analysis: FailureAnalysis) -> dict[str, tuple[TraceFailure, ...]]:
    grouped: dict[str, list[TraceFailure]] = {}
    for entry in analysis.failures:
        grouped.setdefault(entry.case_id, []).append(
            TraceFailure(category=entry.category, severity=entry.severity)
        )
    return {
        case_id: tuple(sorted(items, key=lambda item: (item.category, item.severity)))
        for case_id, items in sorted(grouped.items())
    }


def validate_failure_authority(
    change_dir: Path,
    change_id: str,
    batch_id: str,
) -> FailureAuthorityResult:
    """Return current validated failure links or one canonical failure gap."""
    recorder = TraceSourceRecorder()
    path = change_dir / FAILURE_SOURCE
    _record_path(recorder, FAILURE_SOURCE, path)

    if not path.is_file():
        return FailureAuthorityResult(
            sources=recorder.freeze(),
            gaps=(authority_gap("failure_analysis_missing", FAILURE_SOURCE, batch_id, "missing"),),
            failures_by_case={},
        )

    try:
        analysis = FailureAnalysis.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValidationError, ValueError):
        return FailureAuthorityResult(
            sources=recorder.freeze(),
            gaps=(authority_gap("failure_analysis_missing", FAILURE_SOURCE, batch_id, "malformed"),),
            failures_by_case={},
        )

    if analysis.change_id != change_id:
        return FailureAuthorityResult(
            sources=recorder.freeze(),
            gaps=(
                authority_gap(
                    "failure_analysis_identity_mismatch",
                    FAILURE_SOURCE,
                    batch_id,
                    "change_id_mismatch",
                ),
            ),
            failures_by_case={},
        )
    if analysis.batch_id != batch_id:
        return FailureAuthorityResult(
            sources=recorder.freeze(),
            gaps=(
                authority_gap(
                    "failure_analysis_identity_mismatch",
                    FAILURE_SOURCE,
                    batch_id,
                    "batch_id_mismatch",
                ),
            ),
            failures_by_case={},
        )
    if analysis.source_batch_id != batch_id:
        return FailureAuthorityResult(
            sources=recorder.freeze(),
            gaps=(
                authority_gap(
                    "failure_analysis_identity_mismatch",
                    FAILURE_SOURCE,
                    batch_id,
                    "source_batch_id_mismatch",
                ),
            ),
            failures_by_case={},
        )

    return FailureAuthorityResult(
        sources=recorder.freeze(),
        gaps=(),
        failures_by_case=_index_failures(analysis),
    )


def _read_json_object(path: Path, source: AuthoritySource) -> object:
    if not path.is_file():
        raise AuthorityValidationError(source, "missing")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise AuthorityValidationError(source, "malformed") from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise AuthorityValidationError(source, "malformed") from exc


def _validate_manifest(
    change_dir: Path,
    change_id: str,
    batch_id: str,
    recorder: TraceSourceRecorder,
) -> tuple[IssueEvidenceManifest, str]:
    path = change_dir / MANIFEST_SOURCE
    _record_path(recorder, MANIFEST_SOURCE, path)
    raw = _read_json_object(path, MANIFEST_SOURCE)
    try:
        manifest = IssueEvidenceManifest.model_validate(raw)
    except ValidationError as exc:
        raise AuthorityValidationError(MANIFEST_SOURCE, "malformed") from exc

    if manifest.change_id != change_id:
        raise AuthorityValidationError(MANIFEST_SOURCE, "change_id_mismatch")
    if manifest.batch_id != batch_id:
        raise AuthorityValidationError(MANIFEST_SOURCE, "batch_id_mismatch")

    seen_paths: set[str] = set()
    validated_entries: list[IssueEvidenceManifestEntry] = []
    for entry in manifest.entries:
        try:
            normalized = normalize_evidence_entry_path(entry.path)
        except EvidenceEntryPathError as exc:
            raise AuthorityValidationError(MANIFEST_SOURCE, "entry_path_invalid") from exc
        if normalized in seen_paths:
            raise AuthorityValidationError(MANIFEST_SOURCE, "entry_duplicate")
        seen_paths.add(normalized)
        try:
            opened = read_evidence_entry_v1(change_dir, normalized)
        except EvidenceEntryPathError as exc:
            raise AuthorityValidationError(MANIFEST_SOURCE, "entry_path_invalid") from exc
        recorder.add(
            TraceSource(
                path=opened.path,
                exists=True,
                sha256=opened.raw_sha256,
            )
        )
        if opened.entry_digest != entry.digest:
            raise AuthorityValidationError(MANIFEST_SOURCE, "entry_digest_mismatch")
        validated_entries.append(IssueEvidenceManifestEntry(path=opened.path, digest=opened.entry_digest))

    if _EXECUTION_ANCHOR not in seen_paths:
        raise AuthorityValidationError(MANIFEST_SOURCE, "execution_anchor_missing")

    recomputed = evidence_bundle_digest_v1(validated_entries)
    if recomputed != manifest.digest:
        raise AuthorityValidationError(MANIFEST_SOURCE, "bundle_digest_mismatch")
    return manifest, recomputed


def _validate_candidates(
    change_dir: Path,
    change_id: str,
    batch_id: str,
    manifest_digest: str,
    recorder: TraceSourceRecorder,
) -> tuple[IssueCandidateDocument, str]:
    path = change_dir / CANDIDATES_SOURCE
    _record_path(recorder, CANDIDATES_SOURCE, path)
    raw = _read_json_object(path, CANDIDATES_SOURCE)
    try:
        candidates = IssueCandidateDocument.model_validate(raw)
    except ValidationError as exc:
        raise AuthorityValidationError(CANDIDATES_SOURCE, "malformed") from exc
    if candidates.change_id != change_id:
        raise AuthorityValidationError(CANDIDATES_SOURCE, "change_id_mismatch")
    if candidates.batch_id != batch_id:
        raise AuthorityValidationError(CANDIDATES_SOURCE, "batch_id_mismatch")
    if candidates.evidence_bundle_digest != manifest_digest:
        raise AuthorityValidationError(CANDIDATES_SOURCE, "evidence_digest_mismatch")
    return candidates, candidate_document_digest(candidates)


def _observation_payload_map(observations: ObservationDocument) -> dict[str, bytes]:
    return {
        item.observation_id: dump_projection(item)
        for item in sorted(observations.observations, key=lambda obs: obs.observation_id)
    }


def _validate_observations_identity(
    change_dir: Path,
    change_id: str,
    batch_id: str,
    recorder: TraceSourceRecorder,
) -> ObservationDocument:
    path = change_dir / OBSERVATIONS_SOURCE
    _record_path(recorder, OBSERVATIONS_SOURCE, path)
    raw = _read_json_object(path, OBSERVATIONS_SOURCE)
    try:
        document = ObservationDocument.model_validate(raw)
    except ValidationError as exc:
        raise AuthorityValidationError(OBSERVATIONS_SOURCE, "malformed") from exc
    if document.change_id != change_id:
        raise AuthorityValidationError(OBSERVATIONS_SOURCE, "change_id_mismatch")
    if document.batch_id != batch_id:
        raise AuthorityValidationError(OBSERVATIONS_SOURCE, "batch_id_mismatch")
    return document


def _validate_change_ledger(
    change_dir: Path,
    recorder: TraceSourceRecorder,
) -> tuple[ChangeIssueEvent, ...]:
    path = change_dir / LEDGER_SOURCE
    _record_path(recorder, LEDGER_SOURCE, path)
    if not path.is_file():
        raise AuthorityValidationError(LEDGER_SOURCE, "ledger_missing")
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise AuthorityValidationError(LEDGER_SOURCE, "ledger_malformed") from exc
    try:
        return read_change_issue_events_from_bytes(data)
    except IssueLedgerMissingError as exc:
        raise AuthorityValidationError(LEDGER_SOURCE, "ledger_missing") from exc
    except IssueLedgerIntegrityError as exc:
        message = str(exc).lower()
        if "event_id" in message or "idempotency" in message or "mismatch" in message:
            raise AuthorityValidationError(LEDGER_SOURCE, "event_identity_mismatch") from exc
        raise AuthorityValidationError(LEDGER_SOURCE, "ledger_malformed") from exc
    except (ValidationError, ValueError, UnicodeDecodeError) as exc:
        raise AuthorityValidationError(LEDGER_SOURCE, "ledger_malformed") from exc


def _assert_observations_replay(
    observations: ObservationDocument,
    events: tuple[ChangeIssueEvent, ...],
    batch_id: str,
) -> ChangeIssueSnapshot:
    replayed = project_change_issues(events)
    current = [
        obs
        for obs in replayed.observations
        if obs.batch_id == batch_id and obs.change_id == observations.change_id
    ]
    expected_doc = ObservationDocument(
        schema_version="1.0",
        change_id=observations.change_id,
        batch_id=batch_id,
        observations=current,
    )
    if _observation_payload_map(observations) != _observation_payload_map(expected_doc):
        raise AuthorityValidationError(OBSERVATIONS_SOURCE, "observations_replay_mismatch")
    return replayed


def _load_persisted_snapshot(
    change_dir: Path,
    recorder: TraceSourceRecorder,
) -> ChangeIssueSnapshot | None:
    path = change_dir / SNAPSHOT_SOURCE
    _record_path(recorder, SNAPSHOT_SOURCE, path)
    if not path.is_file():
        return None
    try:
        return ChangeIssueSnapshot.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValidationError, ValueError):
        return None


def _require_persisted_snapshot(
    change_dir: Path,
    change_id: str,
    batch_id: str,
    replayed: ChangeIssueSnapshot,
    recorder: TraceSourceRecorder,
    *,
    require_authoritative_batch: bool,
) -> ChangeIssueSnapshot:
    path = change_dir / SNAPSHOT_SOURCE
    _record_path(recorder, SNAPSHOT_SOURCE, path)
    if not path.is_file():
        raise AuthorityValidationError(SNAPSHOT_SOURCE, "missing")
    try:
        persisted = ChangeIssueSnapshot.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValidationError, ValueError) as exc:
        raise AuthorityValidationError(SNAPSHOT_SOURCE, "malformed") from exc
    if persisted.change_id != change_id:
        raise AuthorityValidationError(SNAPSHOT_SOURCE, "change_id_mismatch")
    if require_authoritative_batch and persisted.authoritative_batch_id != batch_id:
        raise AuthorityValidationError(SNAPSHOT_SOURCE, "batch_id_mismatch")
    if dump_projection(persisted) != dump_projection(replayed):
        raise AuthorityValidationError(SNAPSHOT_SOURCE, "projection_replay_mismatch")
    return persisted


def _validate_failed_analysis_chain(
    replayed: ChangeIssueSnapshot,
    change_id: str,
    batch_id: str,
    manifest_digest: str,
    candidate_digest: str,
    candidate_count: int,
) -> None:
    status = replayed.analysis_status
    if status is None:
        raise AuthorityValidationError(SNAPSHOT_SOURCE, "missing")
    if status.change_id != change_id:
        raise AuthorityValidationError(SNAPSHOT_SOURCE, "change_id_mismatch")
    if status.batch_id != batch_id:
        raise AuthorityValidationError(SNAPSHOT_SOURCE, "batch_id_mismatch")
    if status.evidence_bundle_digest != manifest_digest:
        raise AuthorityValidationError(SNAPSHOT_SOURCE, "evidence_digest_mismatch")
    if status.candidate_digest != candidate_digest:
        raise AuthorityValidationError(SNAPSHOT_SOURCE, "candidate_digest_mismatch")
    if status.status == "pending":
        raise AuthorityValidationError(SNAPSHOT_SOURCE, "analysis_pending")
    if status.candidate_count != candidate_count:
        raise AuthorityValidationError(SNAPSHOT_SOURCE, "candidate_count_mismatch")
    if status.status != "failed":
        raise AuthorityValidationError(SNAPSHOT_SOURCE, "status_inconsistent")


def _validate_completed_analysis_chain(
    replayed: ChangeIssueSnapshot,
    change_id: str,
    batch_id: str,
    manifest_digest: str,
    candidate_digest: str,
    candidate_count: int,
) -> None:
    status = replayed.analysis_status
    if status is None:
        raise AuthorityValidationError(SNAPSHOT_SOURCE, "missing")
    if status.change_id != change_id:
        raise AuthorityValidationError(SNAPSHOT_SOURCE, "change_id_mismatch")
    if status.batch_id != batch_id:
        raise AuthorityValidationError(SNAPSHOT_SOURCE, "batch_id_mismatch")
    if status.evidence_bundle_digest != manifest_digest:
        raise AuthorityValidationError(SNAPSHOT_SOURCE, "evidence_digest_mismatch")
    if status.candidate_digest != candidate_digest:
        raise AuthorityValidationError(SNAPSHOT_SOURCE, "candidate_digest_mismatch")
    if status.status == "pending":
        raise AuthorityValidationError(SNAPSHOT_SOURCE, "analysis_pending")
    if status.candidate_count != candidate_count:
        raise AuthorityValidationError(SNAPSHOT_SOURCE, "candidate_count_mismatch")
    if status.status != "completed":
        raise AuthorityValidationError(SNAPSHOT_SOURCE, "status_inconsistent")


def _load_reconcile_status(
    change_dir: Path,
    recorder: TraceSourceRecorder,
) -> IssueReconcileStatusV1 | IssueReconcileStatusV2 | None:
    path = change_dir / RECONCILE_SOURCE
    _record_path(recorder, RECONCILE_SOURCE, path)
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return load_issue_reconcile_status_document(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValidationError, ValueError):
        return None


def _require_reconcile_status(
    change_dir: Path,
    recorder: TraceSourceRecorder,
) -> IssueReconcileStatusV1 | IssueReconcileStatusV2:
    path = change_dir / RECONCILE_SOURCE
    _record_path(recorder, RECONCILE_SOURCE, path)
    if not path.is_file():
        raise AuthorityValidationError(RECONCILE_SOURCE, "missing")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return load_issue_reconcile_status_document(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValidationError, ValueError) as exc:
        raise AuthorityValidationError(RECONCILE_SOURCE, "malformed") from exc


def _validate_reconcile_identity(
    status: IssueReconcileStatusV1 | IssueReconcileStatusV2,
    change_id: str,
    batch_id: str,
    manifest_digest: str,
    candidate_digest: str,
) -> None:
    if isinstance(status, IssueReconcileStatusV1):
        # V1 is readable history but never current recovery/completed authority.
        raise AuthorityValidationError(RECONCILE_SOURCE, "status_inconsistent")
    if status.change_id != change_id:
        raise AuthorityValidationError(RECONCILE_SOURCE, "change_id_mismatch")
    if status.batch_id != batch_id:
        raise AuthorityValidationError(RECONCILE_SOURCE, "batch_id_mismatch")
    if status.evidence_bundle_digest != manifest_digest:
        raise AuthorityValidationError(RECONCILE_SOURCE, "evidence_digest_mismatch")
    if status.candidate_digest != candidate_digest:
        raise AuthorityValidationError(RECONCILE_SOURCE, "candidate_digest_mismatch")


def _current_analysis_failed(
    events: tuple[ChangeIssueEvent, ...],
    replayed: ChangeIssueSnapshot,
    batch_id: str,
) -> bool:
    if replayed.analysis_status is None or replayed.analysis_status.status != "failed":
        return False
    if replayed.authoritative_batch_id != batch_id:
        return False
    return any(isinstance(event, IssueAnalysisFailedEvent) and event.batch_id == batch_id for event in events)


def _current_project_sync_pending(
    events: tuple[ChangeIssueEvent, ...],
    batch_id: str,
    candidate_digest: str,
) -> bool:
    return any(
        isinstance(event, ProjectSyncPendingEvent)
        and event.batch_id == batch_id
        and event.candidate_digest == candidate_digest
        for event in events
    )


def _record_project_sources(change_dir: Path, recorder: TraceSourceRecorder) -> None:
    root = _project_root(change_dir)
    _record_path(recorder, PROJECT_LEDGER_SOURCE, root / PROJECT_LEDGER_SOURCE)
    _record_path(recorder, PROJECT_PROBLEMS_SOURCE, root / PROJECT_PROBLEMS_SOURCE)


def _unavailable(
    recorder: TraceSourceRecorder,
    batch_id: str,
    err: AuthorityValidationError,
) -> AuthorityPrefixResult:
    return AuthorityPrefixResult(
        state="unavailable",
        sources=recorder.freeze(),
        gaps=(
            authority_gap(
                "issue_reconciliation_unavailable",
                err.source,
                batch_id,
                err.reason,
            ),
        ),
        validated=None,
    )


def validate_issue_authority_prefix(
    change_dir: Path,
    change_id: str,
    batch_id: str,
) -> AuthorityPrefixResult:
    """Validate manifest→ledger prefix and classify current recovery/completed state."""
    recorder = TraceSourceRecorder()
    # Ensure declared sources always appear even when earlier steps fail.
    for source in _ISSUE_SOURCES:
        if source.startswith("qa/"):
            _record_path(recorder, source, _project_root(change_dir) / source)
        else:
            _record_path(recorder, source, change_dir / source)

    try:
        manifest, manifest_digest = _validate_manifest(change_dir, change_id, batch_id, recorder)
        candidates, candidate_digest = _validate_candidates(
            change_dir, change_id, batch_id, manifest_digest, recorder
        )
        observations = _validate_observations_identity(change_dir, change_id, batch_id, recorder)
        events = _validate_change_ledger(change_dir, recorder)
        replayed = _assert_observations_replay(observations, events, batch_id)
    except AuthorityValidationError as err:
        return _unavailable(recorder, batch_id, err)

    candidate_count = len(candidates.candidates)

    # Recovery: analysis failed (masks reconcile/project).
    if _current_analysis_failed(events, replayed, batch_id):
        try:
            _require_persisted_snapshot(
                change_dir,
                change_id,
                batch_id,
                replayed,
                recorder,
                require_authoritative_batch=True,
            )
            _validate_failed_analysis_chain(
                replayed,
                change_id,
                batch_id,
                manifest_digest,
                candidate_digest,
                candidate_count,
            )
        except AuthorityValidationError as err:
            # Re-record reconcile/project existence facts after snapshot work.
            _load_reconcile_status(change_dir, recorder)
            _record_project_sources(change_dir, recorder)
            return _unavailable(recorder, batch_id, err)
        _load_reconcile_status(change_dir, recorder)
        _record_project_sources(change_dir, recorder)
        return AuthorityPrefixResult(
            state="analysis_failed",
            sources=recorder.freeze(),
            gaps=(
                authority_gap(
                    "issue_analysis_failed",
                    SNAPSHOT_SOURCE,
                    batch_id,
                    "status_inconsistent",
                ),
            ),
            validated=None,
        )

    # Inspect reconcile status for failed / pending recovery before completed snapshot rules.
    try:
        status = _require_reconcile_status(change_dir, recorder)
        _validate_reconcile_identity(status, change_id, batch_id, manifest_digest, candidate_digest)
    except AuthorityValidationError as err:
        _load_persisted_snapshot(change_dir, recorder)
        _record_project_sources(change_dir, recorder)
        return _unavailable(recorder, batch_id, err)

    assert isinstance(status, IssueReconcileStatusV2)

    if status.status == "failed":
        _load_persisted_snapshot(change_dir, recorder)
        _record_project_sources(change_dir, recorder)
        return AuthorityPrefixResult(
            state="reconcile_failed",
            sources=recorder.freeze(),
            gaps=(
                authority_gap(
                    "issue_reconcile_failed",
                    RECONCILE_SOURCE,
                    batch_id,
                    "status_inconsistent",
                ),
            ),
            validated=None,
        )

    if status.status == "pending":
        if not _current_project_sync_pending(events, batch_id, candidate_digest):
            _load_persisted_snapshot(change_dir, recorder)
            _record_project_sources(change_dir, recorder)
            return _unavailable(
                recorder,
                batch_id,
                AuthorityValidationError(RECONCILE_SOURCE, "recovery_event_missing"),
            )
        # Pending masks snapshot authoritative-batch mismatch / project gaps.
        _load_persisted_snapshot(change_dir, recorder)
        _record_project_sources(change_dir, recorder)
        return AuthorityPrefixResult(
            state="project_sync_pending",
            sources=recorder.freeze(),
            gaps=(
                authority_gap(
                    "project_sync_pending",
                    RECONCILE_SOURCE,
                    batch_id,
                    "status_inconsistent",
                ),
            ),
            validated=None,
        )

    # Completed branch (project membership deferred to Task 10).
    try:
        _require_persisted_snapshot(
            change_dir,
            change_id,
            batch_id,
            replayed,
            recorder,
            require_authoritative_batch=True,
        )
        _validate_completed_analysis_chain(
            replayed,
            change_id,
            batch_id,
            manifest_digest,
            candidate_digest,
            candidate_count,
        )
        if status.occurrence_count != candidate_count:
            raise AuthorityValidationError(RECONCILE_SOURCE, "occurrence_count_mismatch")
        if replayed.project_sync_status != "completed":
            raise AuthorityValidationError(SNAPSHOT_SOURCE, "status_inconsistent")
    except AuthorityValidationError as err:
        _record_project_sources(change_dir, recorder)
        # Dedicated snapshot missing/identity codes per §7.2.
        if err.source == SNAPSHOT_SOURCE and err.reason == "missing":
            return AuthorityPrefixResult(
                state="unavailable",
                sources=recorder.freeze(),
                gaps=(
                    authority_gap(
                        "issues_snapshot_missing",
                        SNAPSHOT_SOURCE,
                        batch_id,
                        "missing",
                    ),
                ),
                validated=None,
            )
        if err.source == SNAPSHOT_SOURCE and err.reason == "malformed":
            return AuthorityPrefixResult(
                state="unavailable",
                sources=recorder.freeze(),
                gaps=(
                    authority_gap(
                        "issues_snapshot_missing",
                        SNAPSHOT_SOURCE,
                        batch_id,
                        "malformed",
                    ),
                ),
                validated=None,
            )
        if err.source == SNAPSHOT_SOURCE and err.reason == "change_id_mismatch":
            return AuthorityPrefixResult(
                state="unavailable",
                sources=recorder.freeze(),
                gaps=(
                    authority_gap(
                        "issues_snapshot_identity_mismatch",
                        SNAPSHOT_SOURCE,
                        batch_id,
                        "change_id_mismatch",
                    ),
                ),
                validated=None,
            )
        if err.source == SNAPSHOT_SOURCE and err.reason == "batch_id_mismatch":
            return AuthorityPrefixResult(
                state="unavailable",
                sources=recorder.freeze(),
                gaps=(
                    authority_gap(
                        "issues_snapshot_identity_mismatch",
                        SNAPSHOT_SOURCE,
                        batch_id,
                        "batch_id_mismatch",
                    ),
                ),
                validated=None,
            )
        return _unavailable(recorder, batch_id, err)

    _record_project_sources(change_dir, recorder)
    return AuthorityPrefixResult(
        state="completed",
        sources=recorder.freeze(),
        gaps=(),
        validated=ValidatedAuthorityPrefix(
            manifest_digest=manifest_digest,
            candidate_digest=candidate_digest,
            manifest=manifest,
            candidates=candidates,
            observations=observations,
            change_events=events,
            replayed_snapshot=replayed,
            reconcile_status=status,
        ),
    )
