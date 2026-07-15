import sys
from pathlib import Path

import pytest

from assurance_agent.workflow.driver.adapter import Adapter, DriverError, PhaseRequest
from assurance_agent.workflow.driver.headless_adapter import HeadlessAdapter


def _script(tmp_path: Path, body: str) -> str:
    path = tmp_path / "agent.py"
    path.write_text(body, encoding="utf-8")
    return str(path)


def _request() -> PhaseRequest:
    return PhaseRequest(change_id="CH-1", phase_id="explore", skill="aa-explore", prompt="hello")


def test_headless_adapter_satisfies_protocol(tmp_path: Path) -> None:
    adapter = HeadlessAdapter(agent_cmd="echo", cwd=tmp_path)
    assert isinstance(adapter, Adapter)


def test_argv_prompt_success(tmp_path: Path) -> None:
    script = _script(tmp_path, "import sys; sys.stdout.write('AGENT:' + sys.argv[-1])")
    adapter = HeadlessAdapter(agent_cmd=f"{sys.executable} {script}", cwd=tmp_path)
    result = adapter.run_phase(_request())
    assert result.ok is True
    assert result.output == "AGENT:hello"


def test_stdin_prompt_success(tmp_path: Path) -> None:
    script = _script(tmp_path, "import sys; sys.stdout.write('STDIN:' + sys.stdin.read())")
    adapter = HeadlessAdapter(agent_cmd=f"{sys.executable} {script}", cwd=tmp_path, prompt_via="stdin")
    result = adapter.run_phase(_request())
    assert result.ok is True
    assert result.output == "STDIN:hello"


def test_nonzero_exit_returns_not_ok(tmp_path: Path) -> None:
    script = _script(tmp_path, "import sys; sys.stderr.write('kaboom'); sys.exit(2)")
    adapter = HeadlessAdapter(agent_cmd=f"{sys.executable} {script}", cwd=tmp_path)
    result = adapter.run_phase(_request())
    assert result.ok is False
    assert result.error is not None and "exit 2" in result.error
    assert "kaboom" in result.error


def test_timeout_returns_not_ok(tmp_path: Path) -> None:
    script = _script(tmp_path, "import time; time.sleep(5)")
    adapter = HeadlessAdapter(agent_cmd=f"{sys.executable} {script}", cwd=tmp_path, timeout=0.3)
    result = adapter.run_phase(_request())
    assert result.ok is False
    assert result.error is not None and "timed out" in result.error


def test_empty_agent_cmd_raises() -> None:
    with pytest.raises(DriverError):
        HeadlessAdapter(agent_cmd="   ", cwd=Path("."))
