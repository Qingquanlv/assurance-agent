from __future__ import annotations

from collections import Counter

from pydantic import BaseModel, Field

from assurance_agent.retro.types import RetroProposal, RetroPromoteRecord


class ReviewPartition(BaseModel):
    for_review: list[RetroProposal] = Field(default_factory=list)
    pr_only: list[RetroProposal] = Field(default_factory=list)
    stuck_tags: list[str] = Field(default_factory=list)


def partition_proposals_for_review(
    proposals: list[RetroProposal],
    promotions: list[RetroPromoteRecord],
    *,
    min_evidence: int,
    rework_alert: int,
) -> ReviewPartition:
    rework_counts: Counter[str] = Counter()
    for record in promotions:
        if record.decision == "needs_rework":
            rework_counts[record.proposal_id] += 1
    stuck = sorted(pid for pid, n in rework_counts.items() if n >= rework_alert)

    for_review: list[RetroProposal] = []
    pr_only: list[RetroProposal] = []
    for proposal in proposals:
        if proposal.apply_kind == "memory_append":
            for_review.append(proposal)
        else:
            pr_only.append(proposal)
    return ReviewPartition(for_review=for_review, pr_only=pr_only, stuck_tags=stuck)


def build_review_queue_markdown(retro_id: str, partition: ReviewPartition) -> str:
    lines = [f"# Review Queue — {retro_id}", "", "## For review (memory_append)", ""]
    for proposal in partition.for_review:
        flag = " ⚠️ stuck" if proposal.id in partition.stuck_tags else ""
        lines.append(f"- `{proposal.id}` (suite: {proposal.eval_suite or 'n/a'}){flag}: {proposal.summary}")
    if partition.pr_only:
        lines += ["", "## PR-only (manual)", ""]
        lines += [f"- `{p.id}`: {p.summary}" for p in partition.pr_only]
    return "\n".join(lines) + "\n"
