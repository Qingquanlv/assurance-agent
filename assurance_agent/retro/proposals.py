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


def validate_retro_proposals(context: RetroContext, proposals: list[RetroProposal]) -> list[str]:
    errors: list[str] = []
    seen: set[str] = set()
    for proposal in proposals:
        if proposal.id in seen:
            errors.append(f"duplicate proposal id: {proposal.id}")
        seen.add(proposal.id)
        if proposal.apply_kind == "memory_append" and not proposal.body.strip():
            errors.append(f"memory_append proposal {proposal.id} has empty body")
    return errors
