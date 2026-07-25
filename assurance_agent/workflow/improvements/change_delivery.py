"""Change-draft Improvement delivery: export + record_applied + graph ops."""

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
from assurance_agent.workflow.graph.models import ExecutableTask, RuntimeContext, TaskResult
from assurance_agent.workflow.graph.task_runner import task_failure
from assurance_agent.workflow.graph.workspace import TaskWorkspace
from assurance_agent.workflow.improvements.events import (
    ImprovementAppliedEvent,
    ImprovementExportedEvent,
    ImprovementReworkRequestedEvent,
    read_improvement_events,
)
from assurance_agent.workflow.improvements.ledger import ProjectImprovementStore
from assurance_agent.workflow.improvements.memory_delivery import (
    ImprovementDeliveryConflict,
    ImprovementDeliveryError,
)

# Re-export for callers/tests.
__all__ = [
    "ChangeDraftDelivery",
    "ChangeExportReceipt",
    "ImprovementDeliveryConflict",
    "ImprovementDeliveryError",
    "export_change_improvement_operation",
    "record_change_improvement_applied_operation",
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


def _draft_path(project_root: Path, improvement_id: str) -> Path:
    return project_root / "qa" / "improvements" / "drafts" / f"{improvement_id}.yaml"


def _draft_document(projection: ImprovementProjection) -> dict[str, Any]:
    return {
        "improvement_id": projection.improvement_id,
        "improvement_version": projection.version,
        "kind": projection.kind.value,
        "source_refs": projection.source_refs.model_dump(mode="json"),
        "target": projection.target,
        "proposed_change": projection.proposed_change,
        "verification": projection.verification.model_dump(mode="json"),
    }


def _canonicalize_yaml(document: dict[str, Any]) -> bytes:
    # Content hash excludes content_sha256 itself; file includes it after hashing.
    body = dict(document)
    body.pop("content_sha256", None)
    dumped = yaml.safe_dump(body, sort_keys=True, allow_unicode=True)
    digest = _sha256_bytes(dumped.encode("utf-8"))
    body["content_sha256"] = digest
    return yaml.safe_dump(body, sort_keys=True, allow_unicode=True).encode("utf-8")


def _rework_unlocks_draft_overwrite(
    project_root: Path,
    *,
    improvement_id: str,
    prior_draft_version: int,
) -> bool:
    """True when an explicit rework covers the prior draft version.

    Evidence-link (or any other version bump) alone must not unlock overwrite.
    """
    events = read_improvement_events(project_root / "qa/improvements/events.jsonl")
    for event in events:
        if (
            isinstance(event, ImprovementReworkRequestedEvent)
            and event.improvement_id == improvement_id
            and event.expected_improvement_version >= prior_draft_version
        ):
            return True
    return False


@dataclass(frozen=True)
class ChangeExportReceipt:
    sha256: str
    created: bool
    artifact_path: str


class ChangeDraftDelivery:
    """Project-scoped change draft export under ``qa/improvements/drafts/``."""

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.store = ProjectImprovementStore(project_root)

    def _reload(self, improvement_id: str) -> ImprovementProjection:
        item = _load_ledger(self.project_root).improvements.get(improvement_id)
        if item is None:
            raise ImprovementDeliveryError(f"improvement {improvement_id!r} not found")
        return item

    def export(self, improvement: ImprovementProjection) -> ChangeExportReceipt:
        current = self._reload(improvement.improvement_id)
        if current.delivery is not DeliveryKind.CHANGE_DRAFT:
            raise ImprovementDeliveryError(
                f"change draft export requires delivery=change_draft, got {current.delivery.value}"
            )
        if current.state not in {ImprovementState.APPROVED, ImprovementState.EXPORTED}:
            raise ImprovementDeliveryError(
                f"export requires approved or exported state, got {current.state.value}"
            )

        path = _draft_path(self.project_root, current.improvement_id)
        rel = path.relative_to(self.project_root).as_posix() if path.exists() else (
            f"qa/improvements/drafts/{current.improvement_id}.yaml"
        )

        # Already exported: hash-idempotent re-export returns the on-disk artifact.
        if current.state is ImprovementState.EXPORTED:
            if not path.is_file():
                raise ImprovementDeliveryError(f"exported draft missing at {path}")
            existing = path.read_bytes()
            prior = yaml.safe_load(existing.decode("utf-8"))
            digest = (
                prior.get("content_sha256")
                if isinstance(prior, dict) and isinstance(prior.get("content_sha256"), str)
                else _sha256_bytes(existing)
            )
            return ChangeExportReceipt(
                sha256=str(digest), created=False, artifact_path=rel
            )

        payload_bytes = _canonicalize_yaml(_draft_document(current))
        embedded = yaml.safe_load(payload_bytes.decode("utf-8")).get("content_sha256")
        digest = embedded if isinstance(embedded, str) and embedded else _sha256_bytes(payload_bytes)

        created = True
        if path.is_file():
            existing = path.read_bytes()
            if existing == payload_bytes:
                created = False
            else:
                try:
                    prior = yaml.safe_load(existing.decode("utf-8"))
                except yaml.YAMLError as exc:
                    raise ImprovementDeliveryConflict(
                        f"change draft conflict for {current.improvement_id}: unreadable prior bytes"
                    ) from exc
                prior_version = prior.get("improvement_version") if isinstance(prior, dict) else None
                if isinstance(prior_version, int) and _rework_unlocks_draft_overwrite(
                    self.project_root,
                    improvement_id=current.improvement_id,
                    prior_draft_version=prior_version,
                ):
                    created = True
                else:
                    raise ImprovementDeliveryConflict(
                        f"change draft conflict for {current.improvement_id}: different bytes"
                    )

        if created:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload_bytes)

        rel = path.relative_to(self.project_root).as_posix()
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

        return ChangeExportReceipt(sha256=digest, created=created, artifact_path=rel)

    def record_applied(
        self,
        improvement: ImprovementProjection,
        *,
        actor: str,
        reason: str,
        artifact_digest: str,
    ) -> None:
        if not actor or not actor.strip():
            raise ImprovementDeliveryError("record_applied requires actor")
        if not reason or not reason.strip():
            raise ImprovementDeliveryError("record_applied requires reason")
        if not artifact_digest or not artifact_digest.strip():
            raise ImprovementDeliveryError("record_applied requires artifact digest")

        current = self._reload(improvement.improvement_id)
        if current.delivery is not DeliveryKind.CHANGE_DRAFT:
            raise ImprovementDeliveryError("record_applied requires change_draft delivery")
        if current.state is not ImprovementState.EXPORTED:
            raise ImprovementDeliveryError(
                f"record_applied requires exported state, got {current.state.value}"
            )
        path = _draft_path(self.project_root, current.improvement_id)
        if not path.is_file():
            raise ImprovementDeliveryError(f"exported draft missing at {path}")
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        on_disk = raw.get("content_sha256") if isinstance(raw, dict) else None
        if on_disk != artifact_digest:
            raise ImprovementDeliveryError(
                f"artifact digest mismatch: expected {artifact_digest}, found {on_disk}"
            )

        receipt = {
            "actor": actor.strip(),
            "reason": reason.strip(),
            "artifact_digest": artifact_digest,
            "artifact_path": path.relative_to(self.project_root).as_posix(),
            "recorded_at": _utc_now(),
        }
        receipt_sha = _sha256_bytes(
            json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode("utf-8")
        )
        event = ImprovementAppliedEvent(
            schema_version="1.0",
            seq=1,
            event_id=_event_id(
                f"improvement-applied:{current.improvement_id}:{current.version}:{artifact_digest}"
            ),
            idempotency_key=(
                f"improvement-applied:{current.improvement_id}:{current.version}:{artifact_digest}"
            ),
            ts=_utc_now(),
            improvement_id=current.improvement_id,
            expected_improvement_version=current.version,
            type="improvement_applied",
            target=path.relative_to(self.project_root).as_posix(),
            before_sha256=artifact_digest,
            after_sha256=artifact_digest,
            receipt_sha256=receipt_sha,
        )
        self.store.append_and_rebuild([event])


