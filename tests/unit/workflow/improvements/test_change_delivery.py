"""TDD tests for ChangeDraftDelivery export / record_applied."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from assurance_agent.artifacts.models.improvements import (
    ImprovementProjection,
    ImprovementState,
)
from assurance_agent.workflow.improvements.change_delivery import (
    ChangeDraftDelivery,
    ImprovementDeliveryConflict,
    ImprovementDeliveryError,
)
from assurance_agent.workflow.improvements.events import IMPROVEMENT_EVENT_ADAPTER
from assurance_agent.workflow.improvements.ledger import ProjectImprovementStore

IMP_ID = "IMP-CHG000000000000001"


def _seed_approved(project: Path) -> ImprovementProjection:
    store = ProjectImprovementStore(project)
    events: list[dict] = [
        {
            "schema_version": "1.0",
            "seq": 1,
            "event_id": "IMPEVT-PROP",
            "idempotency_key": "IDEM-PROP",
            "ts": "2026-07-26T00:00:00Z",
            "improvement_id": IMP_ID,
            "expected_improvement_version": 0,
            "type": "improvement_proposed",
            "fingerprint": "b" * 64,
            "fingerprint_version": "1",
            "kind": "workflow_improvement",
            "delivery": "change_draft",
            "source_refs": {"problem_ids": ["PROB-1"]},
            "target": "assurance_agent/workflow/inspect",
            "rationale": "Preserve E lines",
            "proposed_change": "Keep pytest E lines",
            "verification": {
                "suites": ["workflow-full"],
                "required_cases": ["case-a"],
                "success_criteria": "No truncation",
            },
            "risk": "low",
            "confidence": "high",
            "retro_id": "retro-1",
            "candidate_id": "C-1",
            "context_sha256": "c" * 64,
            "candidate_batch_digest": "d" * 64,
        },
        {
            "schema_version": "1.0",
            "seq": 2,
            "event_id": "IMPEVT-APP",
            "idempotency_key": "IDEM-APP",
            "ts": "2026-07-26T00:01:00Z",
            "improvement_id": IMP_ID,
            "expected_improvement_version": 1,
            "type": "improvement_review_approved",
            "who": "reviewer",
            "reason": "ok",
            "review_id": "REV-1",
        },
    ]
    store.append_and_rebuild(
        [IMPROVEMENT_EVENT_ADAPTER.validate_python(item) for item in events]
    )
    ledger = json.loads((project / "qa/improvements/improvements.json").read_text(encoding="utf-8"))
    return ImprovementProjection.model_validate(ledger["improvements"][IMP_ID])


def test_change_draft_is_hash_idempotent(tmp_path: Path) -> None:
    improvement = _seed_approved(tmp_path)
    delivery = ChangeDraftDelivery(tmp_path)
    first = delivery.export(improvement)
    second = delivery.export(improvement)
    assert first.sha256 == second.sha256
    assert first.created is True and second.created is False
    draft = yaml.safe_load(
        (tmp_path / "qa/improvements/drafts" / f"{IMP_ID}.yaml").read_text(encoding="utf-8")
    )
    assert draft["improvement_id"] == IMP_ID
    assert "title" not in draft
    assert "classification" not in draft
    assert "severity" not in draft
    assert "status" not in draft
    assert "disposition" not in draft
    assert "root_cause" not in draft


def test_conflicting_draft_bytes_raise_unless_rework_bumped_version(tmp_path: Path) -> None:
    improvement = _seed_approved(tmp_path)
    delivery = ChangeDraftDelivery(tmp_path)
    path = tmp_path / "qa/improvements/drafts" / f"{IMP_ID}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    # Pre-existing draft at the same Improvement version with different bytes.
    path.write_text(
        yaml.safe_dump(
            {
                "improvement_id": IMP_ID,
                "improvement_version": improvement.version,
                "kind": "workflow_improvement",
                "source_refs": {"problem_ids": ["PROB-1"]},
                "target": "assurance_agent/workflow/inspect",
                "proposed_change": "different bytes",
                "verification": {"suites": ["workflow-full"], "success_criteria": "x"},
                "content_sha256": "0" * 64,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    with pytest.raises(ImprovementDeliveryConflict, match="conflict"):
        delivery.export(improvement)

    # Lower draft version + evidence link bump allows overwrite while still approved.
    path.write_text(
        yaml.safe_dump(
            {
                "improvement_id": IMP_ID,
                "improvement_version": 1,
                "kind": "workflow_improvement",
                "source_refs": {"problem_ids": ["PROB-1"]},
                "target": "assurance_agent/workflow/inspect",
                "proposed_change": "old",
                "verification": {"suites": ["workflow-full"], "success_criteria": "x"},
                "content_sha256": "0" * 64,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    store = ProjectImprovementStore(tmp_path)
    store.append_and_rebuild(
        [
            IMPROVEMENT_EVENT_ADAPTER.validate_python(
                {
                    "schema_version": "1.0",
                    "seq": 1,
                    "event_id": "IMPEVT-EVID",
                    "idempotency_key": "IDEM-EVID",
                    "ts": "2026-07-26T00:05:00Z",
                    "improvement_id": IMP_ID,
                    "expected_improvement_version": improvement.version,
                    "type": "improvement_evidence_linked",
                    "source_refs": {"problem_ids": ["PROB-2"]},
                    "retro_id": "retro-2",
                    "candidate_id": "C-2",
                    "context_sha256": "1" * 64,
                    "candidate_batch_digest": "2" * 64,
                }
            )
        ]
    )
    ledger = json.loads((tmp_path / "qa/improvements/improvements.json").read_text(encoding="utf-8"))
    current = ImprovementProjection.model_validate(ledger["improvements"][IMP_ID])
    overwritten = delivery.export(current)
    assert overwritten.created is True
    assert overwritten.sha256 != "0" * 64


def test_record_applied_requires_actor_reason_and_digest(tmp_path: Path) -> None:
    improvement = _seed_approved(tmp_path)
    delivery = ChangeDraftDelivery(tmp_path)
    exported = delivery.export(improvement)
    ledger = json.loads((tmp_path / "qa/improvements/improvements.json").read_text(encoding="utf-8"))
    current = ImprovementProjection.model_validate(ledger["improvements"][IMP_ID])
    with pytest.raises(ImprovementDeliveryError, match="actor"):
        delivery.record_applied(
            current, actor="", reason="done", artifact_digest=exported.sha256
        )
    with pytest.raises(ImprovementDeliveryError, match="reason"):
        delivery.record_applied(
            current, actor="human", reason="", artifact_digest=exported.sha256
        )
    with pytest.raises(ImprovementDeliveryError, match="digest"):
        delivery.record_applied(
            current, actor="human", reason="done", artifact_digest="0" * 64
        )
    delivery.record_applied(
        current, actor="human", reason="merged PR", artifact_digest=exported.sha256
    )
    ledger = json.loads((tmp_path / "qa/improvements/improvements.json").read_text(encoding="utf-8"))
    assert ledger["improvements"][IMP_ID]["state"] == ImprovementState.APPLIED.value
