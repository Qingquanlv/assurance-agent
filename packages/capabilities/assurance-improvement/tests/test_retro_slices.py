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
    RetroCollectInput,
    RetroWindow,
)
from assurance_improvement.operations.common import InputError
from assurance_improvement.operations.retro_slices import RetroBuildSlicesExecutor
from assurance_improvement.operations.retro import AssembleRetroInput, assemble_context
from assurance_improvement.contracts.delivery import artifact_digest
from assurance_execution.contracts.selection import ClosedMappingV1
from assurance_execution.operations.normalize import normalize_evidence
from assurance_intake.contracts import EvidenceArtifactRefV1, build_loop_round_history
from assurance_quality.contracts.obligations import ObligationAssessmentV1
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
    assert "skill_drift_not_assessed" in result.output.workflow_slice.integrity.reasons
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


@pytest.mark.asyncio
async def test_obligation_assessment_plan_ref_only_derives_authenticated_plan_digest(tmp_path: Path) -> None:
    plan, plan_ref_value = install_plan(tmp_path, _CHANGE)
    plan_ref = EvidenceArtifactRefV1.model_validate(plan_ref_value)
    assessment = ObligationAssessmentV1(plan_ref=plan_ref, rows=())
    assessment_ref = _write(
        tmp_path,
        "qa/results/inspect/epochs/0/batches/batch-1/obligation-assessment.json",
        _json_bytes(assessment.model_dump(mode="json")),
    )
    request = RetroBuildSlicesInputV1(
        retro_id="retro-obligation-assessment",
        window=_WINDOW,
        source_refs=(assessment_ref, plan_ref),
    )

    result = await RetroBuildSlicesExecutor().execute(request, _scope(tmp_path))

    source = next(
        source for source in result.output.eval_slice.sources if source.sha256 == assessment_ref.digest
    )
    assert source.plan_digest == plan.plan_digest
    assert source.plan_ref == plan_ref


@pytest.mark.asyncio
async def test_obligation_assessment_plan_ref_only_requires_exact_plan_source(tmp_path: Path) -> None:
    _plan, plan_ref_value = install_plan(tmp_path, _CHANGE)
    assessment = ObligationAssessmentV1(
        plan_ref=EvidenceArtifactRefV1.model_validate(plan_ref_value), rows=()
    )
    assessment_ref = _write(
        tmp_path,
        "qa/results/inspect/epochs/0/batches/batch-1/obligation-assessment.json",
        _json_bytes(assessment.model_dump(mode="json")),
    )

    with pytest.raises(ValueError, match="exact plan source"):
        await RetroBuildSlicesExecutor().execute(
            RetroBuildSlicesInputV1(
                retro_id="retro-obligation-without-plan-source",
                window=_WINDOW,
                source_refs=(assessment_ref,),
            ),
            _scope(tmp_path),
        )


@pytest.mark.asyncio
async def test_plan_bound_source_rejects_claimed_digest_mismatch(tmp_path: Path) -> None:
    plan, plan_ref_value = install_plan(tmp_path, _CHANGE)
    plan_ref = EvidenceArtifactRefV1.model_validate(plan_ref_value)
    report_ref = _write(
        tmp_path,
        "qa/results/report/report-outcome.json",
        _json_bytes(
            {
                "change_id": _CHANGE,
                "plan_digest": "f" * 64 if plan.plan_digest != "f" * 64 else "e" * 64,
                "plan_ref": plan_ref_value,
            }
        ),
    )

    with pytest.raises(ValueError, match="plan_digest does not match"):
        await RetroBuildSlicesExecutor().execute(
            RetroBuildSlicesInputV1(
                retro_id="retro-mismatched-plan-digest",
                window=_WINDOW,
                source_refs=tuple(sorted((report_ref, plan_ref), key=lambda ref: ref.path)),
            ),
            _scope(tmp_path),
        )


