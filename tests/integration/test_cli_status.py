"""aa status — GraphStatus projection (no v1 phases)."""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from assurance_agent.cli import main
from tests.helpers_aa import write_aa_config


def _make_change() -> Path:
    write_aa_config(Path.cwd())
    change = Path("qa/changes/CH-1")
    change.mkdir(parents=True)
    (change / "workflow-state.yaml").write_text("params: {}\n", encoding="utf-8")
    return change


def test_status_no_invocation_json() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        _make_change()
        result = runner.invoke(main, ["status", "--change", "CH-1", "--json"])
        assert result.exit_code == 0
        assert json.loads(result.output) == {"status": None}


def test_status_missing_change_exits_1() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        write_aa_config(Path.cwd())
        result = runner.invoke(main, ["status", "--change", "NOPE", "--json"])
        assert result.exit_code == 1
