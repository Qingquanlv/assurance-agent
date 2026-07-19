"""aa state configure — pre-run convenience; apply/heal removed with v1."""

from pathlib import Path

from click.testing import CliRunner

from assurance_agent.cli import main
from assurance_agent.workflow.core.events import append_event_best_effort
from tests.helpers_aa import write_aa_config

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


def test_state_apply_and_heal_commands_removed() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        make_change()
        apply = runner.invoke(main, ["state", "apply", "--change", "CH-1", "--phase", "inspect"])
        heal = runner.invoke(main, ["state", "heal", "--change", "CH-1", "--status", "resolved"])
        assert apply.exit_code != 0
        assert heal.exit_code != 0


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
        assert state["phases"]["healing"]["status"] == "in_progress"


def test_state_configure_refuses_after_graph_invocation_started() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        change_dir = make_change()
        # Opaque ledger presence is enough for the configure freeze guard.
        append_event_best_effort(
            change_dir,
            {"source": "graph", "type": "graph_invocation_started", "invocation_id": "inv-1"},
        )
        result = runner.invoke(
            main,
            [
                "state",
                "configure",
                "--change",
                "CH-1",
                "--params-json",
                '{"run_mode": "full"}',
                "--orchestrator",
                "aa-workflow",
            ],
        )
        assert result.exit_code == 1
        assert "frozen" in result.output.lower()


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
