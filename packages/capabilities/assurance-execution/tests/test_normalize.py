from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
from graph_engine.canonical import JSONValue

from assurance_execution.contracts import ClosedMappingV1
from assurance_execution.operations.normalize import NormalizeHandler, normalize_evidence
from assurance_execution.operations.runner import classify_exit
from execution_fixtures import (  # pyright: ignore[reportMissingImports]
    PLAN_DIGEST,
    PLAN_REF,
    as_object,
    closed_mapping,
    execute_task,
)


def _normalize_input(
    *,
    selected: list[str],
    tests: list[dict[str, object]],
    exit_code: int = 0,
) -> dict[str, Any]:
    return {
        "change_id": "CH-DEMO-001",
        "plan_digest": PLAN_DIGEST,
        "plan_ref": PLAN_REF,
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
    receipt = as_object(output["receipt"])
    commands = cast(list[object], receipt["commands"])
    assert len(commands) == 1
    assert as_object(commands[0])["family"] == "api"
    assert as_object(commands[0])["passed"] == 1


@pytest.mark.asyncio
async def test_normalize_keeps_durable_qa_tests_mapping_from_view_nodeids(tmp_path: Path) -> None:
    outcome = await execute_task(
        NormalizeHandler(),
        cast(
            JSONValue,
            _normalize_input(
                selected=["qa/tests/generated_test.py::test_tc_a_001__ok"],
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
    mapping = as_object(output["mapping"])
    assert mapping["selected"] == ["qa/tests/generated_test.py::test_tc_a_001__ok"]
    assert tuple(as_object(item)["test"] for item in output["results"]) == (
        "qa/tests/generated_test.py::test_tc_a_001__ok",
    )


@pytest.mark.asyncio
async def test_normalize_preserves_case_level_receipt_counts_for_aggregated_selector(
    tmp_path: Path,
) -> None:
    payload = _normalize_input(
        selected=["tests/generated_test.py"],
        tests=[
            {
                "nodeid": "tests/generated_test.py::test_ok[one]",
                "outcome": "passed",
                "call": {"outcome": "passed", "duration": 0.01},
            },
            {
                "nodeid": "tests/generated_test.py::test_ok[two]",
                "outcome": "failed",
                "call": {"outcome": "failed", "duration": 0.01},
            },
        ],
        exit_code=1,
    )
    payload["report"]["summary"] = {  # type: ignore[index]
        "collected": 2,
        "passed": 1,
        "failed": 1,
        "skipped": 0,
    }

    outcome = await execute_task(NormalizeHandler(), cast(JSONValue, payload), tmp_path)

    assert outcome.status == "succeeded"
    output = as_object(outcome.output)
    assert as_object(output["receipt"]) == {
        "commands": [
            {
                "family": "api",
                "command": ["pytest", "tests/generated_test.py"],
                "exit_code": 1,
                "collected": 2,
                "passed": 1,
                "failed": 1,
                "skipped": 0,
            }
        ]
    }
    results = cast(list[object], output["results"])
    assert len(results) == 1
    assert as_object(results[0])["status"] == "failed"


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


def test_normalize_accepts_the_public_mapping_form_of_selected_targets() -> None:
    selected = ["tests/generated_test.py"]
    mapping = ClosedMappingV1.model_validate(closed_mapping(selected))

    evidence = normalize_evidence(
        change_id="CH-DEMO-001",
        plan_digest=PLAN_DIGEST,
        plan_ref=PLAN_REF,
        batch_id="20260822T000000Z",
        selected_targets={"api": True, "e2e": False, "fuzz": False, "performance": False},
        mapping=mapping,
        capability_leafs=frozenset({"auth.session.create", "entities.item.create"}),
        case_ids=frozenset({"TC_A", "TC_B"}),
        baseline_tree_id="b" * 64,
        runner_profile_digest="c" * 64,
        command=("pytest", *selected),
        exit_code=0,
        report={
            "tests": [
                {
                    "nodeid": "tests/generated_test.py::test_tc_a_001__ok",
                    "outcome": "passed",
                    "call": {"outcome": "passed", "duration": 0.01},
                }
            ],
            "exitcode": 0,
            "summary": {"collected": 1, "passed": 1, "failed": 0, "skipped": 0},
        },
    )

    assert tuple(command.family for command in evidence.receipt.commands) == ("api",)
