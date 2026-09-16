from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest
from graph_engine.canonical import JSONValue
from graph_engine.plugin_api import CandidateWriteSet

from tests.capabilities.conformance import execute_task

from assurance_quality.contracts.metrics import MetricsDocument
from assurance_quality.contracts.pr_metrics import (
    AdversarialYieldEvidence,
    AssertionStrengthEvidence,
    BaselineDriftEvidence,
    MutationEvidence,
)
from assurance_quality.operations.coverage import (
    CollectDiffCoverageHandler,
    ComputeConstraintCoverageHandler,
)
from assurance_quality.operations.metrics import (
    CollectAdversarialYieldHandler,
    CollectPrMetricsBatchHandler,
    ComputeAssertionStrengthHandler,
    ComputeBaselineDriftHandler,
    LoadLatestPrMetricsHandler,
    MaterializePrMetricsHandler,
    RunMutationSampleHandler,
)
from quality_fixtures import (  # pyright: ignore[reportMissingImports]
    BATCH_ID,
    CHANGE_ID,
    HEX_A,
    HEX_B,
    as_object,
    validation_context,
    write_set,
)


def _scope_surface(*, layer: str, total: int, covered: int) -> dict[str, object]:
    return {
        "layer": layer,
        "value": covered / total,
        "declared": {
            "total": total,
            "covered": covered,
            "value": covered / total,
            "uncovered": [],
        },
        "strong": covered,
        "weak": total - covered,
    }


@pytest.mark.asyncio
async def test_pr_metrics_bind_scope_evidence_and_source_digests(tmp_path: Path) -> None:
    diff = await execute_task(
        CollectDiffCoverageHandler(),
        {
            "change_id": CHANGE_ID,
            "batch_id": BATCH_ID,
            "files": [
                {
                    "path": "app/menus.py",
                    "changed_lines": [1],
                    "covered_lines": [1],
                    "uncovered_lines": [],
                }
            ],
            "source": {"tree": HEX_A},
        },
        tmp_path,
    )
    constraint = await execute_task(
        ComputeConstraintCoverageHandler(),
        {
            "change_id": CHANGE_ID,
            "batch_id": BATCH_ID,
            "declared_keys": ["menus.create"],
            "covered_keys": ["menus.create"],
            "source_digest": HEX_A,
        },
        tmp_path,
    )
    batch = await execute_task(
        CollectPrMetricsBatchHandler(),
        {
            "change_id": CHANGE_ID,
            "batch_id": BATCH_ID,
            "policy_digest": HEX_B,
            "diff": as_object(diff.output),
            "constraint": as_object(constraint.output),
        },
        tmp_path,
    )
    assert batch.status == "succeeded"
    assert as_object(batch.output)["source_digests"]["diff"]
    materialized = await execute_task(
        MaterializePrMetricsHandler(),
        {
            "change_id": CHANGE_ID,
            "batch_id": BATCH_ID,
            "policy_digest": HEX_B,
            "computed_at": "2026-08-22T00:00:00Z",
            "diff": as_object(diff.output),
            "constraint": as_object(constraint.output),
        },
        tmp_path,
    )
    assert materialized.status == "succeeded"
    document = MetricsDocument.model_validate(materialized.output)
    assert document.cadence == "pr"
    assert document.metrics["constraint_coverage"].evidence
    assert document.metrics["diff_coverage"].value == 1.0

    latest = await execute_task(
        LoadLatestPrMetricsHandler(),
        {"documents": [document.model_dump(mode="json")], "change_id": CHANGE_ID},
        tmp_path,
    )
    assert latest.status == "succeeded"
    assert as_object(latest.output)["change_id"] == CHANGE_ID


@pytest.mark.asyncio
async def test_mutation_assertion_drift_and_adversarial(tmp_path: Path) -> None:
    mutation = await execute_task(
        RunMutationSampleHandler(),
        {
            "change_id": CHANGE_ID,
            "batch_id": BATCH_ID,
            "killed": 3,
            "survived": 1,
            "selected": 4,
            "source": {"sample": HEX_A},
        },
        tmp_path,
    )
    assert mutation.status == "succeeded"
    MutationEvidence.model_validate(mutation.output)
    assert as_object(mutation.output)["value"] == 0.75

    assertion = await execute_task(
        ComputeAssertionStrengthHandler(),
        cast(
            JSONValue,
            {
                "change_id": CHANGE_ID,
                "batch_id": BATCH_ID,
                "surfaces": [
                    _scope_surface(layer="api", total=2, covered=2),
                    _scope_surface(layer="e2e", total=2, covered=1),
                ],
                "source": {"ast": HEX_A},
            },
        ),
        tmp_path,
    )
    assert assertion.status == "succeeded"
    AssertionStrengthEvidence.model_validate(assertion.output)

    drift = await execute_task(
        ComputeBaselineDriftHandler(),
        {
            "change_id": CHANGE_ID,
            "batch_id": BATCH_ID,
            "scenarios": [
                {
                    "capability": "entities.item.create",
                    "endpoint": "POST /menus",
                    "current_p95_ms": 120.0,
                    "baseline_p95_ms": 100.0,
                    "current_error_rate": 0.01,
                    "baseline_error_rate": 0.01,
                    "drift": 0.2,
                    "sample_count": 8,
                }
            ],
            "source": {"baseline": HEX_B},
        },
        tmp_path,
    )
    assert drift.status == "succeeded"
    BaselineDriftEvidence.model_validate(drift.output)

    yield_doc = await execute_task(
        CollectAdversarialYieldHandler(),
        {
            "change_id": CHANGE_ID,
            "batch_id": BATCH_ID,
            "property": "menus.create",
            "sample_count": 10,
            "counterexample_ids": ["ce-2", "ce-1"],
            "source": {"campaign": HEX_A},
        },
        tmp_path,
    )
    assert yield_doc.status == "succeeded"
    evidence = AdversarialYieldEvidence.model_validate(yield_doc.output)
    assert evidence.counterexample_ids == ("ce-1", "ce-2")
    assert evidence.value == 2.0


def test_metrics_and_cross_artifact_validators() -> None:
    from graph_engine import ENGINE_API_VERSION, RegistryPorts

    from assurance_quality.plugin import QualityPlugin
    from assurance_quality.validators.metrics import CrossArtifactValidator, MetricsValidator

    default_metrics = MetricsValidator()
    assert default_metrics.validate(write_set("inspect/metrics.json"), validation_context()).accepted is False
    default_cross = CrossArtifactValidator()
    assert default_cross.validate(write_set("inspect/metrics.json"), validation_context()).accepted is False

    contribution = QualityPlugin.contribute(RegistryPorts(engine_api=ENGINE_API_VERSION))
    metrics = contribution.commit_validators["assurance.quality.validator.metrics.v1"]
    cross = contribution.commit_validators["assurance.quality.validator.cross-artifact.v1"]
    assert metrics.validate(write_set("inspect/metrics.json"), validation_context()).accepted is True
    assert cross.validate(write_set("inspect/metrics.json"), validation_context()).accepted is True
    assert metrics.validate(write_set("src/app.py"), validation_context()).accepted is False
    empty = CandidateWriteSet(baseline_tree_id=HEX_A, candidate_tree_id="1" * 64, files=())
    assert metrics.validate(empty, validation_context()).accepted is True
