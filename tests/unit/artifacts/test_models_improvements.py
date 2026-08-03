import pytest
from pydantic import ValidationError

from assurance_agent.artifacts.models.improvements import (
    ALLOWED_DELIVERIES,
    DeliveryKind,
    ImprovementCandidate,
    ImprovementCandidateDocument,
    ImprovementKind,
    ImprovementLedgerProjection,
    ImprovementProjection,
    ImprovementReviewQueue,
    ImprovementSourceRefs,
    ImprovementState,
)


def _valid_candidate(**overrides: object) -> dict:
    doc: dict = {
        "candidate_id": "IMP-CAND-1",
        "kind": "workflow_improvement",
        "delivery": "change_draft",
        "source_refs": {"problem_ids": ["PROB-1"]},
        "target": "assurance_agent/workflow/inspect",
        "rationale": "Repeated truncation",
        "proposed_change": "Preserve pytest E lines",
        "verification": {"suites": ["workflow-full"], "success_criteria": "No truncation"},
        "risk": "low",
        "confidence": "high",
    }
    doc.update(overrides)
    return doc


def test_candidate_rejects_wrong_delivery_and_problem_fields() -> None:
    common = {
        "candidate_id": "IMP-CAND-1",
        "source_refs": {"problem_ids": ["PROB-1"]},
        "target": "assurance_agent/workflow/inspect",
        "rationale": "Repeated truncation",
        "proposed_change": "Preserve pytest E lines",
        "verification": {"suites": ["workflow-full"], "success_criteria": "No truncation"},
        "risk": "low",
        "confidence": "high",
    }
    with pytest.raises(ValidationError):
        ImprovementCandidate.model_validate(
            {**common, "kind": "workflow_improvement", "delivery": "memory_patch"}
        )
    with pytest.raises(ValidationError):
        ImprovementCandidate.model_validate(
            {
                **common,
                "kind": "workflow_improvement",
                "delivery": "change_draft",
                "severity": "high",
                "status": "in_progress",
                "root_cause": "copied Problem assessment",
            }
        )


@pytest.mark.parametrize(
    ("kind", "delivery", "index"),
    [
        (ImprovementKind.PROMPT, DeliveryKind.MEMORY_PATCH, 0),
        (ImprovementKind.FIXTURE, DeliveryKind.MEMORY_PATCH, 1),
        (ImprovementKind.FIXTURE, DeliveryKind.CHANGE_DRAFT, 2),
        (ImprovementKind.TEST, DeliveryKind.MEMORY_PATCH, 3),
        (ImprovementKind.TEST, DeliveryKind.CHANGE_DRAFT, 4),
        (ImprovementKind.WORKFLOW, DeliveryKind.CHANGE_DRAFT, 5),
        (ImprovementKind.DOMAIN_KNOWLEDGE, DeliveryKind.KNOWLEDGE_DELTA, 6),
    ],
)
def test_allowed_kind_delivery_matrix(kind: ImprovementKind, delivery: DeliveryKind, index: int) -> None:
    assert delivery in ALLOWED_DELIVERIES[kind]
    overrides: dict = {"kind": kind.value, "delivery": delivery.value}
    if delivery is DeliveryKind.MEMORY_PATCH:
        overrides["target"] = f".aa/memory/candidate-{index}.md"
    if delivery is DeliveryKind.KNOWLEDGE_DELTA:
        overrides["knowledge_delta"] = {
            "schema_version": "1",
            "mode": "delta",
            "entities": {"dept": {"required_fields": ["name"]}},
        }
    candidate = ImprovementCandidate.model_validate(_valid_candidate(**overrides))
    assert candidate.kind is kind
    assert candidate.delivery is delivery


def test_candidate_allows_memory_patch_child_target() -> None:
    candidate = ImprovementCandidate.model_validate(
        _valid_candidate(
            kind="prompt_improvement",
            delivery="memory_patch",
            target=".aa/memory/aa-api-plan.md",
        )
    )
    assert candidate.target == ".aa/memory/aa-api-plan.md"


@pytest.mark.parametrize(
    "target",
    [
        "skills/awe-api-plan:required-field-summary-probe",
        ".aa/memory",
        "/.aa/memory/aa-api-plan.md",
        ".aa/memory/../escape.md",
        ".aa/memory/./aa-api-plan.md",
        ".aa/memory//aa-api-plan.md",
        r".aa\memory\aa-api-plan.md",
    ],
)
def test_candidate_rejects_unsafe_memory_patch_target(target: str) -> None:
    with pytest.raises(
        ValidationError,
        match="memory_patch target must be a child path under .aa/memory/",
    ):
        ImprovementCandidate.model_validate(
            _valid_candidate(
                kind="prompt_improvement",
                delivery="memory_patch",
                target=target,
            )
        )


