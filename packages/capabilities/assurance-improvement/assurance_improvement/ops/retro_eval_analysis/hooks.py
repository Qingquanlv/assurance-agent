"""Seal eval analysis against the locked evidence slice."""

from __future__ import annotations

from agent_runtime_contracts.ops import FinalizeContext, PrepareContext

from assurance_improvement.contracts.agent import RetroAnalysisInputV1, RetroAnalysisResultV3
from assurance_improvement.operations.agent import commit_retro
from assurance_improvement.operations.files import load_analysis_slice


def before(ctx: PrepareContext, business: RetroAnalysisInputV1) -> RetroAnalysisInputV1:
    return load_analysis_slice(ctx, business)


def after(
    ctx: FinalizeContext, business: RetroAnalysisInputV1, result: RetroAnalysisResultV3
) -> RetroAnalysisResultV3:
    return commit_retro(business, result, ctx, expected_domain="eval")
