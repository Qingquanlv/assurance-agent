import json
from pathlib import Path

import yaml
from click.testing import CliRunner

from assurance_agent.cli import main

STATE = """schema_version: "1"
params:
  run_mode: full
phases:
  skill_registry_check:
    status: pass
  explore:
    status: done
"""


def make_change(change_id: str = "CH-1") -> Path:
    change_dir = Path("qa/changes") / change_id
    change_dir.mkdir(parents=True)
    (change_dir / "workflow-state.yaml").write_text(STATE, encoding="utf-8")
    return change_dir


def test_decide_missing_change_exits_1() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(
            main, ["decide", "--change", "NOPE", "--at", "case-review", "--action", "fix_and_proceed", "--reason", "x"]
        )
        assert result.exit_code == 1
        assert "not found" in result.output


def test_decide_unsupported_action_exits_1() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        make_change()
        result = runner.invoke(
            main, ["decide", "--change", "CH-1", "--at", "case-review", "--action", "wibble", "--reason", "x"]
        )
        assert result.exit_code == 1
        assert "wibble" in result.output


def test_decide_records_event_and_appends_state_decision() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        change_dir = make_change()
        result = runner.invoke(
            main,
            ["decide", "--change", "CH-1", "--at", "case-review", "--action", "fix_and_proceed", "--reason", "looks good"],
        )
        assert result.exit_code == 0, result.output
        events = [json.loads(line) for line in (change_dir / "events.jsonl").read_text().strip().splitlines()]
        rec = next(e for e in events if e["type"] == "human_decision")
        assert rec["action"] == "fix_and_proceed"
        assert rec["checkpoint"] == "case-review"
        assert rec["reason"] == "looks good"
        assert len(events) == 1
        state = yaml.safe_load((change_dir / "workflow-state.yaml").read_text())
        assert state["decisions"][-1]["action"] == "fix_and_proceed"


def test_decide_stop_marks_terminal() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        change_dir = make_change()
        result = runner.invoke(
            main, ["decide", "--change", "CH-1", "--at", "workflow", "--action", "stop", "--reason", "halt"]
        )
        assert result.exit_code == 0, result.output
        state = yaml.safe_load((change_dir / "workflow-state.yaml").read_text())
        assert state["terminal"]["kind"] == "stopped"
        events = [json.loads(line) for line in (change_dir / "events.jsonl").read_text().strip().splitlines()]
        assert any(e["type"] == "human_decision" for e in events)
        assert isinstance(state["decisions"][-1]["state_at_stop"]["next"], list)


def test_decide_strict_event_failure_rolls_back_and_exits_40() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        change_dir = make_change()
        before = (change_dir / "workflow-state.yaml").read_text()
        (change_dir / "events.jsonl").mkdir()  # 不可写 -> EventWriteError
        result = runner.invoke(
            main, ["decide", "--change", "CH-1", "--at", "case-review", "--action", "fix_and_proceed", "--reason", "x"]
        )
        assert result.exit_code == 40
        assert (change_dir / "workflow-state.yaml").read_text() == before


def test_decide_empty_reason_rejected() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        make_change()
        result = runner.invoke(
            main, ["decide", "--change", "CH-1", "--at", "case-review", "--action", "fix_and_proceed", "--reason", "   "]
        )
        assert result.exit_code == 1
        assert "reason" in result.output.lower()
