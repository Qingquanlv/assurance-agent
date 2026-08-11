"""Fallback candidates from coverage gaps and confirmed escapes — fingerprint stability."""

from __future__ import annotations

from assurance_agent.artifacts.models.improvements import ImprovementKind, ImprovementSourceRefs
from assurance_agent.artifacts.models.retro_v3 import (
    ConfirmedEscapeSignal,
    ReopenedCoverageGapSignal,
)
from assurance_agent.retro.fallback import (
    candidate_fingerprint,
    candidate_from_confirmed_escape,
    candidate_from_coverage_gaps,
    coverage_gap_fingerprint,
    dedupe_candidates_by_fingerprint,
)


def _gap_signal(*, change_id: str = "CH-1", case_id: str = "CASE-1") -> ReopenedCoverageGapSignal:
    return ReopenedCoverageGapSignal(
        signal_id=f"REOPEN-{change_id}-{case_id}",
        summary=f"Coverage gap reopened for {case_id}",
        occurrence_count=1,
        recommended_change="Close the reopened coverage gap with a regression case.",
        source_refs=ImprovementSourceRefs(workflow_evidence_ids=(f"GAP-{case_id}",)),
        confidence="high",
        gap_kind="uncovered_required_case",
        locator_fingerprint=f"uncovered_required_case|{case_id}|||",
        change_id=change_id,
        case_id=case_id,
    )


def _escape_signal(
    *, problem_id: str = "PROB-ESC-1", missed: tuple[str, ...] = ("obl-a",)
) -> ConfirmedEscapeSignal:
    return ConfirmedEscapeSignal(
        signal_id=f"ESCAPE-{problem_id}",
        summary=f"Confirmed escape {problem_id}",
        occurrence_count=1,
        recommended_change="Capture missed obligations in domain knowledge or tests.",
        source_refs=ImprovementSourceRefs(problem_ids=(problem_id,)),
        confidence="high",
        problem_id=problem_id,
        change_id="CH-1",
        missed_obligation_ids=missed,
    )


def test_coverage_gap_candidate_fingerprint_is_kind_plus_locator() -> None:
    first = candidate_from_coverage_gaps(signals=(_gap_signal(change_id="CH-1"),), retro_id="retro-a")
    second = candidate_from_coverage_gaps(signals=(_gap_signal(change_id="CH-2"),), retro_id="retro-b")
    assert first.kind == ImprovementKind.TEST
    assert coverage_gap_fingerprint(_gap_signal()) == "uncovered_required_case|CASE-1|||"
    assert candidate_fingerprint(first) == candidate_fingerprint(second)
    assert candidate_fingerprint(first) == "uncovered_required_case|CASE-1|||"
    # Same fingerprint ⇒ same candidate_id across batches
    assert first.candidate_id == second.candidate_id


def test_dedupe_candidates_across_batches_keeps_one_fingerprint() -> None:
    a = candidate_from_coverage_gaps(signals=(_gap_signal(change_id="CH-1"),), retro_id="retro-a")
    b = candidate_from_coverage_gaps(signals=(_gap_signal(change_id="CH-2"),), retro_id="retro-b")
    other = candidate_from_coverage_gaps(
        signals=(_gap_signal(change_id="CH-1", case_id="CASE-2"),),
        retro_id="retro-a",
    )
    deduped = dedupe_candidates_by_fingerprint((a, b, other))
    fingerprints = sorted(candidate_fingerprint(c) for c in deduped)
    assert fingerprints == [
        "uncovered_required_case|CASE-1|||",
        "uncovered_required_case|CASE-2|||",
    ]


def test_confirmed_escape_with_missed_obligations_yields_domain_knowledge() -> None:
    candidate = candidate_from_confirmed_escape(signal=_escape_signal(), retro_id="retro-1")
    assert candidate.kind == ImprovementKind.DOMAIN_KNOWLEDGE
    assert candidate_fingerprint(candidate) == "PROB-ESC-1"
    assert candidate.knowledge_delta is not None


def test_confirmed_escape_without_missed_obligations_yields_test_improvement() -> None:
    candidate = candidate_from_confirmed_escape(
        signal=_escape_signal(missed=()),
        retro_id="retro-1",
    )
    assert candidate.kind == ImprovementKind.TEST
    assert candidate_fingerprint(candidate) == "PROB-ESC-1"
