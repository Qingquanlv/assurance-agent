from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

import pytest

from assurance_agent.artifacts.canonical import canonical_json_bytes
from assurance_agent.artifacts.models import (
    CoverageThreshold,
    PerformanceScenarioVerdict,
    QualityGateResult,
)
from assurance_agent.artifacts.models.policy import PlanCheckAction
from assurance_agent.evidence.sufficiency import (
    EvidenceCoverageErrorCode,
    EvidenceCoverageEvaluation,
    RowVerdict,
    SufficiencyReport,
    TraceIntegrity,
)
from assurance_agent.workflow.execution.results import (
    CaseResult,
    CoverageResult,
    PerformanceResult,
    ResultSource,
    TargetResult,
)
from assurance_agent.workflow.report.quality_gate import build_quality_gate, worst_status


@pytest.mark.parametrize(
    "statuses,expected",
    [
        ([], "SKIPPED"),
        (["SKIPPED", "SKIPPED"], "SKIPPED"),
        (["PASS", "SKIPPED"], "PASS"),
        (["PASS", "PASS_WITH_WARNINGS"], "PASS_WITH_WARNINGS"),
        (["PASS_WITH_WARNINGS", "FAIL"], "FAIL"),
        (["FAIL", "PASS"], "FAIL"),
    ],
)
def test_worst_status_matrix(statuses, expected) -> None:
    assert worst_status(statuses) == expected


def make_target(target: str, total: int, passed: int, failed: int, unmapped: int = 0) -> TargetResult:
    cases = [
        CaseResult(
            case_id=f"TC_{target.upper()}_{i:03d}",
            status="passed",
            file="f.py",
            test_name="t",
            duration_ms=1,
            message="",
        )
        for i in range(passed)
    ]
    unmapped_cases = [
        CaseResult(case_id="", status="passed", file="f.py", test_name="t", duration_ms=1, message="")
        for _ in range(unmapped)
    ]
    return TargetResult(
        change_id="CH-1",
        batch_id="b1",
        target=target,  # type: ignore[arg-type]
        status="failed" if failed else ("passed" if total else "skipped"),
        command="cmd",
        source=ResultSource(framework="pytest", raw_log="raw/x.log"),
        total=total,
        passed=passed,
        failed=failed,
        skipped=0,
        cases=cases,
        unmapped_tests=unmapped_cases,
    )


def make_coverage(status: str, available: bool = True, line: float = 85.0) -> CoverageResult:
    return CoverageResult(
        change_id="CH-1",
        batch_id="b1",
        available=available,
        line_coverage=line,
        branch_coverage=70.0,
        threshold=CoverageThreshold(line=70, branch=60),
        status=status,  # type: ignore[arg-type]
    )


def test_all_pass_gate_is_pass() -> None:
    gate = build_quality_gate(
        change_id="CH-1",
        batch_id="b1",
        api=make_target("api", 5, 5, 0),
        e2e=make_target("e2e", 2, 2, 0),
        coverage=make_coverage("PASS"),
        coverage_gate_mode="warn",
    )
    assert gate.dimensions.functional.status == "PASS"
    assert gate.final_status == "PASS"
    assert gate.warnings is None


def test_any_functional_fail_gate_is_fail() -> None:
    gate = build_quality_gate(
        change_id="CH-1",
        batch_id="b1",
        api=make_target("api", 5, 4, 1),
        e2e=make_target("e2e", 2, 2, 0),
        coverage=make_coverage("PASS"),
        coverage_gate_mode="warn",
    )
    assert gate.final_status == "FAIL"


def test_nothing_ran_gate_is_skipped() -> None:
    gate = build_quality_gate(
        change_id="CH-1",
        batch_id="b1",
        api=None,
        e2e=None,
        coverage=make_coverage("SKIPPED", available=False),
        coverage_gate_mode="warn",
    )
    assert gate.final_status == "SKIPPED"


def test_coverage_below_threshold_warn_is_pass_with_warnings() -> None:
    gate = build_quality_gate(
        change_id="CH-1",
        batch_id="b1",
        api=make_target("api", 3, 3, 0),
        e2e=None,
        coverage=make_coverage("PASS_WITH_WARNINGS", line=50.0),
        coverage_gate_mode="warn",
    )
    assert gate.dimensions.coverage.status == "PASS_WITH_WARNINGS"
    assert gate.final_status == "PASS_WITH_WARNINGS"


