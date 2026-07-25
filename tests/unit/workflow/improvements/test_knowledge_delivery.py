"""TDD tests for KnowledgeDeltaDelivery export / record_applied."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from assurance_agent.artifacts.models.data_knowledge import DataKnowledgeProposal
from assurance_agent.artifacts.models.improvements import (
    ImprovementProjection,
    ImprovementState,
)
from assurance_agent.artifacts.models.issues import Problem
from assurance_agent.workflow.graph.contracts import ResourceClaims
from assurance_agent.workflow.graph.models import ExecutableTask, RuntimeContext
from assurance_agent.workflow.graph.schema_v2 import RetryPolicyDef, TimeoutPolicyDef
from assurance_agent.workflow.improvements.events import IMPROVEMENT_EVENT_ADAPTER
from assurance_agent.workflow.improvements.knowledge_delivery import (
    ImprovementDeliveryConflict,
    ImprovementDeliveryError,
    KnowledgeDeltaDelivery,
    assert_knowledge_eligibility,
    export_knowledge_improvement_operation,
)
from assurance_agent.workflow.improvements.ledger import ProjectImprovementStore
from assurance_agent.workflow.issues.history_models import (
    IssueEvidenceSlice,
    IssueHistoryIntegrity,
    IssueWindowSelection,
)

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


def test_knowledge_export_is_hash_idempotent(tmp_path: Path) -> None:
    improvement = _seed_approved(tmp_path)
    _seed_l1(tmp_path)
    delivery = KnowledgeDeltaDelivery(tmp_path)
    first = delivery.export(improvement, problems={PROB_ID: _problem()})
    ledger = json.loads((tmp_path / "qa/improvements/improvements.json").read_text(encoding="utf-8"))
    current = ImprovementProjection.model_validate(ledger["improvements"][IMP_ID])
    proposal = tmp_path / "qa/improvements/knowledge-delta" / f"{IMP_ID}.proposal.yaml"
    before = proposal.read_bytes()
    second = delivery.export(current, problems={PROB_ID: _problem()})
    assert first.sha256 == second.sha256
    assert first.created is True and second.created is False
    assert proposal.read_bytes() == before


def test_knowledge_export_conflicts_on_different_bytes_when_exported(tmp_path: Path) -> None:
    improvement = _seed_approved(tmp_path)
    _seed_l1(tmp_path)
    delivery = KnowledgeDeltaDelivery(tmp_path)
    delivery.export(improvement, problems={PROB_ID: _problem()})
    ledger = json.loads((tmp_path / "qa/improvements/improvements.json").read_text(encoding="utf-8"))
    current = ImprovementProjection.model_validate(ledger["improvements"][IMP_ID])
    assert current.state is ImprovementState.EXPORTED
    proposal = tmp_path / "qa/improvements/knowledge-delta" / f"{IMP_ID}.proposal.yaml"
    proposal.write_text("schema_version: '1'\nmode: delta\nentities: {}\n", encoding="utf-8")
    with pytest.raises(ImprovementDeliveryConflict, match="conflict|different bytes"):
        delivery.export(current, problems={PROB_ID: _problem()})


def test_assert_knowledge_eligibility_hard_fails_missing_cited_problem() -> None:
    improvement = ImprovementProjection.model_validate(
        {
            "improvement_id": IMP_ID,
            "fingerprint": "k" * 64,
            "fingerprint_version": "1",
            "kind": "domain_knowledge",
            "delivery": "knowledge_delta",
            "source_refs": {"problem_ids": [PROB_ID, "PROB-missing"]},
            "target": ".aa/data-knowledge.yaml",
            "rationale": "Stable dept rule",
            "proposed_change": "Require dept.name",
            "knowledge_delta": _delta().model_dump(mode="json"),
            "verification": {"suites": ["workflow-run"], "success_criteria": "L2 validates"},
            "risk": "low",
            "confidence": "high",
            "state": "approved",
            "version": 2,
            "proposed_by_retro_ids": ["retro-1"],
            "last_event_id": "IMPEVT-APP",
        }
    )
    with pytest.raises(ImprovementDeliveryError, match="missing from pinned"):
        assert_knowledge_eligibility(improvement, {PROB_ID: _problem()})


def test_export_operation_uses_pinned_retro_not_live_problems(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Greening live problems.json must not make export succeed when pin is ineligible."""
    _seed_approved(tmp_path)
    _seed_l1(tmp_path)

    pinned_ineligible = _problem(status="detected")
    slice_ = IssueEvidenceSlice(
        selection=IssueWindowSelection(
            change_ids=("CH-1",), project_event_through="PEVT-1"
        ),
        sources=(),
        integrity=IssueHistoryIntegrity(status="complete"),
        problem_snapshots=(pinned_ineligible,),
    )
    retro_dir = tmp_path / "qa" / "retro" / "retro-1"
    retro_dir.mkdir(parents=True)
    context_doc = {
        "schema_version": "2",
        "retro_id": "retro-1",
        "generated_at": "2026-07-26T00:00:00Z",
        "window": {
            "selection": {"mode": "change_ids", "requested_change_ids": ["CH-1"]},
            "change_ids": ["CH-1"],
            "project_event_through": "PEVT-1",
        },
        "source_manifest": {
            "issue_slice_sha256": slice_.digest(),
            "issue_sources": [],
            "workflow_sources": [],
            "eval_sources": [],
        },
        "integrity": {"status": "complete"},
        "signals": {"issue": {}, "workflow": {}, "eval": {}},
        "signal_count": 0,
    }
    (retro_dir / "context.json").write_text(
        json.dumps(context_doc, sort_keys=True) + "\n", encoding="utf-8"
    )
    # Live projection greened after the pin — must not unlock eligibility.
    problems_path = tmp_path / "qa" / "issues" / "problems.json"
    problems_path.parent.mkdir(parents=True, exist_ok=True)
    live = _problem(status="resolved", authority="human_confirmed")
    problems_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "problems": [live.model_dump(mode="json")],
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    class _FakeReader:
        def __init__(self, project_root: Path) -> None:
            self.project_root = project_root

        def read_window(self, selection: IssueWindowSelection) -> IssueEvidenceSlice:
            assert selection.change_ids == ("CH-1",)
            return slice_

    monkeypatch.setattr(
        "assurance_agent.workflow.improvements.knowledge_delivery.LedgerIssueHistoryReader",
        _FakeReader,
    )

    change_dir = tmp_path / "qa" / "changes" / "CH-know"
    change_dir.mkdir(parents=True)
    workspace = SimpleNamespace(project_root=tmp_path, change_dir=change_dir)
    context = RuntimeContext(
        project_root=tmp_path,
        repo_root=tmp_path,
        change_dir=change_dir,
        change_id="CH-know",
        params={"improvement_id": IMP_ID},
    )
    task = ExecutableTask(
        task_id="task-export-know",
        invocation_id="inv-export-know",
        checkpoint_ns="ns-export-know",
        graph_id="improvement-export-workflow",
        node_id="export-knowledge",
        structural_path="export-knowledge",
        input={},
        input_sha256="0" * 64,
        contract_digest="0" * 64,
        retryable_errors=(),
        retry_policy=RetryPolicyDef(max_attempts=1),
        timeout_policy=TimeoutPolicyDef(run_seconds=300, heartbeat_seconds=60),
        target="operation:export-knowledge-improvement",
        resources=ResourceClaims(),
    )
    result = export_knowledge_improvement_operation(task, workspace, context)  # type: ignore[arg-type]
    assert result.status == "failed"
    assert result.error is not None
    assert "eligibility" in result.error or "status" in result.error
