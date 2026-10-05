"""Check the recommended action against the digests locked beside the business input."""

from __future__ import annotations

from agent_runtime_contracts.ops import FinalizeContext, InputError, OutputError

from assurance_quality.contracts.agent import IssueTriageResultV1, QualitySkillInputV1
from assurance_quality.contracts.decisions import IssueTriagePublishedV1, triage_route
from assurance_quality.operations.agent_skills import TRIAGE_ACTIONS


def after(
    ctx: FinalizeContext, business: QualitySkillInputV1, result: IssueTriageResultV1
) -> IssueTriagePublishedV1:
    del business
    if result.recommended_action not in TRIAGE_ACTIONS:
        raise OutputError(f"issue triage recommended_action is not declared: {result.recommended_action}")
    locked = ctx.prepared.get("locked_evidence_digests")
    if not isinstance(locked, dict) or not locked:
        raise InputError("locked_evidence_digests must authenticate triage evidence")
    if dict(result.evidence_digests) != dict(locked):
        raise OutputError("issue triage evidence_digests must equal locked_evidence_digests")
    return IssueTriagePublishedV1(
        route=triage_route("unknown", False),
        classification="unknown",
        fix_eligible=False,
        advice=result,
    )
