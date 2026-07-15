import json
from pathlib import Path

from click.testing import CliRunner

from assurance_agent.cli import main

STATE = """schema_version: "1"
params: {}
phases:
  healing:
    status: in_progress
"""


def make_change(change_id: str = "CH-1") -> Path:
    change_dir = Path("qa/changes") / change_id
    (change_dir / "review").mkdir(parents=True)
    (change_dir / "workflow-state.yaml").write_text(STATE, encoding="utf-8")
    return change_dir


def write_inspect_produces(change_dir: Path) -> None:
    (change_dir / "inspect").mkdir(exist_ok=True)
    (change_dir / "inspect/failure-analysis.json").write_text("{}", encoding="utf-8")
    (change_dir / "inspect/quality-gate-result.json").write_text("{}", encoding="utf-8")


def test_state_apply_unknown_phase_exits_1() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        make_change()
        result = runner.invoke(main, ["state", "apply", "--change", "CH-1", "--phase", "no-such"])
        assert result.exit_code == 1
        assert "phase" in result.output.lower()


def test_state_apply_advances_phase_and_records_skill_load_gate() -> None:
    import yaml

    runner = CliRunner()
    with runner.isolated_filesystem():
        change_dir = make_change()
        write_inspect_produces(change_dir)
        result = runner.invoke(
            main,
            ["state", "apply", "--change", "CH-1", "--phase", "inspect",
             "--skill", "aa-inspect", "--skill-md-path", "skills/aa-inspect/SKILL.md"],
        )
        assert result.exit_code == 0, result.output
        state = yaml.safe_load((change_dir / "workflow-state.yaml").read_text())
        assert state["phases"]["inspect"]["status"] == "done"
        assert state["phases"]["inspect"]["skill_loaded"] is True
        assert state["phases"]["inspect"]["skill_md_path"] == "skills/aa-inspect/SKILL.md"
        assert "skill_loaded_at" in state["phases"]["inspect"]
        events = (change_dir / "events.jsonl").read_text().strip().splitlines()
        event = next(json.loads(line) for line in events if json.loads(line)["type"] == "phase_outcome_committed")
        assert event["phase"] == "inspect"


def test_state_apply_commits_outcome_without_rechecking_exit_gate() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        change_dir = make_change()
        write_inspect_produces(change_dir)
        result = runner.invoke(
            main,
            ["state", "apply", "--change", "CH-1", "--phase", "inspect", "--attempt-id", "a-7"],
        )
        assert result.exit_code == 0, result.output
        event = json.loads((change_dir / "events.jsonl").read_text().strip())
        assert event["type"] == "phase_outcome_committed"
        assert event["attempt_id"] == "a-7"


def test_state_apply_missing_declared_produces_exits_1_without_event() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        change_dir = make_change()
        result = runner.invoke(
            main, ["state", "apply", "--change", "CH-1", "--phase", "inspect"]
        )
        assert result.exit_code == 1
        assert "missing declared produces" in result.output
        assert not (change_dir / "events.jsonl").exists()


def test_state_apply_strict_event_failure_rolls_back_and_exits_40() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        change_dir = make_change()
        write_inspect_produces(change_dir)
        before = (change_dir / "workflow-state.yaml").read_text()
        (change_dir / "events.jsonl").mkdir()
        result = runner.invoke(main, ["state", "apply", "--change", "CH-1", "--phase", "inspect"])
        assert result.exit_code == 40
        assert (change_dir / "workflow-state.yaml").read_text() == before


def test_state_heal_records_transition() -> None:
    import yaml

    runner = CliRunner()
    with runner.isolated_filesystem():
        change_dir = make_change()
        result = runner.invoke(main, ["state", "heal", "--change", "CH-1", "--status", "resolved"])
        assert result.exit_code == 0, result.output
        state = yaml.safe_load((change_dir / "workflow-state.yaml").read_text())
        assert state["phases"]["healing"]["status"] == "resolved"
        events = (change_dir / "events.jsonl").read_text().strip().splitlines()
        assert any(json.loads(line)["type"] == "heal_transition" for line in events)


def test_state_heal_invalid_status_exits_1() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        make_change()
        result = runner.invoke(main, ["state", "heal", "--change", "CH-1", "--status", "banana"])
        assert result.exit_code == 1
        assert "banana" in result.output


def test_state_heal_strict_event_failure_rolls_back_and_exits_40() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        change_dir = make_change()
        before = (change_dir / "workflow-state.yaml").read_text()
        (change_dir / "events.jsonl").mkdir()
        result = runner.invoke(main, ["state", "heal", "--change", "CH-1", "--status", "resolved"])
        assert result.exit_code == 40
        assert (change_dir / "workflow-state.yaml").read_text() == before
