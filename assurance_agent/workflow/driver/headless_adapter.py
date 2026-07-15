"""Headless adapter: spawn an arbitrary agent CLI once per phase.

Clean-room port of the TS createHeadlessAdapter (src/workflow/driver/
headless_adapter.ts). No session tree / streaming UI. The prompt is passed as
the trailing argv item (TS behavior) or, optionally, on stdin.
"""
import shlex
from pathlib import Path
from typing import Literal

from assurance_agent.workflow.driver.adapter import DriverError, PhaseRequest, PhaseResult
from assurance_agent.workflow.driver.process_runner import ProcessRunner, SubprocessRunner


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

    def run_phase(self, request: PhaseRequest) -> PhaseResult:
        if self._prompt_via == "stdin":
            argv = list(self._prefix)
            stdin_text: str | None = request.prompt
        else:
            argv = [*self._prefix, request.prompt]
            stdin_text = None

        result = self._runner.run(argv, self._cwd, timeout=self._timeout, stdin_text=stdin_text)

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
