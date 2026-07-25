"""Issue-domain operation handlers registered in default_operations().

Currently contains:
    collect_observations_operation     — operation:collect-observations
    record_empty_issue_analysis        — operation:record-empty-issue-analysis
    record_issue_analysis_failure      — operation:record-issue-analysis-failure
    record_project_sync_pending        — operation:record-project-sync-pending
    reconcile_issues_operation         — operation:reconcile-issues
    load_problem_review_context_op     — operation:load-problem-review-context
    apply_problem_review_operation     — operation:apply-problem-review

All recovery operations are deterministic (no LLM), idempotent, and
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
    IssueEvidenceManifest,
    IssueReconcileStatus,
    ObservationDocument,
    ProblemProjection,
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
from assurance_agent.workflow.issues.ledger import ChangeIssueStore, ProjectProblemStore
from assurance_agent.workflow.issues.projection import dump_projection
from assurance_agent.workflow.issues.reconciler import (
    ReconciliationValidationError,
    plan_reconciliation,
)
from assurance_agent.workflow.issues.review import (
    ReviewContextError,
    ReviewValidationError,
    build_problem_review_context,
    validate_review_action,
)


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
    return (json.dumps(model_dict, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode(
        "utf-8"
    )


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
        idem_key = f"observation_recorded:{context.change_id}:{result.batch_id}:{obs.observation_id}"
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
    idem_key = f"issue_analysis_failed:{context.change_id}:{batch_id}:{evidence_bundle_digest}"
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
    idem_key = f"project_sync_pending:{context.change_id}:{batch_id}:{candidate_digest}"
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


# ---------------------------------------------------------------------------
# reconcile_issues_operation
# ---------------------------------------------------------------------------


def _load_json_model(path: Path, model_cls, context_label: str):
    """Load and validate a JSON file into a Pydantic model; raise TaskResult failure on error."""
    if not path.is_file():
        raise FileNotFoundError(f"{context_label}: {path} not found")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return model_cls.model_validate(data)
    except Exception as exc:
        raise ValueError(f"{context_label}: invalid JSON/model at {path}: {exc}") from exc


def _load_problem_projection(workspace_root: Path) -> ProblemProjection:
    """Load the project problem projection from the (synchronized) workspace root.

    Returns an empty projection when the file does not exist yet
    (first ever reconcile for this project).
    """
    problems_path = workspace_root / "qa" / "issues" / "problems.json"
    if not problems_path.is_file():
        return ProblemProjection(
            schema_version="1.0",
            generated_at="1970-01-01T00:00:00Z",
            problems=[],
        )
    try:
        data = json.loads(problems_path.read_text(encoding="utf-8"))
        return ProblemProjection.model_validate(data)
    except Exception as exc:
        raise ValueError(
            f"reconcile-issues: corrupt project problem projection at {problems_path}: {exc}"
        ) from exc


def reconcile_issues_operation(
    task: ExecutableTask,
    workspace: TaskWorkspace,
    context: RuntimeContext,
) -> TaskResult:
    """Reconcile a completed candidate batch into Occurrences and Problems.

    Reads (all from workspace):
        change:inspect/issue-candidates.json
        change:inspect/issue-evidence-manifest.json  (trusted boundary)
        change:inspect/observations.json
        change:issues/snapshot.json   (optional — missing means empty snapshot)
        project:qa/issues/problems.json (synchronized; optional — missing means empty)

    Trusted-input checks (against runtime change_id + evidence manifest):
        candidates/observations change_id, batch_id, and evidence_bundle_digest
        must match the collector-owned manifest before any ledger writes.

    Writes on success (atomic, same task write-set):
        change:inspect/issue-reconcile-status.json  (status: completed)
        change:issues/events.jsonl
        change:issues/snapshot.json
        project:qa/issues/events.jsonl
        project:qa/issues/problems.json
        project:qa/issues/review-queue.json

    Writes on semantic validation failure (all-or-nothing):
        change:inspect/issue-reconcile-status.json  (status: failed)
        Returns TaskResult SUCCESS so the workflow continues visibly.

    Infrastructure / ledger integrity failures propagate as typed task failures.
    """
    change_dir = workspace.change_dir
    project_root = workspace.project_root
    change_id = context.change_id
    inspect_dir = change_dir / "inspect"
    inspect_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # 1. Load candidates document
    # ------------------------------------------------------------------
    candidates_path = inspect_dir / "issue-candidates.json"
    try:
        candidates_doc = _load_json_model(candidates_path, IssueCandidateDocument, "reconcile-issues")
    except (FileNotFoundError, ValueError) as exc:
        return task_failure("invalid_input", str(exc))

    # ------------------------------------------------------------------
    # 2. Load observations document
    # ------------------------------------------------------------------
    observations_path = inspect_dir / "observations.json"
    try:
        observations = _load_json_model(observations_path, ObservationDocument, "reconcile-issues")
    except (FileNotFoundError, ValueError) as exc:
        return task_failure("invalid_input", str(exc))

    # ------------------------------------------------------------------
    # 2b. Load trusted evidence manifest (collector-owned boundary)
    # ------------------------------------------------------------------
    manifest_path = inspect_dir / "issue-evidence-manifest.json"
    try:
        evidence_manifest = _load_json_model(manifest_path, IssueEvidenceManifest, "reconcile-issues")
    except (FileNotFoundError, ValueError) as exc:
        return task_failure("invalid_input", str(exc))

    # ------------------------------------------------------------------
    # 3. Load change issue snapshot (optional)
    # ------------------------------------------------------------------
    snapshot_path = change_dir / "issues" / "snapshot.json"
    if snapshot_path.is_file():
        try:
            change_snapshot_data = json.loads(snapshot_path.read_text(encoding="utf-8"))
            change_snapshot = ChangeIssueSnapshot.model_validate(change_snapshot_data)
        except Exception as exc:
            return task_failure(
                "invalid_input",
                f"reconcile-issues: corrupt change issue snapshot: {exc}",
            )
    else:
        change_snapshot = ChangeIssueSnapshot(
            schema_version="1.0",
            change_id=change_id,
            authoritative_batch_id=candidates_doc.batch_id,
            observations=[],
            occurrences=[],
            analysis_status=None,
            project_sync_status="completed",
            batches=[candidates_doc.batch_id],
        )

    # ------------------------------------------------------------------
    # 4. Load project problem projection from synchronized workspace root
    # ------------------------------------------------------------------
    try:
        problems = _load_problem_projection(project_root)
    except ValueError as exc:
        return task_failure("invalid_input", str(exc))

    # ------------------------------------------------------------------
    # 5. Run full semantic validation + event derivation
    # ------------------------------------------------------------------
    batch_id = candidates_doc.batch_id
    evidence_bundle_digest = candidates_doc.evidence_bundle_digest

    try:
        plan = plan_reconciliation(
            candidates_doc,
            observations,
            change_snapshot,
            problems,
            manifest=evidence_manifest,
            expected_change_id=change_id,
        )
    except ReconciliationValidationError as exc:
        # Semantic failure: write failed reconcile-status; return success
        reconcile_status = IssueReconcileStatus(
            schema_version="1.0",
            change_id=change_id,
            batch_id=batch_id,
            status="failed",
            evidence_bundle_digest=evidence_bundle_digest,
            error=str(exc),
        )
        _write_json(
            inspect_dir / "issue-reconcile-status.json",
            _canonical_json(reconcile_status.model_dump(mode="json")),
        )
        return TaskResult(
            status="succeeded",
            value={
                "reconcile_status": "failed",
                "batch_id": batch_id,
                "error": str(exc),
            },
        )

    # ------------------------------------------------------------------
    # 6. Append + rebuild both stores in the same task write-set
    # ------------------------------------------------------------------
    try:
        change_store = ChangeIssueStore(change_dir)
        change_store.append_and_rebuild(list(plan.change_events))
    except Exception as exc:
        return task_failure("invalid_output", f"reconcile-issues: change ledger write failed: {exc}")

    if plan.problem_events:
        try:
            problem_store = ProjectProblemStore(project_root)
            problem_store.append_and_rebuild(list(plan.problem_events))
        except Exception as exc:
            return task_failure(
                "invalid_output",
                f"reconcile-issues: project problem ledger write failed: {exc}",
            )

    # ------------------------------------------------------------------
    # 7. Write completed reconcile status
    # ------------------------------------------------------------------
    reconcile_status = IssueReconcileStatus(
        schema_version="1.0",
        change_id=change_id,
        batch_id=batch_id,
        status="completed",
        evidence_bundle_digest=evidence_bundle_digest,
        candidate_digest=plan.candidate_digest,
        occurrence_count=plan.occurrence_count,
    )
    _write_json(
        inspect_dir / "issue-reconcile-status.json",
        _canonical_json(reconcile_status.model_dump(mode="json")),
    )

    return TaskResult(
        status="succeeded",
        value={
            "reconcile_status": "completed",
            "batch_id": batch_id,
            "occurrence_count": plan.occurrence_count,
            "candidate_digest": plan.candidate_digest,
        },
    )


# ---------------------------------------------------------------------------
# load_problem_review_context_operation
# ---------------------------------------------------------------------------


def load_problem_review_context_operation(
    task: ExecutableTask,
    workspace: TaskWorkspace,
    context: RuntimeContext,
) -> TaskResult:
    """Write a non-canonical review context to change:issue-review/<review_id>/context.json.

    Reads:
        project:qa/issues/problems.json   (from workspace project root)

    Writes:
        change:issue-review/<review_id>/context.json

    The context document is display-only and non-canonical; it is rebuilt each
    time this operation runs.  Advice from the triage-advisor is written
    alongside by skill:aa-issue-triage-advisor and is not read here.

    Idempotent: writing the same problem version twice produces identical bytes.

    Parameters (from context.params):
        problem_id:  ID of the Problem to build context for (required, non-empty).
        review_id:   Unique review session identifier (required, non-empty).
    """
    problem_id = context.params.get("problem_id")
    review_id = context.params.get("review_id")
    if not isinstance(problem_id, str) or not problem_id.strip():
        return task_failure("invalid_input", "load-problem-review-context: problem_id param is required")
    if not isinstance(review_id, str) or not review_id.strip():
        return task_failure("invalid_input", "load-problem-review-context: review_id param is required")
    problem_id = problem_id.strip()
    review_id = review_id.strip()

    # Load the project problem projection.
    try:
        problems = _load_problem_projection(workspace.project_root)
    except ValueError as exc:
        return task_failure("invalid_input", str(exc))

    # Build the review context.
    try:
        review_ctx = build_problem_review_context(problem_id, problems)
    except ReviewContextError as exc:
        return task_failure("invalid_input", f"load-problem-review-context: {exc}")

    # Write context to change:issue-review/<review_id>/context.json
    context_doc: dict[str, object] = {
        "problem_id": review_ctx.problem_id,
        "expected_problem_version": review_ctx.expected_problem_version,
        "problem_status": review_ctx.problem_status,
        "problem_title": review_ctx.problem_title,
        "canonical_alias": review_ctx.canonical_alias,
        "occurrence_ids": list(review_ctx.occurrence_ids),
        "problem_digest": review_ctx.problem_digest,
        "review_id": review_id,
        "generated_at": _utc_now(),
    }
    review_dir = workspace.change_dir / "issue-review" / review_id
    review_dir.mkdir(parents=True, exist_ok=True)
    _write_json(review_dir / "context.json", _canonical_json(context_doc))

    return TaskResult(
        status="succeeded",
        value={
            "problem_id": problem_id,
            "review_id": review_id,
            "problem_version": review_ctx.expected_problem_version,
            "problem_status": review_ctx.problem_status,
        },
    )


# ---------------------------------------------------------------------------
# apply_problem_review_operation
# ---------------------------------------------------------------------------


def _read_resume_event(change_dir: Path, invocation_id: str) -> dict[str, object] | None:
    """Find the graph_resumed event for the given invocation_id in events.jsonl."""
    from assurance_agent.workflow.core.events import read_events_strict

    try:
        events = read_events_strict(change_dir)
    except Exception:
        return None
    # Find the most recent graph_resumed event for this invocation.
    for event in reversed(events):
        if (
            isinstance(event, dict)
            and event.get("type") == "graph_resumed"
            and event.get("invocation_id") == invocation_id
        ):
            return event
    return None


def apply_problem_review_operation(
    task: ExecutableTask,
    workspace: TaskWorkspace,
    context: RuntimeContext,
) -> TaskResult:
    """Apply a human review decision to the project Problem ledger.

    Reads (synchronized workspace overlay):
        change:issue-review/<review_id>/context.json
        project:qa/issues/problems.json
        project:qa/issues/events.jsonl
        project:qa/issues/review-queue.json
        change_dir/events.jsonl            (to find graph_resumed event)

    Writes:
        project:qa/issues/events.jsonl     (appended, synchronized)
        project:qa/issues/problems.json    (rebuilt)
        project:qa/issues/review-queue.json (rebuilt)
        change:issue-review/<review_id>/apply-receipt.json

    Contract: synchronized [project:qa/issues/**] + exclusive [project:issue-registry].
    The operation reloads the problem projection after lock acquisition, so the
    expected_problem_version in the context must still match at apply time.

    Idempotent: the ProjectProblemStore deduplicates on idempotency_key.

    Parameters (from context.params):
        problem_id:  Problem to apply the action to.
        review_id:   Unique review session identifier.
    """
    problem_id = context.params.get("problem_id")
    review_id = context.params.get("review_id")
    if not isinstance(problem_id, str) or not problem_id.strip():
        return task_failure("invalid_input", "apply-problem-review: problem_id param is required")
    if not isinstance(review_id, str) or not review_id.strip():
        return task_failure("invalid_input", "apply-problem-review: review_id param is required")
    problem_id = problem_id.strip()
    review_id = review_id.strip()

    # ------------------------------------------------------------------
    # 1. Read the review context written by load-problem-review-context
    # ------------------------------------------------------------------
    review_dir = workspace.change_dir / "issue-review" / review_id
    context_path = review_dir / "context.json"
    if not context_path.is_file():
        return task_failure(
            "invalid_input",
            f"apply-problem-review: review context not found at {context_path}; "
            "run load-problem-review-context first",
        )
    try:
        saved_ctx = json.loads(context_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return task_failure("invalid_input", f"apply-problem-review: corrupt context.json: {exc}")

    saved_version = saved_ctx.get("expected_problem_version")

    # ------------------------------------------------------------------
    # 2. Find the graph_resumed event for this invocation
    # ------------------------------------------------------------------
    resumed = _read_resume_event(context.change_dir, task.invocation_id)
    if resumed is None:
        return task_failure(
            "invalid_input",
            f"apply-problem-review: no graph_resumed event found for invocation "
            f"{task.invocation_id}; interrupt must be resolved before apply",
        )
    action = str(resumed.get("action", ""))
    reason = str(resumed.get("reason", ""))
    who = str(resumed.get("who", "unknown"))
    raw_payload = resumed.get("payload")
    payload: dict[str, object] = dict(raw_payload) if isinstance(raw_payload, dict) else {}

    if not action:
        return task_failure("invalid_input", "apply-problem-review: graph_resumed has no action")

    # ------------------------------------------------------------------
    # 3. Reload synchronized project problem projection
    # ------------------------------------------------------------------
    try:
        problems = _load_problem_projection(workspace.project_root)
    except ValueError as exc:
        return task_failure("invalid_input", str(exc))

    # ------------------------------------------------------------------
    # 4. Build (or rebuild) review context from live projection
    # ------------------------------------------------------------------
    try:
        review_ctx = build_problem_review_context(problem_id, problems)
    except ReviewContextError as exc:
        return task_failure("invalid_input", f"apply-problem-review: {exc}")

    # Check that the saved version still matches (stale context detection).
    if review_ctx.expected_problem_version != saved_version:
        return task_failure(
            "invalid_input",
            f"apply-problem-review: stale review context — problem {problem_id!r} is "
            f"now at version {review_ctx.expected_problem_version} but context was "
            f"built at version {saved_version}",
        )

    # ------------------------------------------------------------------
    # 5. Validate the action and obtain typed events
    # ------------------------------------------------------------------
    try:
        problem_events = validate_review_action(
            review_ctx,
            action,
            payload,
            reason,
            who,
            projection=problems,
        )
    except ReviewValidationError as exc:
        return task_failure("invalid_input", f"apply-problem-review: {exc}")

    # ------------------------------------------------------------------
    # 6. Append events to project problem ledger (synchronized)
    # ------------------------------------------------------------------
    try:
        store = ProjectProblemStore(workspace.project_root)
        store.append_and_rebuild(list(problem_events))
    except Exception as exc:
        return task_failure(
            "invalid_output",
            f"apply-problem-review: project problem ledger write failed: {exc}",
        )

    # ------------------------------------------------------------------
    # 7. Write apply receipt
    # ------------------------------------------------------------------
    receipt: dict[str, object] = {
        "problem_id": problem_id,
        "review_id": review_id,
        "action": action,
        "reason": reason,
        "who": who,
        "applied_at": _utc_now(),
        "events_appended": len(problem_events),
        "event_ids": [e.event_id for e in problem_events],
    }
    _write_json(review_dir / "apply-receipt.json", _canonical_json(receipt))

    return TaskResult(
        status="succeeded",
        value={
            "problem_id": problem_id,
            "review_id": review_id,
            "action": action,
            "events_appended": len(problem_events),
        },
    )
