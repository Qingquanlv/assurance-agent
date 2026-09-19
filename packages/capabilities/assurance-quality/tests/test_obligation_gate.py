from __future__ import annotations

import pytest
from pydantic import ValidationError

from assurance_intake.contracts.workflow import EvidenceArtifactRefV1
from assurance_quality.contracts.obligations import (
    ObligationAssessmentRowV1,
    ObligationAssessmentV1,
    ObligationGateFactsV1,
)
from assurance_quality.operations.common import InputError
from assurance_quality.contracts.obligations import obligation_gate
from assurance_quality.operations.obligations import derive_obligation_gate_facts

_PLAN = "a" * 64
_REF = EvidenceArtifactRefV1(path="qa/results/plan/resolved-assurance-plan.json", digest=_PLAN)


def _facts(**overrides: int) -> ObligationGateFactsV1:
    payload = {
        "required_count": 1,
        "supported_count": 1,
        "refuted_count": 0,
        "inconclusive_count": 0,
        "repairable_gap_count": 0,
        "human_gap_count": 0,
    }
    payload.update(overrides)
    return ObligationGateFactsV1.model_validate(payload)


def _row(
    mrc_id: str,
    *,
    verdict: str = "supported",
    plan_digest: str = _PLAN,
    gap_codes: tuple[str, ...] = (),
) -> ObligationAssessmentRowV1:
    return ObligationAssessmentRowV1(
        plan_digest=plan_digest,
        mrc_id=mrc_id,
        verdict=verdict,  # type: ignore[arg-type]
        gap_codes=gap_codes,
    )


def _assessment(*rows: ObligationAssessmentRowV1, excluded: tuple[str, ...] = ()) -> ObligationAssessmentV1:
    return ObligationAssessmentV1(plan_ref=_REF, rows=rows, excluded_mrc_ids=excluded)


def test_empty_scope_is_not_vacuously_satisfied() -> None:
    facts = ObligationGateFactsV1(
        required_count=0,
        supported_count=0,
        refuted_count=0,
        inconclusive_count=0,
        repairable_gap_count=0,
        human_gap_count=0,
    )
    assert obligation_gate(facts) == "blocked"


def test_supported_scope_is_satisfied() -> None:
    assert obligation_gate(_facts()) == "satisfied"


def test_refuted_obligation_blocks_delivery() -> None:
    facts = _facts(supported_count=0, refuted_count=1)
    assert obligation_gate(facts) == "blocked"


def test_repairable_gap_requires_rework() -> None:
    facts = _facts(supported_count=0, inconclusive_count=1, repairable_gap_count=1)
    assert obligation_gate(facts) == "repair_required"


def test_human_gap_does_not_enter_test_repair() -> None:
    facts = _facts(supported_count=0, inconclusive_count=1, human_gap_count=1)
    assert obligation_gate(facts) == "needs_human"


def test_unclassified_inconclusive_is_blocked() -> None:
    facts = _facts(supported_count=0, inconclusive_count=1)
    assert obligation_gate(facts) == "blocked"


def test_human_gap_outranks_repairable_on_the_same_batch() -> None:
    facts = _facts(
        required_count=2,
        supported_count=0,
        inconclusive_count=2,
        repairable_gap_count=1,
        human_gap_count=1,
    )
    assert obligation_gate(facts) == "needs_human"


@pytest.mark.parametrize(
    "payload",
    (
        {"required_count": -1, "supported_count": 0, "refuted_count": 0, "inconclusive_count": 0},
        {"required_count": 2, "supported_count": 1, "refuted_count": 0, "inconclusive_count": 0},
        {
            "required_count": 1,
            "supported_count": 0,
            "refuted_count": 0,
            "inconclusive_count": 1,
            "repairable_gap_count": 2,
        },
    ),
)
def test_gate_facts_reject_negative_or_inconsistent_counts(payload: dict[str, int]) -> None:
    with pytest.raises(ValidationError):
        _facts(**payload)


def test_aggregate_coverage_cannot_hide_a_missing_obligation() -> None:
    assessment = _assessment(
        _row("MRC-API-1"),
        _row("MRC-API-2", verdict="inconclusive", gap_codes=("obligation_observation_missing",)),
    )
    facts = derive_obligation_gate_facts(
        assessment,
        required_ids=((_PLAN, "MRC-API-1"), (_PLAN, "MRC-API-2")),
    )
    assert facts.required_count == 2
    assert facts.supported_count == 1
    assert facts.repairable_gap_count == 1
    assert obligation_gate(facts) == "repair_required"


def test_derive_rejects_duplicate_required_ids() -> None:
    with pytest.raises(InputError, match="not unique"):
        derive_obligation_gate_facts(
            _assessment(_row("MRC-API-1")),
            required_ids=((_PLAN, "MRC-API-1"), (_PLAN, "MRC-API-1")),
        )


def test_derive_rejects_missing_or_extra_rows() -> None:
    with pytest.raises(InputError, match="do not match"):
        derive_obligation_gate_facts(
            _assessment(_row("MRC-API-1")),
            required_ids=((_PLAN, "MRC-API-1"), (_PLAN, "MRC-API-2")),
        )


def test_derive_rejects_duplicate_assessment_rows() -> None:
    with pytest.raises(InputError, match="not unique"):
        derive_obligation_gate_facts(
            _assessment(_row("MRC-API-1"), _row("MRC-API-1")),
            required_ids=((_PLAN, "MRC-API-1"),),
        )


def test_derive_rejects_cross_plan_same_mrc_name() -> None:
    other = "b" * 64
    with pytest.raises(InputError, match="do not match"):
        derive_obligation_gate_facts(
            _assessment(_row("MRC-API-1", plan_digest=other)),
            required_ids=((_PLAN, "MRC-API-1"),),
        )


def test_derive_rejects_excluded_overlap() -> None:
    with pytest.raises(InputError, match="overlap"):
        derive_obligation_gate_facts(
            _assessment(_row("MRC-API-1"), excluded=("MRC-API-1",)),
            required_ids=((_PLAN, "MRC-API-1"),),
        )


def test_multiple_gap_codes_count_the_obligation_once() -> None:
    facts = derive_obligation_gate_facts(
        _assessment(
            _row(
                "MRC-API-1",
                verdict="inconclusive",
                gap_codes=("obligation_case_missing", "obligation_observation_missing"),
            )
        ),
        required_ids=((_PLAN, "MRC-API-1"),),
    )
    assert facts.repairable_gap_count == 1
    assert facts.human_gap_count == 0
    assert obligation_gate(facts) == "repair_required"


def test_human_reason_outranks_repairable_on_the_same_obligation() -> None:
    facts = derive_obligation_gate_facts(
        _assessment(
            _row(
                "MRC-API-1",
                verdict="inconclusive",
                gap_codes=("obligation_case_missing", "expectation_unconfirmed"),
            )
        ),
        required_ids=((_PLAN, "MRC-API-1"),),
    )
    assert facts.human_gap_count == 1
    assert facts.repairable_gap_count == 0
    assert obligation_gate(facts) == "needs_human"
