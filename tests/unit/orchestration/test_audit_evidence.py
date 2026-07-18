"""Layer 2: gate verdict evidence recording (reads_sha256)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from assurance_agent.workflow.orchestration.audit_evidence import (
    build_gate_verdict_event,
    compute_reads_sha256,
)
from assurance_agent.workflow.orchestration.schema import parse_schema
from tests.helpers_aa import loc_for

SCHEMA = parse_schema("""
schema_version: "1"
name: t
phases:
  - id: case-review
    skill: aa-case-reviewer
    agent: aa-reviewer
    requires: []
    produces: [review/case-review.json]
    gate: case-review-gate
gates:
  case-review-gate:
    reads: [review/case-review.json]
    pass_when: "decision == 'pass'"
""")


def _write_review(change_dir: Path, payload: dict) -> str:
    d = change_dir / "review"
    d.mkdir(parents=True, exist_ok=True)
    path = d / "case-review.json"
    blob = json.dumps(payload).encode("utf-8")
    path.write_bytes(blob)
    return hashlib.sha256(blob).hexdigest()


def test_compute_reads_sha256_hashes_audited_gate_reads(tmp_path: Path) -> None:
    expected = _write_review(tmp_path, {"decision": "pass"})
    loc = loc_for(tmp_path)
    hashes = compute_reads_sha256(SCHEMA, loc, "case-review-gate")
    assert hashes == {"review/case-review.json": expected}


def test_compute_reads_sha256_returns_none_when_file_missing(tmp_path: Path) -> None:
    loc = loc_for(tmp_path)
    assert compute_reads_sha256(SCHEMA, loc, "case-review-gate") is None


def test_compute_reads_sha256_skips_non_audited_reads(tmp_path: Path) -> None:
    schema = parse_schema("""
schema_version: "1"
name: t
phases:
  - id: inspect
    skill: null
    requires: []
    produces: [inspect/failure-analysis.json]
    gate: g
gates:
  g:
    reads: [inspect/failure-analysis.json]
    pass_when: "true"
""")
    d = tmp_path / "inspect"
    d.mkdir()
    (d / "failure-analysis.json").write_text("{}")
    assert compute_reads_sha256(schema, loc_for(tmp_path), "g") is None


def test_build_gate_verdict_event_includes_reads_sha256(tmp_path: Path) -> None:
    expected = _write_review(tmp_path, {"decision": "pass"})
    loc = loc_for(tmp_path)
    event = build_gate_verdict_event(
        loc,
        SCHEMA,
        phase="case-review",
        gate="case-review-gate",
        verdict="pass",
        matched_rule="pass_when: decision == 'pass'",
    )
    assert event["source"] == "gate"
    assert event["type"] == "gate_verdict"
    assert event["phase"] == "case-review"
    assert event["gate"] == "case-review-gate"
    assert event["verdict"] == "pass"
    assert event["reads_sha256"] == {"review/case-review.json": expected}
    assert event["matched_rule"] == "pass_when: decision == 'pass'"
