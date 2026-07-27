# tests/integration/test_eval_cli.py
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

from tests.helpers_aa import write_aa_config

import yaml
from click.testing import CliRunner

from assurance_agent.cli import main


def _seed(project_root: Path) -> None:
    suites = project_root / "eval" / "suites"
    suites.mkdir(parents=True)
    (suites / "workflow-case.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "workflow-case",
                "scorer": "workflow-case",
                "executor": {"type": "workflow-run", "entrypoint": "case"},
                "thresholds": [
                    {"metric": "case_review_gate_pass_rate", "gate": "hard", "op": "gte", "value": 0.99},
                ],
            }
        ),
        encoding="utf-8",
    )
    ds = project_root / "eval" / "datasets" / "workflow-case"
    ds.mkdir(parents=True)
    (ds / "WC-001.yaml").write_text(
        yaml.safe_dump(
            {
                "id": "WC-001",
                "suite": "workflow-case",
                "input": {"change_id": "eval-sample-001"},
                "expected": {},
            }
        ),
        encoding="utf-8",
    )
    sut = project_root / "sut"
    write_aa_config(sut)
    change = sut / "qa" / "changes" / "eval-sample-001" / "review"
    change.mkdir(parents=True)
    (change / "case-review.json").write_text(json.dumps({"decision": "pass"}), encoding="utf-8")
    # Fresh throwaway repo in the isolated filesystem — write-scan requires the
    # SUT workspace to be a git repo (same precondition as the TS executor).
    subprocess.run(["git", "init"], cwd=sut, check=True, capture_output=True)


def _stub_execute_attempt(monkeypatch):
    from assurance_agent.eval import runner as runner_mod
    from assurance_agent.eval.types import ExecutionResult

    def fake_execute(sample, attempt_dir, **kwargs):
        attempt_dir.mkdir(parents=True, exist_ok=True)
        raw = attempt_dir / "raw-output"
        raw.mkdir(parents=True, exist_ok=True)
        review = raw / "review"
        review.mkdir(parents=True, exist_ok=True)
        (review / "case-review.json").write_text('{"decision":"pass"}', encoding="utf-8")
        (attempt_dir / "stdout.log").write_text("ok\n", encoding="utf-8")
        (attempt_dir / "stderr.log").write_text("", encoding="utf-8")
        (attempt_dir / "execution.json").write_text('{"exit_code":0}', encoding="utf-8")
        return ExecutionResult(
            sample_id=sample.id, attempt=0, executor="workflow-run", status="ok", exit_code=0
        )

    monkeypatch.setattr(runner_mod, "execute_attempt", fake_execute)


def test_eval_run_json_shape(monkeypatch) -> None:
    runner = CliRunner()
    with runner.isolated_filesystem() as fs:
        project_root = Path(fs)
        _seed(project_root)
        monkeypatch.setenv("AA_EVAL_FAKE_ADAPTER", "1")
        _stub_execute_attempt(monkeypatch)
        result = runner.invoke(
            main,
            [
                "eval",
                "run",
                "--suite",
                "workflow-case",
                "--sut-dir",
                str(project_root / "sut"),
                "--json",
            ],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output.strip().splitlines()[-1])
        assert payload["verdict"] == "pass"
        assert payload["run_id"].startswith("eval-")


def test_eval_run_output_id_prints_only_run_id(monkeypatch) -> None:
    runner = CliRunner()
    with runner.isolated_filesystem() as fs:
        project_root = Path(fs)
        _seed(project_root)
        monkeypatch.setenv("AA_EVAL_FAKE_ADAPTER", "1")
        result = runner.invoke(
            main,
            [
                "eval",
                "run",
                "--suite",
                "workflow-case",
                "--sut-dir",
                str(project_root / "sut"),
                "--output",
                "id",
            ],
        )
        assert result.exit_code == 0, result.output
        assert result.output.strip().startswith("eval-")


def test_eval_run_requires_suite_or_plan() -> None:
    result = CliRunner().invoke(main, ["eval", "run"])
    assert result.exit_code == 1
    assert "--suite" in result.output


def test_eval_run_suite_and_plan_mutually_exclusive() -> None:
    result = CliRunner().invoke(main, ["eval", "run", "--suite", "x", "--plan", "p.json"])
    assert result.exit_code == 1
    assert "mutually exclusive" in result.output


def test_eval_run_passes_resolved_extra_memory_dir(monkeypatch) -> None:
    runner = CliRunner()
    with runner.isolated_filesystem() as fs:
        project_root = Path(fs)
        _seed(project_root)
        overlay = project_root / "overlay"
        (overlay / ".aa" / "memory").mkdir(parents=True)
        seen: dict = {}

        def fake_run_suite(**kwargs):  # noqa: ANN003, ANN202
            seen.update(kwargs)
            return "eval-1", SimpleNamespace(verdict="pass")

        monkeypatch.setattr("assurance_agent.commands.eval_cmd.run_suite", fake_run_suite)
        result = runner.invoke(
            main,
            [
                "eval",
                "run",
                "--suite",
                "workflow-case",
                "--sut-dir",
                str(project_root / "sut"),
                "--extra-memory-dir",
                str(overlay),
            ],
        )

        assert result.exit_code == 0, result.output
        assert seen["extra_memory_dir"] == overlay.resolve()


def test_eval_plan_writes_json() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(
            main,
            [
                "eval",
                "plan",
                "--event",
                "manual",
                "--suite",
                "workflow-case",
                "--out",
                "eval-plan.json",
            ],
        )
        assert result.exit_code == 0, result.output
        plan = json.loads(Path("eval-plan.json").read_text())
        assert plan["suites"] == ["workflow-case"]


def test_eval_report_json(monkeypatch) -> None:
    runner = CliRunner()
    with runner.isolated_filesystem() as fs:
        project_root = Path(fs)
        _seed(project_root)
        monkeypatch.setenv("AA_EVAL_FAKE_ADAPTER", "1")
        _stub_execute_attempt(monkeypatch)
        run = runner.invoke(
            main,
            [
                "eval",
                "run",
                "--suite",
                "workflow-case",
                "--change",
                "RET-user-1",
                "--change",
                "RET-role-1",
                "--sut-dir",
                str(project_root / "sut"),
                "--output",
                "id",
            ],
        )
        run_id = run.output.strip()
        result = runner.invoke(
            main,
            [
                "eval",
                "report",
                "--run",
                run_id,
                "--json",
                "--sut-dir",
                str(project_root / "sut"),
            ],
        )
        assert result.exit_code == 0, result.output
        report = json.loads(result.output)
        assert report["run_id"] == run_id
        assert report["verdict"] == "pass"
        assert report["source_change_ids"] == ["RET-user-1", "RET-role-1"]
