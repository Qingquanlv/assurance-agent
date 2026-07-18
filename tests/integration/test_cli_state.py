import json
from pathlib import Path

from tests.helpers_aa import write_aa_config

from click.testing import CliRunner

from assurance_agent.cli import main

STATE = """schema_version: "1"
params: {}
phases:
  healing:
    status: in_progress
"""


def make_change(change_id: str = "CH-1") -> Path:
    write_aa_config(Path.cwd())
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
            [
                "state",
                "apply",
                "--change",
                "CH-1",
                "--phase",
                "inspect",
                "--skill",
                "aa-inspect",
                "--skill-md-path",
                "skills/aa-inspect/SKILL.md",
            ],
        )
        assert result.exit_code == 0, result.output
        state = yaml.safe_load((change_dir / "workflow-state.yaml").read_text())
        assert state["phases"]["inspect"]["status"] == "done"
        assert state["phases"]["inspect"]["skill_loaded"] is True
        assert state["phases"]["inspect"]["skill_md_path"] == "skills/aa-inspect/SKILL.md"
        assert "skill_loaded_at" in state["phases"]["inspect"]
        events = (change_dir / "events.jsonl").read_text().strip().splitlines()
        event = next(
            json.loads(line) for line in events if json.loads(line)["type"] == "phase_outcome_committed"
        )
        assert event["phase"] == "inspect"


def test_state_apply_commits_outcome_without_rechecking_exit_gate() -> None:
    from assurance_agent.workflow.orchestration.operations import record_dispatch

    runner = CliRunner()
    with runner.isolated_filesystem():
        change_dir = make_change()
        write_inspect_produces(change_dir)
        record_dispatch(change_dir, phase_id="inspect", kind="dispatch_phase", attempt_id="a-7")
        result = runner.invoke(
            main,
            [
                "state",
                "apply",
                "--change",
                "CH-1",
                "--phase",
                "inspect",
                "--attempt-id",
                "a-7",
                "--skill",
                "aa-inspect",
            ],
        )
        assert result.exit_code == 0, result.output
        events = [json.loads(line) for line in (change_dir / "events.jsonl").read_text().strip().splitlines()]
        outcome = next(e for e in events if e["type"] == "phase_outcome_committed")
        assert outcome["attempt_id"] == "a-7"


def test_state_apply_missing_declared_produces_exits_1_without_event() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        change_dir = make_change()
        result = runner.invoke(main, ["state", "apply", "--change", "CH-1", "--phase", "inspect"])
        assert result.exit_code == 1
        assert "missing declared produces" in result.output
        assert not (change_dir / "events.jsonl").exists()


def test_state_apply_strict_event_failure_rolls_back_and_exits_40(monkeypatch) -> None:
    from assurance_agent.workflow.core.events import EventWriteError

    def fail_append(*_a, **_k) -> None:
        raise EventWriteError("simulated")

    monkeypatch.setattr(
        "assurance_agent.workflow.core.progression.append_event_strict",
        fail_append,
    )
    runner = CliRunner()
    with runner.isolated_filesystem():
        change_dir = make_change()
        write_inspect_produces(change_dir)
        before = (change_dir / "workflow-state.yaml").read_text()
        result = runner.invoke(
            main,
            [
                "state",
                "apply",
                "--change",
                "CH-1",
                "--phase",
                "inspect",
                "--skill",
                "aa-inspect",
            ],
        )
        assert result.exit_code == 40
        assert (change_dir / "workflow-state.yaml").read_text() == before
        assert not (change_dir / "events.jsonl").exists()


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


def test_state_heal_strict_event_failure_rolls_back_and_exits_40(monkeypatch) -> None:
    from assurance_agent.workflow.core.events import EventWriteError

    def fail_append(*_a, **_k) -> None:
        raise EventWriteError("simulated")

    monkeypatch.setattr(
        "assurance_agent.workflow.core.progression.append_event_strict",
        fail_append,
    )
    runner = CliRunner()
    with runner.isolated_filesystem():
        change_dir = make_change()
        before = (change_dir / "workflow-state.yaml").read_text()
        result = runner.invoke(main, ["state", "heal", "--change", "CH-1", "--status", "resolved"])
        assert result.exit_code == 40
        assert (change_dir / "workflow-state.yaml").read_text() == before
        assert not (change_dir / "events.jsonl").exists()


