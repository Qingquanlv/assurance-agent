"""Emit evidence for required_capabilities keys missing from L1 knowledge."""

from __future__ import annotations

from assurance_agent.artifacts.models.plan_checks import CheckEvidence, Finding
from assurance_agent.knowledge.capabilities import compute_missing_capabilities
from assurance_agent.verification.checks.base import CheckContext, evidence

CHECK_ID = "capability_keys"


def check_capability_keys(ctx: CheckContext) -> CheckEvidence:
    review = {"required_capabilities": list(ctx.required_capabilities)}
    missing = compute_missing_capabilities(review, dict(ctx.data_knowledge))
    findings = [
        Finding(
            locator=f"required_capabilities[{key}]",
            actual=key,
            expected="a fully qualified C4 leaf present in .aa/data-knowledge.yaml",
        )
        for key in missing
    ]
    refs: list[str] = [".aa/data-knowledge.yaml"]
    if ctx.required_capabilities and ctx.review_artifact:
        refs.append(ctx.review_artifact)
    return evidence(CHECK_ID, findings, refs)
