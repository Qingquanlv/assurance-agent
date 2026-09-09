"""Stage Retro results under the Attempt's resource lease; only Kernel promotes."""

from __future__ import annotations

import hashlib
from pathlib import Path

from graph_engine.canonical import canonical_json_bytes
from graph_engine.plugin_api import TaskContext
from assurance_intake.contracts import EvidenceArtifactRefV1
from assurance_improvement.contracts.improvements import ImprovementLedgerProjection, ReconcileResultV1
from assurance_improvement.contracts.retro import (
    RetroReconcileInputV1,
    RetroReconcileResultV1,
    RetroRunStatus,
)
from assurance_improvement.operations.common import InputError
from assurance_improvement.operations.retro import (
    ReconcileInput,
    reconcile_improvements,
    validate_candidates,
)

LEDGER_PATH = "qa/improvements/ledger.json"


def _path(root: Path, relative: str) -> Path:
    path = root / relative
    path.resolve().relative_to(root.resolve())
    for part in (path, *path.parents):
        if part == root:
            break
        if part.is_symlink():
            raise InputError(f"Retro path must not contain symlinks: {relative}")
    return path


def stage_reconciliation(payload: RetroReconcileInputV1, context: TaskContext) -> RetroReconcileResultV1:
    if payload.context.dry_run:
        raise InputError("persisted Retro cannot use a dry-run context")
    # The canonical store is read only after the Attempt acquires its read/write lease.
    source = _path(context.project_root, LEDGER_PATH)
    current = (
        ImprovementLedgerProjection.model_validate_json(source.read_bytes())
        if source.exists()
        else ImprovementLedgerProjection(schema_version="1", last_seq=0, improvements={}, by_fingerprint={})
    )
    candidates = validate_candidates(payload.context, payload.candidates)
    reconciliation = ReconcileResultV1.model_validate(
        reconcile_improvements(
            ReconcileInput(
                context=payload.context,
                candidates=payload.candidates,
                current=current,
                ts=payload.context.generated_at,
            )
        )
    )
    ledger = ImprovementLedgerProjection(
        schema_version="1",
        last_seq=reconciliation.last_seq,
        improvements=reconciliation.improvements,
        by_fingerprint=reconciliation.by_fingerprint,
    )
    status = RetroRunStatus(
        retro_id=payload.context.retro_id,
        result="completed" if payload.context.integrity.status == "complete" else "completed_with_gaps",
        improvement_ids=tuple(reconciliation.improvement_ids),
    )
    prefix = f"qa/changes/{payload.change_id}/retro"
    documents = {
        f"{prefix}/context.json": payload.context,
        f"{prefix}/candidates.json": candidates,
        f"{prefix}/reconciliation.json": reconciliation,
        f"{prefix}/status.json": status,
        LEDGER_PATH: ledger,
    }
    refs = []
    for relative, document in sorted(documents.items()):
        data = canonical_json_bytes(document.model_dump(mode="json")) + b"\n"
        destination = _path(context.write_root, relative)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        if relative != LEDGER_PATH:
            refs.append(EvidenceArtifactRefV1(path=relative, digest=hashlib.sha256(data).hexdigest()))
    return RetroReconcileResultV1(reconciliation=reconciliation, status=status, artifact_refs=tuple(refs))
