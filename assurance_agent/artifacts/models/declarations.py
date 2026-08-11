"""qa/improvements/declarations/*.proposal.yaml — Lane C declaration proposals.

Declaration-layer feedstock for intake (design §8). Peer to proposal.md as
requirement evidence; new cases enter case-review-cycle as ``draft`` only.
"""

from __future__ import annotations

from typing import Literal, Self, get_args

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from assurance_agent.artifacts.models.common import CaseId, NonEmptyStr

_FROZEN = ConfigDict(frozen=True, extra="forbid")

DECLARATION_PROPOSAL_GLOB = "qa/improvements/declarations/*.proposal.yaml"
DECLARATION_PROPOSAL_DIR_REL = "qa/improvements/declarations"

DeclarationProposalSchemaVersion = Literal["1"]
DeclarationProposalStatus = Literal["draft", "accepted", "rejected"]
DeclarationCaseDraftStatus = Literal["draft"]
DeclarationCaseLayer = Literal["api", "e2e", "fuzz", "performance"]
DeclarationEvidenceKind = Literal[
    "counterexample",
    "escape",
    "mutation_survivor",
    "coverage_gap",
]

DECLARATION_EVIDENCE_KIND_ORDER: tuple[DeclarationEvidenceKind, ...] = get_args(DeclarationEvidenceKind)


class DeclarationEvidenceRef(BaseModel):
    """Typed evidence pointer — digests + locators, never copied CE/escape bodies."""

    model_config = _FROZEN

    kind: DeclarationEvidenceKind
    locator: NonEmptyStr
    digest: NonEmptyStr
    path: NonEmptyStr | None = None

    @field_validator("digest")
    @classmethod
    def _digest_non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("evidence digest must be non-empty")
        return value


class DeclarationCaseDraft(BaseModel):
    """Case.yaml-shaped draft entry; status is the draft literal only."""

    model_config = _FROZEN

    case_id: CaseId
    title: NonEmptyStr
    layer: DeclarationCaseLayer
    status: DeclarationCaseDraftStatus = "draft"


class DeclarationProposal(BaseModel):
    """Authoritative Lane C proposal at ``qa/improvements/declarations/<id>.proposal.yaml``."""

    model_config = _FROZEN

    schema_version: DeclarationProposalSchemaVersion
    improvement_id: NonEmptyStr
    change_id: NonEmptyStr | None = None
    status: DeclarationProposalStatus
    case_drafts: tuple[DeclarationCaseDraft, ...] = Field(min_length=1)
    evidence_refs: tuple[DeclarationEvidenceRef, ...] = Field(min_length=1)
    source_improvement_id: NonEmptyStr | None = None
    fingerprint: NonEmptyStr

    @model_validator(mode="after")
    def _case_drafts_are_draft(self) -> Self:
        for entry in self.case_drafts:
            if entry.status != "draft":
                raise ValueError("declaration proposal case_drafts must all have status=draft")
        return self


class DeclarationProposalReceipt(BaseModel):
    """Thin write ack — digest + status for later M4 / delivery wiring."""

    model_config = _FROZEN

    improvement_id: NonEmptyStr
    path: NonEmptyStr
    digest: NonEmptyStr
    status: DeclarationProposalStatus


__all__ = [
    "DECLARATION_EVIDENCE_KIND_ORDER",
    "DECLARATION_PROPOSAL_DIR_REL",
    "DECLARATION_PROPOSAL_GLOB",
    "DeclarationCaseDraft",
    "DeclarationCaseDraftStatus",
    "DeclarationCaseLayer",
    "DeclarationEvidenceKind",
    "DeclarationEvidenceRef",
    "DeclarationProposal",
    "DeclarationProposalReceipt",
    "DeclarationProposalSchemaVersion",
    "DeclarationProposalStatus",
]
