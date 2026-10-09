"""Publish redacted Kernel history for explicit, digest-bound Retro source selection."""

from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
import tempfile
from collections.abc import Awaitable, Callable, Sequence
from typing import cast

from graph_engine.attempts.orchestration.checkpoint import AttemptCheckpoint
from graph_engine.canonical import JSONValue, canonical_digest, canonical_json_bytes
from graph_engine.persistence.attempt_checkpoint import encode_attempt_checkpoint, decode_attempt_checkpoint
from graph_engine.errors import GraphEngineError

from assurance_improvement.contracts.retro import (
    RetroIntegrity,
    TaskFailureEvidenceEntry,
    WorkflowRuntimeEvidenceV2,
)
from assurance_intake.contracts import EvidenceArtifactRefV1
from assurance_product.change_workspace import ChangeWorkspace


async def export_runtime_evidence(
    workspace: ChangeWorkspace,
    read_records: Callable[[], Awaitable[tuple[AttemptCheckpoint, ...]]],
    *,
    invocation_id: str,
) -> None:
    """A diagnostic export must not override graph delivery or terminalization."""
    try:
        publish_runtime_evidence(
            workspace,
            project_runtime_evidence(
                await read_records(),
                change_id=workspace.change_id,
                invocation_id=invocation_id,
            ),
        )
    except (OSError, ValueError, sqlite3.Error, GraphEngineError) as error:
        logging.getLogger(__name__).warning(
            "workflow_evidence_export_failed (%s): no current runtime snapshot was exported; "
            "do not use a previous snapshot to claim this run's failure coverage is complete",
            type(error).__name__,
        )


async def snapshot_runtime_evidence(
    workspace: ChangeWorkspace,
    read_records: Callable[[], Awaitable[tuple[AttemptCheckpoint, ...]]],
    *,
    invocation_id: str,
) -> EvidenceArtifactRefV1:
    """Bind the current checkpoints before Retro; keep the later diagnostic export separate."""
    document = project_runtime_evidence(
        await read_records(), change_id=workspace.change_id, invocation_id=invocation_id
    )
    return publish_runtime_evidence(workspace, document, stage="pre-retro")


def project_runtime_evidence(
    records: Sequence[AttemptCheckpoint],
    *,
    change_id: str,
    invocation_id: str,
) -> WorkflowRuntimeEvidenceV2:
    """Retain every Attempt and compare immutable terminal lease/revision metadata."""
    selected = [record for record in records if record.invocation_id == invocation_id]
    if len({record.attempt_key.digest for record in records}) != len(records):
        raise ValueError("duplicate Attempt checkpoint")
    # Revalidate full records, including mutable nested JSON, before publishing evidence.
    selected = [decode_attempt_checkpoint(encode_attempt_checkpoint(record)) for record in selected]
    entries: list[TaskFailureEvidenceEntry] = []
    for record in selected:
        terminal = record.terminal
        if terminal is None or terminal.resolution_kind == "committed":
            continue
        later_commit = any(
            later.terminal is not None
            and later.terminal.resolution_kind == "committed"
            and (later.semantic_node_id, later.input_digest) == (record.semantic_node_id, record.input_digest)
            and later.terminal_fencing_token is not None
            and record.terminal_fencing_token is not None
            and (
                later.terminal_fencing_token > record.terminal_fencing_token
                or (
                    later.attempt_key == record.attempt_key
                    and later.terminal_revision is not None
                    and record.terminal_revision is not None
                    and later.terminal_revision > record.terminal_revision
                )
            )
            for later in selected
        )
        entries.append(
            TaskFailureEvidenceEntry(
                evidence_id=f"attempt-failure-{canonical_digest([record.attempt_key.digest, record.terminal_revision])}",
                change_id=change_id,
                task_id=record.semantic_node_id,
                attempt_id=record.attempt_key.digest,
                node_id=record.semantic_node_id,
                error_kind=terminal.failure_kind or terminal.resolution_kind,
                message_fingerprint=hashlib.sha256(
                    (terminal.message or terminal.reason).encode()
                ).hexdigest(),
                recovered=True if later_commit else None,
            )
        )
    incomplete = any(record.terminal is None or not record.released for record in selected)
    reasons = ("runtime_attempts_incomplete",) if incomplete else ()
    if not selected:
        reasons = ("runtime_invocation_evidence_absent",)
    return WorkflowRuntimeEvidenceV2(
        change_id=change_id,
        invocation_id=invocation_id,
        checkpoint_digest=canonical_digest(
            cast(JSONValue, sorted(canonical_digest(record.canonical_projection()) for record in selected))
        ),
        entries=tuple(sorted(entries, key=lambda entry: entry.evidence_id)),
        integrity=RetroIntegrity(status="incomplete" if reasons else "complete", reasons=reasons),
    )


