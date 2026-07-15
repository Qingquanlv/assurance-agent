import sys
from pathlib import Path

from assurance_agent.workflow.driver.process_runner import (
    ProcessRunner,
    SubprocessRunner,
    resolve_aa_command,
)


def _script(tmp_path: Path, body: str) -> str:
    path = tmp_path / "stub.py"
    path.write_text(body, encoding="utf-8")
    return str(path)


def test_subprocess_runner_satisfies_protocol() -> None:
    assert isinstance(SubprocessRunner(), ProcessRunner)


def test_run_captures_stdout_and_exit_zero(tmp_path: Path) -> None:
    script = _script(tmp_path, "import sys; sys.stdout.write('hi:' + sys.argv[1])")
    result = SubprocessRunner().run([sys.executable, script, "world"], tmp_path)
    assert result.exit_code == 0
    assert result.stdout == "hi:world"
    assert result.timed_out is False


def test_run_nonzero_exit_and_stderr_captured(tmp_path: Path) -> None:
    script = _script(tmp_path, "import sys; sys.stderr.write('nope'); sys.exit(3)")
    result = SubprocessRunner().run([sys.executable, script], tmp_path)
    assert result.exit_code == 3
    assert "nope" in result.stderr


def test_run_forwards_stdin(tmp_path: Path) -> None:
    script = _script(tmp_path, "import sys; sys.stdout.write('got:' + sys.stdin.read())")
    result = SubprocessRunner().run([sys.executable, script], tmp_path, stdin_text="PROMPT")
    assert result.stdout == "got:PROMPT"


def test_run_timeout_sets_flag(tmp_path: Path) -> None:
    script = _script(tmp_path, "import time; time.sleep(5)")
    result = SubprocessRunner().run([sys.executable, script], tmp_path, timeout=0.3)
    assert result.timed_out is True
    assert result.exit_code != 0


def test_resolve_aa_command_is_nonempty_list() -> None:
    cmd = resolve_aa_command()
    assert isinstance(cmd, list) and cmd and all(isinstance(p, str) for p in cmd)
