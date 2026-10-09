from __future__ import annotations

import asyncio
from dataclasses import replace
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from graph_engine.attempts.orchestration.checkpoint import AttemptResult
from graph_engine.attempts.models.keys import AttemptKey
from tests.attempt_checkpoints import checkpoint, completed_checkpoint
from assurance_improvement.contracts.retro import WorkflowRuntimeEvidenceV2
from assurance_product.change_workspace import ChangeWorkspace
from assurance_product.retro_evidence import (
    ProjectedRuntimeEvidence,
    project_runtime_evidence,
    publish_runtime_evidence,
    snapshot_runtime_evidence,
)


def _records(invocation: str = "inv-full", *, commit_fence: int = 2):
    terminals = [
        AttemptResult(
            resolution_kind="retryable", failure_kind="invalid_output", message="secret-diagnostic"
        ),
        AttemptResult(
            resolution_kind="retryable", failure_kind="invalid_output", message="secret-diagnostic"
        ),
        AttemptResult(
            resolution_kind="committed", receipt_id="receipt", receipt_digest="a" * 64, output={"ok": True}
        ),
    ]
    return tuple(
        completed_checkpoint(
            AttemptKey(digest=key * 64),
            terminal=terminal,
            invocation_id=invocation,
            public_entrypoint="full",
            semantic_node_id="api.codegen-review",
            fencing_token=9,
            terminal_fencing_token=commit_fence if index == 2 else 1,
        )
        for index, (key, terminal) in enumerate(zip(("a", "e", "f"), terminals))
    )


def test_runtime_projection_keeps_each_failure_when_attempt_recovers() -> None:
    snapshot = project_runtime_evidence(_records(), change_id="CH-A", invocation_id="inv-full")
    assert snapshot.integrity.status == "complete"
    assert len(snapshot.entries) == 2
    assert len({entry.evidence_id for entry in snapshot.entries}) == 2
    assert all(entry.recovered is True and entry.ts is None for entry in snapshot.entries)
    assert "secret-diagnostic" not in snapshot.model_dump_json()
    assert all(entry.error_kind == "invalid_output" for entry in snapshot.entries)


def test_runtime_projection_does_not_invent_recovery_or_cross_invocation_evidence() -> None:
    snapshot = project_runtime_evidence(_records()[:2], change_id="CH-A", invocation_id="inv-full")
    assert snapshot.entries[0].recovered is None
    other = project_runtime_evidence(
        _records("another-invocation"), change_id="CH-A", invocation_id="inv-full"
    )
    assert other.entries == ()
    assert other.integrity.status == "incomplete"
    unordered = project_runtime_evidence(_records(commit_fence=1), change_id="CH-A", invocation_id="inv-full")
    assert len(unordered.entries) == 2
    assert all(entry.recovered is None for entry in unordered.entries)


def test_corrupt_checkpoint_payload_is_not_a_complete_empty_snapshot() -> None:
    records = _records()
    assert isinstance(records[-1].activity_outcome, dict)
    records[-1].activity_outcome["ok"] = False
    with pytest.raises(ValueError, match="checkpoint"):
        project_runtime_evidence(records, change_id="CH-A", invocation_id="inv-full")


def test_recovery_never_crosses_input_or_node_identity() -> None:
    failed, second, success = _records()
    for changed in (replace(success, input_digest="8" * 64), replace(success, semantic_node_id="other-node")):
        snapshot = project_runtime_evidence(
            (failed, second, changed), change_id="CH-A", invocation_id="inv-full"
        )
        assert all(entry.recovered is None for entry in snapshot.entries)


def test_original_terminal_fence_orders_recovery_after_later_owner_adoption() -> None:
    records = _records()
    adopted = tuple(
        replace(record, fencing_token=20 if index < 2 else 10) for index, record in enumerate(records)
    )
    snapshot = project_runtime_evidence(adopted, change_id="CH-A", invocation_id="inv-full")
    assert all(entry.recovered is True for entry in snapshot.entries)