def test_coverage_below_threshold_block_is_fail() -> None:
    gate = build_quality_gate(
        change_id="CH-1",
        batch_id="b1",
        api=make_target("api", 3, 3, 0),
        e2e=None,
        coverage=make_coverage("PASS_WITH_WARNINGS", line=50.0),
        coverage_gate_mode="block",
    )
    assert gate.dimensions.coverage.status == "FAIL"
    assert gate.final_status == "FAIL"


def test_unmapped_tests_downgrade_functional_and_add_warning() -> None:
    gate = build_quality_gate(
        change_id="CH-1",
        batch_id="b1",
        api=make_target("api", 3, 3, 0, unmapped=2),
        e2e=None,
        coverage=make_coverage("PASS"),
        coverage_gate_mode="warn",
    )
    assert gate.dimensions.functional.status == "PASS_WITH_WARNINGS"
    assert gate.dimensions.functional.unmapped_tests == 2
    assert gate.final_status == "PASS_WITH_WARNINGS"
    assert gate.warnings is not None
    assert any("TRACEABILITY-BROKEN" in w for w in gate.warnings)


def test_fuzz_folds_into_functional() -> None:
    gate = build_quality_gate(
        change_id="CH-1",
        batch_id="b1",
        api=make_target("api", 2, 2, 0),
        e2e=None,
        coverage=make_coverage("PASS"),
        coverage_gate_mode="warn",
        fuzz=make_target("fuzz", 3, 2, 1),
    )
    assert gate.dimensions.functional.status == "FAIL"
    assert gate.dimensions.functional.fuzz is not None
    assert gate.dimensions.functional.fuzz.failed == 1
    assert gate.final_status == "FAIL"


def test_performance_fail_forms_non_functional_and_fails_gate() -> None:
    perf = PerformanceResult(
        change_id="CH-1",
        batch_id="b1",
        available=True,
        status="FAIL",
        scenarios=[
            PerformanceScenarioVerdict(
                capability="c",
                endpoint="/e",
                measured_p95_ms=900.0,
                threshold_p95_ms=500.0,
                measured_error_rate=0.0,
                threshold_error_rate_max=0.01,
                verdict="FAIL",
            )
        ],
    )
    gate = build_quality_gate(
        change_id="CH-1",
        batch_id="b1",
        api=make_target("api", 2, 2, 0),
        e2e=None,
        coverage=make_coverage("PASS"),
        coverage_gate_mode="warn",
        performance=perf,
    )
    assert gate.dimensions.non_functional is not None
    assert gate.dimensions.non_functional.status == "FAIL"
    assert gate.final_status == "FAIL"


# --------------------------------------------------------------------------- #
# Task 9 (revised): evidence sufficiency is reported here and routed elsewhere
#
# The accepted M1 architecture separates the two questions this gate used to be
# asked to answer at once. `final_status` states what the *execution* found —
# tests that ran and failed, coverage below its threshold — and case evidence
# sufficiency is adjudicated by a dedicated `trace-sufficiency-gate` that Task 11
# materializes. So `evidence_coverage` enters here as an informational
# attachment on `dimensions.coverage.evidence` and nothing else: not the coverage
# status, not `final_status`, not `warnings`.
#
# That makes the interesting assertion a *negative* one, and negative assertions
# rot quietly. These tests therefore build each gate twice from identical inputs
# — once with the evaluation and once without — and compare the two documents
# byte for byte with the attachment removed. Any new read of `evidence_coverage`
# anywhere in the builder shows up as a byte difference, whichever field it
# reaches.
# --------------------------------------------------------------------------- #

AS_OF = datetime(2026, 8, 5, 1, 2, 3, 456789, tzinfo=UTC)


def make_report(
    *, sufficient: bool = True, integrity: TraceIntegrity = "complete", rows: bool = True
) -> SufficiencyReport:
    verdict = (
        RowVerdict(
            case_id="TC_API_001",
            sufficient=True,
            missing_kinds=(),
            reason_codes=(),
            execution_state="fresh",
        )
        if sufficient
        else RowVerdict(
            case_id="TC_API_001",
            sufficient=False,
            missing_kinds=("covered", "execution_recent"),
            reason_codes=("not_covered", "never_run"),
            execution_state="never_run",
        )
    )
    return SufficiencyReport(
        change_id="CH-1",
        as_of=AS_OF,
        recency_hours=72,
        integrity=integrity,
        rows=(verdict,) if rows else (),
    )


def _evaluated(action: PlanCheckAction, **report_kwargs: Any) -> EvidenceCoverageEvaluation:
    return EvidenceCoverageEvaluation.evaluated(report=make_report(**report_kwargs), action=action)


