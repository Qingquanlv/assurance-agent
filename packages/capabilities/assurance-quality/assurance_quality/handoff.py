"""Ledger handles shared by issue analysis and the flow that mounts it.

Quality graphs cannot import op hooks, and op packages cannot import one another.
"""

from __future__ import annotations

from agent_runtime_contracts.ops import ArtifactHandle

from assurance_quality.contracts.assessment import AssessmentInputsV1, InspectionOutcomeV1

INSPECTION: ArtifactHandle[InspectionOutcomeV1] = ArtifactHandle(
    ledger_key="quality.inspection",
    slot="inspection_ref",
    model=InspectionOutcomeV1,
    optional=True,
)
ASSESSMENT: ArtifactHandle[AssessmentInputsV1] = ArtifactHandle(
    ledger_key="quality.assessment",
    slot="assessment_ref",
    model=AssessmentInputsV1,
    optional=True,
)

__all__ = ["ASSESSMENT", "INSPECTION"]
