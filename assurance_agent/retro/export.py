"""Materialize retro issue/knowledge proposals into on-disk drafts (spec C6)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml

from assurance_agent.exceptions import AaError
from assurance_agent.identifiers import assert_path_segment_safe
from assurance_agent.retro.promotions import (
    append_promotion_events,
    proposal_exported_event,
    proposal_states,
    read_promotion_events,
)
from assurance_agent.retro.proposals import read_proposals
from assurance_agent.retro.types import (
    DataKnowledgeProposal,
    IssueDraftPayload,
    RetroProposal,
)

EXPORT_ELIGIBLE_STATES = frozenset({"proposed", "needs_rework", "rolled_back"})
EXPORT_TERMINAL_STATES = frozenset({"applied", "rejected"})
ExportKind = Literal["issue_export", "knowledge_delta"]


@dataclass(frozen=True)
class ExportOutcome:
    proposal_id: str
    target_path: Path
    action: Literal["noop", "exported", "skipped"]
    source_sha256: str


class ExportConflictError(AaError):
    pass


class ExportIneligibleError(AaError):
    pass


def proposal_export_sha256(proposal: RetroProposal) -> str:
    """Canonical sha256 of export-relevant proposal content (excludes export metadata)."""
    if proposal.apply_kind == "issue_export":
        if not isinstance(proposal.payload, IssueDraftPayload):
            raise AaError(f"proposal {proposal.id} missing IssueDraftPayload")
        canonical = {
            "id": proposal.id,
            "finding_kind": proposal.finding_kind,
            **proposal.payload.model_dump(mode="json"),
        }
    elif proposal.apply_kind == "knowledge_delta":
        if not isinstance(proposal.payload, DataKnowledgeProposal):
            raise AaError(f"proposal {proposal.id} missing DataKnowledgeProposal")
        canonical = proposal.payload.model_dump(mode="json")
    else:
        raise AaError(f"proposal {proposal.id} apply_kind={proposal.apply_kind!r} is not exportable")
    payload = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _read_existing_source_sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    if isinstance(data, dict):
        value = data.get("source_sha256")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _issue_draft_document(
    proposal: RetroProposal,
    *,
    source_sha256: str,
    exported_at: str,
) -> dict:
    payload = proposal.payload
    if not isinstance(payload, IssueDraftPayload):
        raise AaError(f"proposal {proposal.id} missing IssueDraftPayload")
    return {
        "source_proposal_id": proposal.id,
        "source_sha256": source_sha256,
        "exported_at": exported_at,
        "id": proposal.id,
        "finding_kind": proposal.finding_kind,
        "title": payload.title,
        "target": payload.target,
        "severity": payload.severity,
        "evidence_ids": list(payload.evidence_ids or proposal.evidence_ids),
        "proposed_change": payload.proposed_change,
    }


def _knowledge_draft_document(
    proposal: RetroProposal,
    *,
    source_sha256: str,
    exported_at: str,
) -> dict:
    payload = proposal.payload
    if not isinstance(payload, DataKnowledgeProposal):
        raise AaError(f"proposal {proposal.id} missing DataKnowledgeProposal")
    document = payload.model_dump(mode="json", by_alias=True)
    document["source_proposal_id"] = proposal.id
    document["source_sha256"] = source_sha256
    document["exported_at"] = exported_at
    return document


def _target_path(retro_dir: Path, proposal: RetroProposal) -> Path:
    if proposal.apply_kind == "issue_export":
        return retro_dir / "issue-drafts" / f"{proposal.id}.yaml"
    if proposal.apply_kind == "knowledge_delta":
        return retro_dir / "knowledge-delta" / f"{proposal.id}.proposal.yaml"
    raise AaError(f"proposal {proposal.id} apply_kind={proposal.apply_kind!r} is not exportable")


def export_proposal(
    retro_dir: Path,
    proposal: RetroProposal,
    *,
    state: str,
    overwrite: bool = False,
    actor: str = "aa",
    exported_at: str | None = None,
) -> ExportOutcome:
    """Export one proposal with hash-first idempotency (spec C6 export lifecycle)."""
    assert_path_segment_safe(proposal.id, label="proposal id")
    if proposal.apply_kind not in ("issue_export", "knowledge_delta"):
        raise AaError(
            f"proposal {proposal.id} apply_kind={proposal.apply_kind!r} is not exportable "
            "(expected issue_export or knowledge_delta)"
        )

    source_sha256 = proposal_export_sha256(proposal)
    target = _target_path(retro_dir, proposal)
    existing_hash = _read_existing_source_sha256(target)
    if existing_hash == source_sha256:
        return ExportOutcome(proposal.id, target, "noop", source_sha256)

    if state in EXPORT_TERMINAL_STATES:
        raise ExportIneligibleError(
            f"proposal {proposal.id} is terminal ({state}) and cannot be exported"
        )
    if state not in EXPORT_ELIGIBLE_STATES and state != "exported":
        raise ExportIneligibleError(
            f"proposal {proposal.id} state={state!r} is not eligible for export"
        )
    if existing_hash is not None and existing_hash != source_sha256:
        if not overwrite:
            raise ExportConflictError(
                f"export conflict for {proposal.id}: existing draft hash "
                f"{existing_hash} differs from source {source_sha256}"
            )
        if state not in EXPORT_ELIGIBLE_STATES:
            raise ExportIneligibleError(
                f"proposal {proposal.id} state={state!r} cannot be overwritten"
            )

    from assurance_agent.retro.promotions import utc_now_iso

    exported_at = exported_at or utc_now_iso()
    if proposal.apply_kind == "issue_export":
        document = _issue_draft_document(proposal, source_sha256=source_sha256, exported_at=exported_at)
    else:
        document = _knowledge_draft_document(proposal, source_sha256=source_sha256, exported_at=exported_at)

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(yaml.safe_dump(document, sort_keys=False, allow_unicode=True), encoding="utf-8")
    append_promotion_events(
        retro_dir,
        [
            proposal_exported_event(
                proposal.id,
                actor=actor,
                at=exported_at,
                target=str(target.relative_to(retro_dir)),
                source_sha256=source_sha256,
            )
        ],
    )
    return ExportOutcome(proposal.id, target, "exported", source_sha256)


def export_proposals(
    retro_dir: Path,
    *,
    apply_kind: ExportKind,
    overwrite: bool = False,
    actor: str = "aa",
) -> list[ExportOutcome]:
    proposals = [p for p in read_proposals(retro_dir) if p.apply_kind == apply_kind]
    states = proposal_states(read_promotion_events(retro_dir))
    outcomes: list[ExportOutcome] = []
    for proposal in proposals:
        state = states.get(proposal.id, "proposed")
        outcomes.append(
            export_proposal(
                retro_dir,
                proposal,
                state=state,
                overwrite=overwrite,
                actor=actor,
            )
        )
    return outcomes
