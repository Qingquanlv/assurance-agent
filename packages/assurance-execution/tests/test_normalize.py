from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
from graph_engine.canonical import JSONValue

from tests.phase4.conformance import execute_task

from assurance_execution.operations.normalize import NormalizeHandler
from assurance_execution.operations.runner import classify_exit
from execution_fixtures import as_object, closed_mapping  # pyright: ignore[reportMissingImports]


def _normalize_input(
    *,
    selected: list[str],
    tests: list[dict[str, object]],
    exit_code: int = 0,
) -> dict[str, Any]:
    return {
        "change_id": "CH-DEMO-001",
        "batch_id": "20260822T000000Z",
        "selected_targets": {"api": True, "e2e": False, "fuzz": False, "performance": False},
        "mapping": closed_mapping(selected),
        "capability_leafs": ["auth.session.create", "entities.item.create"],
        "case_ids": ["TC_A", "TC_B"],
        "baseline_tree_id": "b" * 64,
        "runner_profile_digest": "c" * 64,
        "command": ["pytest", *selected],
        "exit_code": exit_code,
        "report": {
            "tests": tests,
            "exitcode": exit_code,
            "summary": {"collected": len(tests)},
        },
    }


@pytest.mark.asyncio
async def test_normalize_binds_digests_and_covers_mapping(tmp_path: Path) -> None:
    outcome = await execute_task(
        NormalizeHandler(),
        cast(
            JSONValue,
            _normalize_input(
                selected=["tests/generated_test.py"],
                tests=[
                    {
                        "nodeid": "tests/generated_test.py::test_tc_a_001__ok",
                        "outcome": "passed",
                        "call": {"outcome": "passed", "duration": 0.01},
                    }
                ],
            ),
        ),
        tmp_path,
    )
    assert outcome.status == "succeeded"
    output = as_object(outcome.output)
    assert output["mapping_digest"]
    assert output["receipt_digest"]
    assert output["baseline_tree_id"] == "b" * 64
    assert output["runner_profile_digest"] == "c" * 64
    assert tuple(as_object(item)["test"] for item in output["results"]) == ("tests/generated_test.py",)
    assert as_object(output["receipt"])["passed"] == 1


@pytest.mark.asyncio
async def test_normalize_rejects_result_outside_mapping(tmp_path: Path) -> None:
    outcome = await execute_task(
        NormalizeHandler(),
        cast(
            JSONValue,
            _normalize_input(
                selected=["tests/generated_test.py"],
                tests=[
                    {
                        "nodeid": "tests/legacy_test.py::test_old",
                        "outcome": "passed",
                        "call": {"outcome": "passed", "duration": 0.01},
                    }
                ],
            ),
        ),
        tmp_path,
    )
    assert outcome.status == "failed"
    assert outcome.failure is not None
    assert outcome.failure.kind == "invalid_output"
    assert outcome.failure.retryable is True
    assert "outside the closed mapping" in outcome.failure.message


def test_classify_exit_pass_fail_empty() -> None:
    assert classify_exit(0, failed=0, collected=1) == "passed"
    assert classify_exit(1, failed=1, collected=1) == "failed"
    assert classify_exit(5, failed=0, collected=0) == "skipped"
    assert classify_exit(2, failed=0, collected=3) == "failed"
