"""Two-process concurrency for ProjectImprovementStore under project locks."""

from __future__ import annotations

import multiprocessing
from pathlib import Path

from assurance_agent.workflow.improvements.events import (
    IMPROVEMENT_EVENT_ADAPTER,
    ImprovementEvent,
    read_improvement_events,
)
from assurance_agent.workflow.improvements.ledger import ProjectImprovementStore
from assurance_agent.workflow.improvements.projection import project_improvements

IMP_ID = "IMP-ABCDEF0123456789FFFF"
FINGERPRINT = "a" * 64
LOCK_TOKEN = "project:improvement-registry"


def _proposal_event() -> ImprovementEvent:
    return IMPROVEMENT_EVENT_ADAPTER.validate_python(
        {
            "schema_version": "1.0",
            "seq": 1,
            "event_id": "IMPEVT-SHARED",
            "idempotency_key": "IDEM-SHARED-PROPOSAL",
            "ts": "2026-07-26T00:00:00Z",
            "improvement_id": IMP_ID,
            "expected_improvement_version": 0,
            "type": "improvement_proposed",
            "fingerprint": FINGERPRINT,
            "fingerprint_version": "1",
            "kind": "workflow_improvement",
            "delivery": "change_draft",
            "source_refs": {"problem_ids": ["PROB-1"]},
            "target": "assurance_agent/workflow/inspect",
            "rationale": "Repeated truncation",
            "proposed_change": "Preserve pytest E lines",
            "verification": {
                "suites": ["workflow-full"],
                "success_criteria": "No truncation",
            },
            "risk": "low",
            "confidence": "high",
            "retro_id": "RETRO-1",
            "candidate_id": "IMP-CAND-1",
            "context_sha256": "b" * 64,
            "candidate_batch_digest": "c" * 64,
        }
    )


def _worker(project_root: str, start_barrier, results) -> None:
    from assurance_agent.workflow.graph.project_locks import ProjectResourceLockManager
    from assurance_agent.workflow.improvements.events import IMPROVEMENT_EVENT_ADAPTER
    from assurance_agent.workflow.improvements.ledger import ProjectImprovementStore

    try:
        root = Path(project_root)
        event = IMPROVEMENT_EVENT_ADAPTER.validate_python(
            {
                "schema_version": "1.0",
                "seq": 1,
                "event_id": "IMPEVT-SHARED",
                "idempotency_key": "IDEM-SHARED-PROPOSAL",
                "ts": "2026-07-26T00:00:00Z",
                "improvement_id": IMP_ID,
                "expected_improvement_version": 0,
                "type": "improvement_proposed",
                "fingerprint": FINGERPRINT,
                "fingerprint_version": "1",
                "kind": "workflow_improvement",
                "delivery": "change_draft",
                "source_refs": {"problem_ids": ["PROB-1"]},
                "target": "assurance_agent/workflow/inspect",
                "rationale": "Repeated truncation",
                "proposed_change": "Preserve pytest E lines",
                "verification": {
                    "suites": ["workflow-full"],
                    "success_criteria": "No truncation",
                },
                "risk": "low",
                "confidence": "high",
                "retro_id": "RETRO-1",
                "candidate_id": "IMP-CAND-1",
                "context_sha256": "b" * 64,
                "candidate_batch_digest": "c" * 64,
            }
        )
        locks = ProjectResourceLockManager(root)
        start_barrier.wait(timeout=10.0)
        with locks.acquire((LOCK_TOKEN,), timeout_seconds=10.0):
            store = ProjectImprovementStore(root)
            projection = store.append_and_rebuild([event])
        results.put(
            {
                "ok": True,
                "improvement_ids": list(projection.improvements),
                "last_seq": projection.last_seq,
            }
        )
    except BaseException as exc:
        results.put({"ok": False, "error": repr(exc)})
        raise


def test_two_processes_same_fingerprint_under_project_lock(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()

    ctx = multiprocessing.get_context("spawn")
    start_barrier = ctx.Barrier(2)
    results = ctx.Queue()

    workers = [ctx.Process(target=_worker, args=(str(project), start_barrier, results)) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=30)
        assert worker.exitcode == 0

    outcomes = [results.get(timeout=5) for _ in range(2)]
    assert all(item["ok"] for item in outcomes)

    events_path = project / "qa" / "improvements" / "events.jsonl"
    committed = read_improvement_events(events_path)
    assert len(committed) == 1
    assert committed[0].type == "improvement_proposed"
    assert committed[0].seq == 1
    assert committed[0].fingerprint == FINGERPRINT

    store = ProjectImprovementStore(project)
    # Idempotent rebuild from the same proposal must keep a single event.
    projection = store.append_and_rebuild([_proposal_event()])
    assert tuple(projection.improvements) == (IMP_ID,)
    assert projection.by_fingerprint[FINGERPRINT] == IMP_ID
    assert projection.last_seq == 1

    rebuilt = project_improvements(read_improvement_events(events_path))
    assert rebuilt.model_dump(mode="json") == projection.model_dump(mode="json")

    improvements_bytes = (project / "qa/improvements/improvements.json").read_bytes()
    queue_bytes = (project / "qa/improvements/review-queue.json").read_bytes()
    assert improvements_bytes.endswith(b"\n")
    assert queue_bytes.endswith(b"\n")
    assert b'"improvement_ids":["IMP-ABCDEF0123456789FFFF"]' in queue_bytes
