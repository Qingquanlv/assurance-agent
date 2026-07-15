import json
from pathlib import Path

from tests.helpers_aa import write_aa_config

from click.testing import CliRunner

from assurance_agent.cli import main

STATE = """schema_version: "1"
params: {}
phases: {}
"""


def make_change(change_id: str = "CH-1") -> Path:
    write_aa_config(Path.cwd())
    change_dir = Path("qa/changes") / change_id
    (change_dir / "review").mkdir(parents=True)
    (change_dir / "workflow-state.yaml").write_text(STATE, encoding="utf-8")
    return change_dir


def test_gate_missing_change_exits_1() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        write_aa_config(Path.cwd())
        result = runner.invoke(main, ["gate", "check", "--change", "NOPE", "--phase", "case-review"])
        assert result.exit_code == 1
        assert "not found" in result.output


def test_gate_unknown_phase_exits_1() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        make_change()
        result = runner.invoke(main, ["gate", "check", "--change", "CH-1", "--phase", "no-such"])
        assert result.exit_code == 1
        assert "phase" in result.output.lower()


def test_gate_pass_verdict_exits_0_json() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        change_dir = make_change()
        (change_dir / "review/case-review.json").write_text(
            json.dumps({"schema_version": "1.0", "decision": "pass", "findings": []}),
            encoding="utf-8",
        )
        result = runner.invoke(
            main, ["gate", "check", "--change", "CH-1", "--phase", "case-review", "--json"]
        )
        assert result.exit_code == 0, result.output
        doc = json.loads(result.output)
        assert doc["verdict"] == "pass"


def test_gate_needs_fix_verdict_exits_30() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        change_dir = make_change()
        (change_dir / "review/case-review.json").write_text(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "decision": "needs_fix",
                    "auto_fix_allowed": True,
                    "findings": [{"id": "F1"}],
                }
            ),
            encoding="utf-8",
        )
        result = runner.invoke(main, ["gate", "check", "--change", "CH-1", "--phase", "case-review"])
        assert result.exit_code == 30


def test_gate_reject_verdict_exits_40() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        change_dir = make_change()
        (change_dir / "review/case-review.json").write_text(
            json.dumps({"schema_version": "1.0", "decision": "reject", "findings": []}),
            encoding="utf-8",
        )
        result = runner.invoke(main, ["gate", "check", "--change", "CH-1", "--phase", "case-review"])
        assert result.exit_code == 40
