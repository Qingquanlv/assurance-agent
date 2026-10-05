"""The snapshot task commits the host document at the pre-retro path."""

from __future__ import annotations

import hashlib
from pathlib import Path

from graph_engine.canonical import canonical_digest
from graph_engine.plugin_api import (
    InvocationMetadata,
    TaskContext,
    TaskRequest,
    TaskWorkspaceIdentity,
)
from assurance_improvement.contracts.retro import RetroIntegrity, WorkflowRuntimeEvidenceV1
from assurance_improvement.contracts.runtime_snapshot import (
    RetroRuntimeSnapshotOutputV1,
    pre_retro_evidence_path,
)
from assurance_improvement.operations.runtime_snapshot import RetroRuntimeSnapshotHandler

_SHA = "a" * 64


def _document() -> WorkflowRuntimeEvidenceV1:
    return WorkflowRuntimeEvidenceV1(
        change_id="CH-A",
        invocation_id="inv-full",
        journal_digest=_SHA,
        entries=(),
        integrity=RetroIntegrity(status="complete"),
    )


def _context(root: Path, reader) -> TaskContext:
    identity_payload = {
        "task_id": _SHA,
        "attempt": 1,
        "attempt_id": "attempt-1",
        "output_paths": ["qa/results/workflow"],
        "baseline_files": [],
        "project_digest": _SHA,
        "write_root_digest": _SHA,
        "layout_schema_version": "1",
    }
    identity = TaskWorkspaceIdentity(
        **identity_payload,
        identity_digest=canonical_digest(identity_payload),
    )
    return TaskContext(
        project_root=root,
        write_root=root,
        workspace_identity=identity,
        heartbeat=lambda: None,
        cancel_requested=lambda: False,
        invocation=InvocationMetadata(
            invocation_id="inv-full",
            lock_digest=_SHA,
            composition_digest=_SHA,
            entrypoint="full",
        ),
        runtime_evidence=reader,
    )


def _request() -> TaskRequest:
    return TaskRequest(
        invocation_id="inv-full",
        task_id=_SHA,
        graph_instance_id="inv-full",
        node_id="improvement.retro-runtime-snapshot",
        capability_id="assurance.improvement.retro-runtime-snapshot",
        invocation=InvocationMetadata(
            invocation_id="inv-full",
            lock_digest=_SHA,
            composition_digest=_SHA,
            entrypoint="full",
        ),
        attempt=1,
        input={"change_id": "CH-A"},
    )


async def test_snapshot_bytes_match_the_pre_retro_publish_encoding(tmp_path: Path) -> None:
    document = _document()
    relative, encoded = pre_retro_evidence_path(document)

    async def read():
        return document.model_dump(mode="json")

    outcome = await RetroRuntimeSnapshotHandler().execute(_request(), _context(tmp_path, read))
    assert outcome.status == "succeeded"
    written = (tmp_path / relative).read_bytes()
    assert written == encoded
    assert written.endswith(b"\n")
    published = RetroRuntimeSnapshotOutputV1.model_validate(outcome.output)
    assert hashlib.sha256(written).hexdigest() == published.evidence_ref.digest
    assert published.evidence_ref.path == relative


async def test_snapshot_fails_clearly_without_the_port(tmp_path: Path) -> None:
    outcome = await RetroRuntimeSnapshotHandler().execute(_request(), _context(tmp_path, None))
    assert outcome.failure is not None
    assert outcome.failure.kind == "configuration"
    assert outcome.failure.message == "runtime evidence port is not configured"
