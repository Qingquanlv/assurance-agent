import json
from pathlib import Path

from tests.helpers_aa import write_aa_config

from click.testing import CliRunner

from assurance_agent.cli import main

RUNNING_STATE = """schema_version: "1"
params:
  run_mode: full
phases:
  skill_registry_check:
    status: pass
  explore:
    status: done
    skill_loaded: true
"""


def make_change(change_id: str = "CH-1", state_yaml: str = RUNNING_STATE) -> Path:
    write_aa_config(Path.cwd())
    change_dir = Path("qa/changes") / change_id
    change_dir.mkdir(parents=True)
    (change_dir / "workflow-state.yaml").write_text(state_yaml, encoding="utf-8")
    return change_dir


def test_status_missing_change_exits_1() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        write_aa_config(Path.cwd())
        result = runner.invoke(main, ["status", "--change", "NOPE"])
        assert result.exit_code == 1
        assert "not found" in result.output


def test_status_running_json_shape_and_exit_0() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        make_change()
        result = runner.invoke(main, ["status", "--change", "CH-1", "--json"])
        assert result.exit_code == 0, result.output
        doc = json.loads(result.output)
        assert "phases" in doc
        assert "next_dispatch" in doc
        assert doc["terminal"] is None
        for entry in doc["next_dispatch"]:
            assert set(entry) >= {"phase_id", "skill", "agent", "kind"}


def test_status_next_json_limits_to_dispatch_list() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        make_change()
        result = runner.invoke(main, ["status", "--change", "CH-1", "--next", "--json"])
        assert result.exit_code == 0, result.output
        doc = json.loads(result.output)
        assert set(doc) == {"next_dispatch", "terminal"}


def test_status_next_human_lists_phase_ids() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        make_change()
        result = runner.invoke(main, ["status", "--change", "CH-1", "--next"])
        assert result.exit_code == 0, result.output
        assert result.output.strip() != ""


def test_status_stopped_terminal_exits_20() -> None:
    stopped_state = """schema_version: "1"
params: {}
phases:
  skill_registry_check:
    status: fail
"""
    runner = CliRunner()
    with runner.isolated_filesystem():
        make_change(state_yaml=stopped_state)
        result = runner.invoke(main, ["status", "--change", "CH-1", "--json"])
        doc = json.loads(result.output)
        assert doc["terminal"]["kind"] == "stopped"
        assert result.exit_code == 20
