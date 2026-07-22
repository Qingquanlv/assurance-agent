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


def test_with_model_injects_and_rewrites() -> None:
    from assurance_agent.workflow.driver.headless_adapter import _with_model

    injected = _with_model(["cursor-agent", "--print", "--trust"], "cursor-grok-4.5-high-fast")
    assert injected[:4] == ["cursor-agent", "--model", "cursor-grok-4.5-high-fast", "--print"]

    rewritten = _with_model(
        ["cursor-agent", "--print", "--model", "old-model", "--trust"],
        "cursor-grok-4.5-high-fast",
    )
    assert rewritten[rewritten.index("--model") + 1] == "cursor-grok-4.5-high-fast"


def test_headless_adapter_pins_model_in_argv(tmp_path: Path) -> None:
    from assurance_agent.workflow.driver.process_runner import ProcessResult

    class _Capture:
        def __init__(self) -> None:
            self.argv: list[str] | None = None

        def run(self, argv, cwd, timeout=None, stdin_text=None):  # noqa: ANN001
            self.argv = list(argv)
            return ProcessResult(exit_code=0, stdout="", stderr="", timed_out=False)

    capture = _Capture()
    adapter = HeadlessAdapter(
        agent_cmd="cursor-agent --print --trust",
        cwd=tmp_path,
        model="cursor-grok-4.5-high-fast",
        runner=capture,  # type: ignore[arg-type]
    )
    assert adapter.run_phase(_request()).ok is True
    assert capture.argv is not None
    assert capture.argv[capture.argv.index("--model") + 1] == "cursor-grok-4.5-high-fast"


def test_invoke_rewrites_workspace_flag(tmp_path: Path) -> None:
    from assurance_agent.workflow.driver.headless_adapter import _with_workspace
    from assurance_agent.workflow.graph.agent_api import AgentRequest

    private = tmp_path / "task-root"
    private.mkdir()
    rewritten = _with_workspace(
        ["cursor-agent", "--print", "--workspace", str(tmp_path / "sut"), "--trust"],
        private,
    )
    assert rewritten[rewritten.index("--workspace") + 1] == str(private)

    class _Capture:
        def __init__(self) -> None:
            self.argv: list[str] | None = None
            self.cwd: Path | None = None

        def run(self, argv, cwd, timeout=None, stdin_text=None):  # noqa: ANN001
            from assurance_agent.workflow.driver.process_runner import ProcessResult

            self.argv = list(argv)
            self.cwd = Path(cwd)
            return ProcessResult(exit_code=0, stdout="", stderr="", timed_out=False)

    capture = _Capture()
    adapter = HeadlessAdapter(
        agent_cmd=f"cursor-agent --print --workspace {tmp_path / 'wrong'} --trust",
        cwd=tmp_path,
        runner=capture,  # type: ignore[arg-type]
    )
    result = adapter.invoke(
        AgentRequest(
            target="skill:aa-explore",
            node_id="explore",
            change_id="CH-1",
            workspace_root=private,
            allowed_writes=("change:explore/**",),
            prompt="go",
            timeout_seconds=30.0,
        )
    )
    assert result.ok is True
    assert capture.cwd == private
    assert capture.argv is not None
    assert capture.argv[capture.argv.index("--workspace") + 1] == str(private)
    assert capture.argv[-1] == "go"


def test_invoke_injects_workspace_for_cursor_agent(tmp_path: Path) -> None:
    from assurance_agent.workflow.driver.headless_adapter import _with_workspace

    private = tmp_path / "ws"
    private.mkdir()
    injected = _with_workspace(["cursor-agent", "--print", "--trust"], private)
    assert injected[:3] == ["cursor-agent", "--workspace", str(private)]


@pytest.mark.parametrize(
    ("stderr", "kind"),
    [
        ("RetriableError: [unavailable] PING timed out\n", "transport"),
        ("Error: [aborted] Client network socket disconnected before secure TLS connection was established\n", "transport"),
        # cursor-agent transient auth/session flake: it momentarily fails to
        # fetch the model catalogue and rejects the pinned model with an empty
        # available list. Must be retryable, not a fatal internal error.
        ("Cannot use this model: cursor-grok-4.5-high-fast. Available models: \n", "transport"),
        ("error: no available models\n", "transport"),
        ("kaboom: assertion failed\n", "internal"),
    ],
)
def test_invoke_classifies_network_failures_as_transport(
    tmp_path: Path, stderr: str, kind: str
) -> None:
    from assurance_agent.workflow.graph.agent_api import AgentRequest

    class _Fail:
        def run(self, argv, cwd, timeout=None, stdin_text=None):  # noqa: ANN001
            from assurance_agent.workflow.driver.process_runner import ProcessResult

            return ProcessResult(exit_code=1, stdout="", stderr=stderr, timed_out=False)

    private = tmp_path / "ws"
    private.mkdir()
    adapter = HeadlessAdapter(agent_cmd="cursor-agent --print --trust", cwd=tmp_path, runner=_Fail())  # type: ignore[arg-type]
    result = adapter.invoke(
        AgentRequest(
            target="skill:aa-explore",
            node_id="explore",
            change_id="CH-1",
            workspace_root=private,
            allowed_writes=("change:explore/**",),
            prompt="go",
            timeout_seconds=30.0,
        )
    )
    assert result.ok is False
    assert result.error_kind == kind
