"""`inspect/trace-sufficiency.json` — the facts the trace gate routes on.

The document is the whole interface between the materialize operation and the
independent ``trace-sufficiency-gate``: the operation states facts, the gate's
DSL maps them onto a verdict. So the tests here are about what the *document*
can and cannot say — a route it cannot carry, an ordering it cannot contradict,
and a vocabulary that cannot drift away from the evidence layer that produces
the values.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import get_args

import pytest
from pydantic import ValidationError

from assurance_agent.artifacts.models import TraceSufficiencyFacts
from assurance_agent.artifacts.models.trace import TraceGapCodeV1, TraceIntegrity
from assurance_agent.artifacts.models.trace_sufficiency import (
    TraceInsufficientCase,
    TraceSufficiencyErrorCode,
    TraceSufficiencyReasonCode,
)
from assurance_agent.artifacts.registry import match_artifact
from assurance_agent.evidence.sufficiency import (
    EvidenceCoverageErrorCode,
    SufficiencyReasonCode,
)

AS_OF = datetime(2026, 7, 2, 11, 11, 11, tzinfo=UTC)


def _facts(**overrides: object) -> TraceSufficiencyFacts:
    payload: dict[str, object] = {
        "schema_version": "1",
        "change_id": "CH-1",
        "authoritative_batch_id": "20260702-111111",
        "policy_digest": "0" * 64,
        "as_of": AS_OF,
        "integrity": "complete",
        "integrity_blocks_routing": False,
        "sufficient": True,
        "has_open_problems": False,
        "error_code": None,
        "insufficient_cases": (),
        "gap_codes": (),
    }
    payload.update(overrides)
    return TraceSufficiencyFacts(**payload)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# registry
# --------------------------------------------------------------------------- #


def test_the_facts_document_is_registered_must_compat() -> None:
    spec = match_artifact("inspect/trace-sufficiency.json")
    assert spec is not None
    assert spec.artifact_type == "trace_sufficiency_facts"
    assert spec.model is TraceSufficiencyFacts
    # must_compat, not versioned: a gate routes on these fields, so a document
    # this release cannot fully validate must be refused rather than read
    # partially and routed as a pass.
    assert spec.compat == "must_compat"
    assert spec.authoring_model is None


def test_the_batch_scoped_projection_path_is_not_mistaken_for_the_facts() -> None:
    """The runner publishes `execution/runs/<batch>/trace-projection.json`; only
    the change-level inspect path carries the gate's facts."""
    assert match_artifact("inspect/trace-projection.json") is not None
    assert match_artifact("execution/runs/20260702-111111/trace-sufficiency.json") is None


# --------------------------------------------------------------------------- #
# facts, not a route
# --------------------------------------------------------------------------- #


def test_the_document_carries_no_verdict_route_or_action_field() -> None:
    """Routing belongs to the gate DSL, so the document may not precompute it.

    A ``verdict``/``route`` field here would put one routing table in the
    producer and a second in the gate, and the two would drift.
    """
    forbidden = {"verdict", "route", "disposition", "final_status", "quality_gate"}
    assert forbidden.isdisjoint(TraceSufficiencyFacts.model_fields)


def test_every_fact_the_gate_routes_on_is_declared() -> None:
    assert set(TraceSufficiencyFacts.model_fields) >= {
        "schema_version",
        "change_id",
        "authoritative_batch_id",
        "policy_digest",
        "integrity",
        "integrity_blocks_routing",
        "sufficient",
        "has_open_problems",
        "error_code",
        "insufficient_cases",
        "gap_codes",
    }


def test_an_unknown_field_is_refused() -> None:
    with pytest.raises(ValidationError):
        _facts(verdict="pass")


def test_the_document_is_immutable() -> None:
    facts = _facts()
    with pytest.raises(ValidationError):
        facts.sufficient = False  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# integrity is judged before sufficiency, and the document cannot deny it
# --------------------------------------------------------------------------- #


def test_incomplete_integrity_must_declare_that_it_blocks_routing() -> None:
    """The ordering obligation is a checked fact, not a convention.

    ``incomplete`` means an input could not be read, so the row verdicts are not
    about this change's row set — most sharply when the fold read nothing and
    ``sufficient`` is vacuously true. A document claiming otherwise would let a
    gate route that vacuous success as a pass.
    """
    with pytest.raises(ValidationError):
        _facts(integrity="incomplete", integrity_blocks_routing=False)


def test_complete_integrity_must_not_claim_to_block_routing() -> None:
    with pytest.raises(ValidationError):
        _facts(integrity="complete", integrity_blocks_routing=True)


