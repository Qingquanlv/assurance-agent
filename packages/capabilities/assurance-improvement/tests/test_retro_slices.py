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
from assurance_improvement.operations.retro import AssembleRetroInput, assemble_context
from assurance_improvement.contracts.delivery import artifact_digest
from assurance_execution.contracts.selection import ClosedMappingV1
from assurance_execution.operations.normalize import normalize_evidence
from assurance_intake.contracts import EvidenceArtifactRefV1, build_loop_round_history
from tests.acg_plan_fixture import install_plan

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
    underlying = EvidenceArtifactRefV1(path="qa/results/review/case-review.json", digest="b" * 64)
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
        "qa/cases/reviews/epochs/1/rounds/2.json",
        _json_bytes(history.model_dump(mode="json")),
    )
    inspection_ref = _write(
        tmp_path,
        "qa/results/inspect/inspection.json",
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
    assert "task_failure_evidence_absent" in result.output.workflow_slice.integrity.reasons
    assert "skill_drift_evidence_absent" in result.output.workflow_slice.integrity.reasons
    entry = result.output.workflow_slice.entries[0]
    assert isinstance(entry, LoopRoundEvidenceEntry)
    assert entry.coverage_epoch == 1
    assert entry.round_index == 2
    assert result.output.eval_slice.entries == ()
    assert "inspection_execution_missing" in result.output.eval_slice.integrity.reasons


@pytest.mark.asyncio
async def test_missing_loop_history_is_an_integrity_gap_not_a_reconstructed_round(
    tmp_path: Path,
) -> None:
    report_ref = _write(
        tmp_path,
        "qa/results/report/report.md",
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
    assert "loop_history_missing" in result.output.workflow_slice.integrity.reasons
    assert result.output.eval_slice.integrity.status == "incomplete"
    assert "evaluation_evidence_absent" in result.output.eval_slice.integrity.reasons
    source = result.output.eval_slice.sources[0]
    assert source.plan_digest is None
    assert source.plan_ref is None


@pytest.mark.asyncio
async def test_each_plan_bound_source_retains_its_own_authenticated_plan(tmp_path: Path) -> None:
    second_change = "CH-RETRO-002"
    first_plan, first_ref_value = install_plan(tmp_path, _CHANGE)
    second_plan, second_ref_value = install_plan(
        tmp_path,
        second_change,
        candidates=("api", "fuzz"),
        proposed=("fuzz",),
    )
    first_plan_ref = EvidenceArtifactRefV1.model_validate(first_ref_value)
    second_plan_ref = EvidenceArtifactRefV1.model_validate(second_ref_value)
    report_refs = (
        _write(
            tmp_path,
            f"qa/results/report/{_CHANGE}/report-outcome.json",
            _json_bytes(
                {
                    "change_id": _CHANGE,
                    "plan_digest": first_plan.plan_digest,
                    "plan_ref": first_ref_value,
                }
            ),
        ),
        _write(
            tmp_path,
            f"qa/results/report/{second_change}/report-outcome.json",
            _json_bytes(
                {
                    "change_id": second_change,
                    "plan_digest": second_plan.plan_digest,
                    "plan_ref": second_ref_value,
                }
            ),
        ),
    )
    window = RetroWindow.model_validate(
        {
            "selection": {
                "mode": "change_ids",
                "requested_change_ids": [_CHANGE, second_change],
            },
            "change_ids": [_CHANGE, second_change],
        }
    )
    request = RetroBuildSlicesInputV1(
        retro_id="retro-multiple-plans",
        window=window,
        source_refs=tuple(
            sorted(
                (*report_refs, first_plan_ref, second_plan_ref),
                key=lambda item: item.path,
            )
        ),
    )

    result = await RetroBuildSlicesExecutor().execute(request, _scope(tmp_path))

    bindings = {
        source.change_id: (source.plan_digest, source.plan_ref) for source in result.output.eval_slice.sources
    }
    assert bindings == {
        _CHANGE: (first_plan.plan_digest, first_plan_ref),
        second_change: (second_plan.plan_digest, second_plan_ref),
    }


@pytest.mark.asyncio
async def test_plan_bound_source_requires_the_exact_plan_ref_in_source_refs(tmp_path: Path) -> None:
    plan, ref_value = install_plan(tmp_path, _CHANGE)
    report_ref = _write(
        tmp_path,
        "qa/results/report/report-outcome.json",
        _json_bytes(
            {
                "change_id": _CHANGE,
                "plan_digest": plan.plan_digest,
                "plan_ref": ref_value,
            }
        ),
    )
    request = RetroBuildSlicesInputV1(
        retro_id="retro-missing-plan-source",
        window=_WINDOW,
        source_refs=(report_ref,),
    )

    with pytest.raises(ValueError, match="exact plan source"):
        await RetroBuildSlicesExecutor().execute(request, _scope(tmp_path))


def _execution_refs(root: Path, *, executed_at: str | None = "2026-09-14T08:00:00Z"):
    plan, plan_ref = install_plan(root, _CHANGE)
    nodeid = "qa/tests/api/test_dept.py::test_invalid_parent"
    mapping = ClosedMappingV1.model_validate(
        {
            "selected": [nodeid],
            "mappings": [
                {"test": nodeid, "case_id": "TC_A", "capability": "entities.item.create", "layer": "api"}
            ],
        },
        context={"capability_leafs": frozenset({"entities.item.create"}), "case_ids": frozenset({"TC_A"})},
    )
    execution = normalize_evidence(
        change_id=_CHANGE,
        batch_id="batch-not-a-timestamp",
        plan_digest=plan.plan_digest,
        plan_ref=EvidenceArtifactRefV1.model_validate(plan_ref),
        selected_targets={"api": True, "e2e": False, "fuzz": False, "performance": False},
        mapping=mapping,
        capability_leafs=frozenset({"entities.item.create"}),
        case_ids=frozenset({"TC_A"}),
        baseline_tree_id=_SHA,
        runner_profile_digest=_SHA,
        command=("pytest",),
        exit_code=1,
        report={
            "tests": [
                {
                    "nodeid": nodeid,
                    "outcome": "failed",
                    "call": {
                        "outcome": "failed",
                        "duration": 0.001,
                        "longrepr": "assert 200 == 400 secret-value",
                    },
                }
            ],
            "exitcode": 1,
            "summary": {"collected": 1, "passed": 0, "failed": 1, "skipped": 0},
        },
    ).model_dump(mode="json")
    execution["executed_at"] = executed_at
    ref = _write(root, "qa/results/execution/execute-result.json", _json_bytes(execution))
    return ref, EvidenceArtifactRefV1.model_validate(plan_ref), execution


def _inspection_ref(root: Path, execution_digest: str, **overrides: object):
    payload = {
        "schema_version": "1.0",
        "change_id": _CHANGE,
        "batch_id": "batch-not-a-timestamp",
        "inspect_mode": "primary",
        "classification_performed": True,
        "status": "analyzed",
        "execution_digest": execution_digest,
        "healing_digest": None,
        "trace_digest": "e" * 64,
        "coverage_digest": "f" * 64,
        "metrics_digest": "1" * 64,
    }
    payload.update(overrides)
    return _write(root, "qa/results/inspect/inspection.json", _json_bytes(payload))


@pytest.mark.asyncio
async def test_analyzed_inspection_preserves_failed_execution_and_real_timestamp(tmp_path: Path) -> None:
    execution_ref, plan_ref, _ = _execution_refs(tmp_path)
    inspection_ref = _inspection_ref(tmp_path, execution_ref.digest)
    result = await RetroBuildSlicesExecutor().execute(
        RetroBuildSlicesInputV1(
            retro_id="retro-failed",
            window=_WINDOW,
            source_refs=tuple(sorted((execution_ref, plan_ref, inspection_ref), key=lambda ref: ref.path)),
        ),
        _scope(tmp_path),
    )
    (entry,) = result.output.eval_slice.entries
    assert entry.verdict == "failed"
    assert entry.suite == "assurance-execution"
    assert entry.started_at == "2026-09-14T08:00:00+00:00"
    assert entry.failure_signature
    assert entry.sample_ids == ("qa/tests/api/test_dept.py::test_invalid_parent",)
    assert "secret-value" not in result.output.model_dump_json()
    assert result.output.eval_slice.integrity.status == "complete"


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ["digest", "batch", "change"])
async def test_inspection_requires_exact_execution_identity(tmp_path: Path, mismatch: str) -> None:
    execution_ref, plan_ref, _ = _execution_refs(tmp_path)
    overrides = {"batch_id": "different"} if mismatch == "batch" else {}
    if mismatch == "change":
        overrides = {"change_id": "CH-OTHER"}
    inspection_ref = _inspection_ref(
        tmp_path, "b" * 64 if mismatch == "digest" else execution_ref.digest, **overrides
    )
    result = await RetroBuildSlicesExecutor().execute(
        RetroBuildSlicesInputV1(
            retro_id="retro-mismatch",
            window=_WINDOW,
            source_refs=tuple(sorted((execution_ref, plan_ref, inspection_ref), key=lambda ref: ref.path)),
        ),
        _scope(tmp_path),
    )
    assert result.output.eval_slice.integrity.status == "incomplete"
    assert all(entry.verdict != "analyzed" for entry in result.output.eval_slice.entries)


@pytest.mark.asyncio
async def test_missing_execution_time_is_explicit_not_batch_id(tmp_path: Path) -> None:
    execution_ref, plan_ref, _ = _execution_refs(tmp_path, executed_at=None)
    result = await RetroBuildSlicesExecutor().execute(
        RetroBuildSlicesInputV1(
            retro_id="retro-legacy-time",
            window=_WINDOW,
            source_refs=tuple(sorted((execution_ref, plan_ref), key=lambda ref: ref.path)),
        ),
        _scope(tmp_path),
    )
    (entry,) = result.output.eval_slice.entries
    assert entry.verdict == "failed"
    assert entry.started_at is None
    assert "execution_time_missing" in result.output.eval_slice.integrity.reasons


@pytest.mark.asyncio
@pytest.mark.parametrize("corrupt_execution", [False, True])
async def test_typed_runtime_failures_are_preserved_after_recovery(
    tmp_path: Path, corrupt_execution: bool
) -> None:
    entries = [
        {
            "entry_kind": "task_failure",
            "evidence_id": f"failure-{i}",
            "change_id": _CHANGE,
            "task_id": "api-plan-review",
            "attempt_id": f"attempt-{i}",
            "node_id": "api-plan-review",
            "error_kind": "invalid_output",
            "message_fingerprint": _SHA,
            "recovered": True,
            "ts": None,
        }
        for i in range(2)
    ]
    ref = _write(
        tmp_path,
        "qa/results/workflow/inv-1/workflow-evidence.json",
        _json_bytes(
            {
                "schema_version": "1",
                "change_id": _CHANGE,
                "invocation_id": "inv-1",
                "journal_digest": _SHA,
                "entries": entries,
                "integrity": {"status": "complete", "reasons": []},
            }
        ),
    )
    refs = [ref]
    if corrupt_execution:
        refs.append(_write(tmp_path, "qa/results/execution/execute-result.json", b"{invalid-json"))
    result = await RetroBuildSlicesExecutor().execute(
        RetroBuildSlicesInputV1(
            retro_id="retro-technical",
            window=_WINDOW,
            source_refs=tuple(sorted(refs, key=lambda item: item.path)),
        ),
        _scope(tmp_path),
    )
    assert len(result.output.workflow_slice.entries) == 2
    assert "task_failure_evidence_absent" not in result.output.workflow_slice.integrity.reasons
    assert "skill_drift_evidence_absent" in result.output.workflow_slice.integrity.reasons
    (signal,) = result.output.workflow_slice.deterministic_signals
    assert signal.signal_type == "task_failure"
    assert signal.occurrence_count == 2
    assert signal.source_refs.workflow_evidence_ids == ("failure-0", "failure-1")
    if corrupt_execution:
        assert "execution_evidence_corrupt" in result.output.eval_slice.integrity.reasons

    # Even an analyzer returning no additional signals cannot erase proven failures
    # or send this run down the no-signal/synthesis-skipped path.
    assembly = {"generated_at": "2026-09-14T08:00:00Z", "window": _WINDOW.model_dump(mode="json")}
    for domain in ("issue", "workflow", "eval"):
        slice_ = getattr(result.output, f"{domain}_slice")
        digest = artifact_digest(slice_)
        assembly[f"{domain}_slice"] = slice_.model_dump(mode="json")
        assembly[f"{domain}_slice_sha256"] = digest
        assembly[f"{domain}_signals"] = {
            "retro_id": slice_.retro_id,
            "domain": domain,
            "analysis_status": "ok",
            "analyzer": "empty-additional-analysis",
            "slice_sha256": digest,
            "signals": [],
        }
    context = assemble_context(AssembleRetroInput.model_validate(assembly))
    assert context.signal_count == 1
    assert context.integrity.status == "incomplete"
