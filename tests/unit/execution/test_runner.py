import json
import subprocess
from pathlib import Path

import pytest

from assurance_agent.config import AaConfig
from assurance_agent.workflow.execution import runner as runner_mod
from assurance_agent.workflow.execution import runners as runners_mod
from assurance_agent.workflow.execution.runner import run_change


def make_config(*, performance_enabled: bool = False) -> AaConfig:
    return AaConfig.model_validate(
        {
            "version": 1,
            "sources": {"frontend": "./frontend", "backend": "./backend"},
            "qa": {"cases": "./qa/cases", "changes": "./qa/changes"},
            "tests": {"root": "./tests", "api": "./tests/api", "e2e": "./tests/e2e"},
            "frameworks": {
                "api": {"enabled": True, "name": "pytest"},
                "e2e": {"enabled": True, "name": "playwright"},
            },
            "generation": {"prd_input_mode": "prompt", "e2e": {"default_pom": False}},
            "execution": {"entry": "cli", "self_healing": {"mode": "proposal-only"}},
            "coverage": {"enabled": False, "gate_mode": "warn", "threshold": {"line": 70, "branch": 60}},
            "performance": {"enabled": performance_enabled},
        }
    )


def stub_pytest_run(outcome_by_target: dict[str, str]):
    """subprocess.run stub keyed by which target dir appears in argv."""

    def fake_run(args, **kwargs):
        report_file = next(a.split("=", 1)[1] for a in args if a.startswith("--json-report-file="))
        target = "api" if "tests/api" in args else "e2e" if "tests/e2e" in args else "fuzz"
        outcome = outcome_by_target.get(target, "passed")
        tests = [
            {
                "nodeid": f"tests/{target}/t.py::test_tc_{target}_001__x",
                "outcome": outcome,
                "call": {
                    "outcome": outcome,
                    "duration": 0.0,
                    "longrepr": "" if outcome == "passed" else "AssertionError: boom",
                },
            }
        ]
        Path(report_file).parent.mkdir(parents=True, exist_ok=True)
        Path(report_file).write_text(json.dumps({"tests": tests}), encoding="utf-8")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    return fake_run


@pytest.fixture
def change_dir(tmp_path: Path) -> Path:
    (tmp_path / "tests" / "api").mkdir(parents=True)
    (tmp_path / "tests" / "e2e").mkdir(parents=True)
    change = tmp_path / "qa" / "changes" / "CH-1"
    change.mkdir(parents=True)
    (change / "workflow-state.yaml").write_text(
        "selected_targets:\n  api: true\n  e2e: true\n  fuzz: false\n  performance: false\n",
        encoding="utf-8",
    )
    return change


def test_run_change_all_pass_final_status_pass(tmp_path: Path, change_dir: Path, monkeypatch) -> None:
    monkeypatch.setattr(runner_mod, "generate_batch_id", lambda: "20260715-000000")
    monkeypatch.setattr(runners_mod.subprocess, "run", stub_pytest_run({"api": "passed", "e2e": "passed"}))
    manifest = run_change(tmp_path, change_dir, make_config())
    assert manifest.batch_id == "20260715-000000"
    assert manifest.final_status == "PASS"
    assert manifest.selected_targets.api is True
    assert (change_dir / "execution" / "runs" / "20260715-000000" / "api-result.json").is_file()
    assert (change_dir / "execution" / "execution-manifest.yaml").is_file()


def test_run_change_api_fail_final_status_fail(tmp_path: Path, change_dir: Path, monkeypatch) -> None:
    monkeypatch.setattr(runner_mod, "generate_batch_id", lambda: "20260715-000001")
    monkeypatch.setattr(runners_mod.subprocess, "run", stub_pytest_run({"api": "failed", "e2e": "passed"}))
    manifest = run_change(tmp_path, change_dir, make_config())
    assert manifest.final_status == "FAIL"


