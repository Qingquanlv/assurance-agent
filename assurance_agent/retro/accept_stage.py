from __future__ import annotations

import json
from pathlib import Path

from assurance_agent.exceptions import AaError
from assurance_agent.retro.nightly.phase_d import build_review_queue_markdown, partition_proposals_for_review
from assurance_agent.retro.proposals import accept_proposals, validate_retro_proposals
from assurance_agent.retro.state import complete_retro_stage
from assurance_agent.retro.types import RetroContext, RetroProposal


def run_retro_accept(
    sut: Path,
    *,
    retro_id: str,
    min_evidence: int,
    rework_alert: int = 3,
) -> list[RetroProposal]:
    """Write-boundary gate and review-queue writer; shared by nightly and graph ops.

    Reads ``context.json`` and ``proposals.json`` from the retro directory, validates
    proposals against the context, writes ``review-queue.md``, and marks the retro
    stage complete. Raises ``AaError`` (from ``accept_proposals``) if proposals are
    missing, unroutable, or fail semantic validation — callers should treat this as
    a hard failure. Returns the accepted proposal list.
    """
    retro_dir = sut / "qa" / "retro" / retro_id
    context = RetroContext.model_validate(json.loads((retro_dir / "context.json").read_text(encoding="utf-8")))
    proposals = accept_proposals(retro_dir)
    errors = validate_retro_proposals(context, proposals)
    if errors:
        raise AaError(
            f"{retro_dir / 'proposals.json'} failed semantic validation: {'; '.join(errors)}"
        )
    partition = partition_proposals_for_review(
        proposals, promotions=[], min_evidence=min_evidence, rework_alert=rework_alert
    )
    (retro_dir / "review-queue.md").write_text(
        build_review_queue_markdown(retro_id, partition), encoding="utf-8"
    )
    complete_retro_stage(sut, retro_id)
    return proposals
