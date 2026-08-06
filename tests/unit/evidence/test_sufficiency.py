"""Task 7: sufficiency = projection facts × policy × ``as_of``, in one pure pass.

Three things are pinned here, in the order the spec (§8/§13) states them:

- the **recency arithmetic** — the boundary is inclusive to the microsecond, the
  same instant written in two offsets judges identically, and ``as_of`` must be
  aware or the call is a ``TypeError`` rather than a naive/aware comparison;
- the **kind table** — every legal ``EvidenceKind`` evaluated conjunctively, with
  ``execution_state`` derived from ``latest_execution`` and ``pass_status``
  derived from ``freshest_pass`` independently of it;
- the **stability contract** — canonical kind order, an index-aligned reason
  code per missing kind, row order that follows the projection, and fail-closed
  behaviour for a kind the module does not handle (no vacuous success).
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Literal, get_args

import pytest
from pydantic import ValidationError

from assurance_agent.artifacts.models.policy import (
    CoverageFloor,
    EvidenceKind,
    EvidenceSufficiency,
    FuzzPolicy,
    HealingPolicy,
    Policy,
)
from assurance_agent.artifacts.models.trace import (
    TraceCaseType,
    TraceExecution,
    TraceProjection,
    TraceRow,
    TraceTarget,
)
from assurance_agent.evidence import sufficiency as sufficiency_module
from assurance_agent.evidence.sufficiency import (
    RowVerdict,
    SufficiencyReasonCode,
    SufficiencyReport,
    evaluate_sufficiency,
)

UTC_PLUS_8 = timezone(timedelta(hours=8))
AS_OF = datetime(2026, 7, 3, 12, 0, 0, tzinfo=UTC)
RECENCY_HOURS = 72
CUTOFF = AS_OF - timedelta(hours=RECENCY_HOURS)
MICROSECOND = timedelta(microseconds=1)

DEFAULT_REQUIRED_KINDS: dict[TraceCaseType, list[EvidenceKind]] = {
    "API": ["covered", "execution_recent"],
    "E2E": ["covered", "execution_recent"],
    "Fuzz": ["covered", "fuzz_run"],
    "Performance": ["covered", "perf_run"],
}


# --------------------------------------------------------------------------- #
# builders
# --------------------------------------------------------------------------- #


def _execution(
    ts: datetime,
    *,
    status: Literal["passed", "failed", "skipped"] = "passed",
    target: TraceTarget = "api",
    batch_id: str = "20260701-000000",
) -> TraceExecution:
    return TraceExecution(batch_id=batch_id, target=target, status=status, ts=ts, ts_source="executed_at")


def _row(
    *,
    case_id: str = "TC_API_001",
    case_type: TraceCaseType = "API",
    automation_required: bool = True,
    coverage_state: Literal["covered", "uncovered", "not_required"] = "covered",
    latest_execution: TraceExecution | None = None,
    freshest_pass: TraceExecution | None = None,
    atemporal_kinds_present: tuple[str, ...] = ("covered",),
) -> TraceRow:
    return TraceRow(
        case_id=case_id,
        module="dept",
        case_type=case_type,
        automation_required=automation_required,
        assertions=("asserts something",),
        covering_tests=(),
        coverage_state=coverage_state,
        latest_execution=latest_execution,
        freshest_pass=freshest_pass,
        presence_in_current_batch="executed",
        atemporal_kinds_present=atemporal_kinds_present,
        failures=(),
        open_problem_ids=(),
    )


def _projection(
    *rows: TraceRow,
    change_id: str = "CH-SUF-001",
    integrity: Literal["complete", "complete_with_gaps", "incomplete"] = "complete",
) -> TraceProjection:
    return TraceProjection(
        schema_version="1",
        change_id=change_id,
        phase="execution",
        authoritative_batch_id="20260701-000000",
        sources=(),
        rows=rows,
        unmapped_tests=(),
        gaps=(),
        integrity=integrity,
    )


def _manual_row(
    *,
    case_id: str = "TC_API_001",
    coverage_state: Literal["covered", "not_required"] = "not_required",
    latest_execution: TraceExecution | None = None,
    freshest_pass: TraceExecution | None = None,
    atemporal_kinds_present: tuple[str, ...] = (),
) -> TraceRow:
    """A case nobody asked to automate: `automation_required=False` (spec §10).

    The fold pairs that with `coverage_state="not_required"`, unless a test
    happens to exist anyway — then the row is covered but still not required.
    """
    return _row(
        case_id=case_id,
        automation_required=False,
        coverage_state=coverage_state,
        latest_execution=latest_execution,
        freshest_pass=freshest_pass,
        atemporal_kinds_present=atemporal_kinds_present,
    )


def _policy(
    *,
    recency_hours: int = RECENCY_HOURS,
    required_kinds: dict[TraceCaseType, list[EvidenceKind]] | None = None,
    on_insufficient: Literal["warn", "block", "require_human"] = "require_human",
) -> Policy:
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
        evidence_sufficiency=EvidenceSufficiency(
            recency_hours=recency_hours,
            required_kinds=dict(DEFAULT_REQUIRED_KINDS if required_kinds is None else required_kinds),
            on_insufficient=on_insufficient,
        ),
    )


def _api_policy(*kinds: EvidenceKind, recency_hours: int = RECENCY_HOURS) -> Policy:
    """A policy that asks the API type for exactly ``kinds`` and nothing else."""
    required: dict[TraceCaseType, list[EvidenceKind]] = {
        "API": list(kinds),
        "E2E": [],
        "Fuzz": [],
        "Performance": [],
    }
    return _policy(recency_hours=recency_hours, required_kinds=required)


def _verdict(row: TraceRow, policy: Policy, *, as_of: datetime = AS_OF) -> RowVerdict:
    report = evaluate_sufficiency(_projection(row), policy, as_of=as_of)
    assert len(report.rows) == 1
    return report.rows[0]


# --------------------------------------------------------------------------- #
# `as_of` is the only clock, and it must be aware
# --------------------------------------------------------------------------- #


def test_naive_as_of_is_a_type_error() -> None:
    """Refuse the call rather than compare a naive `as_of` against aware facts."""
    with pytest.raises(TypeError, match="as_of"):
        evaluate_sufficiency(_projection(_row()), _policy(), as_of=datetime(2026, 7, 3, 12, 0, 0))


def test_naive_execution_timestamp_is_a_type_error_naming_the_case() -> None:
    """A projection built outside the fold could still carry a naive ts."""
    row = _row(latest_execution=_execution(datetime(2026, 7, 2, 12, 0, 0)))

    with pytest.raises(TypeError, match="TC_API_001.*latest_execution"):
        evaluate_sufficiency(_projection(row), _api_policy("execution_recent"), as_of=AS_OF)


@pytest.mark.parametrize(
    "row",
    [
        _row(latest_execution=_execution(datetime(2026, 7, 2, 12, 0, 0))),
        _row(freshest_pass=_execution(datetime(2026, 7, 2, 12, 0, 0))),
    ],
    ids=("latest-execution", "freshest-pass"),
)
def test_naive_timestamps_are_rejected_whatever_the_policy_requires(row: TraceRow) -> None:
    """Both timestamps are checked once per row, before the kind list is read.

    The default policy requires neither `pass_status` nor — for a covered row —
    anything that reads `freshest_pass`, so gating the check on the required
    kinds would let a naive timestamp through until some future policy happened
    to ask for it.
    """
    with pytest.raises(TypeError, match="TC_API_001"):
        evaluate_sufficiency(_projection(row), _policy(), as_of=AS_OF)

    with pytest.raises(TypeError, match="TC_API_001"):
        evaluate_sufficiency(_projection(row), _api_policy(), as_of=AS_OF)


def test_naive_pass_timestamp_is_a_type_error_naming_the_case() -> None:
    row = _row(freshest_pass=_execution(datetime(2026, 7, 2, 12, 0, 0)))

    with pytest.raises(TypeError, match="TC_API_001.*freshest_pass"):
        evaluate_sufficiency(_projection(row), _api_policy("pass_status"), as_of=AS_OF)


def test_the_same_instant_in_two_offsets_judges_identically() -> None:
    """`as_of` and `ts` may be written in any zone; only the instant decides."""
    executed = CUTOFF + timedelta(minutes=30)
    row = _row(latest_execution=_execution(executed), freshest_pass=_execution(executed))
    policy = _api_policy("execution_recent", "pass_status")

    utc = evaluate_sufficiency(_projection(row), policy, as_of=AS_OF)
    shifted = evaluate_sufficiency(
        _projection(
            _row(
                latest_execution=_execution(executed.astimezone(UTC_PLUS_8)),
                freshest_pass=_execution(executed.astimezone(UTC_PLUS_8)),
            )
        ),
        policy,
        as_of=AS_OF.astimezone(UTC_PLUS_8),
    )

    assert shifted.rows == utc.rows
    assert shifted.as_of == utc.as_of


def test_an_offset_shifted_boundary_still_flips_at_the_same_instant() -> None:
    """The equality case survives the zone change, not just the coarse verdict."""
    policy = _api_policy("execution_recent")
    at_boundary = _row(latest_execution=_execution(CUTOFF.astimezone(UTC_PLUS_8)))
    before = _row(latest_execution=_execution((CUTOFF - MICROSECOND).astimezone(UTC_PLUS_8)))

    assert _verdict(at_boundary, policy, as_of=AS_OF.astimezone(UTC_PLUS_8)).sufficient is True
    assert _verdict(before, policy, as_of=AS_OF.astimezone(UTC_PLUS_8)).sufficient is False


# --------------------------------------------------------------------------- #
# recency boundary and the three execution states
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("offset", "expected"),
    [
        (timedelta(0), "fresh"),
        (MICROSECOND, "fresh"),
        (-MICROSECOND, "stale"),
        (timedelta(hours=1), "fresh"),
        (-timedelta(hours=1), "stale"),
    ],
    ids=(
        "exactly-at-boundary",
        "one-microsecond-after",
        "one-microsecond-before",
        "hour-after",
        "hour-before",
    ),
)
def test_execution_state_flips_exactly_at_the_recency_boundary(offset: timedelta, expected: str) -> None:
    """`ts >= as_of - recency_hours`: equality is fresh, one tick earlier is not."""
    row = _row(latest_execution=_execution(CUTOFF + offset))

    assert _verdict(row, _api_policy("execution_recent")).execution_state == expected


def test_execution_state_is_never_run_without_any_execution() -> None:
    assert _verdict(_row(), _api_policy("execution_recent")).execution_state == "never_run"


def test_execution_state_is_reported_even_when_no_temporal_kind_is_required() -> None:
    """The field is a report fact (spec §8), not a by-product of the kind list."""
    row = _row(latest_execution=_execution(CUTOFF - MICROSECOND))

    verdict = _verdict(row, _api_policy("covered"))

    assert verdict.sufficient is True
    assert verdict.execution_state == "stale"


def test_recency_hours_comes_from_policy() -> None:
    """A shorter window makes the same fact stale — the cutoff is not hard-coded."""
    row = _row(latest_execution=_execution(AS_OF - timedelta(hours=48)))

    assert _verdict(row, _api_policy("execution_recent")).sufficient is True
    assert _verdict(row, _api_policy("execution_recent", recency_hours=24)).sufficient is False


# --------------------------------------------------------------------------- #
# the kind table
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("coverage_state", "sufficient"), [("covered", True), ("uncovered", False)])
def test_covered_reads_coverage_state(
    coverage_state: Literal["covered", "uncovered"], sufficient: bool
) -> None:
    """`covered` is the §7 tree-scan hit, for the rows that must be automated.

    `not_required` never reaches this rule — such a row is exempt before any
    kind is evaluated (see the exemption section).
    """
    row = _row(coverage_state=coverage_state, atemporal_kinds_present=())

    assert _verdict(row, _api_policy("covered")).sufficient is sufficient


@pytest.mark.parametrize("kind", ["fuzz_run", "perf_run"])
def test_atemporal_kinds_read_the_projection_list(kind: EvidenceKind) -> None:
    present = _row(atemporal_kinds_present=("covered", kind))
    absent = _row(atemporal_kinds_present=("covered",))

    assert _verdict(present, _api_policy(kind)).sufficient is True
    assert _verdict(absent, _api_policy(kind)).sufficient is False


def test_atemporal_kinds_ignore_recency() -> None:
    """A fuzz/perf run is a fact about the batch, not about how old it is."""
    ancient = _row(
        atemporal_kinds_present=("covered", "fuzz_run"),
        latest_execution=_execution(AS_OF - timedelta(days=365)),
    )

    assert _verdict(ancient, _api_policy("fuzz_run")).sufficient is True


def test_pass_status_reads_freshest_pass_not_latest_execution() -> None:
    """A fresh failure satisfies `execution_recent` and fails `pass_status`."""
    row = _row(
        latest_execution=_execution(AS_OF, status="failed"),
        freshest_pass=_execution(CUTOFF - MICROSECOND),
    )

    verdict = _verdict(row, _api_policy("execution_recent", "pass_status"))

    assert verdict.execution_state == "fresh"
    assert verdict.missing_kinds == ("pass_status",)
    assert verdict.reason_codes == ("pass_stale",)


def test_pass_status_flips_at_the_same_boundary_as_execution_recency() -> None:
    policy = _api_policy("pass_status")

    assert _verdict(_row(freshest_pass=_execution(CUTOFF)), policy).sufficient is True
    assert _verdict(_row(freshest_pass=_execution(CUTOFF - MICROSECOND)), policy).sufficient is False


def test_a_stale_execution_does_not_hide_a_fresh_pass() -> None:
    """The two temporal kinds are read independently, in both directions."""
    row = _row(
        latest_execution=_execution(CUTOFF - MICROSECOND, status="skipped"),
        freshest_pass=_execution(AS_OF),
    )

    verdict = _verdict(row, _api_policy("execution_recent", "pass_status"))

    assert verdict.execution_state == "stale"
    assert verdict.missing_kinds == ("execution_recent",)


def test_required_kinds_are_indexed_by_the_row_case_type() -> None:
    """Each row asks its own case type's list — no cross-type leakage."""
    policy = _policy(
        required_kinds={
            "API": ["covered"],
            "E2E": ["covered", "execution_recent"],
            "Fuzz": [],
            "Performance": [],
        }
    )
    api = _row(case_id="TC_API_001", case_type="API", atemporal_kinds_present=("covered",))
    e2e = _row(case_id="TC_E2E_001", case_type="E2E", atemporal_kinds_present=("covered",))

    report = evaluate_sufficiency(_projection(api, e2e), policy, as_of=AS_OF)

    assert [row.sufficient for row in report.rows] == [True, False]
    assert report.rows[1].missing_kinds == ("execution_recent",)


