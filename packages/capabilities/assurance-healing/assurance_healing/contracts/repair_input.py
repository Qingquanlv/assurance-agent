"""Parent fields plus the refs prepare opens into the repair models."""

from __future__ import annotations

from pydantic import Field

from graph_engine.attempts.models.resolutions import ReceiptRef
from graph_engine.plugin_api import FrozenModel

from assurance_intake.contracts import PolicyResourceV1
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1


class RepairBoundInputV1(FrozenModel):
    """Parent fields plus the refs prepare opens into the repair models."""

    change_id: str = Field(min_length=1)
    plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_ref: EvidenceArtifactRefV1
    capability_leafs: tuple[str, ...]
    issue_analysis_handoff_ref: EvidenceArtifactRefV1 | None = None
    coverage_epoch: int = Field(ge=0)
    repair_round: int = Field(ge=1)
    product_policy: PolicyResourceV1
    generation_ref: EvidenceArtifactRefV1
    execution_ref: EvidenceArtifactRefV1
    execution_receipt: ReceiptRef


class ApplyBoundInputV1(RepairBoundInputV1):
    proposal_ref: EvidenceArtifactRefV1


__all__ = ["ApplyBoundInputV1", "RepairBoundInputV1"]
