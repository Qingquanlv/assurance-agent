import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from assurance_agent.cli import main
from assurance_agent.commands import run_cmd as run_cmd_mod
from assurance_agent.workflow.execution import runner as runner_mod
from assurance_agent.workflow.execution import runners as runners_mod

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


@pytest.fixture
def project(tmp_path: Path, monkeypatch):
    (tmp_path / ".aa").mkdir()
    (tmp_path / ".aa" / "config.yaml").write_text(_CONFIG, encoding="utf-8")
    (tmp_path / "tests" / "api").mkdir(parents=True)
    (tmp_path / "tests" / "e2e").mkdir(parents=True)
    change = tmp_path / "qa" / "changes" / "CH-1"
    change.mkdir(parents=True)
    (change / "workflow-state.yaml").write_text(
        "selected_targets: {api: true, e2e: true, fuzz: false, performance: false}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(runner_mod, "generate_batch_id", lambda: "20260715-000000")
    monkeypatch.setattr(run_cmd_mod, "append_event_best_effort", lambda *a, **k: None)
    monkeypatch.chdir(tmp_path)
    return tmp_path, change


def test_run_pass_exit_zero(project, monkeypatch) -> None:
    monkeypatch.setattr(runners_mod.subprocess, "run", _stub_pytest("passed"))
    result = CliRunner().invoke(main, ["run", "--change", "CH-1"])
    assert result.exit_code == 0
    assert "PASS" in result.output
    _, change = project
    assert (change / "execution" / "execution-manifest.yaml").is_file()


def test_run_fail_exit_one(project, monkeypatch) -> None:
    monkeypatch.setattr(runners_mod.subprocess, "run", _stub_pytest("failed"))
    result = CliRunner().invoke(main, ["run", "--change", "CH-1"])
    assert result.exit_code == 1
    assert "FAIL" in result.output


def test_run_unknown_change_exit_one(project) -> None:
    result = CliRunner().invoke(main, ["run", "--change", "NOPE"])
    assert result.exit_code == 1
    assert "not found" in result.output.lower()