@pytest.mark.parametrize("integrity", ["complete", "complete_with_gaps"])
def test_the_two_readable_integrity_levels_do_not_block(integrity: str) -> None:
    assert _facts(integrity=integrity, integrity_blocks_routing=False).integrity == integrity


def test_incomplete_integrity_blocks_routing() -> None:
    assert _facts(integrity="incomplete", integrity_blocks_routing=True).integrity_blocks_routing


# --------------------------------------------------------------------------- #
# an error state judged nothing
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("code", get_args(TraceSufficiencyErrorCode))
def test_an_error_state_may_not_also_claim_sufficiency(code: str) -> None:
    """``sufficient`` is a verdict; a document that reached no verdict must not
    assert one, or a gate reading ``sufficient`` alone would pass on it."""
    with pytest.raises(ValidationError):
        _facts(error_code=code, sufficient=True)


def test_an_error_state_is_constructible_with_sufficient_false() -> None:
    facts = _facts(
        error_code="policy_error",
        sufficient=False,
        policy_digest=None,
        as_of=None,
    )
    assert facts.error_code == "policy_error"
    assert facts.policy_digest is None


def test_an_unknown_error_code_is_refused() -> None:
    with pytest.raises(ValidationError):
        _facts(error_code="oops", sufficient=False)


# --------------------------------------------------------------------------- #
# insufficient cases carry their reasons
# --------------------------------------------------------------------------- #


def test_insufficient_cases_carry_case_ids_and_reason_codes() -> None:
    facts = _facts(
        sufficient=False,
        insufficient_cases=(
            TraceInsufficientCase(case_id="TC_API_001", reason_codes=("uncovered", "never_run")),
        ),
    )
    assert facts.insufficient_cases[0].case_id == "TC_API_001"
    assert facts.insufficient_cases[0].reason_codes == ("uncovered", "never_run")


def test_a_sufficient_document_may_not_list_insufficient_cases() -> None:
    with pytest.raises(ValidationError):
        _facts(
            sufficient=True,
            insufficient_cases=(TraceInsufficientCase(case_id="TC_API_001", reason_codes=("never_run",)),),
        )


def test_an_unknown_reason_code_is_refused() -> None:
    with pytest.raises(ValidationError):
        TraceInsufficientCase(case_id="TC_API_001", reason_codes=("vibes",))  # type: ignore[arg-type]


def test_an_unknown_gap_code_is_refused() -> None:
    with pytest.raises(ValidationError):
        _facts(gap_codes=("result_vanished",))


def test_gap_codes_are_recorded_in_the_projection_vocabulary() -> None:
    facts = _facts(
        integrity="complete_with_gaps",
        gap_codes=("mapped_test_missing_from_tree",),
    )
    assert facts.gap_codes == ("mapped_test_missing_from_tree",)


# --------------------------------------------------------------------------- #
# the vocabularies cannot drift from the evidence layer that fills them
# --------------------------------------------------------------------------- #


def test_the_reason_vocabulary_tracks_the_sufficiency_report() -> None:
    """Spelled out in the artifact layer (which must not import evidence), so
    the two literals are pinned equal here instead of shared by import."""
    assert set(get_args(TraceSufficiencyReasonCode)) == set(get_args(SufficiencyReasonCode))


def test_the_error_vocabulary_tracks_the_evaluation() -> None:
    assert set(get_args(TraceSufficiencyErrorCode)) == set(get_args(EvidenceCoverageErrorCode))


def test_the_integrity_vocabulary_is_the_projections_own() -> None:
    assert TraceSufficiencyFacts.model_fields["integrity"].annotation is TraceIntegrity


def test_the_gap_vocabulary_is_the_projections_own() -> None:
    assert get_args(TraceGapCodeV1)
    assert _facts(gap_codes=tuple(get_args(TraceGapCodeV1))[:1]).gap_codes


# --------------------------------------------------------------------------- #
# wire format
# --------------------------------------------------------------------------- #


def test_a_naive_as_of_is_refused() -> None:
    """The instant these verdicts were judged at must carry a zone, or a stored
    document is uninterpretable — the same rows are sufficient or not depending
    on a cutoff nothing else records."""
    with pytest.raises(ValidationError):
        _facts(as_of=datetime(2026, 7, 2, 11, 11, 11))


def test_the_json_dump_round_trips() -> None:
    facts = _facts(
        integrity="complete_with_gaps",
        sufficient=False,
        gap_codes=("mapped_test_missing_from_tree",),
        insufficient_cases=(TraceInsufficientCase(case_id="TC_API_001", reason_codes=("never_run",)),),
    )
    assert TraceSufficiencyFacts.model_validate(facts.model_dump(mode="json")) == facts