def test_state_configure_merges_params_and_stamps_run_context() -> None:
    import yaml

    runner = CliRunner()
    with runner.isolated_filesystem():
        change_dir = make_change()
        result = runner.invoke(
            main,
            [
                "state",
                "configure",
                "--change",
                "CH-1",
                "--params-json",
                '{"run_mode": "api-only", "max_healing_attempts": 2}',
                "--orchestrator",
                "aa-execute",
            ],
        )
        assert result.exit_code == 0, result.output
        state = yaml.safe_load((change_dir / "workflow-state.yaml").read_text())
        assert state["params"] == {"run_mode": "api-only", "max_healing_attempts": 2}
        assert state["run_context"]["orchestrator_skill"] == "aa-execute"
        assert state["run_context"]["interaction_mode"] == "autonomous"
        assert state["run_context"]["active_scope"] == "execute"
        assert state["run_context"]["stamped_at"]
        # 既有 phases 保留；写入后读取方（aa status）不报错
        assert state["phases"]["healing"]["status"] == "in_progress"
        status = runner.invoke(main, ["status", "--change", "CH-1"])
        assert "status failed" not in status.output


def test_state_configure_intake_stamps_interactive_context() -> None:
    import yaml

    runner = CliRunner()
    with runner.isolated_filesystem():
        change_dir = make_change()
        result = runner.invoke(
            main,
            [
                "state",
                "configure",
                "--change",
                "CH-1",
                "--params-json",
                '{"run_mode": "review-case"}',
                "--orchestrator",
                "aa-intake",
            ],
        )
        assert result.exit_code == 0, result.output
        state = yaml.safe_load((change_dir / "workflow-state.yaml").read_text())
        assert state["run_context"]["interaction_mode"] == "interactive"
        assert state["run_context"]["active_scope"] == "intake"


def test_state_configure_unknown_param_key_exits_1_without_write() -> None:
    import yaml

    runner = CliRunner()
    with runner.isolated_filesystem():
        change_dir = make_change()
        result = runner.invoke(
            main,
            [
                "state",
                "configure",
                "--change",
                "CH-1",
                "--params-json",
                '{"bogus": 1}',
                "--orchestrator",
                "aa-workflow",
            ],
        )
        assert result.exit_code == 1
        assert 'unknown param "bogus"' in result.output
        state = yaml.safe_load((change_dir / "workflow-state.yaml").read_text())
        assert state["params"] == {}
        assert "run_context" not in state


def test_state_configure_invalid_orchestrator_exits_1() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        make_change()
        result = runner.invoke(
            main, ["state", "configure", "--change", "CH-1", "--orchestrator", "aws-workflow"]
        )
        assert result.exit_code == 1
        assert 'unsupported orchestrator "aws-workflow"' in result.output


def test_state_configure_invalid_params_json_exits_1() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        make_change()
        for bad in ("not json", "[1, 2]"):
            result = runner.invoke(
                main,
                [
                    "state",
                    "configure",
                    "--change",
                    "CH-1",
                    "--params-json",
                    bad,
                    "--orchestrator",
                    "aa-workflow",
                ],
            )
            assert result.exit_code == 1
            assert "Invalid --params-json" in result.output


def test_state_configure_run_mode_not_allowed_for_orchestrator_exits_1() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        make_change()
        result = runner.invoke(
            main,
            [
                "state",
                "configure",
                "--change",
                "CH-1",
                "--params-json",
                '{"run_mode": "api-only"}',
                "--orchestrator",
                "aa-intake",
            ],
        )
        assert result.exit_code == 1
        assert "aa-intake cannot run with run_mode api-only" in result.output
