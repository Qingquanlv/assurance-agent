"""EvidenceCoverageEvaluation: exclusive success/failure states and stable dump."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from typing import Any, get_args

import pytest

from assurance_agent.artifacts.canonical import canonical_json_bytes
from assurance_agent.artifacts.models.policy import PlanCheckAction
from assurance_agent.artifacts.models.sufficiency import SufficiencyReportV2
from assurance_agent.evidence.sufficiency import (
    EvidenceCoverageErrorCode,
    EvidenceCoverageEvaluation,
)
from tests.helpers_aa import make_report_v2, make_verdict

AS_OF = datetime(2026, 8, 5, 1, 2, 3, 456789, tzinfo=UTC)
ACTIONS: tuple[PlanCheckAction, ...] = get_args(PlanCheckAction)
ERROR_CODES: tuple[EvidenceCoverageErrorCode, ...] = get_args(EvidenceCoverageErrorCode)


def _report(*, sufficient: bool = True, rows: bool = True) -> SufficiencyReportV2:
    verdicts = []
    if rows:
        if sufficient:
            verdicts.append(make_verdict(case_id="TC_API_001"))
        else:
            verdicts.append(
                make_verdict(
                    case_id="TC_API_001",
                    sufficient=False,
                    missing_kinds=["covered", "execution_recent"],
                    reason_codes=["uncovered", "never_run"],
                )
            )
    return SufficiencyReportV2.model_validate(make_report_v2(as_of=AS_OF, verdicts=verdicts))


@pytest.mark.parametrize("action", ACTIONS)
def test_a_success_carries_the_report_and_the_policy_action_verbatim(action: PlanCheckAction) -> None:
    evaluation = EvidenceCoverageEvaluation.evaluated(report=_report(), action=action)

    assert evaluation.action == action
    assert evaluation.report is not None
    assert evaluation.error_code is None


@pytest.mark.parametrize("error_code", ERROR_CODES)
def test_a_failure_carries_only_its_error_code(error_code: EvidenceCoverageErrorCode) -> None:
    evaluation = EvidenceCoverageEvaluation.failed(error_code)

    assert evaluation.error_code == error_code
    assert evaluation.report is None
    assert evaluation.action is None


@pytest.mark.parametrize("action", ACTIONS)
def test_an_insufficient_report_is_still_a_successful_evaluation(action: PlanCheckAction) -> None:
    evaluation = EvidenceCoverageEvaluation.evaluated(report=_report(sufficient=False), action=action)

    assert evaluation.error_code is None
    assert evaluation.report is not None
    assert evaluation.report.all_sufficient is False


def test_an_empty_verdict_list_is_still_a_successful_evaluation() -> None:
    evaluation = EvidenceCoverageEvaluation.evaluated(report=_report(rows=False), action="require_human")

    assert evaluation.error_code is None
    assert evaluation.report is not None
    assert evaluation.report.all_sufficient is True


def test_a_manual_only_change_needs_no_action_from_the_consumer() -> None:
    evaluation = EvidenceCoverageEvaluation.evaluated(report=_report(), action="require_human")

    assert evaluation.report is not None
    assert evaluation.report.all_sufficient is True
    assert evaluation.action == "require_human"


def test_a_success_without_an_action_is_rejected() -> None:
    with pytest.raises(ValueError, match="action"):
        EvidenceCoverageEvaluation(report=_report(), action=None, error_code=None)


def test_a_success_without_a_report_is_rejected() -> None:
    with pytest.raises(ValueError, match="report"):
        EvidenceCoverageEvaluation(report=None, action="warn", error_code=None)


def test_an_empty_evaluation_is_rejected() -> None:
    with pytest.raises(ValueError):
        EvidenceCoverageEvaluation(report=None, action=None, error_code=None)


def test_a_failure_carrying_a_report_is_rejected() -> None:
    with pytest.raises(ValueError, match="report"):
        EvidenceCoverageEvaluation(report=_report(), action=None, error_code="policy_error")


def test_a_failure_carrying_an_action_is_rejected() -> None:
    with pytest.raises(ValueError, match="action"):
        EvidenceCoverageEvaluation(report=None, action="block", error_code="policy_error")


def test_a_fully_populated_evaluation_is_rejected() -> None:
    with pytest.raises(ValueError):
        EvidenceCoverageEvaluation(report=_report(), action="block", error_code="evidence_projection_missing")


def test_the_vocabularies_are_the_ones_their_producers_declare() -> None:
    assert set(ACTIONS) == {"warn", "block", "require_human"}
    assert set(ERROR_CODES) == {"evidence_projection_missing", "policy_error"}


def test_the_evaluation_is_frozen() -> None:
    evaluation = EvidenceCoverageEvaluation.failed("policy_error")

    with pytest.raises(dataclasses.FrozenInstanceError):
        evaluation.error_code = None  # type: ignore[misc]


def _evaluations() -> dict[str, EvidenceCoverageEvaluation]:
    return {
        "sufficient": EvidenceCoverageEvaluation.evaluated(report=_report(), action="warn"),
        "insufficient": EvidenceCoverageEvaluation.evaluated(
            report=_report(sufficient=False), action="block"
        ),
        "policy_error": EvidenceCoverageEvaluation.failed("policy_error"),
        "projection_missing": EvidenceCoverageEvaluation.failed("evidence_projection_missing"),
    }


@pytest.mark.parametrize("name", sorted(_evaluations()))
def test_the_dump_always_carries_the_same_three_keys(name: str) -> None:
    document = _evaluations()[name].to_json_dict()

    assert sorted(document) == ["action", "error_code", "report"]


@pytest.mark.parametrize("name", sorted(_evaluations()))
def test_the_dump_is_json_encodable_and_reproducible(name: str) -> None:
    evaluation = _evaluations()[name]

    assert canonical_json_bytes(evaluation.to_json_dict()) == canonical_json_bytes(evaluation.to_json_dict())


def test_the_dumped_report_is_the_reports_own_json_dump() -> None:
    report = _report(sufficient=False)
    evaluation = EvidenceCoverageEvaluation.evaluated(report=report, action="require_human")

    assert evaluation.to_json_dict()["report"] == report.model_dump(mode="json")


def test_the_dumped_report_keeps_v2_binding_fields() -> None:
    evaluation = EvidenceCoverageEvaluation.evaluated(report=_report(rows=False), action="require_human")
    document: dict[str, Any] = evaluation.to_json_dict()

    assert isinstance(document["report"], dict)
    assert document["report"]["semantics"] == "evidence_sufficiency/v2"
    assert document["report"]["require_current_batch"] is True


@pytest.mark.parametrize("error_code", ERROR_CODES)
def test_a_failure_dumps_nulls_rather_than_a_placeholder_report(
    error_code: EvidenceCoverageErrorCode,
) -> None:
    document = EvidenceCoverageEvaluation.failed(error_code).to_json_dict()

    assert document == {"report": None, "action": None, "error_code": error_code}
