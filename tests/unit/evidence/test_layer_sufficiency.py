"""Evaluator binding provenance and exact four-layer sufficiency join."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal, cast

import pytest

from assurance_agent.artifacts.models.policy import (
    CoverageFloor,
    EvidenceSufficiency,
    FuzzPolicy,
    HealingPolicy,
    Policy,
)
from assurance_agent.artifacts.models.sufficiency import (
    SufficiencyBindingError,
    SufficiencyReportV2,
    SufficiencyRowVerdictV2,
)
from assurance_agent.artifacts.models.trace import TraceExecution, TraceProjection, TraceRow
from assurance_agent.artifacts.policy import policy_digest
from assurance_agent.evidence.digests import projection_digest
from assurance_agent.evidence.layer_summary import (
    TraceLayerSummaryError,
    join_layer_sufficiency,
    summarize_projection_by_layer,
)
from assurance_agent.evidence.sufficiency import (
    RowVerdict,
    SufficiencyReport,
    evaluate_sufficiency,
)
from assurance_agent.evidence.verify import evaluate_verify_verdict
from tests.helpers_aa import AWARE_NOW

RECENCY_HOURS = 72


def _execution(
    *,
    target: Literal["api", "e2e", "fuzz", "performance"] = "api",
    ts: datetime | None = None,
) -> TraceExecution:
    return TraceExecution(
        batch_id="20260729120000",
        target=target,
        status="passed",
        ts=ts or AWARE_NOW - timedelta(hours=1),
        ts_source="executed_at",
    )


def _row(
    *,
    case_id: str,
    case_type: Literal["API", "E2E", "Fuzz", "Performance"],
    latest_execution: TraceExecution | None = None,
    atemporal_kinds_present: tuple[str, ...] | None = None,
) -> TraceRow:
    target = cast(
        Literal["api", "e2e", "fuzz", "performance"],
        {"API": "api", "E2E": "e2e", "Fuzz": "fuzz", "Performance": "performance"}[case_type],
    )
    latest = latest_execution if latest_execution is not None else _execution(target=target)
    kinds = atemporal_kinds_present
    if kinds is None:
        if case_type == "Fuzz":
            kinds = ("covered", "fuzz_run")
        elif case_type == "Performance":
            kinds = ("covered", "perf_run")
        else:
            kinds = ("covered",)
    return TraceRow(
        case_id=case_id,
        module="system.dept",
        case_type=case_type,
        automation_required=True,
        coverage_state="covered",
        latest_execution=latest,
        freshest_pass=latest,
        presence_in_current_batch="executed",
        atemporal_kinds_present=kinds,
    )


def _projection(*rows: TraceRow) -> TraceProjection:
    return TraceProjection(
        change_id="CH-1",
        phase="execution",
        authoritative_batch_id="20260729120000",
        rows=rows,
        integrity="complete",
    )


def _policy() -> Policy:
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
            recency_hours=RECENCY_HOURS,
            required_kinds={
                "API": ["covered", "execution_recent"],
                "E2E": ["covered", "execution_recent"],
                "Fuzz": ["covered", "fuzz_run"],
                "Performance": ["covered", "perf_run"],
            },
            on_insufficient="require_human",
        ),
    )


def _four_layer_projection() -> TraceProjection:
    return _projection(
        _row(case_id="TC_API_001", case_type="API"),
        _row(case_id="TC_E2E_001", case_type="E2E"),
        _row(case_id="TC_FUZZ_001", case_type="Fuzz"),
        _row(case_id="TC_PERF_001", case_type="Performance"),
    )


def test_evaluate_sufficiency_binds_projection_policy_and_mode() -> None:
    projection = _four_layer_projection()
    policy = _policy()
    report = evaluate_sufficiency(
        projection,
        policy,
        as_of=AWARE_NOW,
        require_current_batch=True,
    )
    assert isinstance(report, SufficiencyReportV2)
    assert report.source_projection_digest == projection_digest(projection)
    assert report.source_policy_digest == policy_digest(policy)
    assert report.semantics == "evidence_sufficiency/v2"
    assert report.require_current_batch is True
    assert report.schema_version == "2.0"
    assert report.as_of == AWARE_NOW
    assert report.recency_hours == RECENCY_HOURS
    assert all(isinstance(v, SufficiencyRowVerdictV2) for v in report.verdicts)


def test_join_layer_sufficiency_happy_path_four_layer_order() -> None:
    projection = _four_layer_projection()
    policy = _policy()
    report = evaluate_sufficiency(
        projection,
        policy,
        as_of=AWARE_NOW,
        require_current_batch=True,
    )
    joined = join_layer_sufficiency(
        projection,
        summarize_projection_by_layer(projection),
        report,
        expected_policy_digest=policy_digest(policy),
    )
    assert [row.layer for row in joined.layers] == ["api", "e2e", "fuzz", "performance"]
    assert [row.case_type for row in joined.layers] == ["API", "E2E", "Fuzz", "Performance"]
    assert joined.source_projection_digest == projection_digest(projection)
    assert joined.source_policy_digest == policy_digest(policy)
    assert joined.semantics == "evidence_sufficiency/v2"
    assert joined.require_current_batch is True
    assert sum(layer.sufficient for layer in joined.layers) == len(report.verdicts)
    assert sum(layer.insufficient for layer in joined.layers) == 0
    for layer in joined.layers:
        assert tuple(layer.execution_state_counts) == ("never_run", "stale", "fresh")
        assert layer.sufficient + layer.insufficient == sum(layer.execution_state_counts.values())


def test_join_rejects_missing_case() -> None:
    projection = _four_layer_projection()
    policy = _policy()
    report = evaluate_sufficiency(
        projection, policy, as_of=AWARE_NOW, require_current_batch=True
    )
    truncated = report.model_copy(update={"verdicts": report.verdicts[:-1]})
    with pytest.raises(SufficiencyBindingError):
        join_layer_sufficiency(
            projection,
            summarize_projection_by_layer(projection),
            truncated,
            expected_policy_digest=policy_digest(policy),
        )


def test_join_rejects_extra_case() -> None:
    projection = _four_layer_projection()
    policy = _policy()
    report = evaluate_sufficiency(
        projection, policy, as_of=AWARE_NOW, require_current_batch=True
    )
    extra = SufficiencyRowVerdictV2.model_validate(
        {
            "case_id": "TC_EXTRA",
            "sufficient": True,
            "missing_kinds": [],
            "reason_codes": [],
            "execution_state": "fresh",
        }
    )
    bloated = report.model_copy(update={"verdicts": (*report.verdicts, extra)})
    with pytest.raises(SufficiencyBindingError):
        join_layer_sufficiency(
            projection,
            summarize_projection_by_layer(projection),
            bloated,
            expected_policy_digest=policy_digest(policy),
        )


def test_join_rejects_duplicate_projection_case_ids() -> None:
    policy = _policy()
    row = _row(case_id="TC_API_001", case_type="API")
    projection = TraceProjection.model_construct(
        schema_version="1",
        change_id="CH-1",
        phase="execution",
        authoritative_batch_id="20260729120000",
        rows=(row, row),
        unmapped_tests=(),
        gaps=(),
        sources=(),
        integrity="complete",
    )
    single = _projection(row)
    facts = summarize_projection_by_layer(single).model_copy(
        update={
            "source_projection_digest": "d" * 64,
            "change_id": projection.change_id,
            "phase": projection.phase,
            "authoritative_batch_id": projection.authoritative_batch_id,
        }
    )
    report = SufficiencyReportV2(
        source_projection_digest="d" * 64,
        source_policy_digest=policy_digest(policy),
        require_current_batch=True,
        as_of=AWARE_NOW,
        recency_hours=RECENCY_HOURS,
        verdicts=(
            SufficiencyRowVerdictV2.model_validate(
                {
                    "case_id": "TC_API_001",
                    "sufficient": True,
                    "missing_kinds": [],
                    "reason_codes": [],
                    "execution_state": "fresh",
                }
            ),
        ),
    )
    with pytest.raises(SufficiencyBindingError, match="duplicate"):
        join_layer_sufficiency(
            projection,  # type: ignore[arg-type]
            facts,
            report,
            expected_policy_digest=policy_digest(policy),
        )


def test_join_rejects_wrong_projection_digest() -> None:
    projection = _four_layer_projection()
    policy = _policy()
    report = evaluate_sufficiency(
        projection, policy, as_of=AWARE_NOW, require_current_batch=True
    )
    bad = report.model_copy(update={"source_projection_digest": "0" * 64})
    with pytest.raises(SufficiencyBindingError):
        join_layer_sufficiency(
            projection,
            summarize_projection_by_layer(projection),
            bad,
            expected_policy_digest=policy_digest(policy),
        )


def test_join_rejects_wrong_policy_digest() -> None:
    projection = _four_layer_projection()
    policy = _policy()
    report = evaluate_sufficiency(
        projection, policy, as_of=AWARE_NOW, require_current_batch=True
    )
    with pytest.raises(SufficiencyBindingError):
        join_layer_sufficiency(
            projection,
            summarize_projection_by_layer(projection),
            report,
            expected_policy_digest="0" * 64,
        )


def test_join_rejects_wrong_semantics() -> None:
    projection = _four_layer_projection()
    policy = _policy()
    report = evaluate_sufficiency(
        projection, policy, as_of=AWARE_NOW, require_current_batch=True
    )
    bad = SufficiencyReportV2.model_construct(
        schema_version=report.schema_version,
        source_projection_digest=report.source_projection_digest,
        source_policy_digest=report.source_policy_digest,
        semantics="evidence_sufficiency/v1",  # type: ignore[arg-type]
        require_current_batch=report.require_current_batch,
        as_of=report.as_of,
        recency_hours=report.recency_hours,
        verdicts=report.verdicts,
    )
    with pytest.raises(SufficiencyBindingError):
        join_layer_sufficiency(
            projection,
            summarize_projection_by_layer(projection),
            bad,
            expected_policy_digest=policy_digest(policy),
        )


def test_join_rejects_require_current_batch_false() -> None:
    projection = _four_layer_projection()
    policy = _policy()
    report = evaluate_sufficiency(
        projection, policy, as_of=AWARE_NOW, require_current_batch=False
    )
    assert report.require_current_batch is False
    with pytest.raises(SufficiencyBindingError):
        join_layer_sufficiency(
            projection,
            summarize_projection_by_layer(projection),
            report,
            expected_policy_digest=policy_digest(policy),
        )


def test_join_rejects_facts_projection_digest_mismatch() -> None:
    projection = _four_layer_projection()
    policy = _policy()
    report = evaluate_sufficiency(
        projection, policy, as_of=AWARE_NOW, require_current_batch=True
    )
    facts = summarize_projection_by_layer(projection).model_copy(
        update={"source_projection_digest": "0" * 64}
    )
    with pytest.raises(SufficiencyBindingError):
        join_layer_sufficiency(
            projection,
            facts,
            report,
            expected_policy_digest=policy_digest(policy),
        )


def test_join_rejects_facts_identity_mismatch() -> None:
    projection = _four_layer_projection()
    policy = _policy()
    report = evaluate_sufficiency(
        projection, policy, as_of=AWARE_NOW, require_current_batch=True
    )
    facts = summarize_projection_by_layer(projection).model_copy(update={"change_id": "CH-OTHER"})
    with pytest.raises(SufficiencyBindingError):
        join_layer_sufficiency(
            projection,
            facts,
            report,
            expected_policy_digest=policy_digest(policy),
        )


def test_join_rejects_facts_total_conservation_mismatch() -> None:
    projection = _four_layer_projection()
    policy = _policy()
    report = evaluate_sufficiency(
        projection, policy, as_of=AWARE_NOW, require_current_batch=True
    )
    facts = summarize_projection_by_layer(projection)
    api = facts.layers[0]
    mutated_api = api.model_copy(
        update={
            "total": api.total + 1,
            "covered": api.covered + 1,
            "automated": api.automated + 1,
            "current_executed": api.current_executed + 1,
            "latest_passed": api.latest_passed + 1,
        }
    )
    mutated = facts.model_copy(update={"layers": (mutated_api, *facts.layers[1:])})
    with pytest.raises(SufficiencyBindingError):
        join_layer_sufficiency(
            projection,
            mutated,
            report,
            expected_policy_digest=policy_digest(policy),
        )


def test_join_rejects_legacy_v1_report() -> None:
    projection = _four_layer_projection()
    policy = _policy()
    legacy = SufficiencyReport(
        as_of=AWARE_NOW,
        recency_hours=RECENCY_HOURS,
        verdicts=tuple(
            RowVerdict(
                case_id=row.case_id,
                sufficient=True,
                missing_kinds=(),
                reason_codes=(),
                execution_state="fresh",
            )
            for row in projection.rows
        ),
    )
    with pytest.raises(SufficiencyBindingError):
        join_layer_sufficiency(
            projection,
            summarize_projection_by_layer(projection),
            legacy,  # type: ignore[arg-type]
            expected_policy_digest=policy_digest(policy),
        )


def test_legacy_report_remains_readable_for_verify() -> None:
    projection = _projection(_row(case_id="TC_API_001", case_type="API"))
    policy = _policy()
    legacy = SufficiencyReport(
        as_of=AWARE_NOW,
        recency_hours=RECENCY_HOURS,
        verdicts=(
            RowVerdict(
                case_id="TC_API_001",
                sufficient=True,
                missing_kinds=(),
                reason_codes=(),
                execution_state="fresh",
            ),
        ),
    )
    result = evaluate_verify_verdict(projection, policy, legacy)
    assert result.verdict == "pass"
    with pytest.raises(SufficiencyBindingError):
        join_layer_sufficiency(
            projection,
            summarize_projection_by_layer(projection),
            legacy,  # type: ignore[arg-type]
            expected_policy_digest=policy_digest(policy),
        )


def test_summarize_duplicate_still_raises_trace_layer_summary_error() -> None:
    row = _row(case_id="TC_API_001", case_type="API")
    projection = TraceProjection.model_construct(
        schema_version="1",
        change_id="CH-1",
        phase="execution",
        authoritative_batch_id="20260729120000",
        rows=(row, row),
        unmapped_tests=(),
        gaps=(),
        sources=(),
        integrity="complete",
    )
    with pytest.raises(TraceLayerSummaryError, match="duplicate case_id"):
        summarize_projection_by_layer(projection)  # type: ignore[arg-type]


def test_join_reason_counts_lexical_and_state_order_with_insufficients() -> None:
    stale = _execution(ts=AWARE_NOW - timedelta(hours=RECENCY_HOURS + 1))
    never = TraceRow(
        case_id="TC_API_001",
        module="system.dept",
        case_type="API",
        automation_required=True,
        coverage_state="covered",
        latest_execution=None,
        freshest_pass=None,
        presence_in_current_batch="executed",
        atemporal_kinds_present=("covered",),
    )
    uncovered = TraceRow(
        case_id="TC_API_002",
        module="system.dept",
        case_type="API",
        automation_required=True,
        coverage_state="uncovered",
        latest_execution=stale,
        freshest_pass=None,
        presence_in_current_batch="executed",
        atemporal_kinds_present=(),
    )
    projection = _projection(
        never,
        uncovered,
        _row(case_id="TC_E2E_001", case_type="E2E"),
        _row(case_id="TC_FUZZ_001", case_type="Fuzz"),
        _row(case_id="TC_PERF_001", case_type="Performance"),
    )
    policy = _policy()
    report = evaluate_sufficiency(
        projection, policy, as_of=AWARE_NOW, require_current_batch=True
    )
    joined = join_layer_sufficiency(
        projection,
        summarize_projection_by_layer(projection),
        report,
        expected_policy_digest=policy_digest(policy),
    )
    api = joined.layers[0]
    assert api.insufficient == 2
    assert tuple(api.reason_counts) == tuple(sorted(api.reason_counts))
    assert "never_run" in api.reason_counts
    assert "uncovered" in api.reason_counts
    assert "execution_stale" in api.reason_counts
    assert tuple(api.execution_state_counts) == ("never_run", "stale", "fresh")
    assert api.execution_state_counts["never_run"] == 1
    assert api.execution_state_counts["stale"] == 1
