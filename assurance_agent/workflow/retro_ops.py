"""Retro v3 collection, assembly, recovery, and Improvement reconcile operations.

Both handlers delegate to the shared stage functions in ``assurance_agent.retro``
/ ``workflow.improvements`` so CLI and GraphRuntime share one path.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from collections.abc import Mapping
from typing import Literal, cast

from pydantic import ValidationError

from assurance_agent.artifacts.canonical import canonical_json_bytes, sha256_bytes
from assurance_agent.artifacts.models.improvement_outbox import ImprovementOutboxEntry
from assurance_agent.artifacts.models.improvement_review import (
    AutoReviewFinding,
    ImprovementAutoReviewAssessment,
    ImprovementAutoReviewBatchSummary,
    ImprovementAutoReviewStatus,
    ImprovementReviewSubject,
)
from assurance_agent.artifacts.models.retro_batch import RetroPipelineFailureDocument
from assurance_agent.artifacts.models.retro_batch import RetroPipelineFailure
from assurance_agent.artifacts.models.retro_batch import RetroBatchScope
from assurance_agent.artifacts.models.retro_v3 import (
    EvalEvidenceSlice,
    ImprovementCandidateDocumentV3,
    IssueEvidenceSlice,
    RetroContextV3,
    SignalDraftDocument,
    WorkflowEvidenceSlice,
)
from assurance_agent.exceptions import AaError
from assurance_agent.retro.stages import (
    BatchScopeContractError,
    CandidateBatchInvalid,
    FileCoverageGapHistoryReader,
    FileDiscoveryHistoryReader,
    FileEvalHistoryReader,
    RetroInvocation,
    LedgerWorkflowHistoryReader,
    RetroWindowSelection,
    WorkflowHistoryIntegrityError,
    agent_context_json_bytes,
    assemble_context,
    finalize_retro_status,
    materialize_slices,
    materialize_evidence_gap_fallback,
    materialize_pipeline_failure_fallback,
    run_retro_accept,
    write_noop_receipt,
)
from assurance_agent.workflow.graph.models import ExecutableTask, RuntimeContext, TaskResult
from assurance_agent.workflow.graph.task_runner import task_failure, task_with
from assurance_agent.workflow.graph.workspace import TaskWorkspace
from assurance_agent.workflow.issues.history import (
    IssueHistoryIntegrityError,
    LedgerIssueHistoryReader,
)
from assurance_agent.workflow.improvements.outbox import (
    drain_reconcile_outbox,
    enqueue_reconcile,
)
from assurance_agent.workflow.improvements.reconciler import ImprovementAcceptStatus
from assurance_agent.workflow.improvements.auto_review import (
    AutoReviewItem,
    apply_auto_review_result,
    build_auto_review_gate_input,
    select_auto_review_items,
)
from assurance_agent.workflow.improvements.events import read_improvement_events
from assurance_agent.workflow.improvements.ledger import atomic_write_json
from assurance_agent.workflow.improvements.projection import project_improvements
from assurance_agent.workflow.retro_outputs import backfill_slice_digest


def _selection_from_params(params: dict) -> RetroWindowSelection:
    change_ids = params.get("retro_change_ids") or params.get("change_ids")
    since = params.get("retro_since") or params.get("since")
    until = params.get("retro_until") or params.get("until")
    last = params.get("retro_last") or params.get("last")
    batch_payload = params.get("batch_scope")
    batch_scope: RetroBatchScope | None = None
    if batch_payload not in (None, {}):
        if not isinstance(batch_payload, Mapping):
            raise BatchScopeContractError("manifest_schema_invalid")
        try:
            batch_scope = RetroBatchScope.model_validate(batch_payload)
        except ValueError as exc:
            raise BatchScopeContractError("manifest_schema_invalid", str(exc)) from exc

    if isinstance(change_ids, (list, tuple)) and change_ids:
        return RetroWindowSelection(
            change_ids=tuple(str(item) for item in change_ids),
            last=None,
            batch_scope=batch_scope,
        )
    if batch_scope is not None:
        raise BatchScopeContractError("members_missing_from_selection")
    since_s = since if isinstance(since, str) and since.strip() else None
    until_s = until if isinstance(until, str) and until.strip() else None
    if since_s is not None or until_s is not None:
        return RetroWindowSelection(since=since_s, until=until_s, last=None)
    try:
        last_int = int(last) if isinstance(last, (int, str)) else 10
    except (TypeError, ValueError):
        last_int = 10
    return RetroWindowSelection(last=max(last_int, 1))


def retro_collect_v3(task: ExecutableTask, workspace: TaskWorkspace, context: RuntimeContext) -> TaskResult:
    """Materialize the immutable window and three typed v3 evidence slices."""
    retro_id = context.params.get("retro_id")
    if not isinstance(retro_id, str) or not retro_id.strip():
        return task_failure("invalid_input", "params.retro_id must be a non-empty string")
    try:
        selection = _selection_from_params(context.params)
        bundle = materialize_slices(
            workspace.project_root,
            retro_id=retro_id,
            selection=selection,
            issue_history=LedgerIssueHistoryReader(workspace.project_root),
            workflow_history=LedgerWorkflowHistoryReader(workspace.project_root),
            eval_history=FileEvalHistoryReader(workspace.project_root),
            discovery_history=FileDiscoveryHistoryReader(workspace.project_root),
            coverage_gap_history=FileCoverageGapHistoryReader(workspace.project_root),
            write_root=workspace.project_root,
        )
    except BatchScopeContractError as err:
        return task_failure(err.error_kind, str(err))
    except (IssueHistoryIntegrityError, WorkflowHistoryIntegrityError) as err:
        return task_failure("invalid_input", str(err))
    except (AaError, OSError, ValueError) as err:
        return task_failure("internal", str(err))
    return TaskResult(
        status="succeeded",
        value={
            "retro_id": retro_id,
            "issue_count": len(bundle.issue.entries),
            "workflow_count": len(bundle.workflow.entries),
            "eval_count": len(bundle.eval.entries),
            "gap_count": sum(len(slice_.deterministic_signals) for slice_ in bundle.all_slices),
            "all_domain_evidence_absent": not (any(slice_.entries for slice_ in bundle.all_slices))
            and any(slice_.deterministic_signals for slice_ in bundle.all_slices),
        },
    )


def evidence_gap_fallback(
    task: ExecutableTask, workspace: TaskWorkspace, context: RuntimeContext
) -> TaskResult:
    """Create a deterministic process Candidate when every evidence domain is absent."""
    del task
    retro_id = context.params.get("retro_id")
    if not isinstance(retro_id, str) or not retro_id.strip():
        return task_failure("invalid_input", "params.retro_id must be a non-empty string")
    try:
        assembled, candidate = materialize_evidence_gap_fallback(
            workspace.project_root / "qa" / "retro" / retro_id,
            now=datetime.now(timezone.utc),
        )
    except (AaError, OSError, ValueError) as err:
        return task_failure("invalid_output", str(err))
    return TaskResult(
        status="succeeded",
        value={
            "retro_id": retro_id,
            "signal_count": assembled.signal_count,
            "candidate_id": candidate.candidate_id,
        },
    )


def assemble_retro_context_v3(
    task: ExecutableTask, workspace: TaskWorkspace, context: RuntimeContext
) -> TaskResult:
    retro_id = context.params.get("retro_id")
    if not isinstance(retro_id, str) or not retro_id.strip():
        return task_failure("invalid_input", "params.retro_id must be a non-empty string")
    retro_dir = workspace.project_root / "qa" / "retro" / retro_id
    try:
        assembled = assemble_context(
            retro_dir,
            dry_run=bool(context.params.get("retro_dry_run", False)),
            now=datetime.now(timezone.utc),
        )
        context_path = retro_dir / "context.json"
        payload = canonical_json_bytes(assembled)
        if context_path.is_file() and context_path.read_bytes() != payload:
            return task_failure("conflict", "context.json already exists with different bytes")
        context_path.write_bytes(payload)
        (retro_dir / "context-agent.json").write_bytes(agent_context_json_bytes(assembled))
        if assembled.signal_count == 0 and not assembled.dry_run:
            write_noop_receipt(retro_dir, assembled)
    except (AaError, OSError, ValueError) as err:
        return task_failure("invalid_output", str(err))
    return TaskResult(
        status="succeeded",
        value={
            "retro_id": retro_id,
            "signal_count": assembled.signal_count,
            "dry_run": assembled.dry_run,
        },
    )


def record_analysis_failed(
    task: ExecutableTask, workspace: TaskWorkspace, context: RuntimeContext
) -> TaskResult:
    retro_id = context.params.get("retro_id")
    domain = task_with(task).get("domain")
    if not isinstance(retro_id, str) or not retro_id.strip():
        return task_failure("invalid_input", "params.retro_id must be a non-empty string")
    if domain not in {"issue", "workflow", "eval"}:
        return task_failure("invalid_input", "record-analysis-failed requires with.domain")
    domain_name = cast(Literal["issue", "workflow", "eval"], domain)
    recovery = task.recovery
    error_kind = recovery.error_kind if recovery is not None else task.prior_error_kind or "internal"
    message = recovery.message if recovery is not None else task.prior_failure or "analysis failed"
    retro_dir = workspace.project_root / "qa" / "retro" / retro_id
    slice_path = retro_dir / "evidence" / f"{domain_name}-slice.json"
    signal_path = retro_dir / "signals" / f"{domain_name}.json"
    try:
        slice_bytes = slice_path.read_bytes()
        draft = SignalDraftDocument(
            retro_id=retro_id,
            domain=domain_name,
            analysis_status="failed",
            failure_reason=f"{error_kind}: {message}",
            analyzer="operation:record-analysis-failed",
            signals=(),
        )
        completed = backfill_slice_digest(draft, slice_bytes)
        signal_path.parent.mkdir(parents=True, exist_ok=True)
        signal_path.write_bytes(canonical_json_bytes(completed))
    except (OSError, ValueError) as err:
        return task_failure("invalid_output", str(err))
    return TaskResult(status="succeeded", value={"domain": domain_name, "analysis_status": "failed"})


def materialize_empty_retro_analysis(
    task: ExecutableTask, workspace: TaskWorkspace, context: RuntimeContext
) -> TaskResult:
    """Deterministically settle a domain whose validated evidence slice has no entries."""
    retro_id = context.params.get("retro_id")
    domain = task_with(task).get("domain")
    if not isinstance(retro_id, str) or not retro_id.strip():
        return task_failure("invalid_input", "params.retro_id must be a non-empty string")
    slice_models = {
        "issue": IssueEvidenceSlice,
        "workflow": WorkflowEvidenceSlice,
        "eval": EvalEvidenceSlice,
    }
    if domain not in slice_models:
        return task_failure("invalid_input", "materialize-empty-retro-analysis requires with.domain")
    domain_name = cast(Literal["issue", "workflow", "eval"], domain)
    retro_dir = workspace.project_root / "qa" / "retro" / retro_id
    slice_path = retro_dir / "evidence" / f"{domain_name}-slice.json"
    signal_path = retro_dir / "signals" / f"{domain_name}.json"
    try:
        slice_bytes = slice_path.read_bytes()
        slice_ = slice_models[domain_name].model_validate_json(slice_bytes)
        if slice_.entries:
            return task_failure(
                "invalid_input",
                f"{domain_name} evidence slice is non-empty; agent analysis is required",
            )
        draft = SignalDraftDocument(
            retro_id=retro_id,
            domain=domain_name,
            analysis_status="ok",
            analyzer="operation:materialize-empty-retro-analysis",
            signals=(),
        )
        completed = backfill_slice_digest(draft, slice_bytes)
        atomic_write_json(signal_path, completed.model_dump(mode="json"))
    except (OSError, ValidationError, ValueError) as err:
        return task_failure("invalid_output", str(err))
    return TaskResult(status="succeeded", value={"domain": domain_name, "analysis_status": "ok"})


def reconcile_improvements(
    task: ExecutableTask, workspace: TaskWorkspace, context: RuntimeContext
) -> TaskResult:
    """Reconcile Candidates into the Improvement Ledger and write accept receipt."""
    retro_id = context.params.get("retro_id")
    if not isinstance(retro_id, str) or not retro_id.strip():
        return task_failure("invalid_input", "params.retro_id must be a non-empty string")
    retro_dir = workspace.project_root / "qa" / "retro" / retro_id
    candidate_path = retro_dir / "proposal-candidates.json"
    try:
        candidate_payload = json.loads(candidate_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as err:
        return task_failure("invalid_output", str(err))
    if isinstance(candidate_payload, dict) and candidate_payload.get("schema_version") == "2":
        try:
            legacy_status = run_retro_accept(workspace.project_root, retro_id=retro_id)
        except (AaError, OSError, ValueError) as err:
            return task_failure("invalid_output", str(err))
        if legacy_status.result == "failed":
            return task_failure("invalid_output", legacy_status.error or "reconcile failed")
        return TaskResult(
            status="succeeded",
            value={
                "result": legacy_status.result,
                "improvement_count": len(legacy_status.improvement_ids),
                "candidate_batch_digest": legacy_status.candidate_batch_digest,
                "accepted_statuses": [legacy_status.model_dump(mode="json")],
            },
        )
    try:
        context_model = RetroContextV3.model_validate_json(
            (retro_dir / "context.json").read_text(encoding="utf-8")
        )
        document = ImprovementCandidateDocumentV3.model_validate_json(
            candidate_path.read_text(encoding="utf-8")
        )
        pipeline_failure = None
        failure_path = retro_dir / "pipeline-failure.json"
        if failure_path.is_file():
            failure_doc = RetroPipelineFailureDocument.model_validate_json(
                failure_path.read_text(encoding="utf-8")
            )
            pipeline_failure = failure_doc.failures[-1]
        for candidate in document.candidates:
            enqueue_reconcile(
                workspace.project_root,
                ImprovementOutboxEntry(
                    retro_id=retro_id,
                    candidate_sha256=sha256_bytes(canonical_json_bytes(candidate)),
                    context_sha256=document.context_sha256,
                    context=context_model,
                    candidate=candidate,
                    pipeline_failure=pipeline_failure,
                ),
            )
    except (OSError, ValidationError, ValueError) as err:
        return task_failure("invalid_output", str(err))
    try:
        drained = drain_reconcile_outbox(workspace.project_root)
    except CandidateBatchInvalid as err:
        return task_failure("invalid_output", str(err))
    except (AaError, OSError) as err:
        return TaskResult(
            status="succeeded",
            value={
                "result": "pending_reconcile",
                "improvement_count": 0,
                "error": str(err),
                "accepted_statuses": [],
            },
        )
    current = tuple(status for status in drained if status.retro_id == retro_id)
    improvement_ids = tuple(sorted({item for status in current for item in status.improvement_ids}))
    event_ids = tuple(sorted({item for status in current for item in status.event_ids}))
    receipt = ImprovementAcceptStatus(
        retro_id=retro_id,
        context_sha256=document.context_sha256,
        candidate_batch_digest=sha256_bytes(canonical_json_bytes(document)),
        idempotency_key=f"{retro_id}:{document.context_sha256}",
        result="accepted",
        improvement_ids=improvement_ids,
        event_ids=event_ids,
    )
    (retro_dir / "accept-status.json").write_bytes(canonical_json_bytes(receipt))
    lines = [f"# Improvement review queue for {retro_id}", ""]
    lines.extend(f"- {improvement_id}" for improvement_id in improvement_ids)
    (retro_dir / "review-queue.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return TaskResult(
        status="succeeded",
        value={
            "result": receipt.result,
            "improvement_count": len(receipt.improvement_ids),
            "candidate_batch_digest": receipt.candidate_batch_digest,
            "accepted_statuses": [status.model_dump(mode="json") for status in drained],
        },
    )


def drain_improvement_outbox(
    task: ExecutableTask, workspace: TaskWorkspace, context: RuntimeContext
) -> TaskResult:
    """Drain prior durable reconcile work before collecting a new Retro window."""
    del task, context
    try:
        statuses = drain_reconcile_outbox(workspace.project_root)
    except (AaError, OSError, ValueError) as err:
        return task_failure("internal", str(err))
    return TaskResult(
        status="succeeded",
        value={"accepted_statuses": [status.model_dump(mode="json") for status in statuses]},
    )


def record_retro_pipeline_failure(
    task: ExecutableTask, workspace: TaskWorkspace, context: RuntimeContext
) -> TaskResult:
    """Materialize a typed failure envelope and one deterministic fallback Candidate."""
    retro_id = context.params.get("retro_id")
    stage = task_with(task).get("stage")
    if not isinstance(retro_id, str) or not retro_id.strip():
        return task_failure("invalid_input", "params.retro_id must be a non-empty string")
    if not isinstance(stage, str) or not stage:
        return task_failure("invalid_input", "pipeline failure recovery requires with.stage")
    recovery = task.recovery
    error_kind = recovery.error_kind if recovery is not None else task.prior_error_kind or "internal"
    message = recovery.message if recovery is not None else task.prior_failure or "Retro stage failed"
    failure_identity = {"retro_id": retro_id, "stage": stage, "error_kind": error_kind}
    failure = RetroPipelineFailure(
        failure_id="FAIL-"
        + sha256_bytes(canonical_json_bytes(failure_identity)).removeprefix("sha256:")[:24],
        retro_id=retro_id,
        batch_id=None,
        stage=stage,
        node_id=task.node_id,
        error_kind=error_kind,
        message_fingerprint=sha256_bytes(message.encode("utf-8")),
        occurred_at=datetime.now(timezone.utc),
    )
    try:
        fallback_context, candidate = materialize_pipeline_failure_fallback(
            workspace.project_root,
            failure=failure,
            batch_scope=None,
        )
        if stage == "reconcile":
            enqueue_reconcile(
                workspace.project_root,
                ImprovementOutboxEntry(
                    retro_id=retro_id,
                    candidate_sha256=sha256_bytes(canonical_json_bytes(candidate)),
                    context_sha256=sha256_bytes(canonical_json_bytes(fallback_context)),
                    context=fallback_context,
                    candidate=candidate,
                    pipeline_failure=failure,
                ),
            )
    except (AaError, OSError, ValueError) as err:
        return task_failure("invalid_output", str(err))
    return TaskResult(
        status="succeeded",
        value={"failure_id": failure.failure_id, "stage": stage},
    )


def finalize_retro_run_status(
    task: ExecutableTask, workspace: TaskWorkspace, context: RuntimeContext
) -> TaskResult:
    """Persist the current Retro outcome after every recovered or healthy path."""
    del task
    retro_id = context.params.get("retro_id")
    if not isinstance(retro_id, str) or not retro_id.strip():
        return task_failure("invalid_input", "params.retro_id must be a non-empty string")
    improvement_ids: tuple[str, ...] = ()
    accept_path = workspace.project_root / "qa" / "retro" / retro_id / "accept-status.json"
    try:
        if accept_path.is_file():
            receipt = ImprovementAcceptStatus.model_validate_json(accept_path.read_text(encoding="utf-8"))
            improvement_ids = receipt.improvement_ids
        invocation = RetroInvocation(
            project_root=workspace.project_root,
            shell_change_id=context.change_id,
            retro_id=retro_id,
            params=context.params,
        )
        status = finalize_retro_status(invocation, improvement_ids=improvement_ids)
    except (OSError, ValueError, ValidationError) as err:
        return task_failure("invalid_output", str(err))
    return TaskResult(status="succeeded", value=status.model_dump(mode="json"))


# Compatibility alias: older unit tests still import the previous name.
retro_accept = reconcile_improvements


def load_review_subject(
    task: ExecutableTask, workspace: TaskWorkspace, context: RuntimeContext
) -> TaskResult:
    del task
    digest = context.params.get("subject_sha256")
    if not isinstance(digest, str):
        return task_failure("invalid_input", "subject_sha256 is required")
    path = workspace.project_root / "qa" / "improvements" / "review-subjects" / f"{digest}.json"
    agent_path = path.parent / "agent" / path.name
    try:
        canonical_bytes = path.read_bytes()
        if sha256_bytes(canonical_bytes) != digest:
            return task_failure("conflict", "review subject digest drift")
        agent_subject = ImprovementReviewSubject.model_validate_json(agent_path.read_bytes())
        if canonical_json_bytes(agent_subject) != canonical_bytes:
            return task_failure("conflict", "agent review subject drift")
    except ValidationError:
        return task_failure("conflict", "agent review subject drift")
    except OSError as err:
        return task_failure("invalid_input", str(err))
    return TaskResult(status="succeeded", value={"subject_sha256": digest})


def validate_improvement_review_assessment(
    task: ExecutableTask, workspace: TaskWorkspace, context: RuntimeContext
) -> TaskResult:
    del task
    try:
        raw_version = context.params["expected_improvement_version"]
        raw_attempt = context.params.get("attempt", 1)
        if not isinstance(raw_version, int) or isinstance(raw_version, bool):
            raise ValueError("expected_improvement_version must be an integer")
        if not isinstance(raw_attempt, int) or isinstance(raw_attempt, bool):
            raise ValueError("attempt must be an integer")
        gate_input = build_auto_review_gate_input(
            workspace.project_root,
            review_id=str(context.params["review_id"]),
            improvement_id=str(context.params["improvement_id"]),
            subject_sha256=str(context.params["subject_sha256"]),
            expected_version=raw_version,
            policy_version=str(context.params.get("policy_version", "1")),
            attempt=raw_attempt,
        )
        path = (
            workspace.project_root
            / "qa"
            / "improvements"
            / "reviews"
            / gate_input.review_id
            / "gate-input.json"
        )
        atomic_write_json(path, gate_input.model_dump(mode="json"))
    except (KeyError, TypeError, ValueError, AaError, OSError) as err:
        return task_failure("invalid_output", str(err))
    return TaskResult(status="succeeded", value={"auto_approve": gate_input.auto_approve})


def apply_improvement_auto_review(
    task: ExecutableTask, workspace: TaskWorkspace, context: RuntimeContext
) -> TaskResult:
    verdict = task_with(task).get("verdict", "record")
    if verdict not in {"auto_approve", "record"}:
        return task_failure("invalid_input", "invalid auto review verdict")
    try:
        item = AutoReviewItem.model_validate(
            {
                key: context.params[key]
                for key in (
                    "improvement_id",
                    "subject_sha256",
                    "expected_improvement_version",
                    "review_id",
                    "policy_version",
                    "attempt",
                )
            }
        )
        status = apply_auto_review_result(
            workspace.project_root,
            item,
            gate_verdict=cast(Literal["auto_approve", "record"], verdict),
        )
    except (KeyError, ValueError, AaError, OSError) as err:
        return task_failure("invalid_output", str(err))
    return TaskResult(status="succeeded", value=status.model_dump(mode="json"))


def select_current_retro_auto_review_items(
    task: ExecutableTask, workspace: TaskWorkspace, context: RuntimeContext
) -> TaskResult:
    del task
    retro_id = context.params.get("retro_id")
    if not isinstance(retro_id, str):
        return task_failure("invalid_input", "retro_id is required")
    try:
        accept_path = workspace.project_root / "qa" / "retro" / retro_id / "accept-status.json"
        statuses = (
            [ImprovementAcceptStatus.model_validate_json(accept_path.read_text(encoding="utf-8"))]
            if accept_path.is_file()
            else []
        )
        completed = workspace.project_root / "qa" / "improvements" / "outbox" / "completed"
        if completed.is_dir():
            for path in sorted(completed.glob("*.json")):
                status = ImprovementAcceptStatus.model_validate_json(path.read_text(encoding="utf-8"))
                if status.retro_id == retro_id:
                    statuses.append(status)
        projection = project_improvements(
            read_improvement_events(workspace.project_root / "qa" / "improvements" / "events.jsonl")
        )
        items = select_auto_review_items(projection, statuses)
        orchestration_errors: list[dict[str, str]] = []
        if len(items) > 128:
            items = ()
            orchestration_errors.append({"stage": "selector", "error_kind": "selector_capacity_exceeded"})
        selection = {
            "schema_version": "1",
            "retro_id": retro_id,
            "items": [item.model_dump(mode="json") for item in items],
            "orchestration_errors": orchestration_errors,
        }
        atomic_write_json(
            workspace.project_root / "qa" / "retro" / retro_id / "auto-review-selection.json",
            selection,
        )
    except (AaError, OSError, ValueError) as err:
        return task_failure("invalid_output", str(err))
    return TaskResult(
        status="succeeded",
        value=selection,
    )


def summarize_auto_review_batch(
    task: ExecutableTask, workspace: TaskWorkspace, context: RuntimeContext
) -> TaskResult:
    del task
    retro_id = context.params.get("retro_id")
    if not isinstance(retro_id, str):
        return task_failure("invalid_input", "retro_id is required")
    reviews_root = workspace.project_root / "qa" / "improvements" / "reviews"
    statuses: list[ImprovementAutoReviewStatus] = []
    try:
        selection_path = workspace.project_root / "qa" / "retro" / retro_id / "auto-review-selection.json"
        selection = json.loads(selection_path.read_text(encoding="utf-8"))
        selected_ids = tuple(str(item["review_id"]) for item in selection.get("items", ()))
        orchestration_errors = list(selection.get("orchestration_errors", ()))
        for review_id in selected_ids:
            path = reviews_root / review_id / "status.json"
            if path.is_file():
                statuses.append(ImprovementAutoReviewStatus.model_validate_json(path.read_text()))
            else:
                orchestration_errors.append({"stage": "fan_out", "error_kind": "missing_child_status"})
        summary = ImprovementAutoReviewBatchSummary(
            retro_id=retro_id,
            review_ids=tuple(status.review_id for status in statuses),
            approved=sum(status.result == "approved" for status in statuses),
            escalated=sum(status.result == "escalated" for status in statuses),
            errors=sum(status.result == "review_error" for status in statuses),
            stale=sum(status.result == "stale" for status in statuses),
            orchestration_errors=tuple(orchestration_errors),
        )
        atomic_write_json(
            workspace.project_root / "qa" / "retro" / retro_id / "auto-review-summary.json",
            summary.model_dump(mode="json"),
        )
    except (OSError, ValueError) as err:
        return task_failure("invalid_output", str(err))
    return TaskResult(status="succeeded", value=summary.model_dump(mode="json"))


def record_auto_review_orchestration_error(
    task: ExecutableTask, workspace: TaskWorkspace, context: RuntimeContext
) -> TaskResult:
    """Record a post-Retro orchestration failure without mutating Retro status."""
    retro_id = context.params.get("retro_id")
    if not isinstance(retro_id, str):
        return task_failure("invalid_input", "retro_id is required")
    recovery = task.recovery
    source = recovery.source_node_id if recovery is not None else "summarize-auto-review-batch"
    stage = {
        "select-current-retro-auto-review-items": "selector",
        "auto-review-items": "fan_out",
        "summarize-auto-review-batch": "summarize",
    }.get(source, "summarize")
    error_kind = recovery.error_kind if recovery is not None else task.prior_error_kind or "internal"
    selection_path = workspace.project_root / "qa" / "retro" / retro_id / "auto-review-selection.json"
    try:
        if selection_path.is_file():
            selection = json.loads(selection_path.read_text(encoding="utf-8"))
        else:
            selection = {
                "schema_version": "1",
                "retro_id": retro_id,
                "items": [],
                "orchestration_errors": [],
            }
        errors = list(selection.get("orchestration_errors", ()))
        errors.append({"stage": stage, "error_kind": str(error_kind)})
        selection["orchestration_errors"] = errors
        atomic_write_json(selection_path, selection)
        return summarize_auto_review_batch(task, workspace, context)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as err:
        return task_failure("invalid_output", str(err))


def record_improvement_auto_review_error(
    task: ExecutableTask, workspace: TaskWorkspace, context: RuntimeContext
) -> TaskResult:
    recovery = task.recovery
    error_kind = recovery.error_kind if recovery is not None else task.prior_error_kind or "internal"
    try:
        item = AutoReviewItem.model_validate(
            {
                key: context.params[key]
                for key in (
                    "improvement_id",
                    "subject_sha256",
                    "expected_improvement_version",
                    "review_id",
                    "policy_version",
                    "attempt",
                )
            }
        )
        assessment = ImprovementAutoReviewAssessment(
            review_id=item.review_id,
            improvement_id=item.improvement_id,
            expected_improvement_version=item.expected_improvement_version,
            subject_sha256=item.subject_sha256,
            decision="needs_human_review",
            findings=(
                AutoReviewFinding(
                    finding_id="review-error",
                    severity="blocking",
                    category="review_execution",
                    message=f"automatic review failed: {error_kind}",
                    source_refs=(),
                ),
            ),
            evidence_traceability="invalid",
            scope_readiness="ambiguous",
            verification_readiness="not_ready",
            delivery_safety="needs_human_review",
            human_review_required=True,
        )
        assessment_path = (
            workspace.project_root / "qa" / "improvements" / "reviews" / item.review_id / "assessment.json"
        )
        atomic_write_json(assessment_path, assessment.model_dump(mode="json"))
        status = apply_auto_review_result(workspace.project_root, item, gate_verdict="review_error")
    except (KeyError, ValueError, AaError, OSError) as err:
        return task_failure("invalid_output", str(err))
    return TaskResult(status="succeeded", value=status.model_dump(mode="json"))


__all__ = [
    "assemble_retro_context_v3",
    "apply_improvement_auto_review",
    "drain_improvement_outbox",
    "evidence_gap_fallback",
    "finalize_retro_run_status",
    "load_review_subject",
    "materialize_empty_retro_analysis",
    "record_analysis_failed",
    "record_auto_review_orchestration_error",
    "record_improvement_auto_review_error",
    "record_retro_pipeline_failure",
    "retro_collect_v3",
    "reconcile_improvements",
    "retro_accept",
    "select_current_retro_auto_review_items",
    "summarize_auto_review_batch",
    "validate_improvement_review_assessment",
]
