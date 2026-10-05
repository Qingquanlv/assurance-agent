"""Epoch-scoped handoff from issue analysis to a later repair."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from graph_engine.plugin_api import FrozenModel

from assurance_intake.contracts.workflow import EvidenceArtifactRefV1

ISSUE_ANALYSIS_HANDOFF_PATH = "qa/results/healing/issue-analysis-handoff.json"


class IssueAnalysisHandoffV1(FrozenModel):
    """The analysis ref is current only while ``coverage_epoch`` matches the repair."""

    schema_version: Literal["1"] = "1"
    coverage_epoch: int = Field(ge=0)
    issue_analysis_ref: EvidenceArtifactRefV1


__all__ = ["ISSUE_ANALYSIS_HANDOFF_PATH", "IssueAnalysisHandoffV1"]
