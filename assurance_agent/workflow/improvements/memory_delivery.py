"""Memory-patch Improvement delivery: evaluate / apply / rollback + graph ops.

Eval runners are injected (workflow must not import ``assurance_agent.eval``).
Memory writes reuse Improvement-scoped helpers in ``retro.apply`` (importlint seam).
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from assurance_agent.artifacts.models.improvements import (
    DeliveryKind,
    ImprovementLedgerProjection,
    ImprovementProjection,
    ImprovementState,
)
from assurance_agent.exceptions import AaError
from assurance_agent.retro.apply import (
    apply_improvement_memory_patch,
    deprecate_improvement_memory_block,
    resolve_memory_target,
)
from assurance_agent.workflow.graph.models import ExecutableTask, RuntimeContext, TaskResult
from assurance_agent.workflow.graph.task_runner import task_failure
from assurance_agent.workflow.graph.workspace import TaskWorkspace
from assurance_agent.workflow.improvements.events import (
    EvalOutcome,
    ImprovementAppliedEvent,
    ImprovementEvalCompletedEvent,
    ImprovementEvalRequestedEvent,
    ImprovementEvent,
    ImprovementRolledBackEvent,
    read_improvement_events,
)
from assurance_agent.workflow.improvements.ledger import ProjectImprovementStore

EvalRunner = Callable[..., Mapping[str, object]]

_ACTION_DELIVERIES: dict[str, frozenset[DeliveryKind]] = {
    "evaluate": frozenset({DeliveryKind.MEMORY_PATCH}),
    "export": frozenset({DeliveryKind.CHANGE_DRAFT, DeliveryKind.KNOWLEDGE_DELTA}),
    "apply": frozenset(
        {DeliveryKind.MEMORY_PATCH, DeliveryKind.CHANGE_DRAFT, DeliveryKind.KNOWLEDGE_DELTA}
    ),
    "rollback": frozenset({DeliveryKind.MEMORY_PATCH}),
}


class ImprovementDeliveryError(AaError):
    """Raised when an Improvement delivery adapter rejects the request."""


class ImprovementDeliveryConflict(ImprovementDeliveryError):
    """Raised when an export artifact conflicts with different on-disk bytes."""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _utc_now() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _event_id(idempotency_key: str) -> str:
    return "IMPEVT-" + hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:16]


def _evidence_ids(projection: ImprovementProjection) -> tuple[str, ...]:
    return projection.source_refs.all_ids()


@dataclass(frozen=True)
class MemoryEvalReceipt:
    eval_run_id: str
    outcome: EvalOutcome
    report_sha256: str
    staged_sha256: str
    baseline_sha256: str | None


@dataclass(frozen=True)
class MemoryApplyReceipt:
    target: str
    before_sha256: str
    after_sha256: str
    receipt_sha256: str


@dataclass(frozen=True)
class MemoryRollbackReceipt:
    target: str
    restored_sha256: str
    reason: str


def _load_ledger(project_root: Path) -> ImprovementLedgerProjection:
    path = project_root / "qa" / "improvements" / "improvements.json"
    if not path.is_file():
        raise ImprovementDeliveryError(f"improvement ledger projection missing at {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ImprovementDeliveryError(f"corrupt improvements.json: {exc}") from exc
    return ImprovementLedgerProjection.model_validate(data)


def _require_memory(projection: ImprovementProjection) -> None:
    if projection.delivery is not DeliveryKind.MEMORY_PATCH:
        raise ImprovementDeliveryError(
            f"memory delivery requires delivery=memory_patch, got {projection.delivery.value}"
        )


def _classify_eval(result: Mapping[str, object]) -> EvalOutcome:
    baseline_raw = result.get("baseline_metrics")
    if baseline_raw is None:
        return "awaiting_baseline"
    hard_failures = result.get("hard_gate_failures") or []
    verdict = str(result.get("verdict", ""))
    if verdict in {"fail", "inconclusive", "needs_human_review"} or hard_failures:
        return "regressed"
    metrics = result.get("metrics")
    baseline = baseline_raw if isinstance(baseline_raw, Mapping) else {}
    suite_raw = result.get("suite_contract")
    suite = suite_raw if isinstance(suite_raw, Mapping) else {}
    thresholds = suite.get("thresholds") if isinstance(suite, Mapping) else None
    if isinstance(thresholds, list) and isinstance(metrics, Mapping):
        for threshold in thresholds:
            if not isinstance(threshold, Mapping) or threshold.get("gate") != "hard":
                continue
            metric = threshold.get("metric")
            if not isinstance(metric, str):
                continue
            current = metrics.get(metric)
            base = baseline.get(metric)
            op = threshold.get("op", "gte")
            value = threshold.get("value")
            if current is None or base is None:
                return "regressed"
            try:
                cur_f = float(current)
                base_f = float(base)
                gate_f = float(value) if value is not None else base_f
            except (TypeError, ValueError):
                return "regressed"
            if op == "gte" and cur_f < gate_f:
                return "regressed"
            if op == "lte" and cur_f > gate_f:
                return "regressed"
            # Direction-aware soft regression vs baseline.
            if cur_f + 1e-9 < base_f and op == "gte":
                return "regressed"
    return "passed"


def _stage_overlay(
    project_root: Path,
    projection: ImprovementProjection,
    stage_root: Path,
) -> tuple[Path, str]:
    overlay_memory = stage_root / ".aa" / "memory"
    overlay_memory.mkdir(parents=True)
    apply_improvement_memory_patch(
        project_root,
        improvement_id=projection.improvement_id,
        target=projection.target,
        proposed_change=projection.proposed_change,
        evidence_ids=_evidence_ids(projection),
        stage_dir=overlay_memory,
    )
    staged_files = sorted(overlay_memory.glob("*.md"))
    digest_parts = [
        f"{path.name}:{sha256_bytes(path.read_bytes())}" for path in staged_files
    ]
    staged_sha256 = sha256_bytes("\n".join(digest_parts).encode("utf-8"))
    return stage_root, staged_sha256


def _latest_passed_eval(
    events: Sequence[ImprovementEvent], improvement_id: str
) -> ImprovementEvalCompletedEvent | None:
    for event in reversed(events):
        if (
            isinstance(event, ImprovementEvalCompletedEvent)
            and event.improvement_id == improvement_id
            and event.outcome == "passed"
        ):
            return event
    return None


def _latest_applied(
    events: Sequence[ImprovementEvent], improvement_id: str
) -> ImprovementAppliedEvent | None:
    for event in reversed(events):
        if isinstance(event, ImprovementAppliedEvent) and event.improvement_id == improvement_id:
            return event
    return None


class MemoryPatchDelivery:
    """Stage → eval → apply/rollback for ``memory_patch`` Improvements."""

    def __init__(
        self,
        project_root: Path,
        *,
        eval_runner: EvalRunner | None = None,
        engine_root: Path | None = None,
    ) -> None:
        self.project_root = project_root
        self.eval_runner = eval_runner
        self.engine_root = engine_root or project_root
        self.store = ProjectImprovementStore(project_root)

    def _reload(self, improvement_id: str) -> ImprovementProjection:
        item = _load_ledger(self.project_root).improvements.get(improvement_id)
        if item is None:
            raise ImprovementDeliveryError(f"improvement {improvement_id!r} not found")
        return item

    def evaluate(self, improvement: ImprovementProjection) -> MemoryEvalReceipt:
        _require_memory(improvement)
        current = self._reload(improvement.improvement_id)
        if current.state not in {
            ImprovementState.APPROVED,
            ImprovementState.AWAITING_BASELINE,
            ImprovementState.EVAL_ERROR,
        }:
            raise ImprovementDeliveryError(
                f"evaluate requires approved/awaiting_baseline/eval_error, got {current.state.value}"
            )
        if not current.verification.suites:
            raise ImprovementDeliveryError("memory evaluate requires at least one verification suite")
        if self.eval_runner is None:
            raise ImprovementDeliveryError("eval_runner is required for memory evaluate")

        with tempfile.TemporaryDirectory(prefix="imp-mem-stage-") as tmp:
            stage_root = Path(tmp) / "overlay"
            _, staged_sha256 = _stage_overlay(self.project_root, current, stage_root)
            eval_run_id = f"imp-eval-{current.improvement_id}-{staged_sha256[:12]}"
            suites = list(current.verification.suites)
            requested = ImprovementEvalRequestedEvent(
                schema_version="1.0",
                seq=1,
                event_id=_event_id(
                    f"improvement-eval-requested:{current.improvement_id}:{current.version}:{staged_sha256}"
                ),
                idempotency_key=(
                    f"improvement-eval-requested:{current.improvement_id}:{current.version}:{staged_sha256}"
                ),
                ts=_utc_now(),
                improvement_id=current.improvement_id,
                expected_improvement_version=current.version,
                type="improvement_eval_requested",
                eval_run_id=eval_run_id,
                suites=suites,
                staged_sha256=staged_sha256,
                baseline_sha256=None,
            )
            self.store.append_and_rebuild([requested])
            current = self._reload(current.improvement_id)

            outcome: EvalOutcome = "passed"
            report_parts: list[str] = []
            baseline_sha256: str | None = None
            try:
                for suite in suites:
                    result = self.eval_runner(
                        suite=suite,
                        sut_dir=self.project_root,
                        engine_root=self.engine_root,
                        extra_memory_dir=stage_root,
                    )
                    classified = _classify_eval(result)
                    report_parts.append(
                        json.dumps(
                            {"suite": suite, "result": dict(result), "outcome": classified},
                            sort_keys=True,
                            separators=(",", ":"),
                            default=str,
                        )
                    )
                    baseline_raw = result.get("baseline_metrics")
                    if isinstance(baseline_raw, Mapping):
                        baseline_sha256 = sha256_bytes(
                            json.dumps(
                                dict(baseline_raw), sort_keys=True, separators=(",", ":")
                            ).encode("utf-8")
                        )
                    if classified != "passed":
                        outcome = classified
                        break
            except Exception as exc:  # noqa: BLE001 — surface as eval_error event
                outcome = "error"
                report_parts.append(json.dumps({"error": str(exc)}, sort_keys=True))

            report_sha256 = sha256_bytes("\n".join(report_parts).encode("utf-8"))
            completed = ImprovementEvalCompletedEvent(
                schema_version="1.0",
                seq=1,
                event_id=_event_id(
                    f"improvement-eval-completed:{current.improvement_id}:{current.version}:"
                    f"{eval_run_id}:{outcome}"
                ),
                idempotency_key=(
                    f"improvement-eval-completed:{current.improvement_id}:{current.version}:"
                    f"{eval_run_id}:{outcome}"
                ),
                ts=_utc_now(),
                improvement_id=current.improvement_id,
                expected_improvement_version=current.version,
                type="improvement_eval_completed",
                eval_run_id=eval_run_id,
                outcome=outcome,
                report_sha256=report_sha256,
                staged_sha256=staged_sha256,
                baseline_sha256=baseline_sha256,
                error=None if outcome != "error" else "eval runner failed",
            )
            self.store.append_and_rebuild([completed])
            return MemoryEvalReceipt(
                eval_run_id=eval_run_id,
                outcome=outcome,
                report_sha256=report_sha256,
                staged_sha256=staged_sha256,
                baseline_sha256=baseline_sha256,
            )

    def apply(
        self,
        improvement: ImprovementProjection,
        *,
        expected_target_sha256: str,
    ) -> MemoryApplyReceipt:
        _require_memory(improvement)
        current = self._reload(improvement.improvement_id)
        if current.state is not ImprovementState.EVALUATING:
            raise ImprovementDeliveryError(
                f"apply requires evaluating state after successful eval, got {current.state.value}"
            )
        events = read_improvement_events(self.project_root / "qa/improvements/events.jsonl")
        passed = _latest_passed_eval(events, current.improvement_id)
        if passed is None:
            raise ImprovementDeliveryError("apply requires a successful eval")

        target_path = resolve_memory_target(self.project_root, current.target)
        before = target_path.read_bytes() if target_path.is_file() else b""
        before_sha = sha256_bytes(before)
        if before_sha != expected_target_sha256:
            raise ImprovementDeliveryError(
                f"target digest mismatch: expected {expected_target_sha256}, found {before_sha}"
            )

        apply_improvement_memory_patch(
            self.project_root,
            improvement_id=current.improvement_id,
            target=current.target,
            proposed_change=current.proposed_change,
            evidence_ids=_evidence_ids(current),
        )
        after = target_path.read_bytes()
        after_sha = sha256_bytes(after)
        receipt_body = {
            "improvement_id": current.improvement_id,
            "target": current.target,
            "before_sha256": before_sha,
            "after_sha256": after_sha,
            "staged_sha256": passed.staged_sha256,
            "eval_run_id": passed.eval_run_id,
        }
        receipt_sha = sha256_bytes(
            json.dumps(receipt_body, sort_keys=True, separators=(",", ":")).encode("utf-8")
        )
        applied = ImprovementAppliedEvent(
            schema_version="1.0",
            seq=1,
            event_id=_event_id(
                f"improvement-applied:{current.improvement_id}:{current.version}:{after_sha}"
            ),
            idempotency_key=(
                f"improvement-applied:{current.improvement_id}:{current.version}:{after_sha}"
            ),
            ts=_utc_now(),
            improvement_id=current.improvement_id,
            expected_improvement_version=current.version,
            type="improvement_applied",
            target=current.target,
            before_sha256=before_sha,
            after_sha256=after_sha,
            receipt_sha256=receipt_sha,
        )
        self.store.append_and_rebuild([applied])
        return MemoryApplyReceipt(
            target=current.target,
            before_sha256=before_sha,
            after_sha256=after_sha,
            receipt_sha256=receipt_sha,
        )

    def rollback(
        self,
        improvement: ImprovementProjection,
        *,
        reason: str,
        expected_applied_digest: str,
    ) -> MemoryRollbackReceipt:
        _require_memory(improvement)
        if not reason or not reason.strip():
            raise ImprovementDeliveryError("rollback reason must not be empty")
        current = self._reload(improvement.improvement_id)
        if current.state is not ImprovementState.APPLIED:
            raise ImprovementDeliveryError(
                f"rollback requires applied state, got {current.state.value}"
            )
        events = read_improvement_events(self.project_root / "qa/improvements/events.jsonl")
        applied = _latest_applied(events, current.improvement_id)
        if applied is None:
            raise ImprovementDeliveryError("rollback requires a prior improvement_applied event")

        target_path = resolve_memory_target(self.project_root, current.target)
        current_digest = sha256_bytes(target_path.read_bytes()) if target_path.is_file() else ""
        if (
            expected_applied_digest != applied.after_sha256
            or current_digest != applied.after_sha256
        ):
            raise ImprovementDeliveryError(
                "applied digest mismatch: refuse rollback without matching applied target digest"
            )

        deprecate_improvement_memory_block(
            self.project_root,
            improvement_id=current.improvement_id,
            target=current.target,
        )
        restored = sha256_bytes(target_path.read_bytes()) if target_path.is_file() else ""
        event = ImprovementRolledBackEvent(
            schema_version="1.0",
            seq=1,
            event_id=_event_id(
                f"improvement-rolled-back:{current.improvement_id}:{current.version}:{restored}"
            ),
            idempotency_key=(
                f"improvement-rolled-back:{current.improvement_id}:{current.version}:{restored}"
            ),
            ts=_utc_now(),
            improvement_id=current.improvement_id,
            expected_improvement_version=current.version,
            type="improvement_rolled_back",
            target=current.target,
            restored_sha256=restored,
            reason=reason.strip(),
        )
        self.store.append_and_rebuild([event])
        return MemoryRollbackReceipt(
            target=current.target, restored_sha256=restored, reason=reason.strip()
        )


# ---------------------------------------------------------------------------
# Shared load + memory operations
# ---------------------------------------------------------------------------


def _param_str(params: Mapping[str, object], key: str) -> str | None:
    value = params.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def load_improvement_delivery_operation(
    task: ExecutableTask,
    workspace: TaskWorkspace,
    context: RuntimeContext,
) -> TaskResult:
    """Load Improvement projection and reject non-matching delivery for the action."""
    del task
    improvement_id = _param_str(context.params, "improvement_id")
    action = _param_str(context.params, "delivery_action") or ""
    if not improvement_id:
        return task_failure(
            "invalid_input", "load-improvement-delivery: improvement_id param is required"
        )
    try:
        projection = _load_ledger(workspace.project_root).improvements[improvement_id]
    except (ImprovementDeliveryError, KeyError) as exc:
        return task_failure("invalid_input", f"load-improvement-delivery: {exc}")

    allowed = _ACTION_DELIVERIES.get(action)
    if allowed is not None and projection.delivery not in allowed:
        return task_failure(
            "invalid_input",
            f"load-improvement-delivery: delivery {projection.delivery.value!r} is not "
            f"valid for action {action!r}; reject before any target write",
        )

    delivery_dir = workspace.change_dir / "improvement-delivery" / improvement_id
    delivery_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "improvement_id": projection.improvement_id,
        "version": projection.version,
        "state": projection.state.value,
        "delivery": projection.delivery.value,
        "kind": projection.kind.value,
        "target": projection.target,
        "delivery_action": action,
        "generated_at": _utc_now(),
    }
    (delivery_dir / "context.json").write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return TaskResult(
        status="succeeded",
        value={
            "improvement_id": projection.improvement_id,
            "delivery": projection.delivery.value,
            "state": projection.state.value,
            "version": projection.version,
        },
    )


def _load_projection(workspace: TaskWorkspace, improvement_id: str) -> ImprovementProjection:
    return _load_ledger(workspace.project_root).improvements[improvement_id]


def evaluate_memory_improvement_operation(
    task: ExecutableTask,
    workspace: TaskWorkspace,
    context: RuntimeContext,
) -> TaskResult:
    del task
    improvement_id = _param_str(context.params, "improvement_id")
    if not improvement_id:
        return task_failure("invalid_input", "evaluate-memory-improvement: improvement_id required")
    try:
        projection = _load_projection(workspace, improvement_id)
        if projection.delivery is not DeliveryKind.MEMORY_PATCH:
            return task_failure(
                "invalid_input",
                "evaluate-memory-improvement: reject non-memory_patch before target write",
            )
        runner = context.params.get("eval_runner")
        eval_runner = runner if callable(runner) else None
        delivery = MemoryPatchDelivery(
            workspace.project_root,
            eval_runner=eval_runner,  # type: ignore[arg-type]
            engine_root=workspace.project_root,
        )
        if eval_runner is None:
            return task_failure(
                "invalid_input",
                "evaluate-memory-improvement: eval_runner must be injected via params",
            )
        receipt = delivery.evaluate(projection)
    except (ImprovementDeliveryError, KeyError) as exc:
        return task_failure("invalid_input", f"evaluate-memory-improvement: {exc}")
    return TaskResult(
        status="succeeded",
        value={
            "improvement_id": improvement_id,
            "outcome": receipt.outcome,
            "eval_run_id": receipt.eval_run_id,
            "staged_sha256": receipt.staged_sha256,
        },
    )


def apply_memory_improvement_operation(
    task: ExecutableTask,
    workspace: TaskWorkspace,
    context: RuntimeContext,
) -> TaskResult:
    del task
    improvement_id = _param_str(context.params, "improvement_id")
    expected = _param_str(context.params, "expected_target_sha256")
    if not improvement_id or not expected:
        return task_failure(
            "invalid_input",
            "apply-memory-improvement: improvement_id and expected_target_sha256 required",
        )
    try:
        projection = _load_projection(workspace, improvement_id)
        if projection.delivery is not DeliveryKind.MEMORY_PATCH:
            return task_failure(
                "invalid_input",
                "apply-memory-improvement: reject non-memory_patch before target write",
            )
        receipt = MemoryPatchDelivery(workspace.project_root).apply(
            projection, expected_target_sha256=expected
        )
    except (ImprovementDeliveryError, KeyError, AaError) as exc:
        return task_failure("invalid_input", f"apply-memory-improvement: {exc}")
    return TaskResult(
        status="succeeded",
        value={
            "improvement_id": improvement_id,
            "target": receipt.target,
            "after_sha256": receipt.after_sha256,
        },
    )


def rollback_memory_improvement_operation(
    task: ExecutableTask,
    workspace: TaskWorkspace,
    context: RuntimeContext,
) -> TaskResult:
    del task
    improvement_id = _param_str(context.params, "improvement_id")
    reason = _param_str(context.params, "delivery_reason")
    digest = _param_str(context.params, "expected_applied_digest")
    if not improvement_id or not reason or not digest:
        return task_failure(
            "invalid_input",
            "rollback-memory-improvement: improvement_id, delivery_reason, "
            "expected_applied_digest required",
        )
    try:
        projection = _load_projection(workspace, improvement_id)
        if projection.delivery is not DeliveryKind.MEMORY_PATCH:
            return task_failure(
                "invalid_input",
                "rollback-memory-improvement: reject non-memory_patch before target write",
            )
        receipt = MemoryPatchDelivery(workspace.project_root).rollback(
            projection, reason=reason, expected_applied_digest=digest
        )
    except (ImprovementDeliveryError, KeyError, AaError) as exc:
        return task_failure("invalid_input", f"rollback-memory-improvement: {exc}")
    return TaskResult(
        status="succeeded",
        value={
            "improvement_id": improvement_id,
            "restored_sha256": receipt.restored_sha256,
            "reason": receipt.reason,
        },
    )


__all__ = [
    "ImprovementDeliveryConflict",
    "ImprovementDeliveryError",
    "MemoryApplyReceipt",
    "MemoryEvalReceipt",
    "MemoryPatchDelivery",
    "MemoryRollbackReceipt",
    "apply_memory_improvement_operation",
    "evaluate_memory_improvement_operation",
    "load_improvement_delivery_operation",
    "rollback_memory_improvement_operation",
    "sha256_bytes",
]