def test_published_runtime_evidence_is_explicit_hash_bound_and_repeatable(tmp_path: Path) -> None:
    workspace = ChangeWorkspace.prepare(tmp_path, "CH-A")
    snapshot = project_runtime_evidence(_records(), change_id="CH-A", invocation_id="inv-full")
    ref = publish_runtime_evidence(workspace, snapshot)
    assert ref.path.startswith("qa/results/workflow/")
    assert ref.path.endswith("/workflow-evidence.json")
    assert publish_runtime_evidence(workspace, snapshot) == ref
    assert "secret-diagnostic" not in (tmp_path / ref.path).read_text()


def test_pre_retro_snapshot_is_not_overwritten_by_post_run_export(tmp_path: Path) -> None:
    workspace = ChangeWorkspace.prepare(tmp_path, "CH-A")

    async def read_records():
        return _records()

    ref = asyncio.run(snapshot_runtime_evidence(workspace, read_records, invocation_id="inv-full"))
    before = (tmp_path / ref.path).read_bytes()
    assert "/pre-retro/" in ref.path
    assert ref.path.endswith("/workflow-evidence.json")
    publish_runtime_evidence(
        workspace,
        project_runtime_evidence(_records(), change_id="CH-A", invocation_id="inv-full"),
    )
    assert (tmp_path / ref.path).read_bytes() == before


def test_pre_retro_snapshot_is_immutable_across_replay(tmp_path: Path) -> None:
    workspace = ChangeWorkspace.prepare(tmp_path, "CH-A")
    snapshot = project_runtime_evidence(_records(), change_id="CH-A", invocation_id="inv-full")
    original = publish_runtime_evidence(workspace, snapshot, stage="pre-retro")
    original_bytes = (tmp_path / original.path).read_bytes()

    changed = snapshot.model_copy(update={"checkpoint_digest": "f" * 64})
    replay = publish_runtime_evidence(workspace, changed, stage="pre-retro")

    assert replay.path != original.path
    assert (tmp_path / original.path).read_bytes() == original_bytes
    assert publish_runtime_evidence(workspace, snapshot, stage="pre-retro") == original


def test_pre_retro_snapshot_rejects_existing_different_bytes(tmp_path: Path) -> None:
    workspace = ChangeWorkspace.prepare(tmp_path, "CH-A")
    snapshot = project_runtime_evidence(_records(), change_id="CH-A", invocation_id="inv-full")
    ref = publish_runtime_evidence(workspace, snapshot, stage="pre-retro")
    (tmp_path / ref.path).write_bytes(b"tampered")

    with pytest.raises(ValueError, match="immutable"):
        publish_runtime_evidence(workspace, snapshot, stage="pre-retro")


def test_runtime_publication_rejects_symlink_parent(tmp_path: Path) -> None:
    workspace = ChangeWorkspace.prepare(tmp_path, "CH-A")
    (tmp_path / "qa/results").mkdir()
    (tmp_path / "qa/results/workflow").symlink_to(tmp_path, target_is_directory=True)
    snapshot = project_runtime_evidence(_records(), change_id="CH-A", invocation_id="inv-full")
    with pytest.raises(ValueError, match="symlink"):
        publish_runtime_evidence(workspace, snapshot)


