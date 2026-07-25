"""Knowledge-delta Improvement delivery: export + record_applied + graph ops.

Export writes only L2 proposal files under ``qa/improvements/knowledge-delta/``.
``knowledge.promote`` remains the sole L1 writer; record_applied verifies the
live L1 digest after external promotion.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from assurance_agent.artifacts.models.improvements import (
    DeliveryKind,
    ImprovementLedgerProjection,
    ImprovementProjection,
    ImprovementState,
)
from assurance_agent.artifacts.models.issues import Problem, ProblemProjection, ProblemStatus
from assurance_agent.knowledge.promote import (
    KnowledgePromoteError,
    assert_l1_sha256,
    l1_sha256,
    validate_improvement_knowledge_proposal,
)
from assurance_agent.workflow.graph.models import ExecutableTask, RuntimeContext, TaskResult
from assurance_agent.workflow.graph.task_runner import task_failure
from assurance_agent.workflow.graph.workspace import TaskWorkspace
from assurance_agent.workflow.improvements.events import (
    ImprovementAppliedEvent,
    ImprovementExportedEvent,
)
from assurance_agent.workflow.improvements.ledger import ProjectImprovementStore
from assurance_agent.workflow.improvements.memory_delivery import (
    ImprovementDeliveryError,
)

_PROHIBITED_STATUSES: frozenset[ProblemStatus] = frozenset(
    {
        "detected",
        "triaged",
        "in_progress",
        "verification_pending",
        "accepted_risk",
        "not_an_issue",
    }
)

__all__ = [
    "ImprovementDeliveryError",
    "KnowledgeDeltaDelivery",
    "KnowledgeExportReceipt",
    "export_knowledge_improvement_operation",
    "record_knowledge_improvement_applied_operation",
]


def _utc_now() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _event_id(idempotency_key: str) -> str:
    return "IMPEVT-" + hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:16]


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _load_ledger(project_root: Path) -> ImprovementLedgerProjection:
    path = project_root / "qa" / "improvements" / "improvements.json"
    if not path.is_file():
        raise ImprovementDeliveryError(f"improvement ledger projection missing at {path}")
    return ImprovementLedgerProjection.model_validate(
        json.loads(path.read_text(encoding="utf-8"))
    )


def _proposal_path(project_root: Path, improvement_id: str) -> Path:
    return (
        project_root
        / "qa"
        / "improvements"
        / "knowledge-delta"
        / f"{improvement_id}.proposal.yaml"
    )


def _has_l2_leaves(delta: Mapping[str, Any]) -> bool:
    caps = delta.get("capabilities") if isinstance(delta.get("capabilities"), Mapping) else {}
    adapters = caps.get("adapters") if isinstance(caps, Mapping) else {}
    adapters = adapters if isinstance(adapters, Mapping) else {}
    return bool(
        delta.get("accounts")
        or delta.get("auth")
        or delta.get("entities")
        or (isinstance(caps, Mapping) and caps.get("domain_factories"))
        or (isinstance(caps, Mapping) and caps.get("cleanup"))
        or any(adapters.get(layer) for layer in ("api", "e2e", "fuzz", "performance"))
    )


def assert_knowledge_eligibility(
    projection: ImprovementProjection,
    problems: Mapping[str, Problem],
) -> None:
    """Enforce resolved + human_confirmed + verified-scope Problem eligibility."""
    if projection.delivery is not DeliveryKind.KNOWLEDGE_DELTA:
        raise ImprovementDeliveryError("knowledge export requires knowledge_delta delivery")
    problem_ids = projection.source_refs.problem_ids
    if not problem_ids:
        raise ImprovementDeliveryError(
            "knowledge eligibility requires at least one cited problem_id"
        )
    matched = False
    for problem_id in problem_ids:
        problem = problems.get(problem_id)
        if problem is None:
            continue
        matched = True
        if problem.status in _PROHIBITED_STATUSES:
            raise ImprovementDeliveryError(
                f"knowledge eligibility rejected: problem {problem_id} status={problem.status}"
            )
        if problem.status != "resolved":
            raise ImprovementDeliveryError(
                f"knowledge eligibility rejected: problem {problem_id} status={problem.status}"
            )
        if problem.assessment.authority != "human_confirmed":
            raise ImprovementDeliveryError(
                "knowledge eligibility requires assessment.authority=human_confirmed"
            )
        if problem.resolution is None or not problem.resolution.verification_scope:
            raise ImprovementDeliveryError(
                "knowledge eligibility requires a verified resolution scope"
            )
    if not matched:
        raise ImprovementDeliveryError(
            "knowledge eligibility requires at least one cited Problem snapshot "
            "in the pinned Retro/Issue source"
        )


@dataclass(frozen=True)
class KnowledgeExportReceipt:
    sha256: str
    created: bool
    artifact_path: str


class KnowledgeDeltaDelivery:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.store = ProjectImprovementStore(project_root)

    def _reload(self, improvement_id: str) -> ImprovementProjection:
        item = _load_ledger(self.project_root).improvements.get(improvement_id)
        if item is None:
            raise ImprovementDeliveryError(f"improvement {improvement_id!r} not found")
        return item

    def export(
        self,
        improvement: ImprovementProjection,
        *,
        problems: Mapping[str, Problem],
    ) -> KnowledgeExportReceipt:
        current = self._reload(improvement.improvement_id)
        if current.state not in {ImprovementState.APPROVED, ImprovementState.EXPORTED}:
            raise ImprovementDeliveryError(
                f"export requires approved or exported state, got {current.state.value}"
            )
        assert_knowledge_eligibility(current, problems)
        if current.knowledge_delta is None:
            raise ImprovementDeliveryError("knowledge_delta payload is required")

        delta = current.knowledge_delta.model_dump(mode="json")
        if not _has_l2_leaves(delta):
            raise ImprovementDeliveryError("invalid L2 knowledge_delta: no leaves")

        path = _proposal_path(self.project_root, current.improvement_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Never touch L1 here — only write the Improvement-scoped L2 proposal.
        payload_bytes = yaml.safe_dump(delta, sort_keys=True, allow_unicode=True).encode("utf-8")
        path.write_bytes(payload_bytes)
        try:
            validate_improvement_knowledge_proposal(path)
        except KnowledgePromoteError as exc:
            path.unlink(missing_ok=True)
            raise ImprovementDeliveryError(f"L2 semantic rejection: {exc}") from exc

        digest = _sha256_bytes(payload_bytes)
        created = True
        # Hash-idempotent re-export: identical bytes keep created=False semantics for ledger.
        # First write always created; second call with same bytes is idempotent export event.
        rel = path.relative_to(self.project_root).as_posix()
        if current.state is ImprovementState.EXPORTED:
            created = False
        if current.state is ImprovementState.APPROVED:
            event = ImprovementExportedEvent(
                schema_version="1.0",
                seq=1,
                event_id=_event_id(
                    f"improvement-exported:{current.improvement_id}:{current.version}:{digest}"
                ),
                idempotency_key=(
                    f"improvement-exported:{current.improvement_id}:{current.version}:{digest}"
                ),
                ts=_utc_now(),
                improvement_id=current.improvement_id,
                expected_improvement_version=current.version,
                type="improvement_exported",
                artifact_path=rel,
                artifact_sha256=digest,
            )
            self.store.append_and_rebuild([event])
        return KnowledgeExportReceipt(sha256=digest, created=created, artifact_path=rel)

    def record_applied(
        self,
        improvement: ImprovementProjection,
        *,
        actor: str,
        reason: str,
        artifact_digest: str,
        expected_l1_digest: str,
    ) -> None:
        if not actor or not actor.strip():
            raise ImprovementDeliveryError("record_applied requires actor")
        if not reason or not reason.strip():
            raise ImprovementDeliveryError("record_applied requires reason")
        if not artifact_digest or not artifact_digest.strip():
            raise ImprovementDeliveryError("record_applied requires artifact digest")
        if not expected_l1_digest or not expected_l1_digest.strip():
            raise ImprovementDeliveryError("record_applied requires expected L1 digest")

        current = self._reload(improvement.improvement_id)
        if current.delivery is not DeliveryKind.KNOWLEDGE_DELTA:
            raise ImprovementDeliveryError("record_applied requires knowledge_delta delivery")
        if current.state is not ImprovementState.EXPORTED:
            raise ImprovementDeliveryError(
                f"record_applied requires exported state, got {current.state.value}"
            )
        path = _proposal_path(self.project_root, current.improvement_id)
        if not path.is_file():
            raise ImprovementDeliveryError(f"exported knowledge proposal missing at {path}")
        on_disk = _sha256_bytes(path.read_bytes())
        if on_disk != artifact_digest:
            raise ImprovementDeliveryError(
                f"artifact digest mismatch: expected {artifact_digest}, found {on_disk}"
            )
        try:
            assert_l1_sha256(self.project_root, expected_l1_digest)
        except KnowledgePromoteError as exc:
            raise ImprovementDeliveryError(str(exc)) from exc

        after_l1 = l1_sha256(self.project_root)
        receipt = {
            "actor": actor.strip(),
            "reason": reason.strip(),
            "artifact_digest": artifact_digest,
            "l1_digest": after_l1,
            "recorded_at": _utc_now(),
        }
        receipt_sha = _sha256_bytes(
            json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode("utf-8")
        )
        event = ImprovementAppliedEvent(
            schema_version="1.0",
            seq=1,
            event_id=_event_id(
                f"improvement-applied:{current.improvement_id}:{current.version}:{after_l1}"
            ),
            idempotency_key=(
                f"improvement-applied:{current.improvement_id}:{current.version}:{after_l1}"
            ),
            ts=_utc_now(),
            improvement_id=current.improvement_id,
            expected_improvement_version=current.version,
            type="improvement_applied",
            target=".aa/data-knowledge.yaml",
            before_sha256=expected_l1_digest,
            after_sha256=after_l1,
            receipt_sha256=receipt_sha,
        )
        self.store.append_and_rebuild([event])


def _load_problems(project_root: Path) -> dict[str, Problem]:
    path = project_root / "qa" / "issues" / "problems.json"
    if not path.is_file():
        return {}
    projection = ProblemProjection.model_validate(json.loads(path.read_text(encoding="utf-8")))
    return {problem.problem_id: problem for problem in projection.problems}


def _param_str(params: Mapping[str, object], key: str) -> str | None:
    value = params.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def export_knowledge_improvement_operation(
    task: ExecutableTask,
    workspace: TaskWorkspace,
    context: RuntimeContext,
) -> TaskResult:
    del task
    improvement_id = _param_str(context.params, "improvement_id")
    if not improvement_id:
        return task_failure(
            "invalid_input", "export-knowledge-improvement: improvement_id required"
        )
    try:
        projection = _load_ledger(workspace.project_root).improvements[improvement_id]
        if projection.delivery is not DeliveryKind.KNOWLEDGE_DELTA:
            return task_failure(
                "invalid_input",
                "export-knowledge-improvement: reject non-knowledge_delta before target write",
            )
        problems = _load_problems(workspace.project_root)
        receipt = KnowledgeDeltaDelivery(workspace.project_root).export(
            projection, problems=problems
        )
    except (ImprovementDeliveryError, KeyError) as exc:
        return task_failure("invalid_input", f"export-knowledge-improvement: {exc}")
    return TaskResult(
        status="succeeded",
        value={
            "improvement_id": improvement_id,
            "artifact_sha256": receipt.sha256,
            "created": receipt.created,
            "artifact_path": receipt.artifact_path,
        },
    )


def record_knowledge_improvement_applied_operation(
    task: ExecutableTask,
    workspace: TaskWorkspace,
    context: RuntimeContext,
) -> TaskResult:
    del task
    improvement_id = _param_str(context.params, "improvement_id")
    actor = _param_str(context.params, "delivery_actor")
    reason = _param_str(context.params, "delivery_reason")
    digest = _param_str(context.params, "artifact_digest")
    l1_digest = _param_str(context.params, "expected_l1_digest")
    if not improvement_id or not actor or not reason or not digest or not l1_digest:
        return task_failure(
            "invalid_input",
            "record-knowledge-improvement-applied: improvement_id, delivery_actor, "
            "delivery_reason, artifact_digest, expected_l1_digest required",
        )
    try:
        projection = _load_ledger(workspace.project_root).improvements[improvement_id]
        if projection.delivery is not DeliveryKind.KNOWLEDGE_DELTA:
            return task_failure(
                "invalid_input",
                "record-knowledge-improvement-applied: reject non-knowledge_delta before write",
            )
        KnowledgeDeltaDelivery(workspace.project_root).record_applied(
            projection,
            actor=actor,
            reason=reason,
            artifact_digest=digest,
            expected_l1_digest=l1_digest,
        )
    except (ImprovementDeliveryError, KeyError) as exc:
        return task_failure("invalid_input", f"record-knowledge-improvement-applied: {exc}")
    return TaskResult(
        status="succeeded",
        value={"improvement_id": improvement_id, "artifact_digest": digest},
    )
