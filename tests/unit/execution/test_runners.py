import json
import subprocess
from pathlib import Path

from assurance_agent.artifacts.models import CoverageThreshold
from assurance_agent.workflow.execution import runners
from assurance_agent.workflow.execution.exec_config import PerfConfig
from assurance_agent.workflow.execution.runners import (
    build_scenario_verdicts,
    parse_coverage_result,
    parse_locust_stats,
    run_performance_target,
    run_pytest_target,
)


def _stub_pytest(report_tests: list[dict], coverage_totals: dict | None = None):
    """Return a fake subprocess.run that writes the canned report/coverage files."""

    def fake_run(args, **kwargs):
        report_file = next(a.split("=", 1)[1] for a in args if a.startswith("--json-report-file="))
        Path(report_file).parent.mkdir(parents=True, exist_ok=True)
        Path(report_file).write_text(json.dumps({"tests": report_tests}), encoding="utf-8")
        if coverage_totals is not None:
            cov_arg = next((a for a in args if a.startswith("--cov-report=json:")), None)
            if cov_arg:
                cov_path = Path(cov_arg.split("json:", 1)[1])
                cov_path.parent.mkdir(parents=True, exist_ok=True)
                cov_path.write_text(json.dumps({"totals": coverage_totals}), encoding="utf-8")
        return subprocess.CompletedProcess(args, 0, stdout="pytest output", stderr="")

    return fake_run


def test_run_pytest_target_parses_stubbed_report(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "tests" / "api").mkdir(parents=True)
    monkeypatch.setattr(
        runners.subprocess,
        "run",
        _stub_pytest(
            [
                {
                    "nodeid": "tests/api/t.py::test_tc_api_001__ok",
                    "outcome": "passed",
                    "call": {"outcome": "passed", "duration": 0.01},
                },
            ]
        ),
    )
    result = run_pytest_target(
        project_root=tmp_path,
        batch_dir=tmp_path / "batch",
        change_id="CH-1",
        batch_id="b1",
        target="api",
        test_dir="tests/api",
    )
    assert result.status == "passed"
    assert result.total == 1
    assert result.cases[0].case_id == "TC_API_001"
    assert (tmp_path / "batch" / "raw" / "api.log").is_file()