# --------------------------------------------------------------------------- #
# `automation_required=False` is an exemption, not a failure
# --------------------------------------------------------------------------- #


def test_a_case_that_needs_no_automation_is_exempt_from_every_kind() -> None:
    """Spec §10: a case nobody asked to automate is not an evidence gap.

    Judging it against `covered`/`execution_recent` would make the default policy
    route every manual case to `require_human`, which §10 explicitly says is a row
    state and not a gap. The exemption is keyed on `automation_required`, the fact
    that carries the organisation's decision.
    """
    verdict = _verdict(_manual_row(), _policy())

    assert verdict.sufficient is True
    assert verdict.missing_kinds == ()
    assert verdict.reason_codes == ()
    assert verdict.execution_state == "never_run"


def test_the_exemption_survives_the_strictest_policy() -> None:
    """Every kind at once, none of them present: still exempt."""
    verdict = _verdict(_manual_row(), _api_policy(*get_args(EvidenceKind)))

    assert verdict.sufficient is True
    assert verdict.missing_kinds == ()


def test_a_manually_automated_case_is_still_exempt() -> None:
    """A test that exists anyway does not opt the case back into the policy.

    `automation_required=False` with a covering test is legal (someone automated
    it voluntarily); its staleness is reported, not judged.
    """
    row = _manual_row(
        coverage_state="covered",
        latest_execution=_execution(CUTOFF - MICROSECOND, status="failed"),
        atemporal_kinds_present=("covered",),
    )

    verdict = _verdict(row, _api_policy("covered", "execution_recent", "pass_status"))

    assert verdict.sufficient is True
    assert verdict.missing_kinds == ()
    assert verdict.execution_state == "stale"


