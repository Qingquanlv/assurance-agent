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
    min_evidence: int = 1,
    rework_alert: int = 3,
) -> ImprovementAcceptStatus:
    """Validate Candidates and reconcile them into the Improvement Ledger.

    Shared by nightly and graph ops. ``min_evidence`` / ``rework_alert`` are
    retained for half-cutover call sites and ignored: review thresholds move to
    independent Improvement review (later tasks).
    """
    del min_evidence, rework_alert
    return run_improvement_reconcile(sut, retro_id=retro_id)