def publish_runtime_evidence(
    workspace: ChangeWorkspace,
    document: WorkflowRuntimeEvidenceV2,
    *,
    stage: str = "post-run",
) -> EvidenceArtifactRefV1:
    """Export a runtime projection, never raw checkpoints, prompts or credentials.

    Consumers must explicitly include its exact byte digest in Retro source_refs.
    Pre-Retro snapshots are immutable and content-addressed so replay cannot
    change bytes already bound to Retro. Post-run projections remain replaceable.
    This is not a formal issue or a skill-drift audit.
    """
    if document.change_id != workspace.change_id:
        raise ValueError("runtime evidence change does not match workspace")
    if stage not in {"pre-retro", "post-run"}:
        raise ValueError("unknown runtime evidence stage")
    root = f"qa/results/workflow/{canonical_digest(document.invocation_id)}"
    encoded = canonical_json_bytes(document.model_dump(mode="json")) + b"\n"
    content_digest = hashlib.sha256(encoded).hexdigest()
    relative = (
        f"{root}/pre-retro/{content_digest}/workflow-evidence.json"
        if stage == "pre-retro"
        else f"{root}/workflow-evidence.json"
    )
    path = workspace.paths.project_root / relative
    for parent in reversed(path.parents):
        if parent == workspace.paths.project_root or workspace.paths.project_root in parent.parents:
            if parent.is_symlink():
                raise ValueError("runtime evidence path contains a symlink")
            parent.mkdir(exist_ok=True)
    if path.is_symlink():
        raise ValueError("runtime evidence path contains a symlink")
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".workflow-", delete=False) as pending:
        pending.write(encoded)
        pending.flush()
        os.fsync(pending.fileno())
    try:
        if stage == "pre-retro":
            try:
                os.link(pending.name, path)
            except FileExistsError:
                if path.is_symlink() or path.read_bytes() != encoded:
                    raise ValueError("immutable pre-retro snapshot differs from existing bytes") from None
        else:
            os.replace(pending.name, path)
    finally:
        if os.path.exists(pending.name):
            os.unlink(pending.name)
    return EvidenceArtifactRefV1(path=relative, digest=content_digest)


class ProjectedRuntimeEvidence:
    """Host side of the kernel port. Returns the organized document, not storage rows.

    The reading attempt is still open, so its checkpoint is dropped before projection.
    Post-run export does not use this port and still sees every Attempt.
    """

    def __init__(
        self,
        workspace: ChangeWorkspace,
        read_records: Callable[[], Awaitable[tuple[AttemptCheckpoint, ...]]],
    ) -> None:
        self._workspace = workspace
        self._read_records = read_records

    async def project(
        self,
        *,
        invocation_id: str,
        exclude_attempt_key_digest: str,
    ) -> JSONValue:
        records = await self._read_records()
        filtered = tuple(
            record for record in records if record.attempt_key.digest != exclude_attempt_key_digest
        )
        document = project_runtime_evidence(
            filtered,
            change_id=self._workspace.change_id,
            invocation_id=invocation_id,
        )
        return cast(JSONValue, document.model_dump(mode="json"))
