"""Issue-domain operation handlers registered in default_operations().

Currently contains:
    collect_observations_operation     — operation:collect-observations
    record_empty_issue_analysis        — operation:record-empty-issue-analysis
    record_issue_analysis_failure      — operation:record-issue-analysis-failure
    record_project_sync_pending        — operation:record-project-sync-pending

All three recovery operations are deterministic (no LLM), idempotent, and
pin evidence/candidate digests so the workflow continues visibly even when
the analyzer is unavailable or the project sync resource is contended.

Recovery operations read error context from ``task.recovery`` (production path,
populated by the planner after retry exhaustion) and fall back to
``task.input["error_kind"]`` / ``task.input["message"]`` for unit-test
portability where no RecoveryContext is provided.

Error-kind → analysis failure reason mapping (for record-issue-analysis-failure):
    transport     → unavailable   (model/provider unreachable)
    timeout       → timeout
    rate_limit    → transport     (throttle is a transport-layer concern)
    invalid_output → invalid_output
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from assurance_agent.artifacts.models.issues import (
    ChangeIssueSnapshot,
    IssueCandidateDocument,
    IssueAnalysisStatus,
    ObservationDocument,
)
from assurance_agent.workflow.execution.evidence import EvidenceError
from assurance_agent.workflow.graph.models import ExecutableTask, RuntimeContext, TaskResult
from assurance_agent.workflow.graph.task_runner import task_failure
from assurance_agent.workflow.graph.workspace import TaskWorkspace
from assurance_agent.workflow.issues.collector import collect_observations
from assurance_agent.workflow.issues.events import (
    IssueAnalysisFailedEvent,
    ObservationRecordedEvent,
    ProjectSyncPendingEvent,
)
from assurance_agent.workflow.issues.ledger import ChangeIssueStore
from assurance_agent.workflow.issues.projection import dump_projection


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utc_now() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _event_id(idempotency_key: str) -> str:
    """Deterministic event ID derived from the idempotency key."""
    return "EVT-" + hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:16]


def _write_json(path: Path, data: bytes) -> None:
    """Write bytes to path, creating parent directories as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _canonical_json(model_dict: object) -> bytes:
    """Canonical JSON bytes with sorted keys, compact separators, trailing newline."""
    return (
        json.dumps(model_dict, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\n"
    ).encode("utf-8")


# ---------------------------------------------------------------------------
# collect_observations_operation
# ---------------------------------------------------------------------------


def collect_observations_operation(
    task: ExecutableTask,
    workspace: TaskWorkspace,
    context: RuntimeContext,
) -> TaskResult:
    """Collect immutable Observations from the authoritative execution batch.

    Reads ``execution/execution-manifest.yaml`` (NOT mtime) to identify the
    authoritative batch, loads per-target result files, and emits
    ``ObservationRecordedEvent`` entries into the Change Issue Ledger.

    Idempotent: the ``ChangeIssueStore`` skips events whose
    ``idempotency_key`` is already present in the JSONL ledger; the manifest
    and observations files are overwritten with the same canonical content.

    Raises hard ``invalid_input`` on missing/corrupt execution evidence so the
    caller knows collection is blocked, not just degraded.
    """
    # ------------------------------------------------------------------
    # 1. Collect observations (pure, no LLM)
    # ------------------------------------------------------------------
    try:
        result = collect_observations(workspace.change_dir, context.change_id)
    except EvidenceError as err:
        return task_failure("invalid_input", str(err))
    except Exception as err:  # noqa: BLE001
        return task_failure("invalid_output", f"observation collection failed: {err}")

    # ------------------------------------------------------------------
    # 2. Write inspect/observations.json
    # ------------------------------------------------------------------
    inspect_dir = workspace.change_dir / "inspect"
    obs_doc = ObservationDocument(
        schema_version="1.0",
        change_id=context.change_id,
        batch_id=result.batch_id,
        observations=list(result.observations),
    )
    _write_json(
        inspect_dir / "observations.json",
        _canonical_json(obs_doc.model_dump(mode="json")),
    )

    # ------------------------------------------------------------------
    # 3. Write inspect/issue-evidence-manifest.json
    # ------------------------------------------------------------------
    _write_json(
        inspect_dir / "issue-evidence-manifest.json",
        _canonical_json(result.manifest.model_dump(mode="json")),
    )

    # ------------------------------------------------------------------
    # 4. Append observation_recorded events + rebuild snapshot
    # ------------------------------------------------------------------
    ts = _utc_now()
    events: list[ObservationRecordedEvent] = []

    for obs in result.observations:
        idem_key = (
            f"observation_recorded:{context.change_id}:{result.batch_id}:{obs.observation_id}"
        )
        events.append(
            ObservationRecordedEvent(
                schema_version="1.0",
                # seq is overwritten by ChangeIssueStore._serialize_event; any ≥1 is valid.
                seq=1,
                event_id=_event_id(idem_key),
                idempotency_key=idem_key,
                ts=ts,
                evidence_digest=result.evidence_bundle_digest,
                change_id=context.change_id,
                batch_id=result.batch_id,
                type="observation_recorded",
                observation=obs,
            )
        )

    issues_dir = workspace.change_dir / "issues"
    issues_dir.mkdir(parents=True, exist_ok=True)

    if events:
        store = ChangeIssueStore(workspace.change_dir)
        store.append_and_rebuild(events)
    else:
        # Clean batch — no abnormal observations; write a minimal valid snapshot.
        # ChangeIssueStore.append_and_rebuild requires ≥1 event, so we write
        # the snapshot directly for the empty case.
        snapshot = ChangeIssueSnapshot(
            schema_version="1.0",
            change_id=context.change_id,
            authoritative_batch_id=result.batch_id,
            observations=[],
            occurrences=[],
            analysis_status=None,
            project_sync_status="completed",
            batches=[result.batch_id],
        )
        _write_json(
            issues_dir / "snapshot.json",
            dump_projection(snapshot),
        )

    # ------------------------------------------------------------------
    # 5. Return task result
    # ------------------------------------------------------------------
    return TaskResult(
        status="succeeded",
        value={
            "batch_id": result.batch_id,
            "evidence_bundle_digest": result.evidence_bundle_digest,
            "abnormal_count": len(result.observations),
        },
    )


# ---------------------------------------------------------------------------
# Shared helpers for recovery operations
# ---------------------------------------------------------------------------


def _read_evidence_manifest_info(change_dir: Path) -> tuple[str, str] | None:
    """Return (batch_id, evidence_bundle_digest) from inspect/issue-evidence-manifest.json."""
    manifest_path = change_dir / "inspect" / "issue-evidence-manifest.json"
    if not manifest_path.is_file():
        return None
    try:
        doc = json.loads(manifest_path.read_text(encoding="utf-8"))
        batch_id = doc.get("batch_id")
        digest = doc.get("digest")
        if isinstance(batch_id, str) and batch_id and isinstance(digest, str) and digest:
            return (batch_id, digest)
    except (OSError, ValueError):
        pass
    return None


def _read_candidates_digest(change_dir: Path) -> str | None:
    """Return SHA-256 digest of inspect/issue-candidates.json if it exists."""
    candidates_path = change_dir / "inspect" / "issue-candidates.json"
    if not candidates_path.is_file():
        return None
    return "sha256:" + hashlib.sha256(candidates_path.read_bytes()).hexdigest()


_ERROR_KIND_TO_ANALYSIS_REASON: dict[str, str] = {
    "transport": "unavailable",
    "timeout": "timeout",
    "rate_limit": "transport",
    "invalid_output": "invalid_output",
}


def _error_info_from_task(task: ExecutableTask) -> tuple[str, str]:
    """Return (error_kind, message) preferring task.recovery; falls back to task.input."""
    if task.recovery is not None:
        return task.recovery.error_kind, task.recovery.message
    raw_input = task.input if isinstance(task.input, dict) else {}
    assert isinstance(raw_input, dict)
    error_kind = str(raw_input.get("error_kind", "transport"))
    message = str(raw_input.get("message", "issue analysis failed"))
    return error_kind, message


def _empty_candidate_doc(change_id: str, batch_id: str, evidence_bundle_digest: str) -> bytes:
    """Canonical JSON bytes for an empty IssueCandidateDocument."""
    doc = IssueCandidateDocument(
        schema_version="1.0",
        change_id=change_id,
        batch_id=batch_id,
        evidence_bundle_digest=evidence_bundle_digest,
        candidates=[],
    )
    return _canonical_json(doc.model_dump(mode="json"))


# ---------------------------------------------------------------------------
# record_empty_issue_analysis_operation
# ---------------------------------------------------------------------------


def record_empty_issue_analysis_operation(
    task: ExecutableTask,
    workspace: TaskWorkspace,
    context: RuntimeContext,
) -> TaskResult:
    """Write a completed, zero-candidate analysis status for clean execution batches.

    Idempotent: writing the same empty document twice is a no-op in effect.
    Pins the evidence_bundle_digest from ``inspect/issue-evidence-manifest.json``.
    """
    manifest_info = _read_evidence_manifest_info(workspace.change_dir)
    if manifest_info is None:
        return task_failure(
            "invalid_input",
            "record-empty-issue-analysis: inspect/issue-evidence-manifest.json not found "
            "or missing batch_id/digest fields",
        )
    batch_id, evidence_bundle_digest = manifest_info

    inspect_dir = workspace.change_dir / "inspect"
    inspect_dir.mkdir(parents=True, exist_ok=True)

    # Write empty candidates document.
    candidates_bytes = _empty_candidate_doc(context.change_id, batch_id, evidence_bundle_digest)
    _write_json(inspect_dir / "issue-candidates.json", candidates_bytes)

    candidate_digest = "sha256:" + hashlib.sha256(candidates_bytes).hexdigest()

    # Write completed analysis status.
    analysis_status = IssueAnalysisStatus(
        schema_version="1.0",
        change_id=context.change_id,
        batch_id=batch_id,
        status="completed",
        evidence_bundle_digest=evidence_bundle_digest,
        candidate_count=0,
        candidate_digest=candidate_digest,
    )
    _write_json(
        inspect_dir / "issue-analysis-status.json",
        _canonical_json(analysis_status.model_dump(mode="json")),
    )

    return TaskResult(
        status="succeeded",
        value={
            "batch_id": batch_id,
            "evidence_bundle_digest": evidence_bundle_digest,
            "candidate_count": 0,
            "candidate_digest": candidate_digest,
        },
    )


# ---------------------------------------------------------------------------
# record_issue_analysis_failure_operation
# ---------------------------------------------------------------------------


def record_issue_analysis_failure_operation(
    task: ExecutableTask,
    workspace: TaskWorkspace,
    context: RuntimeContext,
) -> TaskResult:
    """Record a failed analysis: write empty candidates + failed status + append ledger event.

    Uses RecoveryContext from ``task.recovery`` in production.  Falls back to
    ``task.input["error_kind"]`` / ``task.input["message"]`` for unit tests.

    Error-kind → analysis reason mapping:
        transport      → unavailable
        timeout        → timeout
        rate_limit     → transport
        invalid_output → invalid_output

    Idempotent: the ChangeIssueStore deduplicates on idempotency_key so
    replaying this operation does not append duplicate events.
    """
    manifest_info = _read_evidence_manifest_info(workspace.change_dir)
    if manifest_info is None:
        return task_failure(
            "invalid_input",
            "record-issue-analysis-failure: inspect/issue-evidence-manifest.json not found "
            "or missing batch_id/digest fields",
        )
    batch_id, evidence_bundle_digest = manifest_info

    error_kind, message = _error_info_from_task(task)
    analysis_reason = _ERROR_KIND_TO_ANALYSIS_REASON.get(error_kind, "invalid_output")

    inspect_dir = workspace.change_dir / "inspect"
    inspect_dir.mkdir(parents=True, exist_ok=True)

    # Write empty candidates document.
    candidates_bytes = _empty_candidate_doc(context.change_id, batch_id, evidence_bundle_digest)
    _write_json(inspect_dir / "issue-candidates.json", candidates_bytes)

    candidate_digest = "sha256:" + hashlib.sha256(candidates_bytes).hexdigest()

    # Write failed analysis status.
    analysis_status = IssueAnalysisStatus(
        schema_version="1.0",
        change_id=context.change_id,
        batch_id=batch_id,
        status="failed",
        evidence_bundle_digest=evidence_bundle_digest,
        candidate_count=0,
        reason=analysis_reason,  # type: ignore[arg-type]
        retryable=True,
        candidate_digest=candidate_digest,
    )
    _write_json(
        inspect_dir / "issue-analysis-status.json",
        _canonical_json(analysis_status.model_dump(mode="json")),
    )

    # Append issue_analysis_failed event to the Change Ledger (idempotent).
    ts = _utc_now()
    idem_key = (
        f"issue_analysis_failed:{context.change_id}:{batch_id}:{evidence_bundle_digest}"
    )
    failed_event = IssueAnalysisFailedEvent(
        schema_version="1.0",
        seq=1,
        event_id=_event_id(idem_key),
        idempotency_key=idem_key,
        ts=ts,
        evidence_digest=evidence_bundle_digest,
        change_id=context.change_id,
        batch_id=batch_id,
        type="issue_analysis_failed",
        analysis_status=analysis_status,
    )

    issues_dir = workspace.change_dir / "issues"
    issues_dir.mkdir(parents=True, exist_ok=True)
    store = ChangeIssueStore(workspace.change_dir)
    store.append_and_rebuild([failed_event])

    return TaskResult(
        status="succeeded",
        value={
            "batch_id": batch_id,
            "evidence_bundle_digest": evidence_bundle_digest,
            "analysis_reason": analysis_reason,
            "error_kind": error_kind,
            "message": message,
            "candidate_digest": candidate_digest,
        },
    )


# ---------------------------------------------------------------------------
# record_project_sync_pending_operation
# ---------------------------------------------------------------------------


def record_project_sync_pending_operation(
    task: ExecutableTask,
    workspace: TaskWorkspace,
    context: RuntimeContext,
) -> TaskResult:
    """Append a project_sync_pending event indicating reconcile must be retried.

    Reads inspect/issue-candidates.json to pin the candidate digest.
    Idempotent: the ChangeIssueStore deduplicates on idempotency_key.
    """
    manifest_info = _read_evidence_manifest_info(workspace.change_dir)
    if manifest_info is None:
        return task_failure(
            "invalid_input",
            "record-project-sync-pending: inspect/issue-evidence-manifest.json not found "
            "or missing batch_id/digest fields",
        )
    batch_id, evidence_bundle_digest = manifest_info

    # Pin the candidate digest from whatever the analyzer produced.
    candidate_digest = _read_candidates_digest(workspace.change_dir)
    if candidate_digest is None:
        # Candidates file absent — use evidence digest as fallback pin.
        candidate_digest = evidence_bundle_digest

    ts = _utc_now()
    idem_key = (
        f"project_sync_pending:{context.change_id}:{batch_id}:{candidate_digest}"
    )
    sync_event = ProjectSyncPendingEvent(
        schema_version="1.0",
        seq=1,
        event_id=_event_id(idem_key),
        idempotency_key=idem_key,
        ts=ts,
        evidence_digest=evidence_bundle_digest,
        change_id=context.change_id,
        batch_id=batch_id,
        type="project_sync_pending",
        candidate_digest=candidate_digest,
    )

    issues_dir = workspace.change_dir / "issues"
    issues_dir.mkdir(parents=True, exist_ok=True)
    store = ChangeIssueStore(workspace.change_dir)
    store.append_and_rebuild([sync_event])

    return TaskResult(
        status="succeeded",
        value={
            "batch_id": batch_id,
            "evidence_bundle_digest": evidence_bundle_digest,
            "candidate_digest": candidate_digest,
        },
    )