@pytest.mark.parametrize("method", ["_run_langgraph", "_resume_langgraph"])
@pytest.mark.parametrize("entrypoint", ["full", "retro"])
@pytest.mark.parametrize("export_failure", [False, True])
def test_product_execution_exports_runtime_evidence_except_retro(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    entrypoint: str,
    export_failure: bool,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from assurance_product import application as module
    from assurance_product.application import AssuranceProductApplication
    from assurance_improvement.contracts.retro import WorkflowRuntimeEvidenceV2
    from assurance_product import retro_evidence

    workspace = ChangeWorkspace.prepare(tmp_path, "CH-A")

    async def read_records():
        return _records()

    async def execute(**_kwargs):
        return SimpleNamespace(status="failed")

    ports = SimpleNamespace(attempt_checkpoints=SimpleNamespace(read_checkpoints=read_records))

    @asynccontextmanager
    async def open_ports(*_args, **_kwargs):
        yield ports

    application = AssuranceProductApplication()
    if export_failure:

        def unavailable(*_args):
            raise PermissionError("secret-diagnostic")

        monkeypatch.setattr(retro_evidence, "publish_runtime_evidence", unavailable)
    monkeypatch.setattr(module, "_assert_langgraph_revision", lambda *_args: None)
    monkeypatch.setattr(application, "_open_ports", open_ports)
    monkeypatch.setattr(
        application, "_application", lambda *_args: SimpleNamespace(run=execute, resume=execute)
    )
    monkeypatch.setattr(application, "_remember_started", lambda *_args: None)
    monkeypatch.setattr(application, "_execution_factory", lambda *_args, **_kwargs: None)
    kwargs = {"resume": {}} if method == "_resume_langgraph" else {}
    status = asyncio.run(
        getattr(application, method)(
            workspace=workspace,
            composition=object(),
            authorization=object(),
            invocation_id="inv-full",
            record=SimpleNamespace(entrypoint=entrypoint, root_input_digest="a" * 64),
            **kwargs,
        )
    )
    assert status == "failed"
    snapshots = list((tmp_path / "qa/results/workflow").glob("*/workflow-evidence.json"))
    if entrypoint == "retro" or export_failure:
        assert snapshots == []
        if entrypoint != "retro":
            assert "workflow_evidence_export_failed" in caplog.text
            assert "secret-diagnostic" not in caplog.text
    else:
        assert len(snapshots) == 1
        snapshot = WorkflowRuntimeEvidenceV2.model_validate_json(snapshots[0].read_bytes())
        assert len(snapshot.entries) == 2


def test_the_port_drops_the_open_reader_and_keeps_this_invocation() -> None:
    records = _records()
    reader = checkpoint(
        AttemptKey(digest="9" * 64),
        revision=1,
        fencing_token=1,
        invocation_id="inv-full",
        semantic_node_id="improvement.retro-runtime-snapshot",
    )
    assert records[-1].terminal is not None
    assert records[-1].terminal is not None
    other = completed_checkpoint(
        AttemptKey(digest="8" * 64),
        terminal=records[-1].terminal,
        invocation_id="other-invocation",
        semantic_node_id="api.codegen-review",
    )

    async def read_records():
        return (*records, reader, other)

    port = ProjectedRuntimeEvidence(SimpleNamespace(change_id="CH-A"), read_records)  # type: ignore[arg-type]
    excluded = asyncio.run(port.project(invocation_id="inv-full", exclude_attempt_key_digest="9" * 64))
    expected = project_runtime_evidence(records, change_id="CH-A", invocation_id="inv-full")
    assert excluded == expected.model_dump(mode="json")
    assert expected.integrity.status == "complete"
    included = asyncio.run(port.project(invocation_id="inv-full", exclude_attempt_key_digest="0" * 64))
    assert WorkflowRuntimeEvidenceV2.model_validate(included).integrity.status == "incomplete"


def test_pre_retro_publish_bytes_match_the_snapshot_task_encoding(tmp_path: Path) -> None:
    from assurance_improvement.contracts.runtime_snapshot import pre_retro_evidence_path

    workspace = ChangeWorkspace.prepare(tmp_path, "CH-A")
    snapshot = project_runtime_evidence(_records(), change_id="CH-A", invocation_id="inv-full")
    ref = publish_runtime_evidence(workspace, snapshot, stage="pre-retro")
    relative, encoded = pre_retro_evidence_path(snapshot)
    assert relative == ref.path
    assert (tmp_path / ref.path).read_bytes() == encoded