def test_an_exempt_row_still_appears_in_the_report() -> None:
    """Exempt is a verdict, not a filter: the row stays countable and auditable."""
    required = _row(case_id="TC_API_001", coverage_state="uncovered", atemporal_kinds_present=())
    manual = _manual_row(case_id="TC_API_002")

    report = evaluate_sufficiency(_projection(required, manual), _policy(), as_of=AS_OF)

    assert [verdict.case_id for verdict in report.rows] == ["TC_API_001", "TC_API_002"]
    assert [verdict.sufficient for verdict in report.rows] == [False, True]
    assert [verdict.case_id for verdict in report.insufficient_rows] == ["TC_API_001"]


def test_the_exemption_does_not_read_coverage_state() -> None:
    """`coverage_state="not_required"` is a consequence of the exemption, never
    its cause: a covered manual row is exempt and an uncovered required row is not."""
    assert _verdict(_manual_row(coverage_state="covered"), _policy()).sufficient is True
    assert (
        _verdict(_row(coverage_state="uncovered", atemporal_kinds_present=()), _policy()).sufficient is False
    )


@pytest.mark.parametrize(
    ("automation_required", "coverage_state"),
    [(True, "not_required"), (False, "uncovered")],
    ids=("required-but-not-required", "not-required-but-uncovered"),
)
def test_contradictory_row_facts_are_rejected(automation_required: bool, coverage_state: str) -> None:
    """`fold_trace` derives `coverage_state` from `automation_required`, so these
    two combinations cannot both be true. Guessing which fact to believe would
    silently pick a verdict, so the evaluator refuses the row by name."""
    row = _row(
        automation_required=automation_required,
        coverage_state=coverage_state,  # pyright: ignore[reportArgumentType]
        atemporal_kinds_present=(),
    )

    with pytest.raises(ValueError, match="TC_API_001"):
        evaluate_sufficiency(_projection(row), _policy(), as_of=AS_OF)


