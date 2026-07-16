"""Apply retro proposals into a staging directory (memory overlay)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from assurance_agent.exceptions import AaError
from assurance_agent.identifiers import assert_path_segment_safe
from assurance_agent.retro.proposals import read_proposals
from assurance_agent.retro.types import RetroProposal


def apply_proposal_to_stage(
    *,
    sut_root: Path,
    retro_id: str,
    proposal_id: str,
    stage_dir: Path,
) -> RetroProposal:
    """Materialize a ``memory_append`` proposal into ``stage_dir`` as a memory overlay."""
    assert_path_segment_safe(retro_id, label="retro id")
    retro_dir = sut_root / "qa" / "retro" / retro_id
    proposals = read_proposals(retro_dir)
    proposal = next((p for p in proposals if p.id == proposal_id), None)
    if proposal is None:
        raise AaError(f"proposal not found: {proposal_id}")
    if proposal.apply_kind != "memory_append":
        raise AaError(
            f"proposal {proposal_id} apply_kind={proposal.apply_kind!r} cannot be staged "
            "(only memory_append is supported)"
        )

    if stage_dir.exists():
        shutil.rmtree(stage_dir)
    memory_dir = stage_dir / ".aa" / "memory"
    memory_dir.mkdir(parents=True)
    body = proposal.proposed_change or proposal.body or proposal.problem
    (memory_dir / f"{proposal.id}.md").write_text(body + "\n", encoding="utf-8")
    (stage_dir / "proposal-meta.json").write_text(
        json.dumps(proposal.model_dump(mode="json"), indent=2), encoding="utf-8"
    )
    return proposal
