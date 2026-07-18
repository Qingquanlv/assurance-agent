from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from assurance_agent.retro.types import RetroContext, RetroProposal


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
    """All citable evidence ids the aggregator emitted into this context.

    The aa-retro skill contract requires every proposal to cite evidence_ids
    that already exist in context.json; this is the authoritative set that
    `validate_retro_proposals` checks against.
    """
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
        if proposal.apply_kind == "memory_append" and not proposal.body.strip():
            errors.append(f"memory_append proposal {proposal.id} has empty body")
        if not proposal.evidence_ids:
            errors.append(f"proposal {proposal.id} cites no evidence_ids")
            continue
        unknown = [eid for eid in proposal.evidence_ids if eid not in valid_evidence]
        if unknown:
            errors.append(f"proposal {proposal.id} cites evidence_ids absent from context: {sorted(unknown)}")
    return errors
