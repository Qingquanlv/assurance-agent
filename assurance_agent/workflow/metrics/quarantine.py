"""``operation:materialize-quarantine-projection`` — C3 flaky isolation ledger.

Reads discovery replay receipts (+ optional prior projection and CE obligation
maps), folds enter/release rules, writes ``inspect/quarantine-projection.json``.
A2/A4 collectors load this artifact from disk when present (empty = no quarantine).
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path

import yaml
from pydantic import ValidationError

from assurance_agent.artifacts.models.discovery import Counterexample, ReplayAttemptReceipt
from assurance_agent.artifacts.models.quarantine import (
    DEFAULT_RELEASE_CONSECUTIVE_SUCCESSES,
    QUARANTINE_PROJECTION_REL,
    QuarantineProjection,
    QuarantineSubjectKind,
)
from assurance_agent.evidence.quarantine import (
    active_subject_keys,
    fold_receipts_into_projection,
    infer_subject_kind,
)
from assurance_agent.workflow.discovery.replay_receipts import (
    ReplayReceiptIntegrityError,
    load_replay_attempt_receipts,
)
from assurance_agent.workflow.execution.evidence import atomic_write_bytes
from assurance_agent.workflow.graph.models import ExecutableTask, RuntimeContext, TaskResult
from assurance_agent.workflow.graph.task_runner import task_failure, task_with
from assurance_agent.workflow.graph.workspace import TaskWorkspace

_CE_DIR_REL = "discovery/counterexamples"


class QuarantineIntegrityError(ValueError):
    """Quarantine source/projection exists but cannot be trusted."""


def load_quarantine_projection(change_dir: Path) -> QuarantineProjection | None:
    """Load ``inspect/quarantine-projection.json`` when present and valid."""
    path = Path(change_dir) / QUARANTINE_PROJECTION_REL
    if not path.is_file():
        return None
    try:
        return QuarantineProjection.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, ValidationError, json.JSONDecodeError) as err:
        raise QuarantineIntegrityError(
            f"quarantine projection {QUARANTINE_PROJECTION_REL} is invalid: {err}"
        ) from err


def load_active_quarantine_keys(change_dir: Path) -> frozenset[str]:
    """Active subject_keys for A2/A4; a corrupt projection fails closed."""
    return active_subject_keys(load_quarantine_projection(change_dir))


def _load_counterexample_obligation_map(change_dir: Path) -> dict[str, tuple[str, ...]]:
    root = Path(change_dir) / _CE_DIR_REL
    if not root.is_dir():
        return {}
    out: dict[str, tuple[str, ...]] = {}
    for path in sorted(root.glob("*.yaml")) + sorted(root.glob("*.yml")):
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
            ce = Counterexample.model_validate(raw)
        except (OSError, ValueError, ValidationError, yaml.YAMLError) as err:
            rel = path.relative_to(change_dir).as_posix()
            raise QuarantineIntegrityError(f"counterexample {rel} is invalid: {err}") from err
        if path.stem != ce.counterexample_id:
            rel = path.relative_to(change_dir).as_posix()
            raise QuarantineIntegrityError(
                f"counterexample {rel} identity {ce.counterexample_id!r} does not match filename"
            )
        if ce.counterexample_id in out:
            raise QuarantineIntegrityError(f"duplicate counterexample identity {ce.counterexample_id!r}")
        out[ce.counterexample_id] = tuple(ce.obligation_ids)
    return out


def _group_receipts_by_subject(
    receipts: Sequence[ReplayAttemptReceipt],
    obligation_map: Mapping[str, Sequence[str]],
) -> dict[tuple[QuarantineSubjectKind, str], list[ReplayAttemptReceipt]]:
    grouped: dict[tuple[QuarantineSubjectKind, str], list[ReplayAttemptReceipt]] = defaultdict(list)
    for receipt in receipts:
        if receipt.counterexample_id not in obligation_map:
            raise QuarantineIntegrityError(
                f"replay receipt references missing counterexample {receipt.counterexample_id!r}"
            )
        obligation_ids = obligation_map[receipt.counterexample_id]
        if not obligation_ids:
            # A present, valid CE may explicitly have no mapped obligations.
            continue
        for obligation_id in obligation_ids:
            kind = infer_subject_kind(obligation_id)
            grouped[(kind, obligation_id)].append(receipt)
    return grouped


def materialize_quarantine_projection(
    *,
    change_dir: Path,
    change_id: str,
    entered_at: str,
    release_requires: int = DEFAULT_RELEASE_CONSECUTIVE_SUCCESSES,
) -> QuarantineProjection:
    """Fold receipts (+ prior) and write ``inspect/quarantine-projection.json``."""
    prior = load_quarantine_projection(change_dir)
    try:
        receipts = load_replay_attempt_receipts(change_dir)
    except ReplayReceiptIntegrityError as err:
        raise QuarantineIntegrityError(str(err)) from err
    obligation_map = _load_counterexample_obligation_map(change_dir)
    grouped = _group_receipts_by_subject(receipts, obligation_map)
    projection = fold_receipts_into_projection(
        prior,
        change_id=change_id,
        subject_receipts=grouped,
        entered_at=entered_at,
        release_requires=release_requires,
    )
    path = Path(change_dir) / QUARANTINE_PROJECTION_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(projection.model_dump(mode="json"), indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    atomic_write_bytes(path, payload)
    return projection


def materialize_quarantine_projection_operation(
    task: ExecutableTask,
    workspace: TaskWorkspace,
    context: RuntimeContext,
) -> TaskResult:
    with_map = task_with(task)
    entered_at = str(with_map.get("entered_at") or context.params.get("entered_at") or "")
    if not entered_at:
        # Deterministic fallback when callers omit a clock — ISO date from change id
        # is not inventable; use a fixed epoch marker so the fold stays pure-ish.
        entered_at = "1970-01-01T00:00:00Z"
    release_raw = with_map.get("release_requires")
    release_requires = (
        int(release_raw)
        if isinstance(release_raw, (int, str)) and str(release_raw).isdigit()
        else DEFAULT_RELEASE_CONSECUTIVE_SUCCESSES
    )
    try:
        projection = materialize_quarantine_projection(
            change_dir=workspace.change_dir,
            change_id=context.change_id,
            entered_at=entered_at,
            release_requires=release_requires,
        )
    except QuarantineIntegrityError as err:
        return task_failure("invalid_input", str(err))
    return TaskResult(
        status="succeeded",
        value={
            "change_id": context.change_id,
            "written": True,
            "path": QUARANTINE_PROJECTION_REL,
            "active_count": len(active_subject_keys(projection)),
            "entry_count": len(projection.entries),
        },
    )


__all__ = [
    "QUARANTINE_PROJECTION_REL",
    "QuarantineIntegrityError",
    "load_active_quarantine_keys",
    "load_quarantine_projection",
    "materialize_quarantine_projection",
    "materialize_quarantine_projection_operation",
]
