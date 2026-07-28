"""Accept stage reconciles Candidates into the Project Improvement Ledger."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from assurance_agent.artifacts.models.improvements import (
    DeliveryKind,
    ImprovementCandidate,
    ImprovementCandidateDocument,
    ImprovementKind,
    ImprovementSourceRefs,
    ImprovementVerification,
)
from assurance_agent.retro.accept_stage import run_retro_accept
from assurance_agent.retro.candidates import candidate_batch_digest, context_sha256
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
from assurance_agent.workflow.improvements.identity import (
    improvement_fingerprint,
    improvement_id_for_fingerprint,
)


def _context(
    *,
    retro_id: str = "retro-test",
    evidence_ids: tuple[str, ...] = ("PROB-1", "PROB-2"),
) -> RetroContext:
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
                    evidence_ids=evidence_ids,
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


def _candidate(**overrides: object) -> ImprovementCandidate:
    data: dict = {
        "candidate_id": "IMP-CAND-1",
        "kind": ImprovementKind.WORKFLOW,
        "delivery": DeliveryKind.CHANGE_DRAFT,
        "source_refs": ImprovementSourceRefs(problem_ids=("PROB-1",)),
        "target": "assurance_agent/workflow/inspect",
        "rationale": "Repeated truncation across changes",
        "proposed_change": "Preserve pytest E lines when classifying failures",
        "verification": ImprovementVerification(
            suites=("workflow-full",),
            success_criteria="No truncation Observation",
        ),
        "risk": "low",
        "confidence": "high",
    }
    data.update(overrides)
    return ImprovementCandidate.model_validate(data)


def _setup_retro(
    tmp_path: Path,
    *,
    retro_id: str = "retro-test",
    candidates: tuple[ImprovementCandidate, ...] | None = None,
    context: RetroContext | None = None,
) -> tuple[Path, RetroContext, ImprovementCandidateDocument]:
    ctx = context or _context(retro_id=retro_id)
    retro_dir = tmp_path / "qa" / "retro" / retro_id
    retro_dir.mkdir(parents=True, exist_ok=True)
    (retro_dir / "context.json").write_text(
        json.dumps(ctx.model_dump(mode="json"), sort_keys=True) + "\n",
        encoding="utf-8",
    )
    cand_tuple = candidates if candidates is not None else (_candidate(),)
    document = ImprovementCandidateDocument(
        retro_id=ctx.retro_id,
        context_sha256=context_sha256(ctx),
        candidates=cand_tuple,
    )
    (retro_dir / "proposal-candidates.json").write_text(
        json.dumps(document.model_dump(mode="json"), sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return retro_dir, ctx, document


def test_run_retro_accept_writes_completed_receipt(tmp_path: Path) -> None:
    retro_dir, ctx, document = _setup_retro(tmp_path)
    receipt = run_retro_accept(tmp_path, retro_id="retro-test")
    assert receipt.result == "accepted"
    assert receipt.context_sha256 == context_sha256(ctx)
    assert receipt.candidate_batch_digest == candidate_batch_digest(document)
    status_path = retro_dir / "accept-status.json"
    assert status_path.is_file()
    status = json.loads(status_path.read_text(encoding="utf-8"))
    assert status["result"] == "accepted"
    assert status["improvement_ids"]
    assert status["event_ids"]


def test_run_retro_accept_writes_review_queue_and_ledger(tmp_path: Path) -> None:
    retro_dir, _ctx, document = _setup_retro(tmp_path)
    receipt = run_retro_accept(tmp_path, retro_id="retro-test")
    assert (retro_dir / "review-queue.md").is_file()
    queue_text = (retro_dir / "review-queue.md").read_text(encoding="utf-8")
    for improvement_id in receipt.improvement_ids:
        assert improvement_id in queue_text

    events = read_improvement_events(tmp_path / "qa" / "improvements" / "events.jsonl")
    assert len(events) == 1
    assert events[0].type == "improvement_proposed"
    expected_id = improvement_id_for_fingerprint(improvement_fingerprint(document.candidates[0]))
    assert events[0].improvement_id == expected_id
    assert (tmp_path / "qa" / "improvements" / "improvements.json").is_file()


def test_run_retro_accept_retry_rebuilds_same_receipt(tmp_path: Path) -> None:
    retro_dir, _ctx, _document = _setup_retro(tmp_path)
    first = run_retro_accept(tmp_path, retro_id="retro-test")
    first_status = (retro_dir / "accept-status.json").read_bytes()
    first_events = (tmp_path / "qa" / "improvements" / "events.jsonl").read_bytes()
    second = run_retro_accept(tmp_path, retro_id="retro-test")
    assert second.model_dump(mode="json") == first.model_dump(mode="json")
    assert (retro_dir / "accept-status.json").read_bytes() == first_status
    assert (tmp_path / "qa" / "improvements" / "events.jsonl").read_bytes() == first_events


def test_run_retro_accept_exact_duplicate_still_receipts_canonical_improvement(
    tmp_path: Path,
) -> None:
    _setup_retro(tmp_path, retro_id="retro-first")
    first = run_retro_accept(tmp_path, retro_id="retro-first")
    repeated_context = _context(retro_id="retro-repeat")
    _setup_retro(tmp_path, retro_id="retro-repeat", context=repeated_context)

    repeated = run_retro_accept(tmp_path, retro_id="retro-repeat")

    assert repeated.improvement_ids == first.improvement_ids
    assert repeated.event_ids == ()
    queue = (tmp_path / "qa" / "retro" / "retro-repeat" / "review-queue.md").read_text()
    assert first.improvement_ids[0] in queue


def test_run_retro_accept_invalid_batch_writes_failed_receipt_only(tmp_path: Path) -> None:
    retro_dir, _ctx, _document = _setup_retro(
        tmp_path,
        candidates=(
            _candidate(
                source_refs=ImprovementSourceRefs(problem_ids=("PROB-MISSING",)),
            ),
        ),
    )
    receipt = run_retro_accept(tmp_path, retro_id="retro-test")
    assert receipt.result == "failed"
    status = json.loads((retro_dir / "accept-status.json").read_text(encoding="utf-8"))
    assert status["result"] == "failed"
    assert not (tmp_path / "qa" / "improvements").exists()
    assert not (retro_dir / "review-queue.md").exists()


def test_run_retro_accept_schema_invalid_writes_failed_receipt_only(tmp_path: Path) -> None:
    """Schema-invalid Candidates (forbidden Problem fields) must still write a failed receipt."""
    retro_dir, ctx, _document = _setup_retro(tmp_path)
    raw = {
        "schema_version": "2",
        "retro_id": ctx.retro_id,
        "context_sha256": context_sha256(ctx),
        "candidates": [
            {
                "candidate_id": "IMP-CAND-1",
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
                "severity": "high",
                "status": "in_progress",
                "root_cause": "copied Problem assessment",
            }
        ],
    }
    (retro_dir / "proposal-candidates.json").write_text(
        json.dumps(raw, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    receipt = run_retro_accept(tmp_path, retro_id="retro-test")

    assert receipt.result == "failed"
    assert "forbidden_problem_field" in (receipt.error or "")
    status = json.loads((retro_dir / "accept-status.json").read_text(encoding="utf-8"))
    assert status["result"] == "failed"
    assert status["candidate_batch_digest"].startswith("sha256:")
    assert not (tmp_path / "qa" / "improvements").exists()
    assert not (retro_dir / "review-queue.md").exists()


def test_run_retro_accept_missing_candidates_raises(tmp_path: Path) -> None:
    from assurance_agent.exceptions import AaError

    retro_dir = tmp_path / "qa" / "retro" / "retro-test"
    retro_dir.mkdir(parents=True, exist_ok=True)
    ctx = _context()
    (retro_dir / "context.json").write_text(
        json.dumps(ctx.model_dump(mode="json"), sort_keys=True) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(AaError, match="proposal-candidates"):
        run_retro_accept(tmp_path, retro_id="retro-test")


def test_run_retro_accept_empty_candidates_writes_accepted_without_ledger_events(
    tmp_path: Path,
) -> None:
    retro_dir, _ctx, _document = _setup_retro(tmp_path, candidates=())
    receipt = run_retro_accept(tmp_path, retro_id="retro-test")
    assert receipt.result == "accepted"
    assert receipt.improvement_ids == ()
    assert receipt.event_ids == ()
    assert (retro_dir / "accept-status.json").is_file()
    assert (retro_dir / "review-queue.md").is_file()
    assert not (tmp_path / "qa" / "improvements" / "events.jsonl").exists()
