"""Nightly mutation evidence model (M2 Task 3) for aggregate-nightly consumption."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from assurance_agent.artifacts.models.metrics import MetricCollectionGap, MetricShortboard
from assurance_agent.artifacts.models.pr_metric_evidence import (
    MutationEvidence,
    MutationSurvivor,
)


def test_evaluated_score_matches_killed_over_tested() -> None:
    evidence = MutationEvidence(
        schema_version="1",
        change_id="CH-1",
        batch_id="nightly",
        status="evaluated",
        value=0.5,
        killed=1,
        survived=1,
        equivalent=0,
        tested=2,
        selected=2,
        budget_seconds=300,
        elapsed_seconds=1.0,
        budget_exceeded=False,
        cache_hit=False,
        seed=0,
        survivors=(
            MutationSurvivor(
                locator="app/a.py:1:AOR:m1",
                module="app/a.py",
                line=1,
                operator="AOR",
                mutant_id="m1",
                equivalent=False,
            ),
        ),
    )
    assert evidence.value == 0.5
    assert evidence.survivors[0].locator.startswith("app/a.py:")


def test_score_must_agree_with_counts() -> None:
    with pytest.raises(ValidationError, match="value"):
        MutationEvidence(
            schema_version="1",
            change_id="CH-1",
            batch_id="nightly",
            status="evaluated",
            value=0.9,
            killed=1,
            survived=1,
            equivalent=0,
            tested=2,
            selected=2,
            budget_seconds=300,
            elapsed_seconds=1.0,
            budget_exceeded=False,
            cache_hit=False,
            seed=0,
        )


def test_budget_exceeded_is_shortboard_not_gap() -> None:
    evidence = MutationEvidence(
        schema_version="1",
        change_id="CH-1",
        batch_id="nightly",
        status="evaluated",
        value=1.0,
        killed=1,
        survived=0,
        equivalent=0,
        tested=1,
        selected=3,
        budget_seconds=1,
        elapsed_seconds=1.0,
        budget_exceeded=True,
        cache_hit=False,
        seed=0,
        shortboards=(MetricShortboard(code="mutation_budget_exceeded", metric="mutation_score"),),
    )
    assert evidence.budget_exceeded is True
    assert evidence.collection_gaps == ()
    assert evidence.shortboards[0].code == "mutation_budget_exceeded"


def test_tool_failure_is_typed_collection_gap() -> None:
    evidence = MutationEvidence(
        schema_version="1",
        change_id="CH-1",
        batch_id="nightly",
        status="collection_failed",
        value=None,
        killed=0,
        survived=0,
        equivalent=0,
        tested=0,
        selected=0,
        budget_seconds=300,
        elapsed_seconds=0.0,
        budget_exceeded=False,
        cache_hit=False,
        seed=0,
        collection_gaps=(
            MetricCollectionGap(
                code="collection_failed",
                metric="mutation_score",
                detail="mutmut start failed",
            ),
        ),
    )
    assert evidence.status == "collection_failed"
    assert evidence.value is None


def test_survivor_report_cap_marks_truncation() -> None:
    survivors = tuple(
        MutationSurvivor(
            locator=f"app/a.py:{i}:AOR:m{i}",
            module="app/a.py",
            line=i,
            operator="AOR",
            mutant_id=f"m{i}",
        )
        for i in range(1, 4)
    )
    evidence = MutationEvidence(
        schema_version="1",
        change_id="CH-1",
        batch_id="nightly",
        status="evaluated",
        value=0.0,
        killed=0,
        survived=10,
        equivalent=0,
        tested=10,
        selected=10,
        budget_seconds=300,
        elapsed_seconds=2.0,
        budget_exceeded=False,
        cache_hit=True,
        seed=7,
        survivors=survivors,
        survivors_truncated=True,
        survivor_report_cap=3,
    )
    assert len(evidence.survivors) == 3
    assert evidence.survivors_truncated is True
    assert evidence.survived == 10