# Every state an evaluation can reach: all three actions on a sufficient and on
# an insufficient report, an incomplete-integrity projection, and both failures.
EVALUATIONS: dict[str, EvidenceCoverageEvaluation] = {
    **{f"sufficient-{action}": _evaluated(action) for action in ("warn", "block", "require_human")},
    **{
        f"insufficient-{action}": _evaluated(action, sufficient=False)
        for action in ("warn", "block", "require_human")
    },
    "integrity-incomplete": _evaluated("require_human", integrity="incomplete", rows=False),
    "integrity-complete-with-gaps": _evaluated("block", integrity="complete_with_gaps", sufficient=False),
    "error-policy": EvidenceCoverageEvaluation.failed("policy_error"),
    "error-projection-missing": EvidenceCoverageEvaluation.failed("evidence_projection_missing"),
}

# The legacy line/branch behaviour, unchanged by this task and pinned here so
# "identical with and without evidence" cannot be satisfied by both sides being
# equally wrong.
COVERAGE_SCENARIOS: dict[str, tuple[CoverageResult, Literal["warn", "block"], str]] = {
    "above-threshold": (make_coverage("PASS"), "warn", "PASS"),
    "below-threshold-warn": (make_coverage("PASS_WITH_WARNINGS", line=50.0), "warn", "PASS_WITH_WARNINGS"),
    "below-threshold-block": (make_coverage("PASS_WITH_WARNINGS", line=50.0), "block", "FAIL"),
    "unavailable": (make_coverage("SKIPPED", available=False), "warn", "SKIPPED"),
}


def _perf(verdict: str) -> PerformanceResult:
    return PerformanceResult(
        change_id="CH-1",
        batch_id="b1",
        available=True,
        status="FAIL" if verdict == "FAIL" else "PASS",
        scenarios=[
            PerformanceScenarioVerdict(
                capability="c",
                endpoint="/e",
                measured_p95_ms=900.0 if verdict == "FAIL" else 100.0,
                threshold_p95_ms=500.0,
                measured_error_rate=0.0,
                threshold_error_rate_max=0.01,
                verdict=verdict,  # type: ignore[arg-type]
            )
        ],
    )


@dataclass(frozen=True)
class GateShape:
    """The rest of the gate's inputs, apart from coverage.

    The byte comparison is only as strong as the variety of documents it runs
    over: a gate whose functional dimension failed and one carrying a
    non-functional dimension populate different fields and different
    `worst_status` inputs, and `unmapped` reaches the warnings list — the other
    place evidence must not appear.
    """

    api_failed: int = 0
    unmapped: int = 0
    fuzz: TargetResult | None = None
    performance: PerformanceResult | None = None


GATE_SHAPES: dict[str, GateShape] = {
    "all-passing": GateShape(),
    "functional-fail": GateShape(api_failed=1),
    "functional-fail-and-fuzz": GateShape(api_failed=1, fuzz=make_target("fuzz", 3, 2, 1)),
    "unmapped-warning": GateShape(unmapped=2),
    "non-functional-pass": GateShape(performance=_perf("PASS")),
    "non-functional-fail": GateShape(performance=_perf("FAIL")),
    "non-functional-fail-and-functional-fail": GateShape(api_failed=1, performance=_perf("FAIL")),
}


def _gate(
    scenario: str,
    *,
    evidence_coverage: EvidenceCoverageEvaluation | None = None,
    shape: str = "all-passing",
    unmapped: int = 0,
) -> QualityGateResult:
    coverage, gate_mode, _ = COVERAGE_SCENARIOS[scenario]
    options = GATE_SHAPES[shape]
    return build_quality_gate(
        change_id="CH-1",
        batch_id="b1",
        api=make_target(
            "api",
            3,
            3 - options.api_failed,
            options.api_failed,
            unmapped=unmapped or options.unmapped,
        ),
        e2e=make_target("e2e", 2, 2, 0),
        coverage=coverage,
        coverage_gate_mode=gate_mode,
        fuzz=options.fuzz,
        performance=options.performance,
        evidence_coverage=evidence_coverage,
    )


def _without_evidence(gate: QualityGateResult) -> bytes:
    """The whole gate document except the one field evidence may reach."""
    document: dict[str, Any] = gate.model_dump(mode="json")
    coverage = document["dimensions"]["coverage"]
    assert "evidence" in coverage, "the attachment point must exist even when nothing is attached"
    coverage.pop("evidence")
    return canonical_json_bytes(document)


