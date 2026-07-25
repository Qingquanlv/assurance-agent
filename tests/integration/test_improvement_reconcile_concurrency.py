"""Two-process Improvement reconcile for equivalent Candidates from different Retros."""

from __future__ import annotations

import json
import multiprocessing
from pathlib import Path

from assurance_agent.artifacts.models.improvements import (
    DeliveryKind,
    ImprovementCandidate,
    ImprovementCandidateDocument,
    ImprovementKind,
    ImprovementSourceRefs,
    ImprovementVerification,
)
from assurance_agent.retro.candidates import context_sha256
from assurance_agent.retro.types import (
    EvalRetroSignals,
    IssueRetroSignals,
    RetroContext,
    RetroIntegrity,
    RetroSelectionSnapshot,
    RetroSignalSet,
    RetroSourceDescriptor,
    RetroSourceManifest,
    RetroWindow,
    WorkflowRetroSignals,
)
from assurance_agent.workflow.improvements.events import read_improvement_events
from assurance_agent.workflow.improvements.identity import improvement_fingerprint
from assurance_agent.workflow.improvements.projection import dump_projection, project_improvements

LOCK_TOKEN = "project:improvement-registry"


def _context(retro_id: str) -> RetroContext:
    return RetroContext(
        retro_id=retro_id,
        generated_at="2026-07-25T00:00:00Z",
        window=RetroWindow(
            selection=RetroSelectionSnapshot(mode="change_ids", requested_change_ids=("CH-1",)),
            change_ids=("CH-1",),
        ),
        source_manifest=RetroSourceManifest(
            issue_slice_sha256="sha256:slice",
            issue_sources=(
                RetroSourceDescriptor(
                    kind="change_issue_ledger",
                    change_id="CH-1",
                    sha256="sha256:issue",
                    evidence_ids=("PROB-1", "PROB-2"),
                ),
            ),
            workflow_sources=(),
            eval_sources=(),
        ),
        integrity=RetroIntegrity(status="complete"),
        signals=RetroSignalSet(
            issue=IssueRetroSignals(),
            workflow=WorkflowRetroSignals(),
            eval=EvalRetroSignals(),
        ),
        signal_count=0,
    )


def _candidate(*, candidate_id: str, problem_id: str) -> ImprovementCandidate:
    return ImprovementCandidate(
        candidate_id=candidate_id,
        kind=ImprovementKind.WORKFLOW,
        delivery=DeliveryKind.CHANGE_DRAFT,
        source_refs=ImprovementSourceRefs(problem_ids=(problem_id,)),
        target="assurance_agent/workflow/inspect",
        rationale="Repeated truncation across changes",
        proposed_change="Preserve pytest E lines when classifying failures",
        verification=ImprovementVerification(
            suites=("workflow-full",),
            success_criteria="No truncation Observation",
        ),
        risk="low",
        confidence="high",
    )


def _write_retro(project: Path, retro_id: str, problem_id: str) -> None:
    ctx = _context(retro_id)
    retro_dir = project / "qa" / "retro" / retro_id
    retro_dir.mkdir(parents=True, exist_ok=True)
    (retro_dir / "context.json").write_text(
        json.dumps(ctx.model_dump(mode="json"), sort_keys=True) + "\n",
        encoding="utf-8",
    )
    document = ImprovementCandidateDocument(
        retro_id=retro_id,
        context_sha256=context_sha256(ctx),
        candidates=(_candidate(candidate_id=f"IMP-CAND-{retro_id}", problem_id=problem_id),),
    )
    (retro_dir / "proposal-candidates.json").write_text(
        json.dumps(document.model_dump(mode="json"), sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _worker(project_root: str, retro_id: str, start_barrier, results) -> None:
    from assurance_agent.retro.accept_stage import run_retro_accept

    try:
        root = Path(project_root)
        start_barrier.wait(timeout=10.0)
        receipt = run_retro_accept(root, retro_id=retro_id)
        results.put(
            {
                "ok": True,
                "result": receipt.result,
                "improvement_ids": list(receipt.improvement_ids),
                "event_ids": list(receipt.event_ids),
            }
        )
    except BaseException as exc:
        results.put({"ok": False, "error": repr(exc)})
        raise


def test_two_processes_same_fingerprint_different_retro_ids(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    # Equivalent intent (same fingerprint); distinct source-ref sets.
    _write_retro(project, "retro-a", "PROB-1")
    _write_retro(project, "retro-b", "PROB-2")

    fingerprint = improvement_fingerprint(
        _candidate(candidate_id="x", problem_id="PROB-1")
    )

    ctx = multiprocessing.get_context("spawn")
    start_barrier = ctx.Barrier(2)
    results = ctx.Queue()
    workers = [
        ctx.Process(target=_worker, args=(str(project), retro_id, start_barrier, results))
        for retro_id in ("retro-a", "retro-b")
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=30)
        assert worker.exitcode == 0

    outcomes = [results.get(timeout=5) for _ in range(2)]
    assert all(item["ok"] for item in outcomes)
    assert all(item["result"] == "accepted" for item in outcomes)

    events_path = project / "qa" / "improvements" / "events.jsonl"
    committed = read_improvement_events(events_path)
    proposed = [event for event in committed if event.type == "improvement_proposed"]
    linked = [event for event in committed if event.type == "improvement_evidence_linked"]
    assert len(proposed) == 1
    assert len(linked) == 1
    assert proposed[0].fingerprint == fingerprint
    assert {event.improvement_id for event in committed} == {proposed[0].improvement_id}
    assert [event.seq for event in committed] == list(range(1, len(committed) + 1))

    # At most one evidence link per source-ref set.
    linked_ref_sets = {
        tuple(sorted(event.source_refs.problem_ids)) for event in linked
    }
    assert len(linked_ref_sets) == len(linked)

    projection = project_improvements(committed)
    assert len(projection.improvements) == 1
    assert projection.by_fingerprint[fingerprint] == proposed[0].improvement_id

    improvements_bytes = (project / "qa/improvements/improvements.json").read_bytes()
    queue_bytes = (project / "qa/improvements/review-queue.json").read_bytes()
    assert improvements_bytes == dump_projection(projection)
    assert queue_bytes.endswith(b"\n")

    # Re-run either batch → no new ledger bytes.
    before = events_path.read_bytes()
    from assurance_agent.retro.accept_stage import run_retro_accept

    again_a = run_retro_accept(project, retro_id="retro-a")
    again_b = run_retro_accept(project, retro_id="retro-b")
    assert again_a.result == "accepted"
    assert again_b.result == "accepted"
    assert events_path.read_bytes() == before
    assert (project / "qa/improvements/improvements.json").read_bytes() == improvements_bytes
