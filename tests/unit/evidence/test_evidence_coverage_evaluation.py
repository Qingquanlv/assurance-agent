"""Task 9 (revised): the one object that travels from the runner to a consumer.

``EvidenceCoverageEvaluation`` is a two-state discriminated union, and these
tests exist because both halves of it are easy to erode:

- **The states are exclusive.** A success carries a report *and* the policy
  action that a consumer will route on; a failure carries an error code and
  nothing else. Every mixture is rejected at construction, so no consumer ever
  has to decide what an evaluation with a report *and* an error code means.
- **The vocabularies are closed.** ``action`` and ``error_code`` both end up in
  persisted artifacts and are what a future gate dispatches on, so a value
  outside either literal raises rather than being stored and later read as "not
  a case I know" — which is how a routing table fails *open*.
- **The dump is stable.** The same object is serialized into
  ``QualityGateResult.diagnostics`` and into ``dimensions.coverage.evidence``,
  so it must always carry the same three keys (never absent-vs-null) and must
  not vary between two runs of the same evaluation.

What is deliberately *not* here: any mapping from an evaluation to a gate
status. Under the accepted M1 architecture case sufficiency does not move
``QualityGateResult.final_status``; the routing lives in the dedicated
``trace-sufficiency-gate`` that Task 11 materializes. This object is the input
that gate will read, which is why it carries ``action`` rather than a verdict.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from typing import Any, get_args

import pytest

from assurance_agent.artifacts.canonical import canonical_json_bytes
from assurance_agent.artifacts.models.policy import PlanCheckAction
from assurance_agent.evidence.sufficiency import (
    EvidenceCoverageErrorCode,
    EvidenceCoverageEvaluation,
    RowVerdict,
    SufficiencyReport,
    TraceIntegrity,
)

AS_OF = datetime(2026, 8, 5, 1, 2, 3, 456789, tzinfo=UTC)
ACTIONS: tuple[PlanCheckAction, ...] = get_args(PlanCheckAction)
ERROR_CODES: tuple[EvidenceCoverageErrorCode, ...] = get_args(EvidenceCoverageErrorCode)


def _report(
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
        change_id="CH-EV-001",
        as_of=AS_OF,
        recency_hours=72,
        integrity=integrity,
        rows=(verdict,) if rows else (),
    )


# --------------------------------------------------------------------------- #
# the two legal states
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("action", ACTIONS)
def test_a_success_carries_the_report_and_the_policy_action_verbatim(action: PlanCheckAction) -> None:
    """``action`` is `policy.evidence_sufficiency.on_insufficient` as written.

    Not a verdict derived from it: the evaluation states what the organisation
    asked for, and the consumer decides what to do with that. Deriving here
    would put one routing table in the producer and another in the gate.
    """
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
    """ "Evaluated" and "sufficient" are different questions.

    An insufficient report is a conclusion, not a failure to conclude; only a
    fold or policy that could not run at all is the failure state. Collapsing
    the two would make `error_code` unusable for telling "the evidence is thin"
    apart from "nothing was judged".
    """
    evaluation = EvidenceCoverageEvaluation.evaluated(report=_report(sufficient=False), action=action)

    assert evaluation.error_code is None
    assert evaluation.report is not None
    assert evaluation.report.sufficient is False


@pytest.mark.parametrize("integrity", ["complete_with_gaps", "incomplete"])
def test_an_incomplete_projection_is_still_a_successful_evaluation(integrity: TraceIntegrity) -> None:
    """Integrity travels *inside* the report; it is not an evaluation error.

    Task 7 established the obligation this preserves: a consumer must judge
    `report.integrity` **before** `report.sufficient`, because a projection that
    could not read its inputs makes a vacuous "all rows sufficient" verdict
    (`rows=()` is sufficient by definition). Turning incomplete integrity into
    an `error_code` here would hide that obligation behind a code that says
    "not evaluated", which is false — it *was* evaluated, on partial inputs.
    """
    evaluation = EvidenceCoverageEvaluation.evaluated(
        report=_report(integrity=integrity, rows=False), action="require_human"
    )

    assert evaluation.error_code is None
    assert evaluation.report is not None
    assert evaluation.report.integrity == integrity
    assert evaluation.report.sufficient is True, "the premise: zero rows are vacuously sufficient"


@pytest.mark.parametrize(
    ("integrity", "blocks"),
    [("complete", False), ("complete_with_gaps", False), ("incomplete", True)],
)
def test_the_routing_obligation_reaches_a_consumer_through_the_evaluation(
    integrity: TraceIntegrity, blocks: bool
) -> None:
    """A consumer holding only this object can check the ordering obligation.

    `integrity_blocks_routing` is what a consumer MUST branch on before
    `sufficient`; asserting it here rather than only on `SufficiencyReport` pins
    that the evaluation does not hide or flatten it on the way through.
    """
    evaluation = EvidenceCoverageEvaluation.evaluated(
        report=_report(integrity=integrity, rows=False), action="require_human"
    )

    assert evaluation.report is not None
    assert evaluation.report.integrity_blocks_routing is blocks


def test_a_manual_only_change_needs_no_action_from_the_consumer() -> None:
    """Task 7's exemption survives into the object a gate reads.

    A case with `automation.required=false` is a row state, not a gap (spec
    §10), so `evaluate_sufficiency` never marks it insufficient. This
    evaluation therefore reaches a consumer as a *sufficient* report even under
    the default `require_human`, and nothing downstream may re-derive a gap from
    the presence of an action. The action is what to do *if* a row falls short.
    """
    evaluation = EvidenceCoverageEvaluation.evaluated(report=_report(), action="require_human")

    assert evaluation.report is not None
    assert evaluation.report.insufficient_rows == ()
    assert evaluation.action == "require_human"


# --------------------------------------------------------------------------- #
# every mixture is rejected
# --------------------------------------------------------------------------- #


def test_a_success_without_an_action_is_rejected() -> None:
    with pytest.raises(ValueError, match="action"):
        EvidenceCoverageEvaluation(report=_report(), action=None, error_code=None)


def test_a_success_without_a_report_is_rejected() -> None:
    with pytest.raises(ValueError, match="report"):
        EvidenceCoverageEvaluation(report=None, action="warn", error_code=None)


def test_an_empty_evaluation_is_rejected() -> None:
    """Neither state: nothing concluded and no reason why."""
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


# --------------------------------------------------------------------------- #
# closed vocabularies (a routing input must not fail open)
# --------------------------------------------------------------------------- #


def test_an_error_code_outside_the_vocabulary_is_rejected() -> None:
    """The codes are what a gate dispatches on. An unknown one stored today is
    an unmatched branch tomorrow, i.e. a failure that routes as a success."""
    with pytest.raises(ValueError, match="error_code"):
        EvidenceCoverageEvaluation(report=None, action=None, error_code="disk_on_fire")  # type: ignore[arg-type]


def test_an_action_outside_the_vocabulary_is_rejected() -> None:
    with pytest.raises(ValueError, match="action"):
        EvidenceCoverageEvaluation(report=_report(), action="maybe", error_code=None)  # type: ignore[arg-type]


def test_the_vocabularies_are_the_ones_their_producers_declare() -> None:
    """`action` is the policy's own literal and the error codes are the two the
    runner can produce — spelled here so a widened literal is visible."""
    assert set(ACTIONS) == {"warn", "block", "require_human"}
    assert set(ERROR_CODES) == {"evidence_projection_missing", "policy_error"}


def test_the_evaluation_is_frozen() -> None:
    evaluation = EvidenceCoverageEvaluation.failed("policy_error")

    with pytest.raises(dataclasses.FrozenInstanceError):
        evaluation.error_code = None  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# the dumped shape (persisted twice, so it must be one shape)
# --------------------------------------------------------------------------- #


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
    """Absent and null must not both be reachable: a reader of the persisted
    document would otherwise need two ways to ask the same question."""
    document = _evaluations()[name].to_json_dict()

    assert sorted(document) == ["action", "error_code", "report"]


@pytest.mark.parametrize("name", sorted(_evaluations()))
def test_the_dump_is_json_encodable_and_reproducible(name: str) -> None:
    """It is persisted into a gate artifact that reviewers compare byte for
    byte, so it must round-trip through the canonical encoder unchanged."""
    evaluation = _evaluations()[name]

    assert canonical_json_bytes(evaluation.to_json_dict()) == canonical_json_bytes(evaluation.to_json_dict())


def test_the_dumped_report_is_the_reports_own_json_dump() -> None:
    """No second serialisation of `SufficiencyReport` lives here: a summarised
    copy would drift from the report `aa verify` reads."""
    report = _report(sufficient=False)
    evaluation = EvidenceCoverageEvaluation.evaluated(report=report, action="require_human")

    assert evaluation.to_json_dict()["report"] == report.model_dump(mode="json")


def test_the_dumped_report_keeps_integrity_where_a_consumer_must_route_it_first() -> None:
    evaluation = EvidenceCoverageEvaluation.evaluated(
        report=_report(integrity="incomplete", rows=False), action="require_human"
    )
    document: dict[str, Any] = evaluation.to_json_dict()

    assert isinstance(document["report"], dict)
    assert document["report"]["integrity"] == "incomplete"


@pytest.mark.parametrize("error_code", ERROR_CODES)
def test_a_failure_dumps_nulls_rather_than_a_placeholder_report(
    error_code: EvidenceCoverageErrorCode,
) -> None:
    document = EvidenceCoverageEvaluation.failed(error_code).to_json_dict()

    assert document == {"report": None, "action": None, "error_code": error_code}