def test_candidate_requires_at_least_one_source_ref() -> None:
    with pytest.raises(ValidationError, match="candidate requires at least one source ref"):
        ImprovementCandidate.model_validate(_valid_candidate(source_refs={}))


def test_knowledge_delta_required_only_for_knowledge_delta_delivery() -> None:
    delta = {
        "schema_version": "1",
        "mode": "delta",
        "entities": {"dept": {"required_fields": ["name"]}},
    }
    with pytest.raises(ValidationError, match="knowledge_delta payload is required only"):
        ImprovementCandidate.model_validate(
            _valid_candidate(
                kind="domain_knowledge",
                delivery="knowledge_delta",
            )
        )
    with pytest.raises(ValidationError, match="knowledge_delta payload is required only"):
        ImprovementCandidate.model_validate(
            _valid_candidate(
                kind="workflow_improvement",
                delivery="change_draft",
                knowledge_delta=delta,
            )
        )
    candidate = ImprovementCandidate.model_validate(
        _valid_candidate(
            kind="domain_knowledge",
            delivery="knowledge_delta",
            knowledge_delta=delta,
        )
    )
    assert candidate.knowledge_delta is not None


def test_knowledge_delta_rejects_boolean_max_length() -> None:
    domain_knowledge = {
        "schema_version": "1",
        "mode": "delta",
        "entities": {"dept": {"constraints": {"name": {"max_length": True}}}},
    }
    with pytest.raises(ValidationError, match="max_length must be a positive integer"):
        ImprovementCandidate.model_validate(
            _valid_candidate(
                kind="domain_knowledge",
                delivery="knowledge_delta",
                knowledge_delta=domain_knowledge,
            )
        )


def test_knowledge_delta_accepts_positive_max_length() -> None:
    domain_knowledge = {
        "schema_version": "1",
        "mode": "delta",
        "entities": {"dept": {"constraints": {"name": {"max_length": 20}}}},
    }
    candidate = ImprovementCandidate.model_validate(
        _valid_candidate(
            kind="domain_knowledge",
            delivery="knowledge_delta",
            knowledge_delta=domain_knowledge,
        )
    )
    assert candidate.knowledge_delta is not None


def test_source_refs_all_ids_deduplicates_and_sorts() -> None:
    refs = ImprovementSourceRefs(
        problem_ids=("PROB-2", "PROB-1"),
        occurrence_ids=("OCC-1",),
        issue_event_ids=("PROB-2",),
    )
    assert refs.all_ids() == ("OCC-1", "PROB-1", "PROB-2")


def test_improvement_models_are_frozen() -> None:
    candidate = ImprovementCandidate.model_validate(_valid_candidate())
    with pytest.raises(ValidationError):
        candidate.candidate_id = "other"  # type: ignore[misc]


def test_improvement_projection_and_ledger_round_trip() -> None:
    candidate = ImprovementCandidate.model_validate(_valid_candidate())
    projection = ImprovementProjection(
        improvement_id="IMP-1",
        fingerprint="fp-1",
        kind=candidate.kind,
        delivery=candidate.delivery,
        source_refs=candidate.source_refs,
        target=candidate.target,
        rationale=candidate.rationale,
        proposed_change=candidate.proposed_change,
        verification=candidate.verification,
        risk=candidate.risk,
        confidence=candidate.confidence,
        state=ImprovementState.PROPOSED,
        version=1,
        proposed_by_retro_ids=("retro-1",),
        last_event_id="evt-1",
    )
    ledger = ImprovementLedgerProjection(
        last_seq=1,
        improvements={"IMP-1": projection},
        by_fingerprint={"fp-1": "IMP-1"},
    )
    assert ledger.improvements["IMP-1"].state is ImprovementState.PROPOSED


def test_improvement_review_queue_schema() -> None:
    queue = ImprovementReviewQueue(improvement_ids=("IMP-1", "IMP-2"))
    assert queue.schema_version == "1"
    assert queue.improvement_ids == ("IMP-1", "IMP-2")


def test_improvement_candidate_document_schema_v2() -> None:
    doc = ImprovementCandidateDocument(
        retro_id="retro-1",
        context_sha256="sha256:ctx",
        candidates=(ImprovementCandidate.model_validate(_valid_candidate()),),
    )
    assert doc.schema_version == "2"
    assert len(doc.candidates) == 1