def test_run_change_scopes_to_codegen_plan_mapping(tmp_path: Path, change_dir: Path, monkeypatch) -> None:
    (tmp_path / "tests" / "api" / "test_dept_api.py").write_text("", encoding="utf-8")
    (tmp_path / "tests" / "api" / "test_user_api.py").write_text("", encoding="utf-8")
    plans = change_dir / "plans"
    plans.mkdir()
    (plans / "api-codegen-plan.md").write_text(
        "## Test Function Mapping\n\n"
        "| Case ID | Test Function | Target File |\n"
        "|---|---|---|\n"
        "| TC_DEPT_API_001 | `test_tc_dept_api_001__ok` | `tests/api/test_dept_api.py` |\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(runner_mod, "generate_batch_id", lambda: "20260716-000000")

    captured_args: list[list[str]] = []

    def fake_run(args, **kwargs):
        captured_args.append(args)
        report_file = next(a.split("=", 1)[1] for a in args if a.startswith("--json-report-file="))
        Path(report_file).parent.mkdir(parents=True, exist_ok=True)
        Path(report_file).write_text(json.dumps({"tests": []}), encoding="utf-8")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(runners_mod.subprocess, "run", fake_run)
    run_change(tmp_path, change_dir, make_config())

    api_call = next(
        args
        for args in captured_args
        if "test_dept_api.py" in " ".join(args) or "test_user_api.py" in " ".join(args)
    )
    assert "tests/api/test_dept_api.py" in api_call
    assert "tests/api/test_user_api.py" not in api_call


def test_run_change_scopes_performance_to_codegen_target_files(
    tmp_path: Path, change_dir: Path, monkeypatch
) -> None:
    perf_dir = tmp_path / "tests" / "perf"
    perf_dir.mkdir(parents=True)
    dept = perf_dir / "locustfile_dept.py"
    user = perf_dir / "locustfile_user.py"
    dept.write_text("", encoding="utf-8")
    user.write_text("", encoding="utf-8")
    (change_dir / "workflow-state.yaml").write_text(
        "selected_targets:\n  api: false\n  e2e: false\n  fuzz: false\n  performance: true\n",
        encoding="utf-8",
    )
    plans = change_dir / "plans"
    plans.mkdir()
    (plans / "performance-codegen-plan.md").write_text(
        "## Target Files\n\n- `tests/perf/locustfile_dept.py`\n",
        encoding="utf-8",
    )
    cases = change_dir / "cases"
    cases.mkdir()
    (cases / "performance.yaml").write_text(
        "added:\n"
        "  - case_id: TC_DEPT_PERF_001\n"
        "    type: Performance\n"
        "    performance:\n"
        "      capability: dept-list\n"
        "      endpoint: /api/v1/dept/list\n"
        "    thresholds:\n"
        "      p95_ms: 500\n"
        "      error_rate_max: 0.01\n",
        encoding="utf-8",
    )
    captured_args: list[list[str]] = []

    def fake_run(args, **kwargs):
        captured_args.append(args)
        prefix = Path(args[args.index("--csv") + 1])
        prefix.with_name(prefix.name + "_stats.csv").write_text(
            "Type,Name,Request Count,Failure Count,Median Response Time,95%\n"
            "GET,dept-list,10,0,20,30\n",
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(runners_mod.subprocess, "run", fake_run)

    manifest = run_change(
        tmp_path,
        change_dir,
        make_config(performance_enabled=True),
        batch_id="20260726-000000",
    )

    assert manifest.final_status == "PASS"
    assert len(captured_args) == 1
    assert str(dept) in captured_args[0]
    assert str(user) not in captured_args[0]


def test_run_change_fails_when_mapped_performance_locustfile_is_missing(
    tmp_path: Path, change_dir: Path, monkeypatch
) -> None:
    (tmp_path / "tests" / "perf").mkdir(parents=True)
    (change_dir / "workflow-state.yaml").write_text(
        "selected_targets:\n  api: false\n  e2e: false\n  fuzz: false\n  performance: true\n",
        encoding="utf-8",
    )
    plans = change_dir / "plans"
    plans.mkdir()
    (plans / "performance-codegen-plan.md").write_text(
        "## Target Files\n\n- `tests/perf/locustfile_missing.py`\n",
        encoding="utf-8",
    )
    cases = change_dir / "cases"
    cases.mkdir()
    (cases / "performance.yaml").write_text(
        "added:\n"
        "  - case_id: TC_DEPT_PERF_001\n"
        "    type: Performance\n"
        "    performance:\n"
        "      capability: dept-list\n"
        "      endpoint: /api/v1/dept/list\n"
        "    thresholds:\n"
        "      p95_ms: 500\n",
        encoding="utf-8",
    )

    def boom(*args, **kwargs):
        raise AssertionError("Locust must not run when the mapped file is missing")

    monkeypatch.setattr(runners_mod.subprocess, "run", boom)

    manifest = run_change(
        tmp_path,
        change_dir,
        make_config(performance_enabled=True),
        batch_id="20260726-000001",
    )

    assert manifest.final_status == "FAIL"


def test_run_change_missing_test_dirs_all_skipped(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(runner_mod, "generate_batch_id", lambda: "20260715-000002")

    def boom(*a, **k):
        raise AssertionError("no subprocess when all layers skip")

    monkeypatch.setattr(runners_mod.subprocess, "run", boom)

    change = tmp_path / "qa" / "changes" / "CH-2"
    change.mkdir(parents=True)
    (change / "workflow-state.yaml").write_text(
        "selected_targets:\n  api: true\n  e2e: false\n  fuzz: false\n  performance: false\n",
        encoding="utf-8",
    )
    manifest = run_change(tmp_path, change, make_config())
    assert manifest.final_status == "SKIPPED"
