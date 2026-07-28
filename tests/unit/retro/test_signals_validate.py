from __future__ import annotations

import json

import pytest

from assurance_agent.artifacts.models.retro_v3 import EvalEvidenceSlice, IssueEvidenceSlice
from assurance_agent.workflow.retro_outputs import (
    SignalInvalidError,
    backfill_slice_digest,
    complete_signal_outputs,
    validate_signal_draft,
)


WINDOW = {
    "selection": {"mode": "change_ids", "requested_change_ids": ["CH-1"]},
    "change_ids": ["CH-1"],
}


def _slice() -> IssueEvidenceSlice:
    return IssueEvidenceSlice.model_validate(
        {
            "retro_id": "retro-1",
            "window": WINDOW,
            "sources": [
                {
                    "kind": "project_problem_ledger",
                    "sha256": "sha256:source",
                    "evidence_ids": ["PROB-1", "OCC-1"],
                }
            ],
        }
    )


def _draft() -> dict:
    return {
        "schema_version": "3",
        "retro_id": "retro-1",
        "domain": "issue",
        "analysis_status": "ok",
        "analyzer": "aa-retro-issue-analysis",
        "signals": [
            {
                "signal_id": "SIG-1",
                "signal_type": "issue_pattern",
                "summary": "Repeated endpoint failures",
                "occurrence_count": 2,
                "recommended_change": "Tighten the workflow",
                "source_refs": {"problem_ids": ["PROB-1"], "occurrence_ids": ["OCC-1"]},
                "confidence": "high",
                "pattern_kind": "workflow_gap",
                "affected_surface": {"kind": "endpoint", "value": "GET /users"},
                "symptom": "500",
            }
        ],
    }


def test_validate_then_backfill_owns_slice_digest() -> None:
    slice_ = _slice()
    draft = validate_signal_draft("issue", _draft(), slice_)
    slice_bytes = (json.dumps(slice_.model_dump(mode="json"), sort_keys=True) + "\n").encode()

    canonical = backfill_slice_digest(draft, slice_bytes)

    assert canonical.slice_sha256.startswith("sha256:")
    assert canonical.domain == "issue"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda doc: doc.update(slice_sha256="sha256:agent-owned"),
        lambda doc: doc["signals"][0]["source_refs"].update(problem_ids=["UNKNOWN"]),
        lambda doc: doc.update(domain="workflow"),
    ],
)
def test_invalid_digest_unknown_ref_or_wrong_domain_rejects_whole_draft(mutate) -> None:
    doc = _draft()
    mutate(doc)

    with pytest.raises(SignalInvalidError):
        validate_signal_draft("issue", doc, _slice())


def test_failed_analysis_requires_reason_and_no_signals() -> None:
    doc = _draft()
    doc["analysis_status"] = "failed"

    with pytest.raises(SignalInvalidError):
        validate_signal_draft("issue", doc, _slice())


def test_domain_rejects_a_signal_type_owned_by_another_analyzer() -> None:
    doc = _draft()
    doc["domain"] = "eval"
    doc["signals"][0]["source_refs"] = {"eval_run_ids": ["RUN-1"]}

    eval_slice = EvalEvidenceSlice.model_validate(
        {
            "retro_id": "retro-1",
            "window": WINDOW,
            "sources": [
                {
                    "kind": "eval_run",
                    "sha256": "sha256:source",
                    "evidence_ids": ["RUN-1"],
                }
            ],
        }
    )

    with pytest.raises(SignalInvalidError, match="illegal signal_type"):
        validate_signal_draft("eval", doc, eval_slice)


def test_complete_signal_output_rewrites_draft_as_canonical(tmp_path) -> None:
    retro_dir = tmp_path / "qa/retro/retro-1"
    slice_path = retro_dir / "evidence/issue-slice.json"
    signal_path = retro_dir / "signals/issue.json"
    slice_path.parent.mkdir(parents=True)
    signal_path.parent.mkdir(parents=True)
    slice_path.write_text(json.dumps(_slice().model_dump(mode="json")) + "\n", encoding="utf-8")
    signal_path.write_text(json.dumps(_draft()), encoding="utf-8")

    complete_signal_outputs(
        tmp_path,
        ("project:qa/retro/retro-1/signals/issue.json",),
    )

    completed = json.loads(signal_path.read_text())
    assert completed["slice_sha256"].startswith("sha256:")
    assert completed["domain"] == "issue"
