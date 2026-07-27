from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from assurance_agent.artifacts.models.retro_v3 import ImprovementCandidateDocumentV3
from assurance_agent.retro.assemble import assemble_context, write_noop_receipt


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