@pytest.mark.asyncio
async def test_plan_digest_without_ref_is_not_a_plan_binding(tmp_path: Path) -> None:
    report_ref = _write(
        tmp_path,
        "qa/results/report/report-outcome.json",
        _json_bytes({"change_id": _CHANGE, "plan_digest": "f" * 64}),
    )

    with pytest.raises(ValueError, match="incomplete plan binding"):
        await RetroBuildSlicesExecutor().execute(
            RetroBuildSlicesInputV1(
                retro_id="retro-digest-without-plan-ref",
                window=_WINDOW,
                source_refs=(report_ref,),
            ),
            _scope(tmp_path),
        )


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
    assert "skill_drift_not_assessed" in result.output.workflow_slice.integrity.reasons
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


def _inspect_batch_files(
    root: Path,
    *,
    coverage_digest: str | None = None,
    with_gap: bool = True,
) -> tuple[EvidenceArtifactRefV1, EvidenceArtifactRefV1, EvidenceArtifactRefV1, EvidenceArtifactRefV1]:
    batch = "847d09aa43e53799011f682eea02b821263135b6926ab59716aee5370affa79f"
    base = f"qa/results/inspect/epochs/0/batches/{batch}"
    projection = "b1a1180af14e5071324cc9d1cd56c0f79594d78f668dcec77590b409047d9b13"
    gaps_payload: dict[str, object] = {
        "schema_version": "1",
        "change_id": _CHANGE,
        "batch_id": batch,
        "projection_digest": f"sha256:{projection}",
        "gaps": (
            [
                {
                    "kind": "uncovered_required_case",
                    "locator": {"case_id": "TC_DEPT_CREATE"},
                    "layer": "execution",
                    "batch_id": batch,
                }
            ]
            if with_gap
            else []
        ),
    }
    gaps_ref = _write(root, f"{base}/coverage-gaps.json", _json_bytes(gaps_payload))
    if coverage_digest is not None and coverage_digest != gaps_ref.digest:
        raise AssertionError("coverage digest fixture drifted")
    observations_ref = _write(
        root,
        f"{base}/observations.json",
        _json_bytes(
            {
                "schema_version": "1.0",
                "change_id": _CHANGE,
                "batch_id": batch,
                "observations": [],
            }
        ),
    )
    trace_ref = _write(root, f"{base}/trace.json", _json_bytes({"change_id": _CHANGE, "batch_id": batch}))
    metrics_ref = _write(root, f"{base}/metrics.json", _json_bytes({"change_id": _CHANGE, "batch_id": batch}))
    return gaps_ref, observations_ref, trace_ref, metrics_ref


@pytest.mark.asyncio
async def test_same_batch_inspect_projections_fill_coverage_gap_slice(tmp_path: Path) -> None:
    gaps_ref, observations_ref, trace_ref, metrics_ref = _inspect_batch_files(tmp_path)
    inspection_ref = _inspection_ref(
        tmp_path,
        "d" * 64,
        batch_id="847d09aa43e53799011f682eea02b821263135b6926ab59716aee5370affa79f",
        coverage_digest=gaps_ref.digest,
        trace_digest=trace_ref.digest,
        metrics_digest=metrics_ref.digest,
    )

    result = await RetroBuildSlicesExecutor().execute(
        RetroBuildSlicesInputV1(
            retro_id="retro-inspect-batch",
            window=_WINDOW,
            source_refs=(inspection_ref,),
        ),
        _scope(tmp_path),
    )

    slice_ = result.output.coverage_gap_slice
    assert slice_ is not None
    assert slice_.integrity.status == "complete"
    assert [source.kind for source in slice_.sources] == ["coverage_gap_projection"]
    assert slice_.sources[0].sha256 == gaps_ref.digest
    assert slice_.entries[0].gap_kind == "uncovered_required_case"
    assert slice_.entries[0].case_id == "TC_DEPT_CREATE"
    assert slice_.entries[0].event_kind == "current"
    assert {source.sha256 for source in result.output.eval_slice.sources} >= {
        inspection_ref.digest,
        observations_ref.digest,
        trace_ref.digest,
        metrics_ref.digest,
    }
    assert result.output.discovery_slice is None