# --------------------------------------------------------------------------- #
# conjunction, stability and reason codes
# --------------------------------------------------------------------------- #


def test_every_required_kind_must_hold() -> None:
    """Conjunctive: one satisfied kind out of three is still insufficient."""
    row = _row(coverage_state="covered", atemporal_kinds_present=("covered",))

    verdict = _verdict(row, _api_policy("covered", "execution_recent", "pass_status"))

    assert verdict.sufficient is False
    assert verdict.missing_kinds == ("execution_recent", "pass_status")


def test_missing_kinds_follow_the_canonical_order_not_the_policy_order() -> None:
    """Two policies that require the same set produce byte-identical verdicts."""
    row = _row(coverage_state="uncovered", atemporal_kinds_present=())
    declared = _api_policy("covered", "execution_recent", "pass_status")
    permuted = _api_policy("pass_status", "covered", "execution_recent")

    assert _verdict(row, declared) == _verdict(row, permuted)
    assert _verdict(row, declared).missing_kinds == ("covered", "execution_recent", "pass_status")


def test_the_canonical_kind_order_is_the_policy_vocabulary_order() -> None:
    """Pin the order so a reordered `EvidenceKind` literal cannot silently
    reshuffle every stored `missing_kinds`."""
    assert get_args(EvidenceKind) == (
        "covered",
        "execution_recent",
        "fuzz_run",
        "perf_run",
        "pass_status",
    )


