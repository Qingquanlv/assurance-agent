from __future__ import annotations

import hashlib
from pathlib import Path
from typing import cast

import pytest

from graph_engine.attempts import AttemptExecutionContext, AttemptKey, AuthorizedAttemptScope
from graph_engine.canonical import JSONValue, canonical_json_bytes
from graph_engine.plugin_api import DirectoryIdentity, TaskWorkspaceBinding, TaskWorkspaceIdentity

from assurance_improvement.contracts.retro import (
    LoopRoundEvidenceEntry,
    RetroBuildSlicesInputV1,
    RetroWindow,
)
from assurance_improvement.operations.retro_slices import RetroBuildSlicesExecutor
from assurance_intake.contracts import EvidenceArtifactRefV1, build_loop_round_history

_SHA = "a" * 64
_CHANGE = "CH-RETRO-001"
_WINDOW = RetroWindow.model_validate(
    {
        "selection": {"mode": "change_ids", "requested_change_ids": [_CHANGE]},
        "change_ids": [_CHANGE],
    }
)


def _write(root: Path, relative: str, data: bytes) -> EvidenceArtifactRefV1:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return EvidenceArtifactRefV1(path=relative, digest=hashlib.sha256(data).hexdigest())


def _json_bytes(value: object) -> bytes:
    return canonical_json_bytes(cast(JSONValue, value)) + b"\n"


def _scope(root: Path) -> AuthorizedAttemptScope:
    identity = TaskWorkspaceIdentity.model_construct(
        task_id="task-1",
        attempt=1,
        attempt_id="attempt-1",
        output_paths=(),
        baseline_files=(),
        project_digest=_SHA,
        write_root_digest=_SHA,
        identity_digest=_SHA,
        layout_schema_version="1",
    )
    directory = DirectoryIdentity.model_construct(path_digest=_SHA, device=1, inode=1, identity_digest=_SHA)
    return AuthorizedAttemptScope(
        execution=AttemptExecutionContext(
            invocation_id="inv-1",
            public_entrypoint="retro",
            semantic_node_id="improvement.retro-build-slices",
            attempt_key=AttemptKey(digest=_SHA),
            fencing_token=1,
        ),
        workspace=TaskWorkspaceBinding(
            identity=identity,
            project_root=root,
            write_root=root,
            project_root_identity=directory,
            write_root_identity=directory,
        ),
    )


@pytest.mark.asyncio
async def test_builds_workflow_and_eval_slices_from_authenticated_evidence(tmp_path: Path) -> None:
    underlying = EvidenceArtifactRefV1(path=f"qa/changes/{_CHANGE}/review/case-review.json", digest="b" * 64)
    history = build_loop_round_history(
        change_id=_CHANGE,
        coverage_epoch=1,
        loop_kind="case_review",
        family=None,
        round_index=2,
        outcome="approved",
        review_input_digest="c" * 64,
        source_refs=(underlying,),
    )
    history_ref = _write(
        tmp_path,
        f"qa/changes/{_CHANGE}/cases/reviews/epochs/1/rounds/2.json",
        _json_bytes(history.model_dump(mode="json")),
    )
    inspection_ref = _write(
        tmp_path,
        f"qa/changes/{_CHANGE}/inspect/inspection.json",
        _json_bytes(
            {
                "schema_version": "1.0",
                "change_id": _CHANGE,
                "batch_id": "20260905T010203Z",
                "inspect_mode": "primary",
                "classification_performed": True,
                "status": "analyzed",
                "execution_digest": "d" * 64,
                "healing_digest": None,
                "trace_digest": "e" * 64,
                "coverage_digest": "f" * 64,
                "metrics_digest": "1" * 64,
            }
        ),
    )
    request = RetroBuildSlicesInputV1(
        retro_id="retro-1",
        window=_WINDOW,
        source_refs=tuple(sorted((history_ref, inspection_ref), key=lambda item: item.path)),
    )
    result = await RetroBuildSlicesExecutor().execute(request, _scope(tmp_path))
    assert result.output.workflow_slice.integrity.status == "complete"
    entry = result.output.workflow_slice.entries[0]
    assert isinstance(entry, LoopRoundEvidenceEntry)
    assert entry.coverage_epoch == 1
    assert entry.round_index == 2
    assert result.output.eval_slice.entries[0].run_id == "20260905T010203Z"


@pytest.mark.asyncio
async def test_missing_loop_history_is_an_integrity_gap_not_a_reconstructed_round(
    tmp_path: Path,
) -> None:
    report_ref = _write(
        tmp_path,
        f"qa/changes/{_CHANGE}/report/report.md",
        b"# sealed report\n",
    )
    request = RetroBuildSlicesInputV1(
        retro_id="retro-legacy",
        window=_WINDOW,
        source_refs=(report_ref,),
    )
    result = await RetroBuildSlicesExecutor().execute(request, _scope(tmp_path))
    assert result.output.workflow_slice.entries == ()
    assert result.output.workflow_slice.integrity.status == "incomplete"
    assert result.output.workflow_slice.integrity.reasons == ("loop_history_missing",)
