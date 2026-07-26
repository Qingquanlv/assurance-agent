"""Retro accept stage: reconcile Candidates into the Project Improvement Ledger."""

from __future__ import annotations

from pathlib import Path

from assurance_agent.workflow.improvements.reconciler import (
    ImprovementAcceptStatus,
    run_improvement_reconcile,
)


def run_retro_accept(
    sut: Path,
    *,
    retro_id: str,
) -> ImprovementAcceptStatus:
    """Validate Candidates and reconcile them into the Improvement Ledger.

    Shared by the Retro graph ``operation:reconcile-improvements`` handler.
    Review thresholds live on the independent Improvement review entrypoint.
    """
    return run_improvement_reconcile(sut, retro_id=retro_id)