def test_a_kind_declared_twice_is_evaluated_once() -> None:
    row = _row(coverage_state="uncovered", atemporal_kinds_present=())

    assert _verdict(row, _api_policy("covered", "covered")).missing_kinds == ("covered",)


def test_an_empty_required_list_is_the_documented_opt_out() -> None:
    """`API: []` is the organisation declaring "no evidence required" (Task 6)."""
    row = _row(coverage_state="uncovered", atemporal_kinds_present=())

    verdict = _verdict(row, _api_policy())

    assert verdict.sufficient is True
    assert verdict.missing_kinds == ()
    assert verdict.reason_codes == ()


@pytest.mark.parametrize(
    ("kind", "row", "reason"),
    [
        ("covered", _row(coverage_state="uncovered", atemporal_kinds_present=()), "not_covered"),
        ("execution_recent", _row(), "never_run"),
        (
            "execution_recent",
            _row(latest_execution=_execution(CUTOFF - MICROSECOND)),
            "execution_stale",
        ),
        ("fuzz_run", _row(), "fuzz_run_missing"),
        ("perf_run", _row(), "perf_run_missing"),
        ("pass_status", _row(), "never_passed"),
        ("pass_status", _row(freshest_pass=_execution(CUTOFF - MICROSECOND)), "pass_stale"),
    ],
    ids=(
        "uncovered",
        "never-run",
        "execution-stale",
        "fuzz-missing",
        "perf-missing",
        "never-passed",
        "pass-stale",
    ),
)
def test_the_reason_code_mapping_is_stable(kind: EvidenceKind, row: TraceRow, reason: str) -> None:
    verdict = _verdict(row, _api_policy(kind))

    assert verdict.missing_kinds == (kind,)
    assert verdict.reason_codes == (reason,)


def test_the_reason_code_vocabulary_is_exactly_what_the_module_can_emit() -> None:
    """No dead codes: every literal above is produced by the mapping test."""
    assert get_args(SufficiencyReasonCode) == (
        "not_covered",
        "never_run",
        "execution_stale",
        "fuzz_run_missing",
        "perf_run_missing",
        "never_passed",
        "pass_stale",
    )


def test_reason_codes_are_index_aligned_with_missing_kinds() -> None:
    """The two tuples are one mapping read twice, so consumers may zip them."""
    row = _row(coverage_state="uncovered", atemporal_kinds_present=())

    verdict = _verdict(row, _api_policy("covered", "execution_recent", "pass_status"))

    assert verdict.missing_kinds == ("covered", "execution_recent", "pass_status")
    assert verdict.reason_codes == ("not_covered", "never_run", "never_passed")


def test_a_satisfied_row_carries_no_reason_codes() -> None:
    row = _row(latest_execution=_execution(AS_OF), freshest_pass=_execution(AS_OF))

    verdict = _verdict(row, _api_policy("covered", "execution_recent", "pass_status"))

    assert verdict == RowVerdict(
        case_id="TC_API_001",
        sufficient=True,
        missing_kinds=(),
        reason_codes=(),
        execution_state="fresh",
    )


