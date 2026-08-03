"""Candidate document read/digest/whole-batch validation (schema v2)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from assurance_agent.artifacts.models.improvements import (
    DeliveryKind,
    ImprovementCandidate,
    ImprovementCandidateDocument,
    ImprovementKind,
    ImprovementSourceRefs,
    ImprovementVerification,
)
from assurance_agent.retro.candidates import (
    CandidateBatchInvalid,
    candidate_batch_digest,
    context_sha256,
    read_candidate_document,
    validate_candidate_document,
)
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


def _context(
    *,
    retro_id: str = "retro-1",
    evidence_ids: tuple[str, ...] = ("PROB-1", "OCC-1", "PEVT-1"),
    integrity: RetroIntegrity | None = None,
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
        integrity=integrity or RetroIntegrity(status="complete"),
        signals=RetroSignalSet(
            issue=IssueRetroSignals(),
            workflow=WorkflowRetroSignals(),
            eval=EvalRetroSignals(),
        ),
        signal_count=0,
    )


def _valid_candidate(**overrides: object) -> ImprovementCandidate:
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


def _knowledge_delta(**overrides: object) -> dict:
    delta: dict = {
        "schema_version": "1",
        "mode": "delta",
        "entities": {"dept": {"required_fields": ["name"]}},
    }
    delta.update(overrides)
    return delta


@pytest.fixture
def context() -> RetroContext:
    return _context()


@pytest.fixture
def valid_candidate() -> ImprovementCandidate:
    return _valid_candidate()


def test_candidate_batch_is_rejected_as_a_unit(
    context: RetroContext, valid_candidate: ImprovementCandidate
) -> None:
    bad = valid_candidate.model_copy(
        update={
            "candidate_id": "IMP-CAND-2",
            "proposed_change": "Distinct intent with unknown source",
            "source_refs": ImprovementSourceRefs(problem_ids=("PROB-MISSING",)),
        }
    )
    document = ImprovementCandidateDocument(
        retro_id=context.retro_id,
        context_sha256="wrong",
        candidates=(valid_candidate, bad),
    )
    with pytest.raises(CandidateBatchInvalid) as error:
        validate_candidate_document(context, document)
    assert {item.code for item in error.value.errors} == {
        "context_digest_mismatch",
        "unknown_source_ref",
    }


def test_allowed_kind_delivery_matrix_passes_batch_validation(context: RetroContext) -> None:
    cases = [
        (ImprovementKind.PROMPT, DeliveryKind.MEMORY_PATCH, None),
        (ImprovementKind.FIXTURE, DeliveryKind.MEMORY_PATCH, None),
        (ImprovementKind.FIXTURE, DeliveryKind.CHANGE_DRAFT, None),
        (ImprovementKind.TEST, DeliveryKind.MEMORY_PATCH, None),
        (ImprovementKind.TEST, DeliveryKind.CHANGE_DRAFT, None),
        (ImprovementKind.WORKFLOW, DeliveryKind.CHANGE_DRAFT, None),
        (ImprovementKind.DOMAIN_KNOWLEDGE, DeliveryKind.KNOWLEDGE_DELTA, _knowledge_delta()),
    ]
    candidates = []
    for index, (kind, delivery, knowledge_delta) in enumerate(cases):
        overrides: dict = {
            "candidate_id": f"IMP-CAND-{index}",
            "kind": kind,
            "delivery": delivery,
            "proposed_change": f"intent-{index}",
            "target": (
                f".aa/memory/candidate-{index}.md"
                if delivery is DeliveryKind.MEMORY_PATCH
                else f"target-{index}"
            ),
        }
        if knowledge_delta is not None:
            overrides["knowledge_delta"] = knowledge_delta
        candidates.append(_valid_candidate(**overrides))
    document = ImprovementCandidateDocument(
        retro_id=context.retro_id,
        context_sha256=context_sha256(context),
        candidates=tuple(candidates),
    )
    validate_candidate_document(context, document)


def test_read_rejects_unsafe_memory_patch_target_in_schema_v3(tmp_path: Path) -> None:
    retro_dir = tmp_path / "qa" / "retro" / "retro-1"
    retro_dir.mkdir(parents=True)
    candidate = _valid_candidate(
        kind=ImprovementKind.PROMPT,
        delivery=DeliveryKind.MEMORY_PATCH,
        target=".aa/memory/aa-api-plan.md",
    ).model_dump(mode="json")
    candidate["target"] = "skills/awe-api-plan:required-field-summary-probe"
    candidate["signal_ids"] = ["issue-pattern:x"]
    raw = {
        "schema_version": "3",
        "retro_id": "retro-1",
        "context_sha256": "sha256:context",
        "candidates": [candidate],
    }
    (retro_dir / "proposal-candidates.json").write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(CandidateBatchInvalid) as error:
        read_candidate_document(retro_dir, expected_schema="3")
    assert {item.code for item in error.value.errors} == {"invalid_candidate"}


def test_duplicate_candidate_ids_reject_whole_batch(
    context: RetroContext, valid_candidate: ImprovementCandidate
) -> None:
    twin = valid_candidate.model_copy(
        update={
            "proposed_change": "Different intent but same candidate_id",
        }
    )
    document = ImprovementCandidateDocument(
        retro_id=context.retro_id,
        context_sha256=context_sha256(context),
        candidates=(valid_candidate, twin),
    )
    with pytest.raises(CandidateBatchInvalid) as error:
        validate_candidate_document(context, document)
    assert {item.code for item in error.value.errors} == {"duplicate_candidate_id"}


def test_duplicate_fingerprints_reject_whole_batch(
    context: RetroContext, valid_candidate: ImprovementCandidate
) -> None:
    twin = valid_candidate.model_copy(update={"candidate_id": "IMP-CAND-2", "rationale": "Other prose"})
    document = ImprovementCandidateDocument(
        retro_id=context.retro_id,
        context_sha256=context_sha256(context),
        candidates=(valid_candidate, twin),
    )
    with pytest.raises(CandidateBatchInvalid) as error:
        validate_candidate_document(context, document)
    assert {item.code for item in error.value.errors} == {"duplicate_fingerprint"}


def test_forbidden_problem_fields_reject_on_read(tmp_path: Path, context: RetroContext) -> None:
    retro_dir = tmp_path / "qa" / "retro" / context.retro_id
    retro_dir.mkdir(parents=True)
    raw = {
        "schema_version": "2",
        "retro_id": context.retro_id,
        "context_sha256": context_sha256(context),
        "candidates": [
            {
                "candidate_id": "IMP-CAND-1",
                "kind": "workflow_improvement",
                "delivery": "change_draft",
                "source_refs": {"problem_ids": ["PROB-1"]},
                "target": "assurance_agent/workflow/inspect",
                "rationale": "Repeated truncation",
                "proposed_change": "Preserve pytest E lines",
                "verification": {
                    "suites": ["workflow-full"],
                    "success_criteria": "No truncation",
                },
                "risk": "low",
                "confidence": "high",
                "severity": "high",
                "status": "in_progress",
                "root_cause": "copied Problem assessment",
            }
        ],
    }
    (retro_dir / "proposal-candidates.json").write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(CandidateBatchInvalid) as error:
        read_candidate_document(retro_dir)
    assert {item.code for item in error.value.errors} == {"forbidden_problem_field"}


def test_incomplete_context_blocks_only_domain_knowledge(context: RetroContext) -> None:
    incomplete = _context(integrity=RetroIntegrity(status="incomplete", reasons=("analysis_failed",)))
    assert incomplete.allows_domain_knowledge is False
    knowledge = _valid_candidate(
        candidate_id="IMP-CAND-K",
        kind=ImprovementKind.DOMAIN_KNOWLEDGE,
        delivery=DeliveryKind.KNOWLEDGE_DELTA,
        knowledge_delta=_knowledge_delta(),
    )
    process = _valid_candidate(candidate_id="IMP-CAND-P")
    with pytest.raises(CandidateBatchInvalid) as error:
        validate_candidate_document(
            incomplete,
            ImprovementCandidateDocument(
                retro_id=incomplete.retro_id,
                context_sha256=context_sha256(incomplete),
                candidates=(knowledge,),
            ),
        )
    assert {item.code for item in error.value.errors} == {"domain_knowledge_blocked"}
    validate_candidate_document(
        incomplete,
        ImprovementCandidateDocument(
            retro_id=incomplete.retro_id,
            context_sha256=context_sha256(incomplete),
            candidates=(process,),
        ),
    )


def test_knowledge_l2_validation_rejects_invalid_delta(context: RetroContext) -> None:
    candidate = _valid_candidate(
        kind=ImprovementKind.DOMAIN_KNOWLEDGE,
        delivery=DeliveryKind.KNOWLEDGE_DELTA,
        knowledge_delta=_knowledge_delta(mode="bootstrap"),
    )
    document = ImprovementCandidateDocument(
        retro_id=context.retro_id,
        context_sha256=context_sha256(context),
        candidates=(candidate,),
    )
    with pytest.raises(CandidateBatchInvalid) as error:
        validate_candidate_document(context, document)
    assert {item.code for item in error.value.errors} == {"invalid_knowledge_delta"}


def test_knowledge_l2_requires_at_least_one_problem_ref(context: RetroContext) -> None:
    candidate = _valid_candidate(
        kind=ImprovementKind.DOMAIN_KNOWLEDGE,
        delivery=DeliveryKind.KNOWLEDGE_DELTA,
        source_refs=ImprovementSourceRefs(issue_event_ids=("PEVT-1",)),
        knowledge_delta=_knowledge_delta(),
    )
    document = ImprovementCandidateDocument(
        retro_id=context.retro_id,
        context_sha256=context_sha256(context),
        candidates=(candidate,),
    )
    with pytest.raises(CandidateBatchInvalid) as error:
        validate_candidate_document(context, document)
    assert {item.code for item in error.value.errors} == {"invalid_knowledge_delta"}


def test_read_and_digest_round_trip(
    tmp_path: Path, context: RetroContext, valid_candidate: ImprovementCandidate
) -> None:
    retro_dir = tmp_path / "qa" / "retro" / context.retro_id
    retro_dir.mkdir(parents=True)
    document = ImprovementCandidateDocument(
        retro_id=context.retro_id,
        context_sha256=context_sha256(context),
        candidates=(valid_candidate,),
    )
    path = retro_dir / "proposal-candidates.json"
    path.write_text(
        json.dumps(document.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    loaded = read_candidate_document(retro_dir)
    assert loaded == document
    digest = candidate_batch_digest(loaded)
    assert len(digest) == len("sha256:") + 64
    assert digest.startswith("sha256:")
    validate_candidate_document(context, loaded)


def test_retro_id_mismatch_rejects_batch(
    context: RetroContext, valid_candidate: ImprovementCandidate
) -> None:
    document = ImprovementCandidateDocument(
        retro_id="retro-other",
        context_sha256=context_sha256(context),
        candidates=(valid_candidate,),
    )
    with pytest.raises(CandidateBatchInvalid) as error:
        validate_candidate_document(context, document)
    assert {item.code for item in error.value.errors} == {"retro_id_mismatch"}


def test_model_rejects_incompatible_delivery_before_batch() -> None:
    with pytest.raises(ValidationError):
        _valid_candidate(kind=ImprovementKind.WORKFLOW, delivery=DeliveryKind.MEMORY_PATCH)
