"""Tests for issue analyzer output completion / coercion."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from assurance_agent.workflow.issues.analyzer_output import (
    IssueAnalyzerOutputError,
    complete_issue_analyzer_outputs,
)

_OUTPUTS = (
    "change:inspect/issue-candidates.json",
    "change:inspect/issue-analysis-status.json",
)


def _write_pair(change_dir: Path, *, confidence: object) -> None:
    inspect = change_dir / "inspect"
    inspect.mkdir(parents=True)
    candidates = {
        "schema_version": "1.0",
        "change_id": "CH-1",
        "batch_id": "B-1",
        "evidence_bundle_digest": "sha256:" + ("a" * 64),
        "candidates": [
            {
                "candidate_id": "CAND-001",
                "observation_ids": ["OBS-001"],
                "proposed": {
                    "title": "Bug",
                    "classification": "product_bug",
                    "severity": "high",
                    "root_cause_hypothesis": "x",
                },
                "affected_surface": {"kind": "endpoint", "value": "GET /api/x"},
                "fingerprint_inputs": {"surface": "get /api/x", "symptom": "http_500"},
                "possible_problem_ids": [],
                "confidence": confidence,
                "recommended_action": "investigate",
            }
        ],
    }
    status = {
        "schema_version": "1.0",
        "change_id": "CH-1",
        "batch_id": "B-1",
        "status": "completed",
        "evidence_bundle_digest": "sha256:" + ("a" * 64),
        "candidate_count": 1,
    }
    (inspect / "issue-candidates.json").write_text(json.dumps(candidates), encoding="utf-8")
    (inspect / "issue-analysis-status.json").write_text(json.dumps(status), encoding="utf-8")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("high", 0.85),
        ("medium", 0.5),
        ("low", 0.25),
        ("0.7", 0.7),
        (0.91, 0.91),
    ],
)
def test_complete_issue_analyzer_outputs_coerces_confidence(
    tmp_path: Path, raw: object, expected: float
) -> None:
    change_dir = tmp_path / "change"
    change_dir.mkdir()
    _write_pair(change_dir, confidence=raw)

    complete_issue_analyzer_outputs(change_dir, _OUTPUTS)

    candidates = json.loads((change_dir / "inspect" / "issue-candidates.json").read_text())
    status = json.loads((change_dir / "inspect" / "issue-analysis-status.json").read_text())
    assert candidates["candidates"][0]["confidence"] == pytest.approx(expected)
    assert status["candidate_digest"]


def test_complete_issue_analyzer_outputs_rejects_unknown_confidence_label(tmp_path: Path) -> None:
    change_dir = tmp_path / "change"
    change_dir.mkdir()
    _write_pair(change_dir, confidence="maybe")

    with pytest.raises(IssueAnalyzerOutputError, match="invalid Issue analyzer output"):
        complete_issue_analyzer_outputs(change_dir, _OUTPUTS)
