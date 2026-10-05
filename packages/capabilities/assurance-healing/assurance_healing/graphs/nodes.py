"""Approval decision carried by the repair-failure gate."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import model_validator

from assurance_intake.contracts.workflow import EvidenceArtifactRefV1
from graph_engine.plugin_api import FrozenModel


class ProposalApprovalDecision(FrozenModel):
    action: Literal["approve", "reject"]
    approval_ref: EvidenceArtifactRefV1 | None = None

    @model_validator(mode="after")
    def _approved_requires_reference(self) -> Self:
        if self.action == "approve" and self.approval_ref is None:
            raise ValueError("approval requires an authenticated approval reference")
        if self.action == "reject" and self.approval_ref is not None:
            raise ValueError("rejection cannot carry an approval reference")
        return self


__all__ = ["ProposalApprovalDecision"]
