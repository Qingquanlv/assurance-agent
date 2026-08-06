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


# --------------------------------------------------------------------------- #
# gate warnings in the human output
# --------------------------------------------------------------------------- #

_BROKEN_POLICY = "version: 1\nevidence_sufficiency:\n  recency_hours: -5\n"


def test_a_gate_warning_is_printed_without_changing_the_verdict(project, monkeypatch) -> None:
    """A shadow sufficiency failure reaches the operator.

    The gate records it in `warnings`, but `aa run` printed only the manifest,
    and the manifest has no warnings field — so before this the only way to
    learn that nothing was judged was to open the gate JSON.
    """
    root, _ = project
    (root / ".aa" / "policy.yaml").write_text(_BROKEN_POLICY, encoding="utf-8")
    monkeypatch.setattr(runners_mod.subprocess, "run", _stub_pytest("passed"))

    result = CliRunner().invoke(main, ["run", "--change", "CH-1"])

    assert "EVIDENCE-SUFFICIENCY-NOT-EVALUATED" in result.output
    assert "error_code=policy_error" in result.output
    # The verdict, its messaging and the exit code are all untouched by it.
    assert result.exit_code == 0
    assert "Final Status : PASS" in result.output
    assert "Quality gate passed" in result.output
    assert "PASS_WITH_WARNINGS" not in result.output


def test_nothing_is_printed_when_the_gate_has_no_warnings(project, monkeypatch) -> None:
    monkeypatch.setattr(runners_mod.subprocess, "run", _stub_pytest("passed"))

    result = CliRunner().invoke(main, ["run", "--change", "CH-1"])

    assert result.exit_code == 0
    assert "Warnings" not in result.output
    assert "EVIDENCE-SUFFICIENCY-NOT-EVALUATED" not in result.output


@pytest.mark.parametrize("damage", ["malformed", "missing"])
def test_an_unreadable_gate_file_still_reports_the_run(project, monkeypatch, damage: str) -> None:
    """Warnings are a convenience read of a file the run just published, not a
    second source of truth. Losing it must cost the operator the warnings and
    nothing else — the verdict and the exit code come from the manifest."""
    _, change = project
    monkeypatch.setattr(runners_mod.subprocess, "run", _stub_pytest("passed"))
    real_execute = run_cmd_mod._execute

    def damaging(*args, **kwargs):
        manifest = real_execute(*args, **kwargs)
        gate = change / "execution" / "quality-gate-result.json"
        if damage == "malformed":
            gate.write_text("{not json", encoding="utf-8")
        else:
            gate.unlink()
        return manifest

    monkeypatch.setattr(run_cmd_mod, "_execute", damaging)
    result = CliRunner().invoke(main, ["run", "--change", "CH-1"])

    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert result.exit_code == 0
    assert "Final Status : PASS" in result.output
    assert "Quality gate passed" in result.output
