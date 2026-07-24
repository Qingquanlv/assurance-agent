from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from assurance_agent.identifiers import UnsafeIdentifierError, assert_path_segment_safe
from assurance_agent.retro.types import (
    FINDING_TO_APPLY,
    DataKnowledgeProposal,
    IssueDraftPayload,
    MemoryBodyPayload,
    RetroContext,
    RetroProposal,
    memory_body_text,
)


def read_proposals(retro_dir: Path) -> list[RetroProposal]:
    path = retro_dir / "proposals.json"
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    entries = raw.get("proposals", raw) if isinstance(raw, dict) else raw
    proposals: list[RetroProposal] = []
    if isinstance(entries, list):
        for entry in entries:
            try:
                proposals.append(RetroProposal.model_validate(entry))
            except ValidationError:
                continue
    return proposals


def _context_evidence_ids(context: RetroContext) -> set[str]:
    """All citable evidence ids the aggregator emitted into this context."""
    signals = context.signals
    ids: set[str] = set()
    for signal in signals.failure_distribution:
        ids.update(signal.evidence_ids)
    for signal in signals.gate_pushback:
        ids.update(signal.evidence_ids)
    ids.update(signals.healing_efficiency.evidence_ids)
    for signal in signals.reclassifications:
        ids.update(signal.evidence_ids)
    for decision in signals.human_decisions:
        if decision.evidence_id:
            ids.add(decision.evidence_id)
    for signal in signals.skill_execution:
        ids.update(signal.evidence_ids)
    return ids


def validate_retro_proposals(context: RetroContext, proposals: list[RetroProposal]) -> list[str]:
    errors: list[str] = []
    seen: set[str] = set()
    valid_evidence = _context_evidence_ids(context)
    for proposal in proposals:
        if proposal.id in seen:
            errors.append(f"duplicate proposal id: {proposal.id}")
        seen.add(proposal.id)
        try:
            assert_path_segment_safe(proposal.id, label="proposal id")
        except UnsafeIdentifierError as err:
            errors.append(str(err))
        expected_apply = FINDING_TO_APPLY[proposal.finding_kind]
        if proposal.apply_kind != expected_apply:
            errors.append(
                f"proposal {proposal.id}: finding_kind={proposal.finding_kind!r} "
                f"requires apply_kind={expected_apply!r}"
            )
        if proposal.apply_kind == "memory_append" and not memory_body_text(proposal):
            errors.append(f"memory_append proposal {proposal.id} has empty body")
        if proposal.apply_kind == "issue_export" and not isinstance(proposal.payload, IssueDraftPayload):
            errors.append(f"issue_export proposal {proposal.id} missing IssueDraftPayload")
        if proposal.apply_kind == "knowledge_delta":
            if not isinstance(proposal.payload, DataKnowledgeProposal):
                errors.append(f"knowledge_delta proposal {proposal.id} missing DataKnowledgeProposal")
            elif proposal.payload.mode != "delta":
                errors.append(f"knowledge_delta proposal {proposal.id} must have mode: delta")
        if not proposal.evidence_ids:
            errors.append(f"proposal {proposal.id} cites no evidence_ids")
            continue
        unknown = [eid for eid in proposal.evidence_ids if eid not in valid_evidence]
        if unknown:
            errors.append(
                f"proposal {proposal.id} cites evidence_ids absent from context: {sorted(unknown)}"
            )
    return errors
