"""Thin RegressionCandidate builder from confirmed Counterexamples (Phase 1).

Not a full Retro pipeline — fixture helper + deterministic CE→candidate seam.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from assurance_agent.artifacts.models.discovery import Counterexample
from assurance_agent.artifacts.models.promotion import RegressionCandidate


class CandidateBuildError(Exception):
    """Fail-closed candidate construction error."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def build_regression_candidate(
    ce: Counterexample,
    *,
    change_id: str,
    candidate_id: str,
    proposed_targets: Sequence[str],
    source_files: Mapping[str, str],
    purpose: str,
    problem_id: str | None = None,
    evidence_refs: Sequence[str] = (),
) -> RegressionCandidate:
    """Build a ``RegressionCandidate`` from a confirmed CE + file digests."""
    if ce.finding_status != "confirmed":
        raise CandidateBuildError(
            "not_confirmed",
            f"RegressionCandidate requires confirmed CE, got {ce.finding_status!r}",
        )
    if ce.oracle_kind != "hard_oracle":
        raise CandidateBuildError(
            "oracle_not_hard",
            "RegressionCandidate requires hard_oracle counterexample",
        )
    if not source_files:
        raise CandidateBuildError("source_files_empty", "source_files must be non-empty")
    if not proposed_targets:
        raise CandidateBuildError("targets_empty", "proposed_targets must be non-empty")

    refs = tuple(evidence_refs) or tuple(
        f"discovery/counterexamples/{ce.counterexample_id}/replay/attempt-{i}.json"
        for i in range(ce.replay.attempts)
    )

    return RegressionCandidate(
        schema_version="1",
        candidate_id=candidate_id,
        change_id=change_id,
        campaign_id=ce.campaign_id,
        counterexample_id=ce.counterexample_id,
        problem_id=problem_id,
        oracle_id=ce.oracle_id,
        surface=ce.surface,
        proposed_targets=tuple(proposed_targets),
        source_files=dict(source_files),
        minimization_status=ce.minimization.status,
        purpose=purpose,
        evidence_refs=refs,
    )


__all__ = ["CandidateBuildError", "build_regression_candidate"]
