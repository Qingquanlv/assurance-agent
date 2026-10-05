"""Publish redacted Kernel history for explicit, digest-bound Retro source selection."""

from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
import tempfile
from collections.abc import Awaitable, Callable, Sequence
from typing import cast

from graph_engine.attempts.events import AttemptOpened, AttemptTerminated
from graph_engine.canonical import JSONValue, canonical_digest, canonical_json_bytes
from graph_engine.persistence.attempt_journal import AttemptJournalRecord
from graph_engine.errors import GraphEngineError

from assurance_improvement.contracts.retro import (
    RetroIntegrity,
    TaskFailureEvidenceEntry,
    WorkflowRuntimeEvidenceV1,
)
from assurance_intake.contracts import EvidenceArtifactRefV1
from assurance_product.change_workspace import ChangeWorkspace


async def export_runtime_evidence(
    workspace: ChangeWorkspace,
    read_records: Callable[[], Awaitable[tuple[AttemptJournalRecord, ...]]],
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
    read_records: Callable[[], Awaitable[tuple[AttemptJournalRecord, ...]]],
    *,
    invocation_id: str,
) -> EvidenceArtifactRefV1:
    """Bind the current journal before Retro; keep the later diagnostic export separate."""
    document = project_runtime_evidence(
        await read_records(), change_id=workspace.change_id, invocation_id=invocation_id
    )
    return publish_runtime_evidence(workspace, document, stage="pre-retro")


def project_runtime_evidence(
    records: Sequence[AttemptJournalRecord],
    *,
    change_id: str,
    invocation_id: str,
) -> WorkflowRuntimeEvidenceV1:
    """Retain every failed terminal, including retries on an already opened attempt.

    Journal revisions order events within an attempt; fencing tokens order runner
    leases. Neither is a timestamp. Recovery is unknown unless a later commit of
    the same node/input is proven by one of these orderings.
    """
    by_key: dict[str, list[AttemptJournalRecord]] = {}
    for record in records:
        if record.record_digest != record.canonical_digest():
            raise ValueError("runtime journal digest mismatch")
        by_key.setdefault(record.attempt_key_digest, []).append(record)
    terminals: list[tuple[AttemptOpened, AttemptJournalRecord, int, AttemptTerminated]] = []
    selected: list[AttemptJournalRecord] = []
    incomplete = False
    for key_records in by_key.values():
        ordered = sorted(key_records, key=lambda record: record.revision)
        if [record.revision for record in ordered] != list(range(len(ordered))):
            raise ValueError("runtime journal revision gap")
        opened: AttemptOpened | None = None
        for record in ordered:
            for index, event in enumerate(record.events):
                if isinstance(event, AttemptOpened):
                    if opened is not None and opened != event:
                        raise ValueError("runtime journal attempt identity changed")
                    opened = event
                if isinstance(event, AttemptTerminated):
                    if opened is None:
                        raise ValueError("runtime journal terminal without identity")
                    if opened.invocation_id == invocation_id:
                        terminals.append((opened, record, index, event))
            if opened is not None and opened.invocation_id == invocation_id:
                selected.append(record)
        if opened is not None and opened.invocation_id == invocation_id:
            incomplete |= not any(
                isinstance(event, AttemptTerminated) for record in ordered for event in record.events
            )

    entries: list[TaskFailureEvidenceEntry] = []
    for opened, record, index, terminal in terminals:
        if terminal.resolution_kind == "committed":
            continue
        later_commit = any(
            other.resolution_kind == "committed"
            and (identity.semantic_node_id, identity.input_digest)
            == (opened.semantic_node_id, opened.input_digest)
            and (
                later.fencing_token > record.fencing_token
                or (
                    later.attempt_key_digest == record.attempt_key_digest
                    and (later.revision, offset) > (record.revision, index)
                )
            )
            for identity, later, offset, other in terminals
        )
        entries.append(
            TaskFailureEvidenceEntry(
                evidence_id=f"attempt-failure-{canonical_digest([record.record_digest, index])}",
                change_id=change_id,
                task_id=opened.semantic_node_id,
                attempt_id=record.attempt_key_digest,
                node_id=opened.semantic_node_id,
                error_kind=terminal.failure_kind or terminal.resolution_kind,
                message_fingerprint=hashlib.sha256(
                    (terminal.message or terminal.reason).encode()
                ).hexdigest(),
                recovered=True if later_commit else None,
            )
        )
    reasons = ("runtime_attempts_incomplete",) if incomplete else ()
    if not selected:
        reasons = ("runtime_invocation_evidence_absent",)
    return WorkflowRuntimeEvidenceV1(
        change_id=change_id,
        invocation_id=invocation_id,
        journal_digest=canonical_digest(cast(JSONValue, sorted(record.record_digest for record in selected))),
        entries=tuple(sorted(entries, key=lambda entry: entry.evidence_id)),
        integrity=RetroIntegrity(status="incomplete" if reasons else "complete", reasons=reasons),
    )


def publish_runtime_evidence(
    workspace: ChangeWorkspace,
    document: WorkflowRuntimeEvidenceV1,
    *,
    stage: str = "post-run",
) -> EvidenceArtifactRefV1:
    """Export a runtime projection, never the journal, prompts or credentials.

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
    """Host side of the kernel port. Returns the organized document, not journal rows.

    The reading attempt is still open, so its records are dropped before projection.
    Post-run export does not use this port and still sees every terminated attempt.
    """

    def __init__(
        self,
        workspace: ChangeWorkspace,
        read_records: Callable[[], Awaitable[tuple[AttemptJournalRecord, ...]]],
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
            record for record in records if record.attempt_key_digest != exclude_attempt_key_digest
        )
        document = project_runtime_evidence(
            filtered,
            change_id=self._workspace.change_id,
            invocation_id=invocation_id,
        )
        return cast(JSONValue, document.model_dump(mode="json"))
