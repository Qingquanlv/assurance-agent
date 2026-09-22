from __future__ import annotations

import pytest
from pydantic import ValidationError

from assurance_intake.contracts.workflow import EvidenceArtifactRefV1
from assurance_quality.contracts.obligations import ObligationEvidenceFactsV1
from assurance_quality.operations.obligations import decide_obligation

_COUNTEREXAMPLE = EvidenceArtifactRefV1(
    path="qa/results/execution/epochs/0/batches/B-1/runtime-observations.json",
    digest="a" * 64,
)


def _valid_counterexample(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "expectation_confirmed": True,
        "eligible_counterexample": True,
        "subject_valid": True,
        "method_valid": True,
        "prerequisites_valid": True,
        "observations_complete": False,
        "required_reviews_passed": True,
        "supporting_evidence_current": False,
        "counterexample_evidence_current": True,
        "counterexample_refs": (_COUNTEREXAMPLE,),
    }
    payload.update(overrides)
    return payload


def test_valid_counterexample_survives_unrelated_stale_support() -> None:
    facts = ObligationEvidenceFactsV1.model_validate(_valid_counterexample())
    assert decide_obligation(facts) == "refuted"


@pytest.mark.parametrize(
    "field",
    (
        "subject_valid",
        "expectation_confirmed",
        "method_valid",
        "prerequisites_valid",
        "required_reviews_passed",
        "counterexample_evidence_current",
    ),
)
def test_eligible_counterexample_rejects_invalid_qualification(field: str) -> None:
    with pytest.raises(ValidationError):
        ObligationEvidenceFactsV1.model_validate(_valid_counterexample(**{field: False}))


def test_eligible_counterexample_rejects_empty_refs() -> None:
    with pytest.raises(ValidationError):
        ObligationEvidenceFactsV1.model_validate(_valid_counterexample(counterexample_refs=()))


def test_supported_requires_complete_current_evidence() -> None:
    facts = ObligationEvidenceFactsV1.model_validate(
        {
            "expectation_confirmed": True,
            "eligible_counterexample": False,
            "subject_valid": True,
            "method_valid": True,
            "prerequisites_valid": True,
            "observations_complete": True,
            "required_reviews_passed": True,
            "supporting_evidence_current": True,
            "counterexample_evidence_current": False,
            "counterexample_refs": (),
        }
    )
    assert decide_obligation(facts) == "supported"


def test_missing_observation_is_inconclusive_not_refuted() -> None:
    facts = ObligationEvidenceFactsV1.model_validate(
        {
            "expectation_confirmed": True,
            "eligible_counterexample": False,
            "subject_valid": True,
            "method_valid": True,
            "prerequisites_valid": True,
            "observations_complete": False,
            "required_reviews_passed": True,
            "supporting_evidence_current": True,
            "counterexample_evidence_current": False,
            "counterexample_refs": (),
        }
    )
    assert decide_obligation(facts) == "inconclusive"
