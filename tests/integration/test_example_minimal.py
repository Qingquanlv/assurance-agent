"""End-to-end smoke of examples/minimal-sut in a throwaway copy.

Copies the packaged example into tmp, runs `aa init --yes`, then
`aa status --change CH-DEMO-001 --json`, asserting the CLI produces a
parseable WorkflowStatus snapshot. Exit code is a terminal-state signal
(0 running/completed, 20 stopped, 30 needs_human_review); exit 40 is a
command/data error and fails this smoke test. We assert shape, not one terminal.
"""

import json
import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner

from assurance_agent.cli import main

EXAMPLE_DIR = Path(__file__).parents[2] / "examples" / "minimal-sut"


def test_example_minimal_runs_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dest = tmp_path / "aa-demo"
    shutil.copytree(EXAMPLE_DIR, dest)
    monkeypatch.chdir(dest)
    runner = CliRunner()

    init_result = runner.invoke(main, ["init", "--yes"])
    assert init_result.exit_code == 0, init_result.output
    assert Path(".aa/config.yaml").is_file()
    # the pre-seeded change survives init (init only writes .gitkeep under qa/changes)
    assert Path("qa/changes/CH-DEMO-001/.qa.yaml").is_file()
    assert Path("qa/changes/CH-DEMO-001/proposal.md").is_file()

    status_result = runner.invoke(main, ["status", "--change", "CH-DEMO-001", "--json"])
    assert status_result.exit_code in (0, 20, 30), status_result.output
    doc = json.loads(status_result.output)
    assert "phases" in doc
    assert "next_dispatch" in doc
    assert "terminal" in doc