def _assembly_payload(collected: RetroCollectInput, *, with_coverage_gap: bool = True) -> dict[str, object]:
    assembly: dict[str, object] = {
        "generated_at": "2026-09-17T08:00:00Z",
        "window": _WINDOW.model_dump(mode="json"),
    }
    for domain in ("issue", "workflow", "eval"):
        slice_ = getattr(collected, f"{domain}_slice")
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
    coverage_gap = collected.coverage_gap_slice
    assert coverage_gap is not None
    assembly["coverage_gap_slice"] = coverage_gap.model_dump(mode="json")
    if with_coverage_gap:
        assembly["coverage_gap_slice_sha256"] = artifact_digest(coverage_gap)
    return assembly


async def _collected_with_coverage_gap(tmp_path: Path, *, coverage_drift: bool = False) -> RetroCollectInput:
    batch = "847d09aa43e53799011f682eea02b821263135b6926ab59716aee5370affa79f"
    gaps_ref, _, trace_ref, metrics_ref = _inspect_batch_files(tmp_path)
    inspection_ref = _inspection_ref(
        tmp_path,
        "d" * 64,
        batch_id=batch,
        coverage_digest="e" * 64 if coverage_drift else gaps_ref.digest,
        trace_digest=trace_ref.digest,
        metrics_digest=metrics_ref.digest,
    )
    result = await RetroBuildSlicesExecutor().execute(
        RetroBuildSlicesInputV1(
            retro_id="retro-assemble-coverage-gap",
            window=_WINDOW,
            source_refs=(inspection_ref,),
        ),
        _scope(tmp_path),
    )
    return result.output


@pytest.mark.asyncio
async def test_assemble_marks_unanalyzed_coverage_gap_domain_skipped(tmp_path: Path) -> None:
    collected = await _collected_with_coverage_gap(tmp_path)

    context = assemble_context(AssembleRetroInput.model_validate(_assembly_payload(collected)))

    status = context.domain_status.coverage_gap
    assert status is not None
    assert status.status == "skipped"
    assert context.source_manifest.coverage_gap_slice_sha256 is not None
    assert len(context.source_manifest.coverage_gap_sources) == 1
    assert context.signals.coverage_gap == ()


@pytest.mark.asyncio
async def test_assemble_surfaces_unanalyzed_coverage_gap_slice_reasons(tmp_path: Path) -> None:
    collected = await _collected_with_coverage_gap(tmp_path, coverage_drift=True)

    context = assemble_context(AssembleRetroInput.model_validate(_assembly_payload(collected)))

    assert "coverage_gap_evidence_digest_drift" in context.integrity.reasons
    assert context.integrity.status == "incomplete"


@pytest.mark.asyncio
async def test_assemble_rejects_an_unauthenticated_coverage_gap_slice(tmp_path: Path) -> None:
    collected = await _collected_with_coverage_gap(tmp_path)
    assembly = _assembly_payload(collected, with_coverage_gap=False)

    with pytest.raises(InputError, match="coverage_gap"):
        assemble_context(AssembleRetroInput.model_validate(assembly))


@pytest.mark.asyncio
async def test_explicit_coverage_gaps_source_builds_coverage_gap_slice(tmp_path: Path) -> None:
    gaps_ref, _, _, _ = _inspect_batch_files(tmp_path)
    result = await RetroBuildSlicesExecutor().execute(
        RetroBuildSlicesInputV1(
            retro_id="retro-explicit-gaps",
            window=_WINDOW,
            source_refs=(gaps_ref,),
        ),
        _scope(tmp_path),
    )
    slice_ = result.output.coverage_gap_slice
    assert slice_ is not None
    assert len(slice_.entries) == 1
    assert slice_.sources[0].kind == "coverage_gap_projection"