@pytest.mark.parametrize("scenario", sorted(COVERAGE_SCENARIOS))
@pytest.mark.parametrize("name", sorted(EVALUATIONS))
def test_evidence_changes_no_byte_of_the_verdict(name: str, scenario: str) -> None:
    with_evidence = _gate(scenario, evidence_coverage=EVALUATIONS[name])
    without = _gate(scenario)

    assert _without_evidence(with_evidence) == _without_evidence(without)
    assert with_evidence.dimensions.coverage.evidence is not None, "the premise: evidence was attached"


@pytest.mark.parametrize("shape", sorted(GATE_SHAPES))
@pytest.mark.parametrize("name", sorted(EVALUATIONS))
def test_evidence_changes_no_byte_of_a_failing_or_non_functional_verdict(name: str, shape: str) -> None:
    """The same isolation over the documents that are *not* all-passing.

    A gate whose functional dimension failed, one carrying a non-functional
    dimension, and one already warning about unmapped tests each populate fields
    and `worst_status` inputs that the all-passing shape never reaches — so an
    evidence read there would have gone unseen by the coverage-only matrix.
    """
    with_evidence = _gate("above-threshold", evidence_coverage=EVALUATIONS[name], shape=shape)
    without = _gate("above-threshold", shape=shape)

    assert _without_evidence(with_evidence) == _without_evidence(without)
    assert with_evidence.dimensions.coverage.evidence is not None, "the premise: evidence was attached"


@pytest.mark.parametrize(
    ("shape", "expected"),
    [
        ("all-passing", "PASS"),
        ("functional-fail", "FAIL"),
        ("functional-fail-and-fuzz", "FAIL"),
        ("unmapped-warning", "PASS_WITH_WARNINGS"),
        ("non-functional-pass", "PASS"),
        ("non-functional-fail", "FAIL"),
        ("non-functional-fail-and-functional-fail", "FAIL"),
    ],
)
@pytest.mark.parametrize("name", sorted(EVALUATIONS))
def test_the_final_status_of_each_shape_is_what_the_execution_found(
    name: str, shape: str, expected: str
) -> None:
    """Spelled out rather than derived from a second call, so the byte comparison
    above cannot pass by both sides being equally wrong. Notably
    `non-functional-fail` is a `FAIL` no evidence state can soften, and
    `all-passing` is a `PASS` no evidence state can tighten."""
    gate = _gate("above-threshold", evidence_coverage=EVALUATIONS[name], shape=shape)

    assert gate.final_status == expected


@pytest.mark.parametrize("name", sorted(EVALUATIONS))
def test_the_non_functional_dimension_is_untouched_by_evidence(name: str) -> None:
    """`evidence` lives on the coverage dimension; a sibling dimension must not
    acquire one, and its own verdict must not move."""
    gate = _gate("above-threshold", evidence_coverage=EVALUATIONS[name], shape="non-functional-fail")

    assert gate.dimensions.non_functional is not None
    assert gate.dimensions.non_functional.status == "FAIL"
    assert "evidence" not in gate.dimensions.non_functional.model_dump(mode="json")
    assert "evidence" not in gate.dimensions.functional.model_dump(mode="json")


@pytest.mark.parametrize("scenario", sorted(COVERAGE_SCENARIOS))
@pytest.mark.parametrize("name", sorted(EVALUATIONS))
def test_the_legacy_coverage_status_is_the_only_source_of_the_coverage_verdict(
    name: str, scenario: str
) -> None:
    """Named separately from the byte comparison because this is the field the
    superseded plan would have rewritten; the expectation is spelled out rather
    than derived from a second call."""
    _, _, expected = COVERAGE_SCENARIOS[scenario]

    gate = _gate(scenario, evidence_coverage=EVALUATIONS[name])

    assert gate.dimensions.coverage.status == expected


@pytest.mark.parametrize("action", ["block", "require_human"])
def test_a_blocking_insufficiency_does_not_tighten_a_passing_gate(action: PlanCheckAction) -> None:
    """The single most load-bearing case of this task's revision.

    Under the superseded Task 9 this input was a `FAIL`: uncovered, never-run
    evidence with `on_insufficient: block`. It is a `PASS` here because the
    execution genuinely passed, and thin case evidence is the
    `trace-sufficiency-gate`'s verdict to issue — separately, and with its own
    disposition vocabulary.
    """
    gate = _gate("above-threshold", evidence_coverage=_evaluated(action, sufficient=False))

    assert gate.final_status == "PASS"
    assert gate.dimensions.coverage.status == "PASS"
    assert gate.warnings is None


