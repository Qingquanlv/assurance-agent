from __future__ import annotations

import pytest

from assurance_agent.workflow.graph.frozen_output import FrozenOutput, enforce_size_limits


def _output(*, source_path: str, size: int) -> FrozenOutput:
    return FrozenOutput(
        value={"payload": "x" * size},
        source_path=source_path,
        source_sha256="a" * 64,
        model_id="model",
        model_schema_digest="b" * 64,
        catalog_symbol="symbol",
    )


def test_failure_analysis_may_use_task_aggregate_budget() -> None:
    enforce_size_limits(
        {
            "inspect_failure-analysis_json": _output(
                source_path="change:inspect/failure-analysis.json",
                size=70 * 1024,
            )
        }
    )


def test_generic_frozen_output_keeps_64k_limit() -> None:
    with pytest.raises(ValueError, match="exceeds 65536 bytes"):
        enforce_size_limits(
            {
                "generic": _output(
                    source_path="change:other.json",
                    size=70 * 1024,
                )
            }
        )


def test_failure_analysis_still_respects_task_aggregate_limit() -> None:
    with pytest.raises(ValueError, match="aggregate exceeds 262144 bytes"):
        enforce_size_limits(
            {
                "inspect_failure-analysis_json": _output(
                    source_path="change:inspect/failure-analysis.json",
                    size=130 * 1024,
                ),
                "inspect_failure-analysis_copy_json": _output(
                    source_path="change:inspect/failure-analysis.json",
                    size=130 * 1024,
                ),
            }
        )