# --------------------------------------------------------------------------- #
# no vacuous success
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("kind", get_args(EvidenceKind))
def test_every_policy_kind_has_a_rule(kind: EvidenceKind) -> None:
    """Every kind the policy vocabulary accepts is evaluated by a rule here.

    A literal added to ``EvidenceKind`` without a matching rule reaches the
    fall-through in ``_missing_reason`` and raises, so this parametrization
    errors on the new kind instead of the row passing on an unevaluated
    requirement. Each kind is exercised against a row that satisfies it, which
    also proves the rule is reachable rather than always-missing.
    """
    row = _row(
        coverage_state="covered",
        latest_execution=_execution(AS_OF),
        freshest_pass=_execution(AS_OF),
        atemporal_kinds_present=("covered", "fuzz_run", "perf_run"),
    )

    assert _verdict(row, _api_policy(kind)).sufficient is True


def _bypassed_policy(required_kinds: dict[str, list[str]]) -> Policy:
    """A policy in a state Task 6's validator forbids.

    ``model_construct`` is the only way to build one, which is the point: these
    tests describe what the evaluator does when validation was skipped upstream.
    """
    bogus = EvidenceSufficiency.model_construct(
        recency_hours=RECENCY_HOURS,
        required_kinds=required_kinds,
        on_insufficient="warn",
    )
    return _policy().model_copy(update={"evidence_sufficiency": bogus})


@pytest.mark.parametrize("row", [_row(), _manual_row()], ids=("required", "exempt"))
def test_a_kind_outside_the_policy_vocabulary_is_rejected(row: TraceRow) -> None:
    """Validation makes this unreachable; the evaluator still fails closed.

    Including for a row the exemption would otherwise answer without consulting
    the policy: a malformed policy is malformed whether or not this particular
    batch happens to contain a case that needs it. Skipping the check for exempt
    rows would make a broken policy pass silently on any all-manual change.
    """
    policy = _bypassed_policy({"API": ["vibes"], "E2E": [], "Fuzz": [], "Performance": []})

    with pytest.raises(ValueError, match="vibes"):
        evaluate_sufficiency(_projection(row), policy, as_of=AS_OF)


@pytest.mark.parametrize("row", [_row(), _manual_row()], ids=("required", "exempt"))
def test_a_case_type_missing_from_the_policy_is_rejected(row: TraceRow) -> None:
    """Task 6's validator forbids this; a bypassed policy must not fail open.

    A bare ``KeyError('API')`` would leave the reader guessing which mapping and
    which row, so the message names the policy path, the case type and the case.
    The exempt row is checked too, for the reason given on the test above.
    """
    policy = _bypassed_policy({"E2E": [], "Fuzz": [], "Performance": []})

    with pytest.raises(KeyError) as raised:
        evaluate_sufficiency(_projection(row), policy, as_of=AS_OF)

    message = str(raised.value)
    assert "evidence_sufficiency.required_kinds" in message
    assert "API" in message and "TC_API_001" in message


# --------------------------------------------------------------------------- #
# report shape, ordering and purity
# --------------------------------------------------------------------------- #


def test_rows_follow_the_projection_order_one_for_one() -> None:
    rows = (
        _row(case_id="TC_API_003"),
        _row(case_id="TC_API_001"),
        _row(case_id="TC_API_002"),
    )

    report = evaluate_sufficiency(_projection(*rows), _api_policy("covered"), as_of=AS_OF)

    assert [verdict.case_id for verdict in report.rows] == ["TC_API_003", "TC_API_001", "TC_API_002"]


def test_the_same_inputs_produce_an_equal_report() -> None:
    projection = _projection(
        _row(case_id="TC_API_001"), _row(case_id="TC_API_002", coverage_state="uncovered")
    )
    policy = _policy()

    first = evaluate_sufficiency(projection, policy, as_of=AS_OF)
    second = evaluate_sufficiency(projection, policy, as_of=AS_OF)

    assert first == second
    assert first.model_dump(mode="json") == second.model_dump(mode="json")


def test_the_report_echoes_the_inputs_that_decided_it() -> None:
    report = evaluate_sufficiency(_projection(_row()), _policy(recency_hours=6), as_of=AS_OF)

    assert report.change_id == "CH-SUF-001"
    assert report.as_of == AS_OF
    assert report.recency_hours == 6


@pytest.mark.parametrize("integrity", ["complete", "complete_with_gaps", "incomplete"])
def test_the_report_carries_the_projection_integrity_verbatim(
    integrity: Literal["complete", "complete_with_gaps", "incomplete"],
) -> None:
    """Copied, never interpreted: routing on it is the consumer's job (spec §13)."""
    projection = _projection(_row(), integrity=integrity)

    assert evaluate_sufficiency(projection, _policy(), as_of=AS_OF).integrity == integrity


