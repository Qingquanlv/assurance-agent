import json
import subprocess
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from assurance_agent.cli import main
from assurance_agent.commands import run_cmd as run_cmd_mod
from assurance_agent.workflow.core.events import EventWriteError, read_events
from assurance_agent.workflow.execution import runner as runner_mod
from assurance_agent.workflow.execution import runners as runners_mod
from assurance_agent.workflow.execution.tree_hash import hash_test_tree

_CONFIG = """version: 1
sources: {frontend: ./frontend, backend: ./backend}
qa: {cases: ./qa/cases, changes: ./qa/changes}
tests: {root: ./tests, api: ./tests/api, e2e: ./tests/e2e}
frameworks:
  api: {enabled: true, name: pytest}
  e2e: {enabled: true, name: playwright}
generation: {prd_input_mode: prompt, e2e: {default_pom: false}}
execution: {entry: cli, self_healing: {mode: proposal-only}}
coverage: {enabled: false, gate_mode: warn, threshold: {line: 70, branch: 60}}
performance: {enabled: false}
"""


def _stub_pytest(outcome: str):
    def fake_run(args, **kwargs):
        if not any(str(a).startswith("--json-report-file=") for a in args):
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        report_file = next(a.split("=", 1)[1] for a in args if a.startswith("--json-report-file="))
        target = "api" if "tests/api" in args else "e2e"
        Path(report_file).parent.mkdir(parents=True, exist_ok=True)
        Path(report_file).write_text(
            json.dumps(
                {
                    "tests": [
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
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    return fake_run


def _write_manifest(change_dir: Path, tree) -> None:  # noqa: ANN001
    change_dir.mkdir(parents=True, exist_ok=True)
    (change_dir / "execution").mkdir(parents=True, exist_ok=True)
    (change_dir / "execution" / "execution-manifest.yaml").write_text(
        yaml.safe_dump(
            {
                "batch_id": "20260101-000000",
                "tests_tree_sha256": tree.aggregate,
                "test_files_sha256": tree.files,
                "product_tree_sha256": "p0",
                "final_status": "PASS",
                "result_files": {},
            }
        ),
        encoding="utf-8",
    )


@pytest.fixture
def guarded_project(tmp_path: Path, monkeypatch):
    (tmp_path / ".aa").mkdir()
    (tmp_path / ".aa" / "config.yaml").write_text(_CONFIG, encoding="utf-8")
    (tmp_path / "tests" / "api").mkdir(parents=True)
    (tmp_path / "tests" / "api" / "test_x.py").write_text("def test_x(): assert 1\n", encoding="utf-8")
    change = tmp_path / "qa" / "changes" / "CH-1"
    change.mkdir(parents=True)
    (change / "workflow-state.yaml").write_text(
        "selected_targets: {api: true, e2e: false, fuzz: false, performance: false}\n",
        encoding="utf-8",
    )
    baseline = hash_test_tree(tmp_path)
    _write_manifest(change, baseline)
    monkeypatch.setattr(runner_mod, "generate_batch_id", lambda: "20260715-000001")
    monkeypatch.chdir(tmp_path)
    return tmp_path, change


def test_run_guard_blocks_tampered_tests_before_runner(guarded_project, monkeypatch) -> None:
    tmp_path, change = guarded_project
    calls: list[str] = []

    def track_run(*_args, **_kwargs):
        calls.append("run")
        raise AssertionError("run_change should not be called")

    monkeypatch.setattr(run_cmd_mod, "run_change", track_run)
    (tmp_path / "tests" / "api" / "test_x.py").write_text("def test_x(): assert 2\n", encoding="utf-8")
    result = CliRunner().invoke(main, ["run", "--change", "CH-1"])
    assert result.exit_code == 1
    assert calls == []
    assert "TESTS-CHANGED-WITHOUT-HEALING" in result.output


def test_allow_test_changes_requires_reason(guarded_project) -> None:
    tmp_path, _change = guarded_project
    (tmp_path / "tests" / "api" / "test_x.py").write_text("def test_x(): assert 2\n", encoding="utf-8")
    result = CliRunner().invoke(main, ["run", "--change", "CH-1", "--allow-test-changes"])
    assert result.exit_code == 1
    assert "rerun-reason" in result.output.lower()


def test_override_writes_evidence_and_human_decision_before_runner(guarded_project, monkeypatch) -> None:
    tmp_path, change = guarded_project
    monkeypatch.setattr(runners_mod.subprocess, "run", _stub_pytest("passed"))
    monkeypatch.setattr(run_cmd_mod, "append_event_best_effort", lambda *a, **k: None)
    (tmp_path / "tests" / "api" / "test_x.py").write_text("def test_x(): assert 2\n", encoding="utf-8")
    result = CliRunner().invoke(
        main,
        [
            "run",
            "--change",
            "CH-1",
            "--allow-test-changes",
            "--rerun-reason",
            "manual fix",
        ],
    )
    assert result.exit_code == 0
    events = read_events(change)
    decision = next(e for e in events if e.get("type") == "human_decision")
    assert decision["action"] == "allow_test_changes"
    assert decision["checkpoint"] == "test-tree-guard"
    override_files = list((change / "execution" / "runs").rglob("test-changes-override.json"))
    assert override_files


def test_override_event_failure_removes_evidence_and_does_not_run(guarded_project, monkeypatch) -> None:
    tmp_path, change = guarded_project
    calls: list[str] = []

    def track_run(*_args, **_kwargs):
        calls.append("run")
        raise AssertionError("run_change should not be called")

    monkeypatch.setattr(run_cmd_mod, "run_change", track_run)

    def fail_strict(*_args, **_kwargs) -> None:
        raise EventWriteError("simulated")

    monkeypatch.setattr(
        "assurance_agent.workflow.core.progression.append_event_strict",
        fail_strict,
    )
    (tmp_path / "tests" / "api" / "test_x.py").write_text("def test_x(): assert 2\n", encoding="utf-8")
    result = CliRunner().invoke(
        main,
        [
            "run",
            "--change",
            "CH-1",
            "--allow-test-changes",
            "--rerun-reason",
            "manual fix",
        ],
    )
    assert result.exit_code == 40
    assert calls == []
    override_files = list((change / "execution" / "runs").rglob("test-changes-override.json"))
    assert not override_files