def _param_str(params: Mapping[str, object], key: str) -> str | None:
    value = params.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def export_change_improvement_operation(
    task: ExecutableTask,
    workspace: TaskWorkspace,
    context: RuntimeContext,
) -> TaskResult:
    del task
    improvement_id = _param_str(context.params, "improvement_id")
    if not improvement_id:
        return task_failure("invalid_input", "export-change-improvement: improvement_id required")
    try:
        projection = _load_ledger(workspace.project_root).improvements[improvement_id]
        if projection.delivery is not DeliveryKind.CHANGE_DRAFT:
            return task_failure(
                "invalid_input",
                "export-change-improvement: reject non-change_draft before target write",
            )
        receipt = ChangeDraftDelivery(workspace.project_root).export(projection)
    except (ImprovementDeliveryError, ImprovementDeliveryConflict, KeyError) as exc:
        return task_failure("invalid_input", f"export-change-improvement: {exc}")
    return TaskResult(
        status="succeeded",
        value={
            "improvement_id": improvement_id,
            "artifact_sha256": receipt.sha256,
            "created": receipt.created,
            "artifact_path": receipt.artifact_path,
        },
    )


def record_change_improvement_applied_operation(
    task: ExecutableTask,
    workspace: TaskWorkspace,
    context: RuntimeContext,
) -> TaskResult:
    del task
    improvement_id = _param_str(context.params, "improvement_id")
    actor = _param_str(context.params, "delivery_actor")
    reason = _param_str(context.params, "delivery_reason")
    digest = _param_str(context.params, "artifact_digest")
    if not improvement_id or not actor or not reason or not digest:
        return task_failure(
            "invalid_input",
            "record-change-improvement-applied: improvement_id, delivery_actor, "
            "delivery_reason, artifact_digest required",
        )
    try:
        projection = _load_ledger(workspace.project_root).improvements[improvement_id]
        if projection.delivery is not DeliveryKind.CHANGE_DRAFT:
            return task_failure(
                "invalid_input",
                "record-change-improvement-applied: reject non-change_draft before write",
            )
        ChangeDraftDelivery(workspace.project_root).record_applied(
            projection, actor=actor, reason=reason, artifact_digest=digest
        )
    except (ImprovementDeliveryError, KeyError) as exc:
        return task_failure("invalid_input", f"record-change-improvement-applied: {exc}")
    return TaskResult(
        status="succeeded",
        value={"improvement_id": improvement_id, "artifact_digest": digest},
    )
