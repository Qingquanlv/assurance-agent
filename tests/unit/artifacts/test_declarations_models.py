"""Declaration proposal models — Lane C intake feedstock (dual-source Task 2)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from assurance_agent.artifacts.models.declarations import (
    DECLARATION_PROPOSAL_GLOB,
    DeclarationCaseDraft,
    DeclarationEvidenceKind,
    DeclarationEvidenceRef,
    DeclarationProposal,
    DeclarationProposalStatus,
)
from assurance_agent.artifacts.registry import match_artifact


def _evidence(**overrides: object) -> DeclarationEvidenceRef:
    payload: dict[str, object] = {
        "kind": "coverage_gap",
        "locator": "kind=uncovered_required_case;case_id=TC_API_001",
        "digest": "sha256:deadbeef",
    }
    payload.update(overrides)
    return DeclarationEvidenceRef.model_validate(payload)


def _case_draft(**overrides: object) -> DeclarationCaseDraft:
    payload: dict[str, object] = {
        "case_id": "TC_API_DECL_001",
        "title": "Declare obligation for uncovered gap",
        "layer": "api",
        "status": "draft",
    }
    payload.update(overrides)
    return DeclarationCaseDraft.model_validate(payload)


def _proposal(**overrides: object) -> DeclarationProposal:
    payload: dict[str, object] = {
        "schema_version": "1",
        "improvement_id": "IMP-DECL-001",
        "change_id": "CH-DECL-001",
        "status": "draft",
        "case_drafts": (_case_draft(),),
        "evidence_refs": (_evidence(),),
        "source_improvement_id": "IMP-SRC-001",
        "fingerprint": "a" * 64,
    }
    payload.update(overrides)
    return DeclarationProposal.model_validate(payload)


def test_unknown_evidence_kind_is_fail_closed() -> None:
    with pytest.raises(ValidationError):
        _evidence(kind="invented_kind")


def test_closed_evidence_kind_vocabulary() -> None:
    kinds = set(DeclarationEvidenceKind.__args__)  # type: ignore[attr-defined]
    assert kinds == {
        "counterexample",
        "escape",
        "mutation_survivor",
        "coverage_gap",
    }


def test_closed_proposal_status_vocabulary() -> None:
    statuses = set(DeclarationProposalStatus.__args__)  # type: ignore[attr-defined]
    assert statuses == {"draft", "accepted", "rejected"}


def test_case_draft_status_is_draft_literal_only() -> None:
    with pytest.raises(ValidationError):
        _case_draft(status="active")


def test_evidence_digest_must_be_non_empty() -> None:
    with pytest.raises(ValidationError):
        _evidence(digest="")


def test_proposal_requires_at_least_one_case_draft() -> None:
    with pytest.raises(ValidationError):
        _proposal(case_drafts=())


def test_proposal_requires_at_least_one_evidence_ref() -> None:
    with pytest.raises(ValidationError):
        _proposal(evidence_refs=())


def test_change_id_and_source_improvement_optional() -> None:
    proposal = _proposal(change_id=None, source_improvement_id=None)
    assert proposal.change_id is None
    assert proposal.source_improvement_id is None


def test_registry_matches_declaration_proposal_must_compat() -> None:
    rel = "qa/improvements/declarations/IMP-DECL-001.proposal.yaml"
    spec = match_artifact(rel)
    assert spec is not None
    assert spec.artifact_type == "declaration_proposal"
    assert spec.compat == "must_compat"
    assert spec.model is DeclarationProposal
    assert DECLARATION_PROPOSAL_GLOB == "qa/improvements/declarations/*.proposal.yaml"
