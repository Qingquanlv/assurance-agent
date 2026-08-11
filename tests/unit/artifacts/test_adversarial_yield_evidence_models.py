"""Nightly adversarial-yield evidence model (M3 Task 1) for aggregate-nightly."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from assurance_agent.artifacts.models.metrics import MetricCollectionGap, MetricShortboard
from assurance_agent.artifacts.models.pr_metric_evidence import AdversarialYieldEvidence


def test_evaluated_publishes_confirmed_yield_scalar() -> None:
    evidence = AdversarialYieldEvidence(
        schema_version="1",
        change_id="CH-1",
        batch_id="nightly",
        property="api",
        layer="api",
        sample_count=4,
        counterexample_ids=("CE-1", "CE-2"),
        unclosed_count=1,
        seed=7,
        status="evaluated",
        value=2.0,
        source={"campaign_result_sha256": "a" * 64},
    )
    assert evidence.value == 2.0
    assert evidence.counterexample_ids == ("CE-1", "CE-2")
    assert evidence.unclosed_count == 1
    assert evidence.property == "api"


def test_evaluated_value_must_match_unique_confirmed_count() -> None:
    with pytest.raises(ValidationError, match="value"):
        AdversarialYieldEvidence(
            schema_version="1",
            change_id="CH-1",
            batch_id="nightly",
            property="api",
            layer="api",
            sample_count=2,
            counterexample_ids=("CE-1", "CE-2"),
            unclosed_count=2,
            seed=1,
            status="evaluated",
            value=1.0,
        )


def test_unclosed_cannot_exceed_confirmed_ids() -> None:
    with pytest.raises(ValidationError, match="unclosed"):
        AdversarialYieldEvidence(
            schema_version="1",
            change_id="CH-1",
            batch_id="nightly",
            property="api",
            layer="api",
            sample_count=1,
            counterexample_ids=("CE-1",),
            unclosed_count=2,
            seed=1,
            status="evaluated",
            value=1.0,
        )


def test_collection_failed_carries_gap_and_no_value() -> None:
    evidence = AdversarialYieldEvidence(
        schema_version="1",
        change_id="CH-1",
        batch_id="nightly",
        property="api",
        layer="api",
        sample_count=0,
        counterexample_ids=(),
        unclosed_count=0,
        seed=0,
        status="collection_failed",
        value=None,
        collection_gaps=(
            MetricCollectionGap(
                code="identity_mismatch",
                metric="adversarial_yield",
                detail="seed missing on discovery/counterexamples/CE-1.yaml",
            ),
        ),
    )
    assert evidence.value is None
    assert evidence.collection_gaps[0].code == "identity_mismatch"


def test_not_evaluated_requires_pending_nightly() -> None:
    with pytest.raises(ValidationError, match="pending_nightly"):
        AdversarialYieldEvidence(
            schema_version="1",
            change_id="CH-1",
            batch_id="nightly",
            property="api",
            layer="api",
            sample_count=0,
            counterexample_ids=(),
            unclosed_count=0,
            seed=0,
            status="not_evaluated",
            value=None,
        )

    evidence = AdversarialYieldEvidence(
        schema_version="1",
        change_id="CH-1",
        batch_id="nightly",
        property="api",
        layer="api",
        sample_count=0,
        counterexample_ids=(),
        unclosed_count=0,
        seed=0,
        status="not_evaluated",
        value=None,
        shortboards=(MetricShortboard(code="pending_nightly", metric="adversarial_yield"),),
    )
    assert evidence.status == "not_evaluated"
    assert evidence.value is None
