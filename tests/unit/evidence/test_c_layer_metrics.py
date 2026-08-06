"""C1/C2/C3 pure aggregate — four independent vectors, no gate coupling."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from assurance_agent.artifacts.models.coverage_gaps import (
    CoverageGap,
    CoverageGapLocator,
    CoverageGapsDocument,
)
from assurance_agent.artifacts.models.discovery import ReplayAttemptReceipt
from assurance_agent.artifacts.models.issues import Problem
from assurance_agent.artifacts.models.promotion import (
    PromotionReceipt,
    RegressionCandidate,
    WriteSetEntry,
)
from assurance_agent.evidence.c_layer_metrics import aggregate_c_layer_metrics
from tests.unit.artifacts.test_models_issues import make_problem

CHANGE_ID = "CH-CLAYER-001"
COMPUTED_AT = datetime(2026, 8, 5, 15, 0, tzinfo=UTC)


def _problem(**escape_overrides: object) -> Problem:
    return Problem.model_validate(make_problem(escape_analysis=dict(escape_overrides)))


def _candidate(**overrides: Any) -> RegressionCandidate:
    base: dict[str, Any] = dict(
        schema_version="1",
        candidate_id="RC-001",
        change_id=CHANGE_ID,
        campaign_id="CAM-001",
        counterexample_id="CE-001",
        oracle_id="ORACLE-1",
        surface="api",
        proposed_targets=("tests/api/test_ce.py",),
        source_files={"discovery/candidates/RC-001/files/test_ce.py": "sha256:" + ("b" * 64)},
        minimization_status="minimized",
        purpose="stable regression",
    )
    base.update(overrides)
    return RegressionCandidate(**base)


def _receipt(**overrides: Any) -> PromotionReceipt:
    base: dict[str, Any] = dict(
        schema_version="1",
        receipt_id="PROM-RCPT-001",
        improvement_id="IMP-001",
        candidate_id="RC-001",
        applied_at="2026-08-05T06:00:00Z",
        write_set=(
            WriteSetEntry(
                path="tests/api/test_ce.py",
                before_sha256=None,
                after_sha256="sha256:" + ("e" * 64),
            ),
        ),
        write_authorization=("tests/api/test_ce.py",),
        status="applied",
    )
    base.update(overrides)
    return PromotionReceipt(**base)


def _gap_doc(batch_id: str, case_ids: tuple[str, ...]) -> CoverageGapsDocument:
    gaps = tuple(
        CoverageGap(
            kind="uncovered_required_case",
            locator=CoverageGapLocator(case_id=case_id),
            layer="execution",
            batch_id=batch_id,
            evidence_refs=("digest-" + case_id,),
        )
        for case_id in case_ids
    )
    return CoverageGapsDocument(
        schema_version="1",
        change_id=CHANGE_ID,
        batch_id=batch_id,
        projection_digest="sha256:" + ("f" * 64),
        gaps=gaps,
    )


def _replay(*, outcome: str = "violate", attempt_index: int = 0) -> ReplayAttemptReceipt:
    return ReplayAttemptReceipt(
        schema_version="1",
        counterexample_id="CE-001",
        seed=1,
        attempt_index=attempt_index,
        base_revision="deadbeef",
        oracle_set_digest="sha256:" + ("a" * 64),
        outcome=outcome,  # type: ignore[arg-type]
        observed_digest="sha256:" + ("b" * 64),
    )


def _aggregate(**overrides: Any):
    base: dict[str, Any] = dict(
        problems=None,
        promotion_receipts=None,
        candidates=None,
        total_counterexamples=None,
        previous_gaps=None,
        current_gaps=None,
        replay_receipts=None,
        change_id=CHANGE_ID,
        computed_at=COMPUTED_AT,
    )
    base.update(overrides)
    return aggregate_c_layer_metrics(**base)


def test_all_missing_inputs_yield_four_not_evaluated_vectors() -> None:
    doc = _aggregate()
    for key in (
        "escape_rate",
        "counterexample_promotion_rate",
        "coverage_gap_closure_rate",
        "seed_replay_stability",
    ):
        entry = doc.vector(key)
        assert entry.status == "not_evaluated"
        assert entry.rate is None
        assert entry.numerator is None
        assert entry.denominator is None


def test_c1_escape_evaluated_human_confirmed_only() -> None:
    escape = _problem(
        is_escape=True,
        authority="human_confirmed",
        confirmed_at="2026-08-05T12:00:00Z",
        confirmed_by="qa",
    )
    not_escape = Problem.model_validate(
        make_problem(
            problem_id="PROB-other",
            escape_analysis={
                "is_escape": False,
                "authority": "human_confirmed",
                "confirmed_at": "2026-08-05T12:00:00Z",
                "confirmed_by": "qa",
            },
        )
    )
    draft = Problem.model_validate(
        make_problem(
            problem_id="PROB-draft",
            escape_analysis={"is_escape": True, "authority": "llm_provisional"},
        )
    )
    doc = _aggregate(problems=[escape, not_escape, draft])
    assert doc.escape_rate.status == "evaluated"
    assert doc.escape_rate.numerator == 1
    assert doc.escape_rate.denominator == 2
    assert doc.escape_rate.rate == 0.5
    # Others remain not_evaluated in isolation.
    assert doc.counterexample_promotion_rate.status == "not_evaluated"
    assert doc.coverage_gap_closure_rate.status == "not_evaluated"
    assert doc.seed_replay_stability.status == "not_evaluated"


def test_c1_zero_analyzed_is_not_evaluated_not_zero_rate() -> None:
    bare = Problem.model_validate(make_problem())
    doc = _aggregate(problems=[bare])
    assert doc.escape_rate.status == "not_evaluated"
    assert doc.escape_rate.rate is None
    assert doc.escape_rate.numerator == 0
    assert doc.escape_rate.denominator == 0


def test_c2_promotion_evaluated_separately_from_gap_closure() -> None:
    candidate = _candidate()
    receipt = _receipt()
    prev = _gap_doc("batch-1", ("TC_A", "TC_B"))
    current = _gap_doc("batch-2", ("TC_B",))  # TC_A closed
    doc = _aggregate(
        promotion_receipts=[receipt],
        candidates=[candidate],
        total_counterexamples=4,
        previous_gaps=prev,
        current_gaps=current,
    )
    promo = doc.counterexample_promotion_rate
    gaps = doc.coverage_gap_closure_rate
    assert promo.status == "evaluated"
    assert promo.numerator == 1
    assert promo.denominator == 4
    assert promo.rate == 0.25
    assert gaps.status == "evaluated"
    assert gaps.numerator == 1
    assert gaps.denominator == 2
    assert gaps.rate == 0.5
    # Never a single merged C2 ratio field.
    dumped = doc.model_dump(mode="json")
    assert "c2_rate" not in dumped
    assert dumped["counterexample_promotion_rate"]["rate"] != dumped["coverage_gap_closure_rate"]["rate"]


def test_c2_promotion_zero_total_not_evaluated() -> None:
    doc = _aggregate(
        promotion_receipts=[],
        candidates=[],
        total_counterexamples=0,
    )
    assert doc.counterexample_promotion_rate.status == "not_evaluated"
    assert doc.counterexample_promotion_rate.rate is None
    assert doc.counterexample_promotion_rate.denominator == 0


def test_c2_promotion_partial_inputs_missing_stays_not_evaluated() -> None:
    # receipts without total → missing, not 0/0 invention
    doc = _aggregate(promotion_receipts=[_receipt()], candidates=[_candidate()])
    assert doc.counterexample_promotion_rate.status == "not_evaluated"
    assert doc.counterexample_promotion_rate.numerator is None
    assert doc.counterexample_promotion_rate.denominator is None


def test_c2_gap_closure_missing_previous_not_evaluated() -> None:
    doc = _aggregate(current_gaps=_gap_doc("batch-2", ("TC_A",)))
    assert doc.coverage_gap_closure_rate.status == "not_evaluated"
    assert doc.coverage_gap_closure_rate.numerator is None


def test_c2_gap_closure_empty_previous_not_evaluated() -> None:
    prev = _gap_doc("batch-1", ())
    current = _gap_doc("batch-2", ("TC_NEW",))
    doc = _aggregate(previous_gaps=prev, current_gaps=current)
    assert doc.coverage_gap_closure_rate.status == "not_evaluated"
    assert doc.coverage_gap_closure_rate.denominator == 0
    assert doc.coverage_gap_closure_rate.rate is None


def test_c3_seed_replay_evaluated_in_isolation() -> None:
    receipts = (
        _replay(outcome="violate", attempt_index=0),
        _replay(outcome="hold", attempt_index=1),
        _replay(outcome="violate", attempt_index=2),
    )
    doc = _aggregate(replay_receipts=receipts)
    assert doc.seed_replay_stability.status == "evaluated"
    assert doc.seed_replay_stability.numerator == 2
    assert doc.seed_replay_stability.denominator == 3
    assert doc.seed_replay_stability.rate is not None
    assert abs(doc.seed_replay_stability.rate - (2 / 3)) < 1e-9
    assert doc.escape_rate.status == "not_evaluated"


def test_c3_empty_receipts_not_evaluated() -> None:
    doc = _aggregate(replay_receipts=())
    assert doc.seed_replay_stability.status == "not_evaluated"
    assert doc.seed_replay_stability.rate is None
    assert doc.seed_replay_stability.denominator == 0


def test_no_confidence_alias_on_aggregate() -> None:
    doc = _aggregate(
        problems=[
            _problem(
                is_escape=True,
                authority="human_confirmed",
                confirmed_at="2026-08-05T12:00:00Z",
                confirmed_by="qa",
            )
        ],
        replay_receipts=(_replay(),),
    )
    text = doc.model_dump_json()
    assert "confidence" not in text