def test_an_incomplete_projection_still_reports_its_row_verdicts() -> None:
    """Sufficiency is not silently downgraded by integrity — but it is visible.

    `sufficient` stays a statement about rows, so a consumer that forgets to look
    at `integrity` cannot be rescued here; carrying the field means it no longer
    has to reach back to the projection to find out.
    """
    satisfied = _row(latest_execution=_execution(AS_OF))
    report = evaluate_sufficiency(_projection(satisfied, integrity="incomplete"), _policy(), as_of=AS_OF)

    assert report.rows[0].sufficient is True
    assert report.sufficient is True
    assert report.integrity == "incomplete"


def test_zero_rows_with_an_incomplete_projection_is_visible_in_the_report() -> None:
    """The vacuous-`all()` case: `sufficient` is True and integrity says why not
    to believe it. Task 9/11 must order integrity before sufficiency (spec §13)."""
    report = evaluate_sufficiency(_projection(integrity="incomplete"), _policy(), as_of=AS_OF)

    assert report.rows == ()
    assert report.sufficient is True
    assert report.insufficient_rows == ()
    assert report.integrity == "incomplete"


# --------------------------------------------------------------------------- #
# `integrity_blocks_routing`: the integrity-first obligation, made mechanical
#
# Carrying `integrity` made the obligation *visible*; it did not make it
# checkable. Every consumer still had to know which of the three levels forbids
# believing `sufficient`, and a consumer that got that wrong looked exactly like
# one that got it right. The predicate names the one comparison so there is a
# single place to be wrong, and so a new integrity level forces a decision here
# instead of defaulting into "does not block".
#
# It is deliberately a *fact*, not a disposition: it says the row verdicts must
# not be believed, never what to do about that. The `pass|needs_human|stop`
# mapping stays in the independent gate (Task 11).
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("integrity", "blocks"),
    [("complete", False), ("complete_with_gaps", False), ("incomplete", True)],
)
def test_only_incomplete_integrity_blocks_routing(
    integrity: Literal["complete", "complete_with_gaps", "incomplete"], blocks: bool
) -> None:
    """`complete_with_gaps` does not block: the fold read every input it needed
    and is reporting *known* absences, which is what `sufficient` already judges.
    `incomplete` means an input could not be read at all, so the row set is not
    the change's row set and no verdict over it means anything."""
    report = evaluate_sufficiency(_projection(_row(), integrity=integrity), _policy(), as_of=AS_OF)

    assert report.integrity_blocks_routing is blocks


def test_every_integrity_level_is_decided_by_the_predicate() -> None:
    """Closure: a level added to the literal without a decision here would
    silently inherit "does not block", i.e. would fail open."""
    levels = get_args(SufficiencyReport.model_fields["integrity"].annotation)

    assert set(levels) == {"complete", "complete_with_gaps", "incomplete"}
    decided = {
        level: evaluate_sufficiency(
            _projection(_row(), integrity=level), _policy(), as_of=AS_OF
        ).integrity_blocks_routing
        for level in levels
    }
    assert decided == {"complete": False, "complete_with_gaps": False, "incomplete": True}


def test_the_predicate_is_true_exactly_where_sufficient_must_not_be_believed() -> None:
    """The two facts are independent, and this is the pair that matters: an
    unreadable projection folds to zero rows, which `all()` calls sufficient."""
    report = evaluate_sufficiency(_projection(integrity="incomplete"), _policy(), as_of=AS_OF)

    assert report.sufficient is True, "the premise: zero rows are vacuously sufficient"
    assert report.integrity_blocks_routing is True


def test_the_predicate_does_not_alter_any_row_verdict() -> None:
    """It is derived, not a second judgement: the same rows under blocking and
    non-blocking integrity produce identical verdicts."""
    row = _row(latest_execution=_execution(AS_OF))
    policy = _api_policy("covered", "execution_recent")

    blocked = evaluate_sufficiency(_projection(row, integrity="incomplete"), policy, as_of=AS_OF)
    clear = evaluate_sufficiency(_projection(row, integrity="complete"), policy, as_of=AS_OF)

    assert blocked.rows == clear.rows
    assert blocked.sufficient == clear.sufficient is True
    assert (blocked.integrity_blocks_routing, clear.integrity_blocks_routing) == (True, False)


def test_a_manual_row_stays_exempt_under_blocking_integrity() -> None:
    """The predicate must not be a back door to re-judging the Task 7 exemption:
    a case nobody asked to automate is still not a gap, whatever integrity says."""
    report = evaluate_sufficiency(_projection(_manual_row(), integrity="incomplete"), _policy(), as_of=AS_OF)

    assert report.rows[0].sufficient is True
    assert report.insufficient_rows == ()
    assert report.integrity_blocks_routing is True


