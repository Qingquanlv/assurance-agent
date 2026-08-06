"""Pure Improvement candidate reconciliation into ledger events."""

from __future__ import annotations

import pytest

from assurance_agent.artifacts.models.data_knowledge import to_persisted_data_knowledge_proposal
from assurance_agent.artifacts.models.improvements import (
    DeliveryKind,
    ImprovementCandidate,
    ImprovementCandidateDocument,
    ImprovementKind,
    ImprovementLedgerProjection,
    ImprovementProjection,
    ImprovementSourceRefs,
    ImprovementState,
    ImprovementVerification,
)
from assurance_agent.retro.candidates import CandidateBatchInvalid, context_sha256
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
from assurance_agent.workflow.improvements.events import (
    ImprovementProposedEvent,
    ImprovementSupersededEvent,
)
from assurance_agent.workflow.improvements.identity import (
    improvement_fingerprint,
    improvement_id_for_fingerprint,
)
from assurance_agent.workflow.improvements.reconciler import (
    ImprovementReconciliationPlan,
    reconcile_improvement_candidates,
)


def _context(
    *,
    retro_id: str = "retro-1",
    evidence_ids: tuple[str, ...] = ("PROB-1", "PROB-2", "OCC-1", "IMP-OLD"),
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


def _document(
    context: RetroContext,
    *candidates: ImprovementCandidate,
) -> ImprovementCandidateDocument:
    return ImprovementCandidateDocument(
        retro_id=context.retro_id,
        context_sha256=context_sha256(context),
        candidates=candidates,
    )


def _empty_projection() -> ImprovementLedgerProjection:
    return ImprovementLedgerProjection(
        schema_version="1",
        last_seq=0,
        improvements={},
        by_fingerprint={},
    )


def _projection_with(
    candidate: ImprovementCandidate,
    *,
    version: int = 1,
    source_refs: ImprovementSourceRefs | None = None,
    retro_ids: tuple[str, ...] = ("retro-prior",),
) -> ImprovementLedgerProjection:
    fingerprint = improvement_fingerprint(candidate)
    improvement_id = improvement_id_for_fingerprint(fingerprint)
    item = ImprovementProjection(
        improvement_id=improvement_id,
        fingerprint=fingerprint,
        fingerprint_version="1",
        kind=candidate.kind,
        delivery=candidate.delivery,
        source_refs=source_refs or candidate.source_refs,
        target=candidate.target,
        rationale=candidate.rationale,
        proposed_change=candidate.proposed_change,
        knowledge_delta=to_persisted_data_knowledge_proposal(candidate.knowledge_delta),
        verification=candidate.verification,
        risk=candidate.risk,
        confidence=candidate.confidence,
        state=ImprovementState.PROPOSED,
        version=version,
        proposed_by_retro_ids=retro_ids,
        supersedes=None,
        last_event_id="IMPEVT-PRIOR",
    )
    return ImprovementLedgerProjection(
        schema_version="1",
        last_seq=1,
        improvements={improvement_id: item},
        by_fingerprint={fingerprint: improvement_id},
    )


@pytest.fixture
def context() -> RetroContext:
    return _context()


@pytest.fixture
def candidate() -> ImprovementCandidate:
    return _candidate()


def test_existing_fingerprint_adds_evidence_only(
    context: RetroContext, candidate: ImprovementCandidate
) -> None:
    current = _projection_with(candidate)
    # New source refs from a later Retro → evidence link only.
    linked = candidate.model_copy(
        update={
            "source_refs": ImprovementSourceRefs(problem_ids=("PROB-2",)),
            "rationale": "Different prose must not change fingerprint",
        }
    )
    document = _document(context, linked)
    plan = reconcile_improvement_candidates(document, context, current)
    assert [event.type for event in plan.events] == ["improvement_evidence_linked"]
    assert plan.events[0].improvement_id == current.by_fingerprint[improvement_fingerprint(linked)]


def test_same_retry_is_byte_identical(context: RetroContext, candidate: ImprovementCandidate) -> None:
    document = _document(context, candidate)
    current = _empty_projection()
    first = reconcile_improvement_candidates(document, context, current)
    second = reconcile_improvement_candidates(document, context, current)
    assert first.model_dump_json() == second.model_dump_json()
    assert isinstance(first, ImprovementReconciliationPlan)


def test_new_fingerprint_proposes_improvement(context: RetroContext, candidate: ImprovementCandidate) -> None:
    document = _document(context, candidate)
    plan = reconcile_improvement_candidates(document, context, _empty_projection())
    assert [event.type for event in plan.events] == ["improvement_proposed"]
    event = plan.events[0]
    assert isinstance(event, ImprovementProposedEvent)
    assert event.improvement_id == improvement_id_for_fingerprint(improvement_fingerprint(candidate))
    assert event.expected_improvement_version == 0
    assert event.fingerprint == improvement_fingerprint(candidate)


def test_duplicate_refs_cause_no_event(context: RetroContext, candidate: ImprovementCandidate) -> None:
    current = _projection_with(candidate)
    document = _document(context, candidate)
    plan = reconcile_improvement_candidates(document, context, current)
    assert plan.events == ()
    assert plan.improvement_ids == tuple(current.improvements)


def test_explicit_supersedes_emits_proposed_and_superseded(
    context: RetroContext, candidate: ImprovementCandidate
) -> None:
    old = _candidate(
        candidate_id="IMP-CAND-OLD",
        proposed_change="Old intent that will be superseded",
        source_refs=ImprovementSourceRefs(problem_ids=("PROB-2",)),
    )
    old_projection = _projection_with(old)
    old_id = next(iter(old_projection.improvements))
    newer = candidate.model_copy(
        update={
            "proposed_change": "Replacement intent after rework",
            "supersedes": old_id,
        }
    )
    document = _document(context, newer)
    plan = reconcile_improvement_candidates(document, context, old_projection)
    assert [event.type for event in plan.events] == [
        "improvement_proposed",
        "improvement_superseded",
    ]
    proposed = plan.events[0]
    superseded = plan.events[1]
    assert isinstance(proposed, ImprovementProposedEvent)
    assert isinstance(superseded, ImprovementSupersededEvent)
    assert proposed.supersedes == old_id
    assert superseded.improvement_id == old_id
    assert superseded.superseded_by == proposed.improvement_id


def test_invalid_batch_raises_and_produces_no_plan_events(
    context: RetroContext, candidate: ImprovementCandidate
) -> None:
    bad = candidate.model_copy(
        update={
            "source_refs": ImprovementSourceRefs(problem_ids=("PROB-MISSING",)),
        }
    )
    document = ImprovementCandidateDocument(
        retro_id=context.retro_id,
        context_sha256=context_sha256(context),
        candidates=(bad,),
    )
    with pytest.raises(CandidateBatchInvalid):
        reconcile_improvement_candidates(document, context, _empty_projection())


def test_same_fingerprint_across_two_retro_ids_links_evidence(
    candidate: ImprovementCandidate,
) -> None:
    first_context = _context(retro_id="retro-a")
    second_context = _context(retro_id="retro-b")
    first_doc = _document(first_context, candidate)
    first_plan = reconcile_improvement_candidates(first_doc, first_context, _empty_projection())
    assert [event.type for event in first_plan.events] == ["improvement_proposed"]

    # Simulate projection after first Retro wrote the proposal.
    after_first = _projection_with(candidate, retro_ids=("retro-a",))
    second_candidate = candidate.model_copy(
        update={
            "candidate_id": "IMP-CAND-2",
            "source_refs": ImprovementSourceRefs(problem_ids=("PROB-2",)),
        }
    )
    second_doc = _document(second_context, second_candidate)
    second_plan = reconcile_improvement_candidates(second_doc, second_context, after_first)
    assert [event.type for event in second_plan.events] == ["improvement_evidence_linked"]
    assert second_plan.events[0].improvement_id == first_plan.events[0].improvement_id
    assert second_plan.idempotency_key != first_plan.idempotency_key


def test_semantic_intent_change_produces_new_improvement(
    context: RetroContext, candidate: ImprovementCandidate
) -> None:
    current = _projection_with(candidate)
    changed = candidate.model_copy(
        update={
            "candidate_id": "IMP-CAND-2",
            "proposed_change": "Entirely different semantic intent",
        }
    )
    document = _document(context, changed)
    plan = reconcile_improvement_candidates(document, context, current)
    assert [event.type for event in plan.events] == ["improvement_proposed"]
    event = plan.events[0]
    assert isinstance(event, ImprovementProposedEvent)
    assert event.improvement_id != next(iter(current.improvements))
    assert event.fingerprint == improvement_fingerprint(changed)
    assert event.fingerprint != improvement_fingerprint(candidate)
