import json
import subprocess
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner, Result

from assurance_agent.cli import main
from assurance_agent.commands import run_cmd as run_cmd_mod
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


def _write_policy(tmp_path: Path, test_changes_override) -> None:  # noqa: ANN001
    (tmp_path / ".aa" / "execution-policy.json").write_text(
        json.dumps({"healing": {"testChangesOverride": test_changes_override}}, indent=2),
        encoding="utf-8",
    )


def _tamper_test(tmp_path: Path) -> None:
    (tmp_path / "tests" / "api" / "test_x.py").write_text("def test_x(): assert 2\n", encoding="utf-8")


def _invoke_run() -> Result:
    return CliRunner().invoke(
        main,
        ["run", "--change", "CH-1", "--allow-test-changes", "--rerun-reason", "manual fix"],
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


def test_forbidden_policy_rejects_allow_test_changes(guarded_project, monkeypatch) -> None:
    tmp_path, change = guarded_project
    calls: list[str] = []

    def track_run(*_args, **_kwargs):
        calls.append("run")
        raise AssertionError("run_change should not be called")

    monkeypatch.setattr(run_cmd_mod, "run_change", track_run)
    _write_policy(tmp_path, "forbidden")
    _tamper_test(tmp_path)
    result = _invoke_run()
    assert result.exit_code == 1
    assert calls == []
    assert "ALLOW-TEST-CHANGES-FORBIDDEN" in result.output
    override_files = list((change / "execution" / "runs").rglob("test-changes-override.json"))
    assert not override_files


def test_missing_policy_file_defaults_to_allow_with_evidence(guarded_project, monkeypatch) -> None:
    tmp_path, change = guarded_project
    monkeypatch.setattr(runners_mod.subprocess, "run", _stub_pytest("passed"))
    monkeypatch.setattr(run_cmd_mod, "append_event_best_effort", lambda *a, **k: None)
    _tamper_test(tmp_path)
    result = _invoke_run()
    assert result.exit_code == 0
    override_files = list((change / "execution" / "runs").rglob("test-changes-override.json"))
    assert override_files


def test_conditional_policy_allows_within_allowed_path_globs(guarded_project, monkeypatch) -> None:
    tmp_path, change = guarded_project
    monkeypatch.setattr(runners_mod.subprocess, "run", _stub_pytest("passed"))
    monkeypatch.setattr(run_cmd_mod, "append_event_best_effort", lambda *a, **k: None)
    _write_policy(tmp_path, {"mode": "conditional", "allowedPathGlobs": ["tests/**"]})
    _tamper_test(tmp_path)
    result = _invoke_run()
    assert result.exit_code == 0
    override_files = list((change / "execution" / "runs").rglob("test-changes-override.json"))
    assert override_files


def test_conditional_policy_denies_outside_allowed_path_globs(guarded_project, monkeypatch) -> None:
    tmp_path, change = guarded_project
    calls: list[str] = []

    def track_run(*_args, **_kwargs):
        calls.append("run")
        raise AssertionError("run_change should not be called")

    monkeypatch.setattr(run_cmd_mod, "run_change", track_run)
    _write_policy(tmp_path, {"mode": "conditional", "allowedPathGlobs": ["tests/e2e/**"]})
    _tamper_test(tmp_path)
    result = _invoke_run()
    assert result.exit_code == 1
    assert calls == []
    assert "TEST-CHANGES-OVERRIDE-PATH-DENIED" in result.output
    override_files = list((change / "execution" / "runs").rglob("test-changes-override.json"))
    assert not override_files