@pytest.mark.asyncio
async def test_locked_inspect_projections_are_not_followed_when_digest_drifts(tmp_path: Path) -> None:
    gaps_ref, _, _, metrics_ref = _inspect_batch_files(tmp_path)
    inspection_ref = _inspection_ref(
        tmp_path,
        "d" * 64,
        batch_id="847d09aa43e53799011f682eea02b821263135b6926ab59716aee5370affa79f",
        coverage_digest="e" * 64,
        trace_digest="e" * 64,
        metrics_digest="1" * 64,
    )
    result = await RetroBuildSlicesExecutor().execute(
        RetroBuildSlicesInputV1(
            retro_id="retro-digest-drift",
            window=_WINDOW,
            source_refs=(inspection_ref,),
        ),
        _scope(tmp_path),
    )
    admitted = {source.sha256 for source in result.output.eval_slice.sources}
    assert gaps_ref.digest not in admitted
    assert metrics_ref.digest not in admitted
    coverage_slice = result.output.coverage_gap_slice
    assert coverage_slice is not None
    assert coverage_slice.entries == ()
    assert "coverage_gap_evidence_digest_drift" in coverage_slice.integrity.reasons
    assert "inspect_projection_digest_drift" in result.output.eval_slice.integrity.reasons


@pytest.mark.asyncio
async def test_ambiguous_inspect_batch_directories_report_a_reason(tmp_path: Path) -> None:
    batch = "847d09aa43e53799011f682eea02b821263135b6926ab59716aee5370affa79f"
    gaps_ref, _, trace_ref, metrics_ref = _inspect_batch_files(tmp_path)
    (tmp_path / "qa" / "results" / "inspect" / "epochs" / "1" / "batches" / batch).mkdir(parents=True)
    inspection_ref = _inspection_ref(
        tmp_path,
        "d" * 64,
        batch_id=batch,
        coverage_digest=gaps_ref.digest,
        trace_digest=trace_ref.digest,
        metrics_digest=metrics_ref.digest,
    )

    result = await RetroBuildSlicesExecutor().execute(
        RetroBuildSlicesInputV1(
            retro_id="retro-ambiguous-batch",
            window=_WINDOW,
            source_refs=(inspection_ref,),
        ),
        _scope(tmp_path),
    )

    assert result.output.coverage_gap_slice is None
    assert gaps_ref.digest not in {source.sha256 for source in result.output.eval_slice.sources}
    assert "inspect_batch_ambiguous" in result.output.eval_slice.integrity.reasons


def _issue_snapshot_bytes(*, occurrence_id: str, problem_id: str) -> bytes:
    batch = "20260917T000000Z"
    return _json_bytes(
        {
            "schema_version": "1.0",
            "change_id": _CHANGE,
            "authoritative_batch_id": batch,
            "batches": [batch],
            "observations": [
                {
                    "observation_id": "OBS-1",
                    "change_id": _CHANGE,
                    "batch_id": batch,
                    "kind": "test_failure",
                    "target": "api",
                    "case_id": "TC_DEPT_CREATE",
                    "source": {
                        "artifact": "qa/results/execution/execute-result.json",
                        "json_pointer": "/results/0",
                    },
                    "evidence_refs": ["qa/results/execution/execute-result.json"],
                    "signature": "assert 500 < 500",
                    "observed_at": "2026-09-17T06:29:27.761613+00:00",
                }
            ],
            "occurrences": [
                {
                    "occurrence_id": occurrence_id,
                    "change_id": _CHANGE,
                    "batch_id": batch,
                    "observation_ids": ["OBS-1"],
                    "problem_id": problem_id,
                    "provisional_assessment": {
                        "classification": "product_bug",
                        "severity": "high",
                        "authority": "llm_provisional",
                        "root_cause_hypothesis": "unique name collision is unhandled",
                    },
                    "analysis": {
                        "evidence_bundle_digest": f"sha256:{'1' * 64}",
                        "analyzer": "assurance.quality",
                        "prompt_version": "1",
                        "candidate_digest": f"sha256:{'2' * 64}",
                    },
                }
            ],
        }
    )