@pytest.mark.parametrize("error_code", ["policy_error", "evidence_projection_missing"])
def test_an_unevaluable_evaluation_does_not_fail_the_gate_closed(
    error_code: EvidenceCoverageErrorCode,
) -> None:
    """Fail-closed belongs to whatever *routes* on this evaluation, not to a
    dimension that never consulted it. A gate that failed here would report an
    execution outcome that did not happen."""
    gate = _gate("above-threshold", evidence_coverage=EvidenceCoverageEvaluation.failed(error_code))

    assert gate.final_status == "PASS"
    assert gate.dimensions.coverage.status == "PASS"
    assert gate.warnings is None


def test_evidence_does_not_displace_a_warning_the_gate_raised_itself() -> None:
    gate = _gate("above-threshold", evidence_coverage=_evaluated("block", sufficient=False), unmapped=2)

    assert gate.warnings is not None
    assert [w for w in gate.warnings if "TRACEABILITY-BROKEN" in w]
    assert not [w for w in gate.warnings if "sufficien" in w.lower()]


@pytest.mark.parametrize("name", sorted(EVALUATIONS))
def test_the_attachment_is_the_evaluations_own_dump(name: str) -> None:
    """One serializer, shared with the runner's diagnostics channel, so the two
    copies of an evaluation in one document cannot disagree."""
    evaluation = EVALUATIONS[name]

    gate = _gate("above-threshold", evidence_coverage=evaluation)

    assert gate.dimensions.coverage.evidence == evaluation.to_json_dict()


def test_the_coverage_dimension_reports_no_evidence_when_none_is_supplied() -> None:
    """The parameter is optional: `inspect` rebuilds and compat fallbacks have no
    evaluation to hand, and must not be made to invent one."""
    gate = _gate("above-threshold")

    assert gate.dimensions.coverage.evidence is None


@pytest.mark.parametrize("name", sorted(EVALUATIONS))
def test_the_attachment_survives_the_artifact_round_trip(name: str) -> None:
    gate = _gate("above-threshold", evidence_coverage=EVALUATIONS[name])

    reloaded = QualityGateResult.model_validate_json(gate.model_dump_json())

    assert reloaded.dimensions.coverage.evidence == gate.dimensions.coverage.evidence
    assert reloaded.final_status == gate.final_status


def test_a_gate_document_written_before_this_task_still_loads() -> None:
    """`evidence` is additive: batches published by earlier runs, and the eval
    fixtures locked against them, carry no such key."""
    reloaded = QualityGateResult.model_validate_json(
        canonical_json_bytes(
            {
                "schema_version": "1.0",
                "change_id": "CH-1",
                "batch_id": "b1",
                "dimensions": {
                    "functional": {
                        "status": "PASS",
                        "api": {"total": 1, "passed": 1, "failed": 0},
                        "e2e": {"total": 0, "passed": 0, "failed": 0},
                    },
                    "coverage": {
                        "status": "SKIPPED",
                        "available": False,
                        "line_coverage": 0.0,
                        "branch_coverage": 0.0,
                        "threshold": {"line": 70, "branch": 60},
                    },
                },
                "final_status": "PASS",
            }
        ).decode("utf-8")
    )

    assert reloaded.dimensions.coverage.evidence is None


# --------------------------------------------------------------------------- #
# informational metrics dimension — never enters worst_status / final_status
# --------------------------------------------------------------------------- #


def test_informational_metrics_dimension_does_not_move_final_status() -> None:
    """Task 8: metrics may sit on QualityGateDimensions for report/retro, but
    ``worst_status`` / ``final_status`` must not read them."""
    from assurance_agent.artifacts.models.inspect import MetricsDimension

    bare = build_quality_gate(
        change_id="CH-1",
        batch_id="b1",
        api=make_target("api", 5, 5, 0),
        e2e=None,
        coverage=make_coverage("PASS"),
        coverage_gate_mode="warn",
    )
    with_metrics = bare.model_copy(
        update={
            "dimensions": bare.dimensions.model_copy(
                update={
                    "metrics": MetricsDimension(
                        status="FAIL",
                        available=True,
                        summary="collection gaps present",
                    )
                }
            )
        }
    )

    assert with_metrics.dimensions.metrics is not None
    assert with_metrics.dimensions.metrics.status == "FAIL"
    assert with_metrics.final_status == bare.final_status == "PASS"
    assert worst_status([bare.final_status, with_metrics.dimensions.metrics.status]) == "FAIL"
    # The builder itself never folds metrics into final_status.
    rebuilt = build_quality_gate(
        change_id="CH-1",
        batch_id="b1",
        api=make_target("api", 5, 5, 0),
        e2e=None,
        coverage=make_coverage("PASS"),
        coverage_gate_mode="warn",
    )
    assert rebuilt.dimensions.metrics is None
    assert rebuilt.final_status == "PASS"
