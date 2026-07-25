from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from assurance_agent.retro.nightly.agent import build_retro_proposal_prompt, run_agent


def test_build_retro_proposal_prompt_references_skill_and_retro_id() -> None:
    prompt = build_retro_proposal_prompt("retro-20260716-000000")
    assert "aa-retro" in prompt
    assert "qa/retro/retro-20260716-000000/context.json" in prompt
    assert "qa/retro/retro-20260716-000000/proposals.json" in prompt
    assert "finding_kind" in prompt
    assert "payload" in prompt


def test_run_agent_appends_prompt_as_trailing_argv(tmp_path: Path) -> None:
    """Regression: without an appended prompt, `--print` mode agents like
    `cursor-agent` block waiting on a prompt that never arrives (observed as
    an indefinite hang: process alive, zero CPU progress, empty output).
    """
    retro_dir = tmp_path / "qa" / "retro" / "retro-1"
    retro_dir.mkdir(parents=True)

    captured_argv: list[str] = []

    class _FakeCompleted:
        returncode = 0

    def fake_run(argv, cwd, check):  # noqa: ANN001
        captured_argv.extend(argv)
        return _FakeCompleted()

    with patch("assurance_agent.retro.nightly.agent.subprocess.run", side_effect=fake_run) as mock_run:
        exit_code = run_agent("cursor-agent --print --trust", retro_dir)

    assert exit_code == 0
    mock_run.assert_called_once()
    assert captured_argv[:3] == ["cursor-agent", "--print", "--trust"]
    assert len(captured_argv) == 4
    assert "retro-1" in captured_argv[-1]
