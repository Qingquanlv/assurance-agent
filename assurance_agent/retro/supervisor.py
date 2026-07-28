"""Phase-aware outer supervisor for canonical Retro graph invocations."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from pydantic import ValidationError

from assurance_agent.artifacts.canonical import canonical_json_bytes, sha256_bytes
from assurance_agent.artifacts.models.improvement_outbox import ImprovementOutboxEntry
from assurance_agent.artifacts.models.retro_batch import (
    RetroInvocationResult,
    RetroPipelineFailure,
    RetroPipelineFailureDocument,
    RetroRunStatus,
)
from assurance_agent.artifacts.models.retro_v3 import RetroContextV3
from assurance_agent.retro.fallback import materialize_pipeline_failure_fallback
from assurance_agent.workflow.improvements.outbox import (
    drain_reconcile_outbox,
    enqueue_reconcile,
)


@dataclass(frozen=True)
class RetroInvocation:
    project_root: Path
    shell_change_id: str
    retro_id: str
    params: Mapping[str, object]


class GraphRunner(Protocol):
    def __call__(self, invocation: RetroInvocation) -> object: ...


def _status_path(invocation: RetroInvocation) -> Path:
    return invocation.project_root / "qa" / "retro" / invocation.retro_id / "retro-status.json"


def _load_status(invocation: RetroInvocation) -> RetroRunStatus | None:
    path = _status_path(invocation)
    if not path.is_file():
        return None
    try:
        return RetroRunStatus.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError, ValueError):
        return None


def _pending_outbox(invocation: RetroInvocation) -> tuple[str, ...]:
    pending = invocation.project_root / "qa" / "improvements" / "outbox" / "pending"
    if not pending.is_dir():
        return ()
    ids: list[str] = []
    for path in sorted(pending.glob("*.json"), key=lambda item: item.name):
        try:
            entry = ImprovementOutboxEntry.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValidationError, ValueError):
            continue
        if entry.retro_id == invocation.retro_id:
            ids.append(path.stem)
    return tuple(ids)


def finalize_retro_status(
    invocation: RetroInvocation,
    *,
    improvement_ids: tuple[str, ...] = (),
) -> RetroRunStatus:
    """Persist one final Retro status from current-run artifacts and pending outbox."""
    existing = _load_status(invocation)
    if existing is not None:
        return existing
    retro_dir = invocation.project_root / "qa" / "retro" / invocation.retro_id
    failure_ids: tuple[str, ...] = ()
    failure_path = retro_dir / "pipeline-failure.json"
    if failure_path.is_file():
        failure_doc = RetroPipelineFailureDocument.model_validate_json(
            failure_path.read_text(encoding="utf-8")
        )
        failure_ids = tuple(item.failure_id for item in failure_doc.failures)
    context_incomplete = False
    context_path = retro_dir / "context.json"
    if context_path.is_file():
        context = RetroContextV3.model_validate_json(context_path.read_text(encoding="utf-8"))
        context_incomplete = context.integrity.status == "incomplete"
    pending = _pending_outbox(invocation)
    batch_payload = invocation.params.get("batch_scope")
    batch_id = batch_payload.get("batch_id") if isinstance(batch_payload, Mapping) else None
    if pending:
        result = "pending_reconcile"
    elif failure_ids or context_incomplete:
        result = "completed_with_gaps"
    else:
        result = "completed"
    status = RetroRunStatus(
        retro_id=invocation.retro_id,
        batch_id=str(batch_id) if batch_id else None,
        result=result,
        improvement_ids=tuple(sorted(set(improvement_ids))),
        outbox_id=pending[0] if pending else None,
        failure_ids=failure_ids,
    )
    path = _status_path(invocation)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json_bytes(status))
    return status


def _failure_for_exception(invocation: RetroInvocation, exc: BaseException) -> RetroPipelineFailure:
    error_kind = getattr(exc, "error_kind", "internal")
    if not isinstance(error_kind, str) or not error_kind:
        error_kind = "internal"
    identity = {
        "retro_id": invocation.retro_id,
        "stage": "graph_dispatch",
        "error_kind": error_kind,
    }
    failure_id = "FAIL-" + sha256_bytes(canonical_json_bytes(identity)).removeprefix("sha256:")[:24]
    return RetroPipelineFailure(
        failure_id=failure_id,
        retro_id=invocation.retro_id,
        batch_id=None,
        stage="graph_dispatch",
        error_kind=error_kind,
        message_fingerprint=sha256_bytes(str(exc).encode("utf-8")),
        occurred_at=datetime.now(timezone.utc),
    )


def _compensate(invocation: RetroInvocation, failure: RetroPipelineFailure) -> RetroInvocationResult:
    try:
        context, candidate = materialize_pipeline_failure_fallback(
            invocation.project_root,
            failure=failure,
            batch_scope=None,
        )
        entry = ImprovementOutboxEntry(
            retro_id=invocation.retro_id,
            candidate_sha256=sha256_bytes(canonical_json_bytes(candidate)),
            context_sha256=sha256_bytes(canonical_json_bytes(context)),
            context=context,
            candidate=candidate,
            pipeline_failure=failure,
        )
        enqueue_reconcile(invocation.project_root, entry)
        accepted = drain_reconcile_outbox(invocation.project_root)
        improvement_ids = tuple(
            sorted(
                {
                    item
                    for status in accepted
                    if status.retro_id == invocation.retro_id
                    for item in status.improvement_ids
                }
            )
        )
        status = finalize_retro_status(invocation, improvement_ids=improvement_ids)
        return RetroInvocationResult(status=status, result=status.result)
    except (OSError, ValueError, ValidationError):
        return RetroInvocationResult(status=None, result="technical_failure")


def run_retro_supervised(
    invocation: RetroInvocation,
    *,
    graph_runner: GraphRunner | Callable[[RetroInvocation], object],
    preflight_failure: RetroPipelineFailure | None = None,
) -> RetroInvocationResult:
    """Run Graph, compensate pre-final failures, and never rewrite final Retro status."""
    existing = _load_status(invocation)
    if existing is not None:
        return RetroInvocationResult(status=existing, result=existing.result)
    if preflight_failure is not None:
        return _compensate(invocation, preflight_failure)
    try:
        result = graph_runner(invocation)
        exit_code = getattr(result, "exit_code", 0)
        if isinstance(exit_code, int) and exit_code != 0:
            error = RuntimeError(str(getattr(result, "reason", "Retro graph failed")))
            error.error_kind = getattr(result, "error_kind", "internal")  # type: ignore[attr-defined]
            raise error
        status = _load_status(invocation) or finalize_retro_status(invocation)
        return RetroInvocationResult(status=status, result=status.result)
    except BaseException as exc:
        final = _load_status(invocation)
        if final is not None:
            return RetroInvocationResult(status=final, result=final.result)
        return _compensate(invocation, _failure_for_exception(invocation, exc))


__all__ = [
    "GraphRunner",
    "RetroInvocation",
    "finalize_retro_status",
    "run_retro_supervised",
]
