"""Closed-model tests for Sufficiency V2 DTOs and layer sufficiency summaries."""

from __future__ import annotations

from datetime import datetime
from itertools import product
from typing import Any

import pytest
from pydantic import ValidationError

from assurance_agent.artifacts.models.policy import EvidenceKind
from assurance_agent.artifacts.models.sufficiency import (
    EXECUTION_STATES,
    SUFFICIENCY_REASON_CODES,
    LayerSufficiencyCounts,
    SufficiencyReasonCode,
    SufficiencyReportV2,
    SufficiencyRowVerdictV2,
    TraceLayerSufficiencySummary,
)
from tests.helpers_aa import AWARE_NOW, make_layer_counts, make_report_v2, make_verdict

_ALLOWED_KIND_REASONS: tuple[tuple[str, str], ...] = (
    ("covered", "uncovered"),
    ("execution_recent", "not_in_current_batch"),
    ("execution_recent", "never_run"),
    ("execution_recent", "execution_stale"),
    ("fuzz_run", "not_in_current_batch"),
    ("fuzz_run", "fuzz_run_missing"),
    ("perf_run", "not_in_current_batch"),
    ("perf_run", "perf_run_missing"),
    ("pass_status", "not_in_current_batch"),
    ("pass_status", "no_pass"),
    ("pass_status", "pass_stale"),
)

_ALL_KINDS: tuple[EvidenceKind, ...] = (
    "covered",
    "execution_recent",
    "fuzz_run",
    "perf_run",
    "pass_status",
)


@pytest.mark.parametrize(("kind", "reason"), _ALLOWED_KIND_REASONS)
def test_sufficiency_v2_accepts_declared_kind_reason_pairs(
    kind: str,
    reason: str,
) -> None:
    verdict = make_verdict(missing_kinds=[kind], reason_codes=[reason])
    assert SufficiencyRowVerdictV2.model_validate(verdict).sufficient is False


def test_sufficient_true_requires_empty_missing_and_reasons() -> None:
    assert SufficiencyRowVerdictV2.model_validate(make_verdict()).sufficient is True
    with pytest.raises(ValidationError):
        SufficiencyRowVerdictV2.model_validate(
            make_verdict(sufficient=True, missing_kinds=["covered"], reason_codes=["uncovered"])
        )
    with pytest.raises(ValidationError):
        SufficiencyRowVerdictV2.model_validate(
            make_verdict(sufficient=False, missing_kinds=[], reason_codes=[])
        )


def test_missing_and_reason_lengths_must_match() -> None:
    with pytest.raises(ValidationError):
        SufficiencyRowVerdictV2.model_validate(
            make_verdict(
                sufficient=False,
                missing_kinds=["covered", "execution_recent"],
                reason_codes=["uncovered"],
                execution_state="fresh",
            )
        )


def test_unknown_reason_code_is_rejected() -> None:
    with pytest.raises(ValidationError):
        SufficiencyRowVerdictV2.model_validate(
            make_verdict(missing_kinds=["covered"], reason_codes=["not_a_reason"])
        )


@pytest.mark.parametrize(
    ("kind", "reason"),
    [
        (kind, reason)
        for kind, reason in product(_ALL_KINDS, SUFFICIENCY_REASON_CODES)
        if (kind, reason) not in _ALLOWED_KIND_REASONS
    ],
)
def test_undeclared_kind_reason_pairs_are_rejected(kind: str, reason: str) -> None:
    with pytest.raises(ValidationError):
        SufficiencyRowVerdictV2.model_validate(make_verdict(missing_kinds=[kind], reason_codes=[reason]))


def test_duplicate_missing_kinds_are_rejected() -> None:
    with pytest.raises(ValidationError):
        SufficiencyRowVerdictV2.model_validate(
            make_verdict(
                missing_kinds=["covered", "covered"],
                reason_codes=["uncovered", "uncovered"],
            )
        )


def test_execution_recent_never_run_requires_never_run_state() -> None:
    with pytest.raises(ValidationError):
        SufficiencyRowVerdictV2.model_validate(
            {
                "case_id": "TC_API_001",
                "sufficient": False,
                "missing_kinds": ["execution_recent"],
                "reason_codes": ["never_run"],
                "execution_state": "stale",
            }
        )


def test_execution_recent_stale_requires_stale_state() -> None:
    with pytest.raises(ValidationError):
        SufficiencyRowVerdictV2.model_validate(
            {
                "case_id": "TC_API_001",
                "sufficient": False,
                "missing_kinds": ["execution_recent"],
                "reason_codes": ["execution_stale"],
                "execution_state": "fresh",
            }
        )


def test_strict_bool_rejects_int_coercion() -> None:
    with pytest.raises(ValidationError):
        SufficiencyRowVerdictV2.model_validate({**make_verdict(), "sufficient": 1})


def test_report_rejects_naive_as_of() -> None:
    with pytest.raises(ValidationError):
        SufficiencyReportV2.model_validate(make_report_v2(as_of=datetime(2026, 7, 30, 12, 0, 0)))


def test_report_rejects_nonpositive_recency() -> None:
    with pytest.raises(ValidationError):
        SufficiencyReportV2.model_validate(make_report_v2(recency_hours=0))
    with pytest.raises(ValidationError):
        SufficiencyReportV2.model_validate(make_report_v2(recency_hours=-1))


