"""Seal retro synthesis against the locked context."""

from __future__ import annotations

from agent_runtime_contracts.ops import FinalizeContext

from assurance_improvement.contracts.agent import RetroAnalysisResultV3, RetroSynthesisInputV1
from assurance_improvement.operations.agent import commit_retro


def after(
    ctx: FinalizeContext, business: RetroSynthesisInputV1, result: RetroAnalysisResultV3
) -> RetroAnalysisResultV3:
    return commit_retro(business, result, ctx, expected_domain=None)
