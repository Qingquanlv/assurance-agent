"""Runner trace fold injection, shadow diagnostics, and manifest executed_at (Task 8)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from assurance_agent.artifacts.models.trace import TraceProjection
from assurance_agent.evidence.trace import ExecutionFoldInput, canonical_json_bytes, fold_trace
from assurance_agent.workflow.execution import runner as runner_mod
from assurance_agent.workflow.execution import runners as runners_mod
from assurance_agent.workflow.execution.runner import run_change
from tests.helpers_aa import write_aa_config
from tests.unit.execution.test_runner import make_config, stub_pytest_run

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
    change_dir = tmp_path / "qa" / "changes" / CHANGE_ID
    change_dir.mkdir(parents=True)
    (change_dir / "workflow-state.yaml").write_text(
        "selected_targets:\n  api: true\n  e2e: false\n  fuzz: false\n  performance: false\n",
        encoding="utf-8",
    )
    _write_api_case(change_dir)
    return tmp_path, change_dir


def _run_with_stubs(monkeypatch: pytest.MonkeyPatch, project_root: Path, change_dir: Path):
    monkeypatch.setattr(runner_mod, "generate_batch_id", lambda: BATCH_ID)
    monkeypatch.setattr(runner_mod, "_now_aware", lambda: EXECUTED_AT)
    monkeypatch.setattr(
        runners_mod.subprocess,
        "run",
        stub_pytest_run({"api": "passed"}),
    )
    return run_change(project_root, change_dir, make_config(), batch_id=BATCH_ID)


def test_injected_and_disk_fold_byte_equal_after_manifest_publish(
    trace_project: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root, change_dir = trace_project
    manifest = _run_with_stubs(monkeypatch, project_root, change_dir)

    projection_path = change_dir / "execution" / "runs" / BATCH_ID / "trace-projection.json"
    assert projection_path.is_file()

    from_manifest = fold_trace(project_root, CHANGE_ID)
    reinjected = fold_trace(
        project_root,
        CHANGE_ID,
        current=ExecutionFoldInput(
            batch_id=manifest.batch_id,
            executed_at=manifest.executed_at,
            selected_targets=manifest.selected_targets,
            test_files_sha256=manifest.test_files_sha256 or {},
        ),
    )

    disk_bytes = canonical_json_bytes(from_manifest.model_dump(mode="json"))
    injected_bytes = canonical_json_bytes(reinjected.model_dump(mode="json"))
    assert injected_bytes == disk_bytes
    assert manifest.executed_at == EXECUTED_AT


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


def test_final_status_unchanged_by_shadow(
    trace_project: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root, change_dir = trace_project
    manifest = _run_with_stubs(monkeypatch, project_root, change_dir)
    assert manifest.final_status == "PASS"
    gate_path = change_dir / "execution" / "runs" / BATCH_ID / "quality-gate-result.json"
    gate = json.loads(gate_path.read_text(encoding="utf-8"))
    assert gate["final_status"] == "PASS"


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


def test_policy_error_in_diagnostics_does_not_change_final_status(
    trace_project: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root, change_dir = trace_project
    policy_path = project_root / ".aa" / "policy.yaml"
    policy_path.write_text("not: valid: policy\n", encoding="utf-8")
    manifest = _run_with_stubs(monkeypatch, project_root, change_dir)
    assert manifest.final_status == "PASS"
    gate_path = change_dir / "execution" / "runs" / BATCH_ID / "quality-gate-result.json"
    gate = json.loads(gate_path.read_text(encoding="utf-8"))
    assert gate["final_status"] == "PASS"
    assert "policy_error" in gate["diagnostics"]["evidence_sufficiency"]
