"""Runner trace fold injection, shadow diagnostics, and manifest executed_at (Task 8/9)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from assurance_agent.artifacts.models.trace import TraceProjection
from assurance_agent.evidence.trace import canonical_json_bytes, fold_trace
from assurance_agent.workflow.execution import runner as runner_mod
from assurance_agent.workflow.execution import runners as runners_mod
from assurance_agent.workflow.execution.runner import run_change
from tests.helpers_aa import write_aa_config
from tests.unit.execution.test_runner import make_config

EXECUTED_AT = datetime(2026, 7, 30, 4, 0, 0, tzinfo=UTC)
BATCH_ID = "20260730-040000"
CHANGE_ID = "CH-TRACE-1"


def _write_api_case(change_dir: Path, case_id: str = "TC_DEPT_API_001") -> None:
    cases = change_dir / "cases" / "dept"
    cases.mkdir(parents=True)
    (cases / "case.yaml").write_text(
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


@pytest.fixture
def trace_project(tmp_path: Path) -> tuple[Path, Path]:
    write_aa_config(tmp_path)
    (tmp_path / "tests" / "api").mkdir(parents=True)
    test_path = tmp_path / "tests" / "api" / "test_dept.py"
    test_path.write_text(
        "def test_tc_dept_api_001__ok():\n    assert True\n",
        encoding="utf-8",
    )
    change_dir = tmp_path / "qa" / "changes" / CHANGE_ID
    change_dir.mkdir(parents=True)
    (change_dir / "workflow-state.yaml").write_text(
        "selected_targets:\n  api: true\n  e2e: false\n  fuzz: false\n  performance: false\n",
        encoding="utf-8",
    )
    _write_api_case(change_dir)
    return tmp_path, change_dir


def _stub_api_passed_run(args, **kwargs):
    import subprocess

    report_file = next(a.split("=", 1)[1] for a in args if a.startswith("--json-report-file="))
    Path(report_file).parent.mkdir(parents=True, exist_ok=True)
    Path(report_file).write_text(
        json.dumps(
            {
                "tests": [
                    {
                        "nodeid": "tests/api/test_dept.py::test_tc_dept_api_001__ok",
                        "outcome": "passed",
                        "call": {
                            "outcome": "passed",
                            "duration": 0.0,
                            "longrepr": "",
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return subprocess.CompletedProcess(args, 0, stdout="", stderr="")


def _run_with_stubs(monkeypatch: pytest.MonkeyPatch, project_root: Path, change_dir: Path):
    monkeypatch.setattr(runner_mod, "generate_batch_id", lambda: BATCH_ID)
    monkeypatch.setattr(runner_mod, "_now_aware", lambda: EXECUTED_AT)
    monkeypatch.setattr(runners_mod.subprocess, "run", _stub_api_passed_run)
    return run_change(project_root, change_dir, make_config(), batch_id=BATCH_ID)


def test_saved_projection_matches_disk_fold_after_publish(
    trace_project: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root, change_dir = trace_project
    manifest = _run_with_stubs(monkeypatch, project_root, change_dir)

    projection_path = change_dir / "execution" / "runs" / BATCH_ID / "trace-projection.json"
    assert projection_path.is_file()

    saved = TraceProjection.model_validate_json(projection_path.read_text(encoding="utf-8"))
    from_disk = fold_trace(project_root, CHANGE_ID, current=None)

    assert canonical_json_bytes(saved.model_dump(mode="json")) == canonical_json_bytes(
        from_disk.model_dump(mode="json")
    )
    assert manifest.executed_at == EXECUTED_AT

    executed_rows = [row for row in saved.rows if row.presence_in_current_batch == "executed"]
    assert executed_rows, "expected at least one row executed in current batch"
    passed_row = next(row for row in executed_rows if row.latest_execution is not None)
    assert passed_row.latest_execution is not None
    assert passed_row.latest_execution.status == "passed"
    assert passed_row.presence_in_current_batch == "executed"


def test_trace_projection_written_under_batch_dir(
    trace_project: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root, change_dir = trace_project
    _run_with_stubs(monkeypatch, project_root, change_dir)
    path = change_dir / "execution" / "runs" / BATCH_ID / "trace-projection.json"
    assert path.is_file()
    TraceProjection.model_validate_json(path.read_text(encoding="utf-8"))


def test_quality_gate_diagnostics_contain_evidence_sufficiency_shadow(
    trace_project: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root, change_dir = trace_project
    _run_with_stubs(monkeypatch, project_root, change_dir)
    gate_path = change_dir / "execution" / "runs" / BATCH_ID / "quality-gate-result.json"
    gate = json.loads(gate_path.read_text(encoding="utf-8"))
    assert gate["diagnostics"] is not None
    shadow = gate["diagnostics"]["evidence_sufficiency"]
    assert "policy_error" not in shadow
    assert "as_of" in shadow
    assert "verdicts" in shadow


def test_coverage_dimension_uses_evidence_sufficiency(
    trace_project: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root, change_dir = trace_project
    _run_with_stubs(monkeypatch, project_root, change_dir)
    gate_path = change_dir / "execution" / "runs" / BATCH_ID / "quality-gate-result.json"
    gate = json.loads(gate_path.read_text(encoding="utf-8"))
    coverage = gate["dimensions"]["coverage"]
    assert coverage["evidence"] is not None
    assert "verdicts" in coverage["evidence"]
    assert coverage["status"] in {"PASS", "PASS_WITH_WARNINGS", "FAIL"}


def test_final_status_reflects_evidence_coverage_with_default_policy(
    trace_project: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root, change_dir = trace_project
    manifest = _run_with_stubs(monkeypatch, project_root, change_dir)
    gate_path = change_dir / "execution" / "runs" / BATCH_ID / "quality-gate-result.json"
    gate = json.loads(gate_path.read_text(encoding="utf-8"))
    # Default on_insufficient=require_human: stub execution may leave never_run → FAIL.
    assert gate["dimensions"]["coverage"]["status"] == gate["final_status"] or gate["final_status"] == "FAIL"
    assert manifest.final_status == gate["final_status"]


def test_manifest_contains_executed_at(
    trace_project: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from assurance_agent.artifacts.models import ExecutionManifest

    project_root, change_dir = trace_project
    _run_with_stubs(monkeypatch, project_root, change_dir)
    manifest_path = change_dir / "execution" / "runs" / BATCH_ID / "execution-manifest.yaml"
    manifest = ExecutionManifest.model_validate(yaml.safe_load(manifest_path.read_text(encoding="utf-8")))
    assert manifest.executed_at == EXECUTED_AT


def test_policy_error_fails_closed_on_coverage(
    trace_project: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root, change_dir = trace_project
    policy_path = project_root / ".aa" / "policy.yaml"
    policy_path.write_text("not: valid: policy\n", encoding="utf-8")
    manifest = _run_with_stubs(monkeypatch, project_root, change_dir)
    assert manifest.final_status == "FAIL"
    gate_path = change_dir / "execution" / "runs" / BATCH_ID / "quality-gate-result.json"
    gate = json.loads(gate_path.read_text(encoding="utf-8"))
    assert gate["final_status"] == "FAIL"
    assert gate["dimensions"]["coverage"]["evidence"] == {"error_code": "policy_error"}
    assert "policy_error" in gate["diagnostics"]["evidence_sufficiency"]
