"""Tests for deterministic Improvement identity helpers."""

from __future__ import annotations

import hashlib
import json
import unicodedata

import pytest

from assurance_agent.artifacts.models.improvements import (
    DeliveryKind,
    ImprovementCandidate,
    ImprovementKind,
    ImprovementSourceRefs,
    ImprovementVerification,
)
from assurance_agent.workflow.improvements.identity import (
    improvement_event_id,
    improvement_fingerprint,
    improvement_id_for_fingerprint,
)


@pytest.fixture
def candidate() -> ImprovementCandidate:
    return ImprovementCandidate(
        candidate_id="IMP-CAND-1",
        kind=ImprovementKind.WORKFLOW,
        delivery=DeliveryKind.CHANGE_DRAFT,
        source_refs=ImprovementSourceRefs(problem_ids=("PROB-1", "PROB-2")),
        target="assurance_agent/workflow/inspect",
        rationale="Repeated truncation across changes",
        proposed_change="Preserve pytest E lines when inspect truncates",
        verification=ImprovementVerification(
            suites=("workflow-full",),
            success_criteria="No truncation Observation",
        ),
        risk="medium",
        confidence="high",
    )


def test_fingerprint_ignores_retro_metadata_and_source_order(
    candidate: ImprovementCandidate,
) -> None:
    reordered = candidate.model_copy(
        update={
            "source_refs": candidate.source_refs.model_copy(
                update={"problem_ids": tuple(reversed(candidate.source_refs.problem_ids))}
            ),
            "rationale": "different wording",
            "confidence": "low",
        }
    )
    assert improvement_fingerprint(candidate) == improvement_fingerprint(reordered)


def test_fingerprint_ignores_risk_and_verification(candidate: ImprovementCandidate) -> None:
    noisy = candidate.model_copy(
        update={
            "risk": "high",
            "verification": ImprovementVerification(
                suites=("other-suite",),
                required_cases=("CASE-1",),
                success_criteria="Different criteria",
            ),
        }
    )
    assert improvement_fingerprint(candidate) == improvement_fingerprint(noisy)


def test_fingerprint_changes_with_kind_delivery_target_intent_version(
    candidate: ImprovementCandidate,
) -> None:
    baseline = improvement_fingerprint(candidate)
    assert improvement_fingerprint(candidate.model_copy(update={"kind": ImprovementKind.TEST})) != baseline
    assert (
        improvement_fingerprint(
            candidate.model_copy(
                update={
                    "kind": ImprovementKind.TEST,
                    "delivery": DeliveryKind.MEMORY_PATCH,
                }
            )
        )
        != baseline
    )
    assert improvement_fingerprint(candidate.model_copy(update={"target": "other/module"})) != baseline
    assert (
        improvement_fingerprint(candidate.model_copy(update={"proposed_change": "Different intent text"}))
        != baseline
    )
    assert improvement_fingerprint(candidate, version="2") != baseline


def test_fingerprint_normalizes_unicode_case_and_whitespace(
    candidate: ImprovementCandidate,
) -> None:
    # NFKC: fullwidth Latin letters fold; casefold + whitespace collapse
    left = candidate.model_copy(
        update={
            "target": "  Assurance_Agent/Workflow/Inspect  ",
            "proposed_change": "Preserve   pytest E lines",
        }
    )
    right = candidate.model_copy(
        update={
            "target": "assurance_agent/workflow/inspect",
            "proposed_change": "preserve pytest e lines",
        }
    )
    assert improvement_fingerprint(left) == improvement_fingerprint(right)

    composed = candidate.model_copy(update={"target": "café/path"})
    decomposed = candidate.model_copy(update={"target": unicodedata.normalize("NFD", "café/path")})
    assert improvement_fingerprint(composed) == improvement_fingerprint(decomposed)


def test_fingerprint_matches_canonical_sha256(candidate: ImprovementCandidate) -> None:
    payload = {
        "version": "1",
        "kind": candidate.kind.value,
        "delivery": candidate.delivery.value,
        "target": " ".join(unicodedata.normalize("NFKC", candidate.target).casefold().split()),
        "intent": " ".join(unicodedata.normalize("NFKC", candidate.proposed_change).casefold().split()),
    }
    wire = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    expected = hashlib.sha256(wire.encode("utf-8")).hexdigest()
    assert improvement_fingerprint(candidate) == expected


def test_improvement_id_for_fingerprint_is_prefixed_uppercase() -> None:
    fingerprint = "abcdef0123456789ffff" + "0" * 44
    improvement_id = improvement_id_for_fingerprint(fingerprint)
    assert improvement_id == "IMP-ABCDEF0123456789FFFF"
    assert improvement_id.startswith("IMP-")
    assert len(improvement_id) == len("IMP-") + 20


def test_improvement_event_id_is_deterministic() -> None:
    first = improvement_event_id("IDEM-1", "improvement_proposed", 0)
    second = improvement_event_id("IDEM-1", "improvement_proposed", 0)
    assert first == second
    assert first.startswith("IMPEVT-")
    assert len(first) == len("IMPEVT-") + 24
    assert first == first.upper()

    other_type = improvement_event_id("IDEM-1", "improvement_applied", 0)
    other_ordinal = improvement_event_id("IDEM-1", "improvement_proposed", 1)
    other_key = improvement_event_id("IDEM-2", "improvement_proposed", 0)
    assert len({first, other_type, other_ordinal, other_key}) == 4
