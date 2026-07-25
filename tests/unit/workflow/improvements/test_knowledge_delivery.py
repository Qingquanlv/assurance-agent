"""TDD tests for KnowledgeDeltaDelivery export / record_applied."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import yaml

from assurance_agent.artifacts.models.data_knowledge import DataKnowledgeProposal
from assurance_agent.artifacts.models.improvements import (
    ImprovementProjection,
    ImprovementState,
)
from assurance_agent.artifacts.models.issues import Problem
from assurance_agent.workflow.improvements.events import IMPROVEMENT_EVENT_ADAPTER
from assurance_agent.workflow.improvements.knowledge_delivery import (
    ImprovementDeliveryError,
    KnowledgeDeltaDelivery,
)
from assurance_agent.workflow.improvements.ledger import ProjectImprovementStore

IMP_ID = "IMP-KNOW00000000000001"
PROB_ID = "PROB-know1"

_PROHIBITED = (
    "detected",
    "triaged",
    "in_progress",
    "verification_pending",
    "accepted_risk",
    "not_an_issue",
)


def _delta() -> DataKnowledgeProposal:
    return DataKnowledgeProposal.model_validate(
        {
            "schema_version": "1",
            "mode": "delta",
            "entities": {"dept": {"required_fields": ["name"]}},
        }
    )


def _problem(*, status: str = "resolved", authority: str = "human_confirmed") -> Problem:
    payload: dict = {
        "problem_id": PROB_ID,
        "fingerprint": {"version": "1", "digest": "b" * 64},
        "title": "Missing dept name rule",
        "assessment": {
            "classification": "test_data_issue",
            "severity": "medium",
            "authority": authority,
        },
        "status": status,
        "first_seen": {"change_id": "CH-1", "occurrence_id": "OCC-1"},
        "last_seen": {"change_id": "CH-1", "occurrence_id": "OCC-1"},
        "occurrences": ["OCC-1"],
        "version": 2,
    }
    if status == "resolved":
        payload["resolution"] = {
            "resolved_at": "2026-07-26T00:00:00Z",
            "change_id": "CH-1",
            "batch_id": "B-1",
            "disposition": "fixed",
            "verification_scope": ["api"],
            "evidence_digest": "e" * 64,
        }
    return Problem.model_validate(payload)


def _seed_approved(project: Path) -> ImprovementProjection:
    store = ProjectImprovementStore(project)
    store.append_and_rebuild(
        [
            IMPROVEMENT_EVENT_ADAPTER.validate_python(
                {
                    "schema_version": "1.0",
                    "seq": 1,
                    "event_id": "IMPEVT-PROP",
                    "idempotency_key": "IDEM-PROP",
                    "ts": "2026-07-26T00:00:00Z",
                    "improvement_id": IMP_ID,
                    "expected_improvement_version": 0,
                    "type": "improvement_proposed",
                    "fingerprint": "k" * 64,
                    "fingerprint_version": "1",
                    "kind": "domain_knowledge",
                    "delivery": "knowledge_delta",
                    "source_refs": {"problem_ids": [PROB_ID]},
                    "target": ".aa/data-knowledge.yaml",
                    "rationale": "Stable dept rule",
                    "proposed_change": "Require dept.name",
                    "knowledge_delta": _delta().model_dump(mode="json"),
                    "verification": {
                        "suites": ["workflow-run"],
                        "success_criteria": "L2 validates",
                    },
                    "risk": "low",
                    "confidence": "high",
                    "retro_id": "retro-1",
                    "candidate_id": "C-1",
                    "context_sha256": "c" * 64,
                    "candidate_batch_digest": "d" * 64,
                }
            ),
            IMPROVEMENT_EVENT_ADAPTER.validate_python(
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
                }
            ),
        ]
    )
    ledger = json.loads((project / "qa/improvements/improvements.json").read_text(encoding="utf-8"))
    return ImprovementProjection.model_validate(ledger["improvements"][IMP_ID])


def _seed_l1(project: Path) -> str:
    path = project / ".aa" / "data-knowledge.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    text = (
        "version: 1\n"
        "accounts: {}\n"
        "auth: {}\n"
        "entities: {}\n"
        "capabilities:\n"
        "  domain_factories: {}\n"
        "  adapters: {}\n"
        "  cleanup: {}\n"
    )
    path.write_text(text, encoding="utf-8")
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize("status", _PROHIBITED)
def test_knowledge_export_rejects_prohibited_problem_states(
    tmp_path: Path, status: str
) -> None:
    improvement = _seed_approved(tmp_path)
    _seed_l1(tmp_path)
    delivery = KnowledgeDeltaDelivery(tmp_path)
    with pytest.raises(ImprovementDeliveryError, match="eligibility|status"):
        delivery.export(improvement, problems={PROB_ID: _problem(status=status)})


def test_knowledge_export_rejects_non_human_confirmed(tmp_path: Path) -> None:
    improvement = _seed_approved(tmp_path)
    _seed_l1(tmp_path)
    delivery = KnowledgeDeltaDelivery(tmp_path)
    with pytest.raises(ImprovementDeliveryError, match="human_confirmed|eligibility"):
        delivery.export(
            improvement,
            problems={PROB_ID: _problem(authority="llm_provisional")},
        )


def test_knowledge_export_rejects_invalid_l2_semantics(tmp_path: Path) -> None:
    store = ProjectImprovementStore(tmp_path)
    store.append_and_rebuild(
        [
            IMPROVEMENT_EVENT_ADAPTER.validate_python(
                {
                    "schema_version": "1.0",
                    "seq": 1,
                    "event_id": "IMPEVT-PROP",
                    "idempotency_key": "IDEM-PROP",
                    "ts": "2026-07-26T00:00:00Z",
                    "improvement_id": IMP_ID,
                    "expected_improvement_version": 0,
                    "type": "improvement_proposed",
                    "fingerprint": "k" * 64,
                    "fingerprint_version": "1",
                    "kind": "domain_knowledge",
                    "delivery": "knowledge_delta",
                    "source_refs": {"problem_ids": [PROB_ID]},
                    "target": ".aa/data-knowledge.yaml",
                    "rationale": "Stable dept rule",
                    "proposed_change": "Require dept.name",
                    "knowledge_delta": {"schema_version": "1", "mode": "delta"},
                    "verification": {
                        "suites": ["workflow-run"],
                        "success_criteria": "L2 validates",
                    },
                    "risk": "low",
                    "confidence": "high",
                    "retro_id": "retro-1",
                    "candidate_id": "C-1",
                    "context_sha256": "c" * 64,
                    "candidate_batch_digest": "d" * 64,
                }
            ),
            IMPROVEMENT_EVENT_ADAPTER.validate_python(
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
                }
            ),
        ]
    )
    improvement = ImprovementProjection.model_validate(
        json.loads((tmp_path / "qa/improvements/improvements.json").read_text())["improvements"][
            IMP_ID
        ]
    )
    _seed_l1(tmp_path)
    delivery = KnowledgeDeltaDelivery(tmp_path)
    with pytest.raises(ImprovementDeliveryError, match="semantic|L2|leaves|invalid"):
        delivery.export(improvement, problems={PROB_ID: _problem()})


def test_knowledge_export_does_not_mutate_l1(tmp_path: Path) -> None:
    improvement = _seed_approved(tmp_path)
    l1_digest = _seed_l1(tmp_path)
    before = (tmp_path / ".aa/data-knowledge.yaml").read_bytes()
    delivery = KnowledgeDeltaDelivery(tmp_path)
    receipt = delivery.export(improvement, problems={PROB_ID: _problem()})
    assert receipt.created is True
    assert (tmp_path / ".aa/data-knowledge.yaml").read_bytes() == before
    assert (
        hashlib.sha256((tmp_path / ".aa/data-knowledge.yaml").read_bytes()).hexdigest()
        == l1_digest
    )
    proposal_path = tmp_path / "qa/improvements/knowledge-delta" / f"{IMP_ID}.proposal.yaml"
    assert proposal_path.is_file()
    raw = yaml.safe_load(proposal_path.read_text(encoding="utf-8"))
    assert raw["mode"] == "delta"
    assert "title" not in raw


def test_record_applied_verifies_current_l1_digest(tmp_path: Path) -> None:
    improvement = _seed_approved(tmp_path)
    l1_digest = _seed_l1(tmp_path)
    delivery = KnowledgeDeltaDelivery(tmp_path)
    exported = delivery.export(improvement, problems={PROB_ID: _problem()})
    ledger = json.loads((tmp_path / "qa/improvements/improvements.json").read_text(encoding="utf-8"))
    current = ImprovementProjection.model_validate(ledger["improvements"][IMP_ID])
    with pytest.raises(ImprovementDeliveryError, match="L1 digest"):
        delivery.record_applied(
            current,
            actor="human",
            reason="promoted",
            artifact_digest=exported.sha256,
            expected_l1_digest="0" * 64,
        )
    delivery.record_applied(
        current,
        actor="human",
        reason="promoted",
        artifact_digest=exported.sha256,
        expected_l1_digest=l1_digest,
    )
    ledger = json.loads((tmp_path / "qa/improvements/improvements.json").read_text(encoding="utf-8"))
    assert ledger["improvements"][IMP_ID]["state"] == ImprovementState.APPLIED.value
