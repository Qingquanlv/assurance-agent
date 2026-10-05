"""Seal retro synthesis against the locked context."""

from __future__ import annotations

from agent_runtime_contracts.ops import FinalizeContext, PrepareContext

from assurance_improvement.contracts.agent import RetroAnalysisResultV3, RetroSynthesisInputV1
from assurance_improvement.contracts.handoff import CANDIDATES
from assurance_improvement.contracts.retro import RetroCandidatesFile
from assurance_improvement.operations.agent import commit_retro
from assurance_improvement.operations.files import load_retro_context


def before(ctx: PrepareContext, business: RetroSynthesisInputV1) -> RetroSynthesisInputV1:
    return load_retro_context(ctx, business)


def after(
    ctx: FinalizeContext, business: RetroSynthesisInputV1, result: RetroAnalysisResultV3
) -> RetroAnalysisResultV3:
    sealed = commit_retro(business, result, ctx, expected_domain=None)
    ctx.stage(CANDIDATES, RetroCandidatesFile(candidates=sealed.candidates))
    return sealed
