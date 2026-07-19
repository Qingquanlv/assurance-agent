"""Headless adapter: spawn an arbitrary agent CLI once per phase.

Clean-room port of the TS createHeadlessAdapter (src/workflow/driver/
headless_adapter.ts). No session tree / streaming UI. The prompt is passed as
the trailing argv item (TS behavior) or, optionally, on stdin.

同时实现 graph 的 ``AgentInvoker``：v2 路径在 ``request.workspace_root``（task
私有 workspace 的物化 root）中运行进程，并把失败归一化为 typed error kind
（timeout → ``timeout``，非零退出 → ``internal``，spawn/transport → ``transport``）。
"""

import shlex
from pathlib import Path
from typing import Literal

from assurance_agent.workflow.driver.adapter import DriverError, PhaseRequest, PhaseResult
from assurance_agent.workflow.driver.process_runner import (
    ProcessResult,
    ProcessRunner,
    SubprocessRunner,
)
from assurance_agent.workflow.graph.agent_api import AgentRequest, AgentResult


class HeadlessAdapter:
    def __init__(
        self,
        agent_cmd: str,
        cwd: Path,
        *,
        runner: ProcessRunner | None = None,
        timeout: float | None = None,
        prompt_via: Literal["argv", "stdin"] = "argv",
    ) -> None:
        self._prefix = shlex.split(agent_cmd)
        if not self._prefix:
            raise DriverError("--agent-cmd must not be empty")
        self._cwd = cwd
        self._runner = runner or SubprocessRunner()
        self._timeout = timeout
        self._prompt_via = prompt_via

    def _run(self, prompt: str, cwd: Path, timeout: float | None) -> ProcessResult:
        if self._prompt_via == "stdin":
            argv = list(self._prefix)
            stdin_text: str | None = prompt
        else:
            argv = [*self._prefix, prompt]
            stdin_text = None
        return self._runner.run(argv, cwd, timeout=timeout, stdin_text=stdin_text)

    def run_phase(self, request: PhaseRequest) -> PhaseResult:
        result = self._run(request.prompt, self._cwd, self._timeout)

        if result.timed_out:
            return PhaseResult(
                ok=False,
                output=result.stdout,
                error=f"headless agent timed out after {self._timeout}s",
            )
        if result.exit_code != 0:
            detail = (result.stderr or result.stdout)[:800]
            return PhaseResult(
                ok=False,
                output=result.stdout,
                error=f"headless agent failed (exit {result.exit_code}): {detail}",
            )
        return PhaseResult(ok=True, output=result.stdout)

    def invoke(self, request: AgentRequest) -> AgentResult:
        """graph AgentInvoker：在 task 私有 workspace root 中运行 agent 进程。"""
        try:
            result = self._run(
                request.prompt,
                Path(request.workspace_root),
                request.timeout_seconds,
            )
        except OSError as exc:
            return AgentResult(
                ok=False,
                error_kind="transport",
                error=f"headless agent spawn failed: {exc}",
            )
        if result.timed_out:
            return AgentResult(
                ok=False,
                error_kind="timeout",
                error=f"headless agent timed out after {request.timeout_seconds}s",
            )
        if result.exit_code != 0:
            detail = (result.stderr or result.stdout)[:800]
            return AgentResult(
                ok=False,
                error_kind="internal",
                error=f"headless agent failed (exit {result.exit_code}): {detail}",
            )
        return AgentResult(ok=True)
