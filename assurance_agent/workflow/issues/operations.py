"""Issue-domain operation handlers registered in default_operations().

Currently contains:
    collect_observations_operation — operation:collect-observations

Writes (in one logical write-set):
    change:inspect/observations.json
    change:inspect/issue-evidence-manifest.json
    change:issues/events.jsonl       (via ChangeIssueStore)
    change:issues/snapshot.json      (via ChangeIssueStore)

Returns: batch_id, evidence_bundle_digest, abnormal_count.
Idempotent: replaying with the same evidence produces the same output.
Hard failure (invalid_input) on missing/corrupt authoritative execution evidence.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from assurance_agent.artifacts.models.issues import (
    ChangeIssueSnapshot,
    ObservationDocument,
)
from assurance_agent.workflow.execution.evidence import EvidenceError
from assurance_agent.workflow.graph.models import ExecutableTask, RuntimeContext, TaskResult
from assurance_agent.workflow.graph.task_runner import task_failure
from assurance_agent.workflow.graph.workspace import TaskWorkspace
from assurance_agent.workflow.issues.collector import collect_observations
from assurance_agent.workflow.issues.events import ObservationRecordedEvent
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
