from __future__ import annotations

from pathlib import Path

import pytest

from tests.phase4.conformance import execute_task

from assurance_execution.operations.runner import RunTestsHandler
from execution_fixtures import (  # pyright: ignore[reportMissingImports]
    as_object,
    executed_paths,
    fake_pytest_host,
    run_request,
    write_test,
)


@pytest.mark.asyncio
async def test_run_tests_executes_only_closed_mapping(tmp_path: Path) -> None:
    write_test(tmp_path / "tests/generated_test.py")
    write_test(tmp_path / "tests/legacy_test.py")
    outcome = await execute_task(
        RunTestsHandler(process_host=fake_pytest_host()),
        run_request(selected=["tests/generated_test.py"]),
        tmp_path,
    )
    assert outcome.status == "succeeded"
    assert executed_paths(outcome) == ("tests/generated_test.py",)


@pytest.mark.asyncio
async def test_run_tests_rejects_symlink_and_traversal(tmp_path: Path) -> None:
    write_test(tmp_path / "tests/generated_test.py")
    (tmp_path / "tests/legacy_test.py").symlink_to(tmp_path / "tests/generated_test.py")
    linked = await execute_task(
        RunTestsHandler(process_host=fake_pytest_host()),
        run_request(selected=["tests/legacy_test.py"]),
        tmp_path,
    )
    assert linked.status == "failed"
    assert linked.failure is not None
    assert linked.failure.kind == "invalid_input"
    escaped = await execute_task(
        RunTestsHandler(process_host=fake_pytest_host()),
        {
            **run_request(selected=["tests/generated_test.py"]),
            "mapping": {
                "selected": ["../secret.py"],
                "mappings": [
                    {
                        "test": "../secret.py",
                        "case_id": "TC_A",
                        "capability": "entities.item.create",
                        "layer": "api",
                    }
                ],
            },
        },
        tmp_path,
    )
    assert escaped.status == "failed"
    assert escaped.failure is not None
    assert escaped.failure.kind == "invalid_input"
    assert escaped.failure.retryable is False


@pytest.mark.asyncio
async def test_run_tests_builds_argv_without_shell(tmp_path: Path) -> None:
    write_test(tmp_path / "tests/generated_test.py")
    host = fake_pytest_host()
    outcome = await execute_task(
        RunTestsHandler(process_host=host),
        run_request(selected=["tests/generated_test.py"]),
        tmp_path,
    )
    assert outcome.status == "succeeded"
    assert host.commands == [("pytest", "tests/generated_test.py", "-p", "no:cacheprovider")]
    assert " " not in host.commands[0][1]


@pytest.mark.asyncio
async def test_run_tests_and_collect_pr_metrics_uses_selected_only(tmp_path: Path) -> None:
    from assurance_execution.operations.runner import RunTestsAndCollectPrMetricsHandler

    write_test(tmp_path / "tests/generated_test.py")
    write_test(tmp_path / "tests/legacy_test.py")
    outcome = await execute_task(
        RunTestsAndCollectPrMetricsHandler(process_host=fake_pytest_host()),
        run_request(selected=["tests/generated_test.py"]),
        tmp_path,
    )
    assert outcome.status == "succeeded"
    assert executed_paths(outcome) == ("tests/generated_test.py",)
    metric = as_object(as_object(outcome.output)["pr_metric_input"])
    assert metric["selected"] == ["tests/generated_test.py"]
    assert as_object(metric["results"][0])["test"] == "tests/generated_test.py"