def test_run_pytest_target_scoped_to_test_paths(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "tests" / "api").mkdir(parents=True)
    (tmp_path / "tests" / "api" / "test_dept_api.py").write_text("", encoding="utf-8")
    (tmp_path / "tests" / "api" / "test_user_api.py").write_text("", encoding="utf-8")
    captured_args: list[list[str]] = []

    def fake_run(args, **kwargs):
        captured_args.append(args)
        report_file = next(a.split("=", 1)[1] for a in args if a.startswith("--json-report-file="))
        Path(report_file).parent.mkdir(parents=True, exist_ok=True)
        Path(report_file).write_text(
            json.dumps(
                {
                    "tests": [
                        {
                            "nodeid": "tests/api/test_dept_api.py::test_tc_dept_api_001__ok",
                            "outcome": "passed",
                            "call": {"outcome": "passed", "duration": 0.0},
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(runners.subprocess, "run", fake_run)
    result = run_pytest_target(
        project_root=tmp_path,
        batch_dir=tmp_path / "batch",
        change_id="CH-1",
        batch_id="b1",
        target="api",
        test_dir="tests/api",
        test_paths=["tests/api/test_dept_api.py"],
    )
    assert result.status == "passed"
    assert "tests/api/test_dept_api.py" in captured_args[0]
    assert "tests/api/test_user_api.py" not in captured_args[0]
    assert "tests/api" not in captured_args[0]


def test_run_pytest_target_missing_mapped_files_fails_closed_no_subprocess(
    tmp_path: Path, monkeypatch
) -> None:
    (tmp_path / "tests" / "api").mkdir(parents=True)

    def boom(*a, **k):
        raise AssertionError("subprocess must not run when every mapped file is missing")

    monkeypatch.setattr(runners.subprocess, "run", boom)
    result = run_pytest_target(
        project_root=tmp_path,
        batch_dir=tmp_path / "batch",
        change_id="CH-1",
        batch_id="b1",
        target="api",
        test_dir="tests/api",
        test_paths=["tests/api/test_missing.py"],
    )
    assert result.status == "failed"
    assert result.total == 1
    assert result.failed == 1
    assert "test_missing.py" in result.cases[0].message


def test_run_pytest_target_partial_mapping_fails_closed_no_subprocess(tmp_path: Path, monkeypatch) -> None:
    api_dir = tmp_path / "tests" / "api"
    api_dir.mkdir(parents=True)
    (api_dir / "test_dept.py").write_text("", encoding="utf-8")

    def boom(*args, **kwargs):
        raise AssertionError("subprocess must not run against a partial mapped file set")

    monkeypatch.setattr(runners.subprocess, "run", boom)
    result = run_pytest_target(
        project_root=tmp_path,
        batch_dir=tmp_path / "batch",
        change_id="CH-1",
        batch_id="b1",
        target="api",
        test_dir="tests/api",
        test_paths=["tests/api/test_dept.py", "tests/api/test_missing.py"],
    )

    assert result.status == "failed"
    assert "test_missing.py" in result.cases[0].message


def test_run_pytest_target_scoped_empty_fails_closed_no_subprocess(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "tests" / "fuzz").mkdir(parents=True)

    def boom(*args, **kwargs):
        raise AssertionError("subprocess must not run for a scoped-empty current plan")

    monkeypatch.setattr(runners.subprocess, "run", boom)
    result = run_pytest_target(
        project_root=tmp_path,
        batch_dir=tmp_path / "batch",
        change_id="CH-1",
        batch_id="b1",
        target="fuzz",
        test_dir="tests/fuzz",
        test_paths=[],
    )

    assert result.status == "failed"
    assert result.failed == 1
    assert "no executable test_*.py" in result.cases[0].message


def test_run_pytest_target_rejects_traversal_without_subprocess(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "tests" / "fuzz").mkdir(parents=True)
    outside = tmp_path / "tests" / "api"
    outside.mkdir()
    (outside / "test_history.py").write_text("", encoding="utf-8")

    def boom(*args, **kwargs):
        raise AssertionError("subprocess must not run for an escaped mapped path")

    monkeypatch.setattr(runners.subprocess, "run", boom)
    result = run_pytest_target(
        project_root=tmp_path,
        batch_dir=tmp_path / "batch",
        change_id="CH-1",
        batch_id="b1",
        target="fuzz",
        test_dir="tests/fuzz",
        test_paths=["tests/fuzz/../api/test_history.py"],
    )

    assert result.status == "failed"
    assert "test_history.py" in result.cases[0].message


def test_run_pytest_target_missing_dir_is_skipped_no_subprocess(tmp_path: Path, monkeypatch) -> None:
    def boom(*a, **k):
        raise AssertionError("subprocess must not run when the test dir is absent")

    monkeypatch.setattr(runners.subprocess, "run", boom)
    result = run_pytest_target(
        project_root=tmp_path,
        batch_dir=tmp_path / "batch",
        change_id="CH-1",
        batch_id="b1",
        target="fuzz",
        test_dir="tests/fuzz",
    )
    assert result.status == "skipped"
    assert result.total == 0
    assert "SKIPPED" in result.unmapped_tests[0].message


def test_run_pytest_target_collects_coverage(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "tests" / "api").mkdir(parents=True)
    monkeypatch.setattr(
        runners.subprocess,
        "run",
        _stub_pytest(
            [
                {
                    "nodeid": "tests/api/t.py::test_tc_api_001__ok",
                    "outcome": "passed",
                    "call": {"outcome": "passed", "duration": 0.0},
                }
            ],
            coverage_totals={"percent_covered": 88.0, "num_branches": 10, "covered_branches": 7},
        ),
    )
    batch_dir = tmp_path / "batch"
    run_pytest_target(
        project_root=tmp_path,
        batch_dir=batch_dir,
        change_id="CH-1",
        batch_id="b1",
        target="api",
        test_dir="tests/api",
        cov_package="app",
    )
    cov = parse_coverage_result(
        change_id="CH-1",
        batch_id="b1",
        batch_dir=batch_dir,
        threshold=CoverageThreshold(line=70, branch=60),
    )
    assert cov.available is True
    assert cov.line_coverage == 88.0
    assert cov.branch_coverage == 70.0
    assert cov.status == "PASS"


def test_parse_coverage_missing_is_skipped(tmp_path: Path) -> None:
    cov = parse_coverage_result(
        change_id="CH-1",
        batch_id="b1",
        batch_dir=tmp_path,
        threshold=CoverageThreshold(line=70, branch=60),
    )
    assert cov.available is False
    assert cov.status == "SKIPPED"


def test_parse_locust_stats_reads_p95_and_counts(tmp_path: Path) -> None:
    csv = tmp_path / "locust_stats.csv"
    csv.write_text(
        "Type,Name,Request Count,Failure Count,Median Response Time,95%\n"
        "GET,list_menus,100,2,120,450\n"
        "Aggregated,Aggregated,100,2,120,450\n",
        encoding="utf-8",
    )
    rows = parse_locust_stats(csv)
    assert len(rows) == 1  # Aggregated excluded
    assert rows[0]["name"] == "list_menus"
    assert rows[0]["requests"] == 100
    assert rows[0]["failures"] == 2
    assert rows[0]["p95"] == 450


def test_build_scenario_verdicts_pass_fail_skip() -> None:
    scenarios = [
        {"capability": "fast", "endpoint": "/f", "thresholds": {"p95_ms": 500, "error_rate_max": 0.01}},
        {"capability": "slow", "endpoint": "/s", "thresholds": {"p95_ms": 500, "error_rate_max": 0.01}},
        {"capability": "quiet", "endpoint": "/q", "thresholds": {"p95_ms": 500, "error_rate_max": 0.01}},
    ]
    stats: dict[str, dict[str, float]] = {
        "fast": {"p95": 200.0, "requests": 50.0, "failures": 0.0},
        "slow": {"p95": 900.0, "requests": 50.0, "failures": 0.0},
    }
    verdicts = build_scenario_verdicts(scenarios, stats)
    by_cap = {v.capability: v.verdict for v in verdicts}
    assert by_cap["fast"] == "PASS"
    assert by_cap["slow"] == "FAIL"
    assert by_cap["quiet"] == "SKIPPED"


def test_build_scenario_verdicts_accepts_dict_endpoint() -> None:
    scenarios = [
        {
            "capability": "user-list-pagination",
            "endpoint": {"method": "GET", "path": "/api/v1/user/list"},
            "thresholds": {"p95_ms": 2000, "error_rate_max": 0.01},
        }
    ]
    stats = {
        "user-list-paginated-query": {"p95": 40.0, "requests": 100.0, "failures": 0.0},
        "user-list-pagination": {"p95": 40.0, "requests": 100.0, "failures": 0.0},
    }
    verdicts = build_scenario_verdicts(scenarios, stats)
    assert len(verdicts) == 1
    assert verdicts[0].endpoint == "GET /api/v1/user/list"
    assert verdicts[0].verdict == "PASS"


def test_run_performance_target_only_executes_change_mapped_locustfiles(tmp_path: Path, monkeypatch) -> None:
    perf_dir = tmp_path / "tests" / "perf"
    perf_dir.mkdir(parents=True)
    dept = perf_dir / "locustfile_dept.py"
    user = perf_dir / "locustfile_user.py"
    dept.write_text("", encoding="utf-8")
    user.write_text("", encoding="utf-8")
    change_dir = tmp_path / "qa" / "changes" / "CH-1"
    change_dir.mkdir(parents=True)
    monkeypatch.setattr(
        runners,
        "load_perf_scenarios",
        lambda _change_dir: [
            {
                "capability": "dept-list",
                "endpoint": "/api/v1/dept/list",
                "thresholds": {"p95_ms": 500, "error_rate_max": 0.01},
            }
        ],
    )
    calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        calls.append(args)
        prefix = Path(args[args.index("--csv") + 1])
        prefix.with_name(prefix.name + "_stats.csv").write_text(
            "Type,Name,Request Count,Failure Count,Median Response Time,95%\nGET,dept-list,10,0,20,30\n",
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(runners.subprocess, "run", fake_run)

    result = run_performance_target(
        project_root=tmp_path,
        change_dir=change_dir,
        batch_dir=tmp_path / "batch",
        change_id="CH-1",
        batch_id="b1",
        perf_config=PerfConfig(enabled=True),
        test_paths=["tests/perf/locustfile_dept.py"],
    )

    assert result.status == "PASS"
    assert len(calls) == 1
    assert str(dept) in calls[0]
    assert str(user) not in calls[0]


def test_run_performance_target_fails_closed_when_any_mapped_locustfile_is_missing(
    tmp_path: Path, monkeypatch
) -> None:
    perf_dir = tmp_path / "tests" / "perf"
    perf_dir.mkdir(parents=True)
    (perf_dir / "locustfile_dept.py").write_text("", encoding="utf-8")
    change_dir = tmp_path / "qa" / "changes" / "CH-1"
    change_dir.mkdir(parents=True)
    monkeypatch.setattr(
        runners,
        "load_perf_scenarios",
        lambda _change_dir: [
            {
                "capability": "dept-list",
                "endpoint": "/api/v1/dept/list",
                "thresholds": {"p95_ms": 500, "error_rate_max": 0.01},
            }
        ],
    )

    def boom(*args, **kwargs):
        raise AssertionError("Locust must not run against a partial mapped file set")

    monkeypatch.setattr(runners.subprocess, "run", boom)

    result = run_performance_target(
        project_root=tmp_path,
        change_dir=change_dir,
        batch_dir=tmp_path / "batch",
        change_id="CH-1",
        batch_id="b1",
        perf_config=PerfConfig(enabled=True),
        test_paths=[
            "tests/perf/locustfile_dept.py",
            "tests/perf/locustfile_missing.py",
        ],
    )

    assert result.available is True
    assert result.status == "FAIL"
    assert "locustfile_missing.py" in (tmp_path / "batch/raw/performance.log").read_text()