def test_the_predicate_is_not_a_persisted_field() -> None:
    """Derived from `integrity`, so it must not be storable independently of it —
    a stored copy could contradict the level it was derived from."""
    report = evaluate_sufficiency(_projection(_row(), integrity="incomplete"), _policy(), as_of=AS_OF)

    assert "integrity_blocks_routing" not in report.model_dump(mode="json")
    assert "integrity_blocks_routing" not in SufficiencyReport.model_fields


def test_the_report_integrity_literal_equals_the_projection_literal() -> None:
    """The literal is spelled out in `sufficiency.py` rather than imported, so
    the two copies are compared here: a projection integrity level added on one
    side and not the other would otherwise surface as a validation error at the
    first fold that produced it."""
    assert get_args(SufficiencyReport.model_fields["integrity"].annotation) == get_args(
        TraceProjection.model_fields["integrity"].annotation
    )


def test_the_report_fields_are_exactly_the_five_it_promises() -> None:
    assert list(SufficiencyReport.model_fields) == [
        "change_id",
        "as_of",
        "recency_hours",
        "integrity",
        "rows",
    ]


def test_the_report_aggregates_over_rows() -> None:
    good = _row(case_id="TC_API_001", latest_execution=_execution(AS_OF))
    bad = _row(case_id="TC_API_002", coverage_state="uncovered", atemporal_kinds_present=())
    policy = _api_policy("covered", "execution_recent")

    report = evaluate_sufficiency(_projection(good, bad), policy, as_of=AS_OF)

    assert report.sufficient is False
    assert [verdict.case_id for verdict in report.insufficient_rows] == ["TC_API_002"]
    assert evaluate_sufficiency(_projection(good), policy, as_of=AS_OF).sufficient is True


def test_a_projection_with_no_rows_has_nothing_to_judge() -> None:
    """Zero rows is `integrity=incomplete` upstream (Task 4); the verdict layer
    that consumes this report fails on that, so the report itself stays honest
    and reports an empty row set rather than inventing a verdict."""
    report = evaluate_sufficiency(_projection(), _policy(), as_of=AS_OF)

    assert report.rows == ()
    assert report.sufficient is True


def test_the_report_extra_fields_are_rejected_including_integrity_typos() -> None:
    with pytest.raises(ValidationError, match="integrity"):
        SufficiencyReport(
            change_id="CH-SUF-001",
            as_of=AS_OF,
            recency_hours=RECENCY_HOURS,
            integrity="mostly_fine",  # pyright: ignore[reportArgumentType]
            rows=(),
        )


def test_the_result_dtos_are_frozen() -> None:
    report = evaluate_sufficiency(_projection(_row()), _policy(), as_of=AS_OF)

    with pytest.raises(ValidationError):
        report.rows[0].sufficient = False  # pyright: ignore[reportAttributeAccessIssue]
    with pytest.raises(ValidationError):
        report.recency_hours = 1  # pyright: ignore[reportAttributeAccessIssue]


def test_the_result_dtos_reject_extra_fields() -> None:
    """Closed DTOs: a consumer cannot smuggle a field the report never promised."""
    with pytest.raises(ValidationError, match="grace"):
        RowVerdict(
            case_id="TC_API_001",
            sufficient=True,
            missing_kinds=(),
            reason_codes=(),
            execution_state="fresh",
            grace=True,  # pyright: ignore[reportCallIssue]
        )
    with pytest.raises(ValidationError, match="grace"):
        SufficiencyReport(
            change_id="CH-SUF-001",
            as_of=AS_OF,
            recency_hours=RECENCY_HOURS,
            integrity="complete",
            rows=(),
            grace=True,  # pyright: ignore[reportCallIssue]
        )


def test_the_row_verdict_fields_are_exactly_the_planned_five() -> None:
    assert list(RowVerdict.model_fields) == [
        "case_id",
        "sufficient",
        "missing_kinds",
        "reason_codes",
        "execution_state",
    ]


def test_the_module_reads_no_ambient_state() -> None:
    """Purity by construction: no clock, no disk, and no entropy either.

    Randomness matters as much as the clock here: a report that varied between
    two evaluations of the same inputs would break the byte-equality the fold and
    the two call sites (runner, verify) rely on.
    """
    module_path = Path(str(sufficiency_module.__file__))
    tree = ast.parse(module_path.read_text(encoding="utf-8"))

    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    forbidden_calls = {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and node.attr
        in {
            "now",
            "utcnow",
            "today",
            "open",
            "read_text",
            "read_bytes",
            "glob",
            "uuid4",
            "random",
            "choice",
            "shuffle",
            "token_hex",
        }
    }

    assert (
        imported
        & {
            "os",
            "pathlib",
            "time",
            "io",
            "json",
            "yaml",
            "subprocess",
            "random",
            "uuid",
            "secrets",
        }
        == set()
    )
    assert forbidden_calls == set()
