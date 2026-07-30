"""Pure sufficiency evaluation (Task 7)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from assurance_agent.artifacts.models.policy import (
    CoverageFloor,
    EvidenceSufficiency,
    FuzzPolicy,
    HealingPolicy,
    Policy,
)
from assurance_agent.artifacts.models.trace import TraceExecution, TraceProjection, TraceRow
from assurance_agent.evidence.sufficiency import evaluate_sufficiency

AS_OF = datetime(2026, 7, 30, 12, 0, 0, tzinfo=UTC)
RECENCY_HOURS = 72


def _execution(
    *,
    batch_id: str = "20260729120000",
    target: str = "api",
    status: str = "passed",
    ts: datetime | None = None,
) -> TraceExecution:
    return TraceExecution(
        batch_id=batch_id,
        target=target,  # type: ignore[arg-type]
        status=status,  # type: ignore[arg-type]
        ts=ts or AS_OF - timedelta(hours=1),
        ts_source="executed_at",
    )


def _row(
    *,
    case_id: str = "TC_API_001",
    case_type: str = "API",
    coverage_state: str = "covered",
    latest_execution: TraceExecution | None = None,
    freshest_pass: TraceExecution | None = None,
    atemporal_kinds_present: tuple[str, ...] = ("covered",),
) -> TraceRow:
    return TraceRow(
        case_id=case_id,
        module="system.dept",
        case_type=case_type,  # type: ignore[arg-type]
        automation_required=True,
        coverage_state=coverage_state,  # type: ignore[arg-type]
        latest_execution=latest_execution,
        freshest_pass=freshest_pass,
        presence_in_current_batch="executed",
        atemporal_kinds_present=atemporal_kinds_present,
    )


def _projection(*rows: TraceRow) -> TraceProjection:
    return TraceProjection(
        change_id="CH-1",
        phase="execution",
        authoritative_batch_id="20260729120000",
        rows=rows,
        integrity="complete",
    )


def _policy(**overrides: object) -> Policy:
    sufficiency = EvidenceSufficiency(
        recency_hours=RECENCY_HOURS,
        required_kinds={
            "API": ["covered", "execution_recent"],
            "E2E": ["covered", "execution_recent"],
            "Fuzz": ["covered", "fuzz_run"],
            "Performance": ["covered", "perf_run"],
        },
        on_insufficient="require_human",
    )
    if overrides:
        sufficiency = sufficiency.model_copy(update=overrides)
    return Policy(
        version=1,
        human_review_risk_levels=["high"],
        force_continue_allowed=True,
        plan_checks={
            "l1_path": "warn",
            "shared_factory": "warn",
            "assert_ideal": "warn",
            "capability_keys": "warn",
        },
        coverage_floor=CoverageFloor(risk_high=0.9, risk_medium=0.7),
        fuzz=FuzzPolicy(required_when_endpoint_has_auth=True),
        healing=HealingPolicy(auth_module="require_human"),
        evidence_sufficiency=sufficiency,
    )


def test_naive_as_of_is_rejected() -> None:
    with pytest.raises(TypeError, match="timezone-aware"):
        evaluate_sufficiency(_projection(_row()), _policy(), as_of=datetime(2026, 7, 30, 12, 0, 0))


def test_fully_sufficient_api_row() -> None:
    latest = _execution(ts=AS_OF - timedelta(hours=1))
    report = evaluate_sufficiency(
        _projection(_row(latest_execution=latest, freshest_pass=latest)),
        _policy(),
        as_of=AS_OF,
    )
    verdict = report.verdicts[0]
    assert verdict.sufficient is True
    assert verdict.missing_kinds == ()
    assert verdict.reason_codes == ()
    assert verdict.execution_state == "fresh"


def test_recency_boundary_inclusive_at_cutoff() -> None:
    cutoff = AS_OF - timedelta(hours=RECENCY_HOURS)
    latest = _execution(ts=cutoff)
    report = evaluate_sufficiency(
        _projection(_row(latest_execution=latest)),
        _policy(),
        as_of=AS_OF,
    )
    verdict = report.verdicts[0]
    assert verdict.sufficient is True
    assert verdict.execution_state == "fresh"


def test_recency_boundary_flips_one_microsecond_before_cutoff() -> None:
    cutoff = AS_OF - timedelta(hours=RECENCY_HOURS)
    latest = _execution(ts=cutoff - timedelta(microseconds=1))
    report = evaluate_sufficiency(
        _projection(_row(latest_execution=latest)),
        _policy(),
        as_of=AS_OF,
    )
    verdict = report.verdicts[0]
    assert verdict.sufficient is False
    assert verdict.missing_kinds == ("execution_recent",)
    assert verdict.reason_codes == ("execution_stale",)
    assert verdict.execution_state == "stale"


def test_never_run_execution_state_and_reason() -> None:
    report = evaluate_sufficiency(
        _projection(_row(latest_execution=None, atemporal_kinds_present=("covered",))),
        _policy(),
        as_of=AS_OF,
    )
    verdict = report.verdicts[0]
    assert verdict.sufficient is False
    assert verdict.missing_kinds == ("execution_recent",)
    assert verdict.reason_codes == ("never_run",)
    assert verdict.execution_state == "never_run"


def test_uncovered_kind() -> None:
    latest = _execution(ts=AS_OF - timedelta(hours=1))
    report = evaluate_sufficiency(
        _projection(_row(coverage_state="uncovered", latest_execution=latest, atemporal_kinds_present=())),
        _policy(),
        as_of=AS_OF,
    )
    verdict = report.verdicts[0]
    assert verdict.sufficient is False
    assert verdict.missing_kinds == ("covered",)
    assert verdict.reason_codes == ("uncovered",)


def test_fuzz_row_requires_fuzz_run_in_atemporal_kinds() -> None:
    latest = _execution(target="fuzz", status="passed", ts=AS_OF - timedelta(hours=1))
    sufficient = evaluate_sufficiency(
        _projection(
            _row(
                case_id="TC_FUZZ_001",
                case_type="Fuzz",
                latest_execution=latest,
                atemporal_kinds_present=("covered", "fuzz_run"),
            )
        ),
        _policy(),
        as_of=AS_OF,
    ).verdicts[0]
    assert sufficient.sufficient is True

    missing = evaluate_sufficiency(
        _projection(
            _row(
                case_id="TC_FUZZ_001",
                case_type="Fuzz",
                latest_execution=latest,
                atemporal_kinds_present=("covered",),
            )
        ),
        _policy(),
        as_of=AS_OF,
    ).verdicts[0]
    assert missing.sufficient is False
    assert missing.missing_kinds == ("fuzz_run",)
    assert missing.reason_codes == ("fuzz_run_missing",)


def test_performance_row_requires_perf_run_in_atemporal_kinds() -> None:
    latest = _execution(target="performance", status="passed", ts=AS_OF - timedelta(hours=1))
    sufficient = evaluate_sufficiency(
        _projection(
            _row(
                case_id="TC_PERF_001",
                case_type="Performance",
                latest_execution=latest,
                atemporal_kinds_present=("covered", "perf_run"),
            )
        ),
        _policy(),
        as_of=AS_OF,
    ).verdicts[0]
    assert sufficient.sufficient is True

    missing = evaluate_sufficiency(
        _projection(
            _row(
                case_id="TC_PERF_001",
                case_type="Performance",
                latest_execution=latest,
                atemporal_kinds_present=("covered",),
            )
        ),
        _policy(),
        as_of=AS_OF,
    ).verdicts[0]
    assert missing.sufficient is False
    assert missing.missing_kinds == ("perf_run",)
    assert missing.reason_codes == ("perf_run_missing",)


def test_pass_status_kind_uses_freshest_pass_recency() -> None:
    policy = _policy(
        required_kinds={
            "API": ["covered", "execution_recent", "pass_status"],
            "E2E": ["covered", "execution_recent"],
            "Fuzz": ["covered", "fuzz_run"],
            "Performance": ["covered", "perf_run"],
        }
    )
    latest = _execution(ts=AS_OF - timedelta(hours=1))
    stale_pass = _execution(batch_id="20260720120000", ts=AS_OF - timedelta(hours=RECENCY_HOURS + 1))

    no_pass = evaluate_sufficiency(
        _projection(_row(latest_execution=latest, freshest_pass=None)),
        policy,
        as_of=AS_OF,
    ).verdicts[0]
    assert no_pass.missing_kinds == ("pass_status",)
    assert no_pass.reason_codes == ("no_pass",)

    stale = evaluate_sufficiency(
        _projection(_row(latest_execution=latest, freshest_pass=stale_pass)),
        policy,
        as_of=AS_OF,
    ).verdicts[0]
    assert stale.missing_kinds == ("pass_status",)
    assert stale.reason_codes == ("pass_stale",)

    fresh = evaluate_sufficiency(
        _projection(_row(latest_execution=latest, freshest_pass=latest)),
        policy,
        as_of=AS_OF,
    ).verdicts[0]
    assert fresh.sufficient is True


def test_report_is_deterministic_for_same_inputs() -> None:
    latest = _execution(ts=AS_OF - timedelta(hours=1))
    projection = _projection(_row(latest_execution=latest))
    policy = _policy()
    first = evaluate_sufficiency(projection, policy, as_of=AS_OF)
    second = evaluate_sufficiency(projection, policy, as_of=AS_OF)
    assert first == second


def test_report_carries_as_of_and_recency_hours() -> None:
    report = evaluate_sufficiency(_projection(_row()), _policy(), as_of=AS_OF)
    assert report.as_of == AS_OF
    assert report.recency_hours == RECENCY_HOURS


def test_all_sufficient_property() -> None:
    latest = _execution(ts=AS_OF - timedelta(hours=1))
    ok = evaluate_sufficiency(
        _projection(_row(latest_execution=latest)),
        _policy(),
        as_of=AS_OF,
    )
    assert ok.all_sufficient is True

    bad = evaluate_sufficiency(
        _projection(_row(latest_execution=None)),
        _policy(),
        as_of=AS_OF,
    )
    assert bad.all_sufficient is False


def test_require_current_batch_rejects_stale_presence() -> None:
    latest = _execution(ts=AS_OF - timedelta(hours=1))
    row = _row(latest_execution=latest, freshest_pass=latest).model_copy(
        update={"presence_in_current_batch": "not_in_current_batch"}
    )
    report = evaluate_sufficiency(
        _projection(row),
        _policy(),
        as_of=AS_OF,
        require_current_batch=True,
    )
    verdict = report.verdicts[0]
    assert verdict.sufficient is False
    assert verdict.missing_kinds == ("execution_recent",)
    assert verdict.reason_codes == ("not_in_current_batch",)


def test_require_current_batch_false_allows_historical_execution() -> None:
    latest = _execution(ts=AS_OF - timedelta(hours=1))
    row = _row(latest_execution=latest).model_copy(
        update={"presence_in_current_batch": "not_in_current_batch"}
    )
    report = evaluate_sufficiency(
        _projection(row),
        _policy(),
        as_of=AS_OF,
        require_current_batch=False,
    )
    assert report.verdicts[0].sufficient is True


def test_require_current_batch_pass_status_requires_executed_presence() -> None:
    policy = _policy(
        required_kinds={
            "API": ["covered", "execution_recent", "pass_status"],
            "E2E": ["covered", "execution_recent"],
            "Fuzz": ["covered", "fuzz_run"],
            "Performance": ["covered", "perf_run"],
        }
    )
    latest = _execution(ts=AS_OF - timedelta(hours=1))
    row = _row(latest_execution=latest, freshest_pass=latest).model_copy(
        update={"presence_in_current_batch": "not_in_current_batch"}
    )
    report = evaluate_sufficiency(
        _projection(row),
        policy,
        as_of=AS_OF,
        require_current_batch=True,
    )
    verdict = report.verdicts[0]
    assert verdict.sufficient is False
    assert "pass_status" in verdict.missing_kinds
    assert "not_in_current_batch" in verdict.reason_codes


def test_require_current_batch_fuzz_run_requires_executed_presence() -> None:
    latest = _execution(target="fuzz", status="passed", ts=AS_OF - timedelta(hours=1))
    row = _row(
        case_id="TC_FUZZ_001",
        case_type="Fuzz",
        latest_execution=latest,
        atemporal_kinds_present=("covered", "fuzz_run"),
    ).model_copy(update={"presence_in_current_batch": "not_in_current_batch"})
    report = evaluate_sufficiency(
        _projection(row),
        _policy(),
        as_of=AS_OF,
        require_current_batch=True,
    )
    verdict = report.verdicts[0]
    assert verdict.sufficient is False
    assert verdict.missing_kinds == ("fuzz_run",)
    assert verdict.reason_codes == ("not_in_current_batch",)


def test_require_current_batch_perf_run_requires_executed_presence() -> None:
    latest = _execution(target="performance", status="passed", ts=AS_OF - timedelta(hours=1))
    row = _row(
        case_id="TC_PERF_001",
        case_type="Performance",
        latest_execution=latest,
        atemporal_kinds_present=("covered", "perf_run"),
    ).model_copy(update={"presence_in_current_batch": "not_in_current_batch"})
    report = evaluate_sufficiency(
        _projection(row),
        _policy(),
        as_of=AS_OF,
        require_current_batch=True,
    )
    verdict = report.verdicts[0]
    assert verdict.sufficient is False
    assert verdict.missing_kinds == ("perf_run",)
    assert verdict.reason_codes == ("not_in_current_batch",)
