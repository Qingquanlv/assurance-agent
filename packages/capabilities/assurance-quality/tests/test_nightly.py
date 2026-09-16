from __future__ import annotations

from pathlib import Path

import pytest

from tests.capabilities.conformance import execute_task

from assurance_quality.contracts.metrics import MetricsDocument
from assurance_quality.operations.metrics import ComputeAssertionStrengthHandler, RunMutationSampleHandler
from assurance_quality.operations.nightly import (
    AggregateNightlyMetricsHandler,
    EvaluateRetrospectiveShortboardsHandler,
    RunNightlyMetricsPipelineHandler,
)
from quality_fixtures import BATCH_ID, CHANGE_ID, HEX_A, HEX_B, as_object  # pyright: ignore[reportMissingImports]


@pytest.mark.asyncio
async def test_nightly_pipeline_binds_injected_sources(tmp_path: Path) -> None:
    mutation = await execute_task(
        RunMutationSampleHandler(),
        {
            "change_id": CHANGE_ID,
            "batch_id": BATCH_ID,
            "killed": 2,
            "survived": 2,
            "selected": 4,
            "source": {"sample": HEX_A},
        },
        tmp_path,
    )
    assertion = await execute_task(
        ComputeAssertionStrengthHandler(),
        {
            "change_id": CHANGE_ID,
            "batch_id": BATCH_ID,
            "surfaces": [
                {
                    "layer": "api",
                    "value": 1.0,
                    "declared": {"total": 1, "covered": 1, "value": 1.0, "uncovered": []},
                    "strong": 1,
                    "weak": 0,
                },
                {
                    "layer": "e2e",
                    "value": 1.0,
                    "declared": {"total": 1, "covered": 1, "value": 1.0, "uncovered": []},
                    "strong": 1,
                    "weak": 0,
                },
            ],
            "source": {"ast": HEX_A},
        },
        tmp_path,
    )
    pipeline = await execute_task(
        RunNightlyMetricsPipelineHandler(),
        {
            "change_id": CHANGE_ID,
            "batch_id": BATCH_ID,
            "policy_digest": HEX_B,
            "computed_at": "2026-08-22T00:00:00Z",
            "mutation": as_object(mutation.output),
            "assertion": as_object(assertion.output),
        },
        tmp_path,
    )
    assert pipeline.status == "succeeded"
    metrics = MetricsDocument.model_validate(as_object(pipeline.output)["metrics"])
    assert metrics.cadence == "nightly"
    assert metrics.metrics["mutation_score"].evidence
    assert metrics.metrics["assertion_strength"].evidence

    aggregated = await execute_task(
        AggregateNightlyMetricsHandler(),
        {
            "change_id": CHANGE_ID,
            "batch_id": BATCH_ID,
            "policy_digest": HEX_B,
            "computed_at": "2026-08-22T00:00:00Z",
            "mutation": as_object(mutation.output),
        },
        tmp_path,
    )
    assert aggregated.status == "succeeded"
    assert MetricsDocument.model_validate(aggregated.output).cadence == "nightly"

    boards = await execute_task(
        EvaluateRetrospectiveShortboardsHandler(),
        {"metrics": as_object(aggregated.output)},
        tmp_path,
    )
    assert boards.status == "succeeded"
    assert as_object(boards.output)["shortboards"]
