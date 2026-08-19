"""Headless adapter: spawn an arbitrary agent CLI once per phase.

Clean-room port of the TS createHeadlessAdapter (src/workflow/driver/
headless_adapter.ts). No session tree / streaming UI. The prompt is passed as
the trailing argv item (TS behavior) or, optionally, on stdin.

同时实现 graph 的 ``AgentInvoker``：v2 路径在 ``request.workspace_root``（task
私有 workspace 的物化 root）中运行进程，并把失败归一化为 typed error kind
（timeout → ``timeout``，网络/TLS/unavailable → ``transport``，spawn 失败 →
``transport``，其余非零退出 → ``internal``）。

``--agent-cmd`` 里若带有 ``--workspace``/``-w``，invoke 会改写成 task 私有 root；
否则对 cursor-agent 类 CLI 自动补上 ``--workspace <root>``。否则 agent 会写到
SUT 真根，freeze 在私有副本里找不到 outputs，repair 又会把真根上的文件当成
untracked drift。
"""

import re
import shlex
from pathlib import Path
from typing import Literal

from assurance_kernel.workflow.driver.adapter import DriverError, PhaseRequest, PhaseResult
from assurance_kernel.workflow.driver.process_runner import (
    ProcessResult,
    ProcessRunner,
    SubprocessRunner,
)
from assurance_kernel.workflow.graph.agent_api import AgentRequest, AgentResult

# cursor-agent / network flakes that should consume transport retry budget.
_TRANSPORT_FAILURE = re.compile(
    r"(?i)("
    r"PING timed out|RetriableError|\[unavailable\]|\[aborted\]|"
    r"TLS|ECONNRESET|ENOTFOUND|ETIMEDOUT|EAI_AGAIN|"
    r"socket disconnected|network socket|connection reset|"
    r"Connection refused|Temporary failure in name resolution|"
    # cursor-agent transiently fails to fetch its model catalogue and rejects
    # the pinned model with an empty available list — an auth/session flake,
    # not a deterministic internal error, so it must stay retryable.
    r"Cannot use this model|no available models|Available models:\s*$"
    r")"
)

_AUTO_MODEL_REQUIRED = re.compile(r"(?is)Named models unavailable.*Free plans can only use Auto")


def _classify_nonzero_exit(detail: str) -> Literal["transport", "internal"]:
    return "transport" if _TRANSPORT_FAILURE.search(detail or "") else "internal"


def _requires_auto_model_fallback(result: ProcessResult) -> bool:
    if result.exit_code == 0:
        return False
    detail = f"{result.stderr}\n{result.stdout}"
    return bool(_AUTO_MODEL_REQUIRED.search(detail))


def _with_workspace(prefix: list[str], workspace_root: Path) -> list[str]:
    """Rewrite or inject ``--workspace`` so the agent edits the task-private root."""
    root = str(workspace_root)
    out: list[str] = []
    i = 0
    replaced = False
    while i < len(prefix):
        arg = prefix[i]
        if arg in ("--workspace", "-w") and i + 1 < len(prefix):
            out.extend([arg, root])
            i += 2
            replaced = True
            continue
        if arg.startswith("--workspace="):
            out.append(f"--workspace={root}")
            i += 1
            replaced = True
            continue
        out.append(arg)
        i += 1
    if replaced:
        return out
    binary = Path(prefix[0]).name if prefix else ""
    if binary in {"cursor-agent", "cursor", "agent"} or binary.startswith("cursor-agent"):
        # Insert after the binary so later flags stay intact.
        return [prefix[0], "--workspace", root, *prefix[1:]]
    return out


