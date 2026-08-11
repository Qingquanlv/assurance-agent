from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from assurance_agent.artifacts.models.retro_v3 import ImprovementCandidateDocumentV3
from assurance_agent.retro.assemble import RetroAssembleError, assemble_context, write_noop_receipt


def _canon(value: dict) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _seed(retro_dir: Path, *, failed_domain: str | None = None, mismatch: str | None = None) -> None:
    window = {
        "selection": {"mode": "change_ids", "requested_change_ids": ["CH-1"]},
        "change_ids": ["CH-1"],
        "since": None,
        "until": None,
        "project_event_through": "PE-1",
    }
    (retro_dir / "evidence").mkdir(parents=True)
    (retro_dir / "signals").mkdir(parents=True)
    (retro_dir / "window.json").write_bytes(_canon(window))
    for domain in ("issue", "workflow", "eval"):
        source_kind = {
            "issue": "project_problem_ledger",
            "workflow": "workflow_ledger",
            "eval": "eval_run",
        }[domain]
        evidence_id = {"issue": "PROB-1", "workflow": "WF-1", "eval": "RUN-1"}[domain]
        slice_doc = {
            "schema_version": "3",
            "retro_id": "retro-1",
            "domain": domain,
            "window": window,
            "sources": [
                {
                    "kind": source_kind,
                    "sha256": f"sha256:{domain}",
                    "evidence_ids": [evidence_id],
                }
            ],
            "integrity": {"status": "complete", "reasons": []},
            "entries": [],
        }
        slice_bytes = _canon(slice_doc)
        (retro_dir / "evidence" / f"{domain}-slice.json").write_bytes(slice_bytes)
        signal_doc = {
            "schema_version": "3",
            "retro_id": "retro-1",
            "domain": domain,
            "analysis_status": "failed" if domain == failed_domain else "ok",
            "failure_reason": "agent failed" if domain == failed_domain else None,
            "analyzer": f"aa-retro-{domain}-analysis",
            "signals": [],
            "slice_sha256": "sha256:" + hashlib.sha256(slice_bytes).hexdigest(),
        }
        if domain == mismatch:
            signal_doc["slice_sha256"] = "sha256:mismatch"
        (retro_dir / "signals" / f"{domain}.json").write_bytes(_canon(signal_doc))


def _seed_overlapping_issue_signals(retro_dir: Path, *, conflict: bool = False) -> None:
    _seed(retro_dir)
    gap = {
        "signal_id": "BATCH-GAP-1",
        "signal_type": "batch_member_evidence_gap",
        "summary": "Issue evidence unavailable for CH-1",
        "occurrence_count": 1,
        "recommended_change": "Restore complete, immutable issue evidence.",
        "source_refs": {"workflow_evidence_ids": ["BATCH-GAP-1"]},
        "confidence": "high",
        "change_id": "CH-1",
        "execution_status": "failed",
        "domain": "issue",
        "reason_code": "ledger_missing",
    }
    issue_slice_path = retro_dir / "evidence/issue-slice.json"
    issue_slice = json.loads(issue_slice_path.read_text(encoding="utf-8"))
    issue_slice["sources"].append(
        {
            "kind": "batch_manifest",
            "sha256": "sha256:batch",
            "evidence_ids": ["BATCH-GAP-1"],
        }
    )
    issue_slice["deterministic_signals"] = [gap]
    issue_slice_bytes = _canon(issue_slice)
    issue_slice_path.write_bytes(issue_slice_bytes)

    repeated_gap = json.loads(json.dumps(gap))
    if conflict:
        repeated_gap["recommended_change"] = "Trust the analyzer instead."
    novel = {
        "signal_id": "SIG-NOVEL",
        "signal_type": "issue_pattern",
        "summary": "Repeated workflow gap",
        "occurrence_count": 2,
        "recommended_change": "Tighten the workflow contract.",
        "source_refs": {"problem_ids": ["PROB-1"]},
        "confidence": "medium",
        "pattern_kind": "workflow_gap",
        "affected_surface": {"kind": "endpoint", "value": "GET /users"},
        "symptom": "repeated_failure",
    }
    issue_signal_path = retro_dir / "signals/issue.json"
    issue_signal = json.loads(issue_signal_path.read_text(encoding="utf-8"))
    issue_signal["signals"] = [repeated_gap, novel]
    issue_signal["slice_sha256"] = "sha256:" + hashlib.sha256(issue_slice_bytes).hexdigest()
    issue_signal_path.write_bytes(_canon(issue_signal))


def test_assemble_marks_failed_domain_without_hiding_other_domains(tmp_path: Path) -> None:
    retro_dir = tmp_path / "qa/retro/retro-1"
    _seed(retro_dir, failed_domain="workflow")

    context = assemble_context(retro_dir, dry_run=False, now=datetime(2026, 7, 27, tzinfo=timezone.utc))

    assert context.domain_status.workflow.status == "failed"
    assert context.domain_status.issue.status == "ok"
    assert context.integrity.status == "incomplete"
    assert "workflow_signal_analysis_failed" in context.integrity.reasons


def test_digest_mismatch_is_explicit_failed_domain(tmp_path: Path) -> None:
    retro_dir = tmp_path / "qa/retro/retro-1"
    _seed(retro_dir, mismatch="eval")

    context = assemble_context(retro_dir, dry_run=False, now=datetime(2026, 7, 27, tzinfo=timezone.utc))

    assert context.domain_status.eval.status == "failed"
    assert context.domain_status.eval.failure_reason == "slice_digest_mismatch"


def test_assemble_merges_exact_duplicate_signals_in_stable_order(tmp_path: Path) -> None:
    retro_dir = tmp_path / "qa/retro/retro-1"
    _seed_overlapping_issue_signals(retro_dir)

    context = assemble_context(retro_dir, dry_run=False, now=datetime(2026, 7, 27, tzinfo=timezone.utc))

    assert tuple(signal.signal_id for signal in context.signals.issue) == (
        "BATCH-GAP-1",
        "SIG-NOVEL",
    )
    assert context.signal_count == 2


def test_assemble_rejects_conflicting_payloads_for_the_same_signal_id(tmp_path: Path) -> None:
    retro_dir = tmp_path / "qa/retro/retro-1"
    _seed_overlapping_issue_signals(retro_dir, conflict=True)

    with pytest.raises(RetroAssembleError, match="conflicting signal_id.*BATCH-GAP-1"):
        assemble_context(retro_dir, dry_run=False, now=datetime(2026, 7, 27, tzinfo=timezone.utc))


def test_healthy_zero_signal_writes_deterministic_noop_receipt(tmp_path: Path) -> None:
    retro_dir = tmp_path / "qa/retro/retro-1"
    _seed(retro_dir)
    context = assemble_context(retro_dir, dry_run=False, now=datetime(2026, 7, 27, tzinfo=timezone.utc))

    write_noop_receipt(retro_dir, context)

    assert context.signal_count == 0
    candidates = ImprovementCandidateDocumentV3.model_validate_json(
        (retro_dir / "proposal-candidates.json").read_text()
    )
    assert candidates.candidates == ()
    assert "no_actionable_signals" in (retro_dir / "retro-summary.md").read_text()
