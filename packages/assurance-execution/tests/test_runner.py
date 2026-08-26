from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from assurance_execution.operations.common import InputError
from assurance_execution.operations.runner import ConfinedExecutionProcessHost, RunTestsHandler
from execution_fixtures import (  # pyright: ignore[reportMissingImports]
    as_object,
    execute_task,
    executed_paths,
    fake_pytest_host,
    materialize_execution_view,
    run_request,
    write_test,
)


@pytest.mark.asyncio
async def test_run_tests_executes_only_closed_mapping(tmp_path: Path) -> None:
    write_test(tmp_path / "tests/generated_test.py")
    write_test(tmp_path / "tests/legacy_test.py")
    materialize_execution_view(tmp_path, ["tests/generated_test.py"])
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
    materialize_execution_view(tmp_path, ["tests/generated_test.py"])
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
    materialize_execution_view(tmp_path, ["tests/generated_test.py"])
    host = fake_pytest_host()
    outcome = await execute_task(
        RunTestsHandler(process_host=host),
        run_request(selected=["tests/generated_test.py"]),
        tmp_path,
    )
    assert outcome.status == "succeeded"
    argv = host.commands[0]
    assert argv[0] == "pytest"
    assert "tests/generated_test.py" in argv
    assert "-p" in argv and "no:cacheprovider" in argv
    assert "--json-report" in argv
    report_flags = [item for item in argv if item.startswith("--json-report-file=")]
    assert len(report_flags) == 1
    report_path = Path(report_flags[0].split("=", 1)[1])
    assert not report_path.is_absolute()
    assert ".." not in report_path.parts
    assert " " not in argv[1]


@pytest.mark.asyncio
async def test_run_tests_empty_mapping_does_not_spawn(tmp_path: Path) -> None:
    write_test(tmp_path / "tests/legacy_test.py")
    write_test(tmp_path / "tests/generated_test.py")
    host = fake_pytest_host()
    outcome = await execute_task(
        RunTestsHandler(process_host=host),
        run_request(selected=[]),
        tmp_path,
    )
    assert outcome.status == "succeeded"
    assert host.commands == []
    assert executed_paths(outcome) == ()
    output = as_object(outcome.output)
    assert output["results"] == []
    assert as_object(output["evidence"])["results"] == []
    assert as_object(as_object(output["evidence"])["mapping"])["selected"] == []


def test_confined_host_scrubs_env_and_confines_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    def fake_run(
        argv: list[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        captured["argv"] = tuple(argv)
        captured["env"] = kwargs.get("env")
        captured["shell"] = kwargs.get("shell")
        report_flag = next(item for item in argv if item.startswith("--json-report-file="))
        report_path = Path(report_flag.split("=", 1)[1])
        if not report_path.is_absolute():
            report_path = tmp_path / report_path
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(
                {
                    "tests": [],
                    "exitcode": 0,
                    "summary": {"collected": 0, "passed": 0, "failed": 0, "skipped": 0},
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr("assurance_execution.operations.runner.subprocess.run", fake_run)
    monkeypatch.setenv("PYTEST_ADDOPTS", "tests/legacy_test.py")
    host = ConfinedExecutionProcessHost()
    receipt = host.spawn(
        (
            "pytest",
            "tests/generated_test.py",
            "-p",
            "no:cacheprovider",
            "--json-report",
            "--json-report-file=.assurance-execution-report.json",
        ),
        tmp_path,
    )
    assert captured["shell"] is False
    env = captured["env"]
    assert isinstance(env, dict)
    assert "PYTEST_ADDOPTS" not in env
    argv = captured["argv"]
    assert isinstance(argv, tuple)
    assert "--json-report" in argv
    report_flags = [item for item in argv if item.startswith("--json-report-file=")]
    assert report_flags == ["--json-report-file=.assurance-execution-report.json"]
    assert receipt.report is not None


def test_confined_host_rejects_report_outside_cwd(tmp_path: Path) -> None:
    host = ConfinedExecutionProcessHost()
    with pytest.raises(InputError, match="report"):
        host.spawn(("pytest", "--json-report", "--json-report-file=/tmp/out.json"), tmp_path)
    with pytest.raises(InputError, match="report"):
        host.spawn(("pytest", "--json-report", "--json-report-file=../escape.json"), tmp_path)
    escaped = tmp_path / "escape.json"
    escaped.write_text("{}", encoding="utf-8")
    (tmp_path / "link-report.json").symlink_to(escaped)
    with pytest.raises(InputError, match="report"):
        host.spawn(("pytest", "--json-report", "--json-report-file=link-report.json"), tmp_path)


@pytest.mark.asyncio
async def test_run_tests_and_collect_pr_metrics_uses_selected_only(tmp_path: Path) -> None:
    from assurance_execution.operations.runner import RunTestsAndCollectPrMetricsHandler

    write_test(tmp_path / "tests/generated_test.py")
    write_test(tmp_path / "tests/legacy_test.py")
    materialize_execution_view(tmp_path, ["tests/generated_test.py"])
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
