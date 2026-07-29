"""aa trace — on-demand execution-phase fold (read-only, separate from aa status)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from assurance_agent.artifacts.models import SelectedTargets
from assurance_agent.cli import main
from assurance_agent.commands import status_cmd, trace_cmd
from assurance_agent.evidence.trace import fold_trace
from tests.helpers_aa import write_aa_config

CHANGE_ID = "CH-TRACE-1"
BATCH_ID = "20260729-120000"
EXECUTED_AT = datetime(2026, 7, 29, 12, 0, 0, tzinfo=UTC)


def _write_api_case(change_dir: Path, case_id: str = "TC_DEPT_API_001") -> None:
    path = change_dir / "cases/dept/case.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"""
schema_version: "1.0"
added:
  - case_id: {case_id}
    module: system.dept
    type: API
    title: create
    status: active
    priority: P0
    severity: blocker
    automation:
      required: true
modified: []
removed: []
""",
        encoding="utf-8",
    )


def _write_manifest(change_dir: Path, *, test_files_sha256: dict[str, str] | None = None) -> None:
    payload: dict[str, object] = {
        "schema_version": "1.0",
        "change_id": CHANGE_ID,
        "batch_id": BATCH_ID,
        "executed_at": EXECUTED_AT.isoformat(),
        "selected_targets": SelectedTargets(api=True, e2e=False, fuzz=False, performance=False).model_dump(),
        "result_files": {},
    }
    if test_files_sha256 is not None:
        payload["test_files_sha256"] = test_files_sha256
    manifest_path = change_dir / "execution/execution-manifest.yaml"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def _write_api_result(change_dir: Path) -> None:
    payload = {
        "schema_version": "1.0",
        "change_id": CHANGE_ID,
        "batch_id": BATCH_ID,
        "target": "api",
        "status": "passed",
        "command": "cmd",
        "source": {"framework": "pytest", "raw_log": "raw.log"},
        "total": 1,
        "passed": 1,
        "failed": 0,
        "skipped": 0,
        "cases": [
            {
                "case_id": "TC_DEPT_API_001",
                "status": "passed",
                "file": "tests/api/test_dept.py",
                "test_name": "test_tc_dept_api_001__ok",
                "duration_ms": 1,
                "message": "",
            }
        ],
        "unmapped_tests": [],
    }
    batch_dir = change_dir / "execution/runs" / BATCH_ID
    batch_dir.mkdir(parents=True, exist_ok=True)
    (batch_dir / "api-result.json").write_text(json.dumps(payload), encoding="utf-8")


def _write_test_file(project_root: Path) -> str:
    rel = "tests/api/test_dept.py"
    path = project_root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "def test_tc_dept_api_001__ok():\n    assert True\n",
        encoding="utf-8",
    )
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def _seed_happy_path(project_root: Path) -> None:
    write_aa_config(project_root)
    change_dir = project_root / "qa/changes" / CHANGE_ID
    change_dir.mkdir(parents=True)
    _write_api_case(change_dir)
    digest = _write_test_file(project_root)
    _write_manifest(change_dir, test_files_sha256={rel: digest for rel in ["tests/api/test_dept.py"]})
    _write_api_result(change_dir)


@pytest.fixture
def project():
    runner = CliRunner()
    ctx = runner.isolated_filesystem()
    root = Path(ctx.__enter__())
    yield runner, root
    ctx.__exit__(None, None, None)


def test_missing_change_exits_40(project) -> None:
    runner, root = project
    write_aa_config(root)
    (root / "qa/changes/CH-OTHER").mkdir(parents=True)
    result = runner.invoke(main, ["trace", "--change", "NOPE"])
    assert result.exit_code == 40
    assert "NOPE" in result.stderr


def test_no_cases_exits_40_with_stable_error(project) -> None:
    runner, root = project
    write_aa_config(root)
    (root / "qa/changes" / CHANGE_ID).mkdir(parents=True)
    result = runner.invoke(main, ["trace", "--change", CHANGE_ID])
    assert result.exit_code == 40
    assert result.stderr.strip() == trace_cmd.trace_error_no_cases(CHANGE_ID)


def test_all_unmapped_exits_40_with_stable_error(project) -> None:
    runner, root = project
    write_aa_config(root)
    change_dir = root / "qa/changes" / CHANGE_ID
    change_dir.mkdir(parents=True)
    _write_manifest(change_dir)
    payload = {
        "schema_version": "1.0",
        "change_id": CHANGE_ID,
        "batch_id": BATCH_ID,
        "target": "api",
        "status": "passed",
        "command": "cmd",
        "source": {"framework": "pytest", "raw_log": "raw.log"},
        "total": 1,
        "passed": 1,
        "failed": 0,
        "skipped": 0,
        "cases": [],
        "unmapped_tests": [{"file": "tests/api/x.py", "test_name": "test_orphan"}],
    }
    batch_dir = change_dir / "execution/runs" / BATCH_ID
    batch_dir.mkdir(parents=True, exist_ok=True)
    (batch_dir / "api-result.json").write_text(json.dumps(payload), encoding="utf-8")
    result = runner.invoke(main, ["trace", "--change", CHANGE_ID])
    assert result.exit_code == 40
    assert result.stderr.strip() == trace_cmd.trace_error_all_unmapped(CHANGE_ID)


def test_json_output_matches_fold_trace(project) -> None:
    runner, root = project
    _seed_happy_path(root)
    expected = fold_trace(root, CHANGE_ID, phase="execution", current=None)
    result = runner.invoke(main, ["trace", "--change", CHANGE_ID, "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == json.loads(expected.model_dump_json())


def test_json_output_is_deterministic(project) -> None:
    runner, root = project
    _seed_happy_path(root)
    first = runner.invoke(main, ["trace", "--change", CHANGE_ID, "--json"])
    second = runner.invoke(main, ["trace", "--change", CHANGE_ID, "--json"])
    assert first.exit_code == 0
    assert second.stdout == first.stdout


def test_type_filter_limits_rows(project) -> None:
    runner, root = project
    _seed_happy_path(root)
    result = runner.invoke(main, ["trace", "--change", CHANGE_ID, "--json", "--type", "API"])
    assert result.exit_code == 0
    doc = json.loads(result.stdout)
    assert all(row["case_type"] == "API" for row in doc["rows"])
    assert len(doc["rows"]) == 1


def test_only_gaps_json_is_stable(project) -> None:
    runner, root = project
    write_aa_config(root)
    change_dir = root / "qa/changes" / CHANGE_ID
    change_dir.mkdir(parents=True)
    _write_api_case(change_dir)
    result = runner.invoke(main, ["trace", "--change", CHANGE_ID, "--json", "--only-gaps"])
    assert result.exit_code == 0
    doc = json.loads(result.stdout)
    assert doc["change_id"] == CHANGE_ID
    assert doc["gaps"] == [
        {
            "code": "manifest_missing",
            "source": "execution/execution-manifest.yaml",
            "batch_id": None,
            "target": None,
            "detail": "",
        }
    ]


def test_does_not_use_status_read_path(project, monkeypatch: pytest.MonkeyPatch) -> None:
    runner, root = project
    _seed_happy_path(root)

    def _boom(*_args, **_kwargs):
        raise AssertionError("aa trace must not call read_latest_graph_status")

    monkeypatch.setattr(status_cmd, "read_latest_graph_status", _boom)
    result = runner.invoke(main, ["trace", "--change", CHANGE_ID, "--json"])
    assert result.exit_code == 0, result.output


def test_help_lists_trace_command() -> None:
    result = CliRunner().invoke(main, ["trace", "--help"])
    assert result.exit_code == 0
    assert "--change" in result.output
    assert "--only-gaps" in result.output