def test_report_rejects_duplicate_case_ids() -> None:
    with pytest.raises(ValidationError):
        SufficiencyReportV2.model_validate(
            make_report_v2(
                verdicts=[
                    make_verdict(case_id="TC_DUP"),
                    make_verdict(case_id="TC_DUP"),
                ]
            )
        )


def test_report_accepts_aware_as_of_and_positive_recency() -> None:
    report = SufficiencyReportV2.model_validate(make_report_v2())
    assert report.as_of == AWARE_NOW
    assert report.recency_hours == 72
    assert report.schema_version == "2.0"
    assert report.semantics == "evidence_sufficiency/v2"
    assert report.all_sufficient is True


def test_layer_counts_reject_zero_reason_count_entry() -> None:
    payload: dict[str, Any] = make_layer_counts(
        reason_counts={"uncovered": 1},
        sufficient=0,
        insufficient=1,
        execution_state_counts={"never_run": 0, "stale": 0, "fresh": 1},
    )
    payload["reason_counts"] = {"uncovered": 0}
    with pytest.raises(ValidationError):
        LayerSufficiencyCounts.model_validate(payload)


def test_layer_counts_require_all_execution_state_keys_including_zeros() -> None:
    ok = LayerSufficiencyCounts.model_validate(
        make_layer_counts(
            sufficient=1,
            insufficient=0,
            reason_counts={},
            execution_state_counts={"never_run": 0, "stale": 0, "fresh": 1},
        )
    )
    assert tuple(ok.execution_state_counts) == EXECUTION_STATES
    assert ok.execution_state_counts["never_run"] == 0

    with pytest.raises(ValidationError):
        LayerSufficiencyCounts.model_validate(
            make_layer_counts(
                sufficient=1,
                insufficient=0,
                reason_counts={},
                execution_state_counts={"fresh": 1},
            )
        )


def test_layer_counts_reject_wrong_execution_state_key_order() -> None:
    with pytest.raises(ValidationError):
        LayerSufficiencyCounts.model_validate(
            make_layer_counts(
                sufficient=1,
                insufficient=0,
                reason_counts={},
                execution_state_counts={"fresh": 1, "never_run": 0, "stale": 0},
            )
        )


def test_layer_counts_reject_non_lexical_reason_order() -> None:
    with pytest.raises(ValidationError):
        LayerSufficiencyCounts.model_validate(
            make_layer_counts(
                sufficient=0,
                insufficient=2,
                reason_counts={"uncovered": 1, "execution_stale": 1},
                execution_state_counts={"never_run": 0, "stale": 1, "fresh": 1},
            )
        )


def test_layer_counts_conservation() -> None:
    with pytest.raises(ValidationError):
        LayerSufficiencyCounts.model_validate(
            make_layer_counts(
                sufficient=2,
                insufficient=0,
                reason_counts={},
                execution_state_counts={"never_run": 0, "stale": 0, "fresh": 1},
            )
        )


def test_summary_requires_exact_four_layer_order() -> None:
    layers = [
        make_layer_counts(layer="api", case_type="API"),
        make_layer_counts(layer="e2e", case_type="E2E"),
        make_layer_counts(layer="fuzz", case_type="Fuzz"),
        make_layer_counts(layer="performance", case_type="Performance"),
    ]
    ok = TraceLayerSufficiencySummary.model_validate(
        {
            "schema_version": "1",
            "source_projection_digest": "a" * 64,
            "source_policy_digest": "b" * 64,
            "semantics": "evidence_sufficiency/v2",
            "require_current_batch": True,
            "as_of": AWARE_NOW,
            "recency_hours": 72,
            "layers": layers,
        }
    )
    assert [row.layer for row in ok.layers] == ["api", "e2e", "fuzz", "performance"]

    swapped = list(layers)
    swapped[0], swapped[1] = swapped[1], swapped[0]
    with pytest.raises(ValidationError):
        TraceLayerSufficiencySummary.model_validate(
            {
                "schema_version": "1",
                "source_projection_digest": "a" * 64,
                "source_policy_digest": "b" * 64,
                "semantics": "evidence_sufficiency/v2",
                "require_current_batch": True,
                "as_of": AWARE_NOW,
                "recency_hours": 72,
                "layers": swapped,
            }
        )


def test_summary_require_current_batch_must_be_true() -> None:
    with pytest.raises(ValidationError):
        TraceLayerSufficiencySummary.model_validate(
            {
                "schema_version": "1",
                "source_projection_digest": "a" * 64,
                "source_policy_digest": "b" * 64,
                "semantics": "evidence_sufficiency/v2",
                "require_current_batch": False,
                "as_of": AWARE_NOW,
                "recency_hours": 72,
                "layers": [
                    make_layer_counts(layer="api", case_type="API"),
                    make_layer_counts(layer="e2e", case_type="E2E"),
                    make_layer_counts(layer="fuzz", case_type="Fuzz"),
                    make_layer_counts(layer="performance", case_type="Performance"),
                ],
            }
        )


def test_reason_code_literal_surface() -> None:
    assert set(SUFFICIENCY_REASON_CODES) == {
        "not_in_current_batch",
        "uncovered",
        "never_run",
        "execution_stale",
        "fuzz_run_missing",
        "perf_run_missing",
        "no_pass",
        "pass_stale",
    }
    _: SufficiencyReasonCode = "uncovered"