@pytest.mark.asyncio
async def test_issue_ledger_authenticates_both_occurrence_and_problem_identities(
    tmp_path: Path,
) -> None:
    # The issue analysis skill may cite an occurrence or the problem grouping it, so
    # both identities have to be resolvable against this one snapshot.
    snapshot_ref = _write(
        tmp_path,
        "qa/results/issues/snapshot.json",
        _issue_snapshot_bytes(occurrence_id="OCC-1", problem_id="PROB-1"),
    )

    result = await RetroBuildSlicesExecutor().execute(
        RetroBuildSlicesInputV1(
            retro_id="retro-issue-identities",
            window=_WINDOW,
            source_refs=(snapshot_ref,),
        ),
        _scope(tmp_path),
    )

    slice_ = result.output.issue_slice
    assert [source.kind for source in slice_.sources] == ["change_issue_ledger"]
    assert slice_.sources[0].evidence_ids == ("OCC-1", "PROB-1")
    assert slice_.resolvable_ids() >= {"OCC-1", "PROB-1"}


@pytest.mark.asyncio
async def test_corrupt_observations_projection_reports_a_reason(tmp_path: Path) -> None:
    batch = "847d09aa43e53799011f682eea02b821263135b6926ab59716aee5370affa79f"
    gaps_ref, _, trace_ref, metrics_ref = _inspect_batch_files(tmp_path)
    corrupt_ref = _write(
        tmp_path,
        f"qa/results/inspect/epochs/0/batches/{batch}/observations.json",
        _json_bytes({"schema_version": "9.9", "change_id": _CHANGE, "batch_id": batch}),
    )
    inspection_ref = _inspection_ref(
        tmp_path,
        "d" * 64,
        batch_id=batch,
        coverage_digest=gaps_ref.digest,
        trace_digest=trace_ref.digest,
        metrics_digest=metrics_ref.digest,
    )

    result = await RetroBuildSlicesExecutor().execute(
        RetroBuildSlicesInputV1(
            retro_id="retro-corrupt-observations",
            window=_WINDOW,
            source_refs=(inspection_ref,),
        ),
        _scope(tmp_path),
    )

    assert corrupt_ref.digest not in {source.sha256 for source in result.output.eval_slice.sources}
    assert "observations_evidence_corrupt" in result.output.issue_slice.integrity.reasons


@pytest.mark.asyncio
async def test_observations_projection_from_another_batch_reports_a_reason(tmp_path: Path) -> None:
    batch = "847d09aa43e53799011f682eea02b821263135b6926ab59716aee5370affa79f"
    gaps_ref, _, trace_ref, metrics_ref = _inspect_batch_files(tmp_path)
    foreign_ref = _write(
        tmp_path,
        f"qa/results/inspect/epochs/0/batches/{batch}/observations.json",
        _json_bytes(
            {
                "schema_version": "1.0",
                "change_id": _CHANGE,
                "batch_id": "a-different-batch",
                "observations": [],
            }
        ),
    )
    inspection_ref = _inspection_ref(
        tmp_path,
        "d" * 64,
        batch_id=batch,
        coverage_digest=gaps_ref.digest,
        trace_digest=trace_ref.digest,
        metrics_digest=metrics_ref.digest,
    )

    result = await RetroBuildSlicesExecutor().execute(
        RetroBuildSlicesInputV1(
            retro_id="retro-foreign-observations",
            window=_WINDOW,
            source_refs=(inspection_ref,),
        ),
        _scope(tmp_path),
    )

    assert foreign_ref.digest not in {source.sha256 for source in result.output.eval_slice.sources}
    assert "observations_identity_mismatch" in result.output.issue_slice.integrity.reasons