def _with_agent(prefix: list[str], agent: str | None) -> list[str]:
    """Rewrite an explicit OpenCode persona without changing other CLIs."""
    if not agent:
        return list(prefix)
    if not prefix or Path(prefix[0]).name != "opencode":
        return list(prefix)
    out: list[str] = []
    i = 0
    replaced = False
    while i < len(prefix):
        arg = prefix[i]
        if arg == "--agent":
            out.extend([arg, agent])
            replaced = True
            if i + 1 < len(prefix) and not prefix[i + 1].startswith("-"):
                i += 2
            else:
                i += 1
            continue
        if arg.startswith("--agent="):
            out.append(f"--agent={agent}")
            i += 1
            replaced = True
            continue
        out.append(arg)
        i += 1
    if replaced:
        return out
    if prefix and Path(prefix[0]).name == "opencode":
        return [prefix[0], "--agent", agent, *prefix[1:]]
    return out


def _with_model(prefix: list[str], model: str) -> list[str]:
    """Rewrite or inject ``--model`` for cursor-agent style CLIs."""
    if not model:
        return list(prefix)
    out: list[str] = []
    i = 0
    replaced = False
    while i < len(prefix):
        arg = prefix[i]
        if arg == "--model" and i + 1 < len(prefix):
            out.extend([arg, model])
            i += 2
            replaced = True
            continue
        if arg.startswith("--model="):
            out.append(f"--model={model}")
            i += 1
            replaced = True
            continue
        out.append(arg)
        i += 1
    if replaced:
        return out
    binary = Path(prefix[0]).name if prefix else ""
    if binary in {"cursor-agent", "cursor", "agent"} or binary.startswith("cursor-agent"):
        return [prefix[0], "--model", model, *prefix[1:]]
    # Non-cursor agents: append so custom CLIs can still opt into --model.
    return [*prefix, "--model", model]


class HeadlessAdapter:
    def __init__(
        self,
        agent_cmd: str,
        cwd: Path,
        *,
        model: str | None = None,
        runner: ProcessRunner | None = None,
        timeout: float | None = None,
        prompt_via: Literal["argv", "stdin"] = "argv",
    ) -> None:
        self._prefix = shlex.split(agent_cmd)
        if not self._prefix:
            raise DriverError("--agent-cmd must not be empty")
        if model:
            self._prefix = _with_model(self._prefix, model)
        self._cwd = cwd
        self._runner = runner or SubprocessRunner()
        self._timeout = timeout
        self._prompt_via = prompt_via

    def _run(
        self, prompt: str, cwd: Path, timeout: float | None, *, argv_prefix: list[str] | None = None
    ) -> ProcessResult:
        prefix = list(self._prefix if argv_prefix is None else argv_prefix)
        if self._prompt_via == "stdin":
            argv = prefix
            stdin_text: str | None = prompt
        else:
            argv = [*prefix, prompt]
            stdin_text = None
        return self._runner.run(argv, cwd, timeout=timeout, stdin_text=stdin_text)

    def _run_with_auto_fallback(
        self, prompt: str, cwd: Path, timeout: float | None, *, argv_prefix: list[str] | None = None
    ) -> ProcessResult:
        prefix = list(self._prefix if argv_prefix is None else argv_prefix)
        result = self._run(prompt, cwd, timeout, argv_prefix=prefix)
        auto_prefix = _with_model(prefix, "auto")
        if _requires_auto_model_fallback(result) and auto_prefix != prefix:
            return self._run(prompt, cwd, timeout, argv_prefix=auto_prefix)
        return result

    def run_phase(self, request: PhaseRequest) -> PhaseResult:
        result = self._run_with_auto_fallback(request.prompt, self._cwd, self._timeout)

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
        workspace_root = Path(request.workspace_root)
        try:
            result = self._run_with_auto_fallback(
                request.prompt,
                workspace_root,
                request.timeout_seconds,
                argv_prefix=_with_agent(_with_workspace(self._prefix, workspace_root), request.agent),
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
                error_kind=_classify_nonzero_exit(detail),
                error=f"headless agent failed (exit {result.exit_code}): {detail}",
            )
        return AgentResult(ok=True)
