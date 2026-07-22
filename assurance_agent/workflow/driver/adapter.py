"""Phase-agent adapter contract (spec 5a) + graph ``AgentInvoker`` 兼容再导出。

A phase adapter abstracts "hand one workflow phase to some Agent and get a
result back". Two implementations follow: HeadlessAdapter (subprocess) and
OpenCodeAdapter (HTTP). The deterministic loop only ever sees this Protocol.

graph 运行时的 agent seam（``AgentRequest``/``AgentResult``/``AgentInvoker``）由
``workflow.graph.agent_api`` 拥有；本模块在 Task 17 删除 v1 shim 前再导出这些
类型，并提供 v1 ``PhaseRequest``/``PhaseResult`` 的翻译函数（保持
``driver → graph`` 单向依赖）。
"""

from collections.abc import Sequence
from pathlib import Path
from typing import Protocol, runtime_checkable

from pydantic import BaseModel

from assurance_agent.exceptions import AaError
from assurance_agent.workflow.graph.agent_api import AgentRequest, AgentResult, AgentInvoker

__all__ = [
    "Adapter",
    "AgentInvoker",
    "AgentRequest",
    "AgentResult",
    "DriverError",
    "PhaseRequest",
    "PhaseResult",
    "agent_to_phase_result",
    "phase_to_agent_request",
]


class DriverError(AaError):
    """Any driver-layer failure surfaced to the CLI as EXIT_ERROR."""


class PhaseRequest(BaseModel):
    change_id: str
    phase_id: str
    skill: str | None = None
    agent: str | None = None
    prompt: str


class PhaseResult(BaseModel):
    ok: bool
    output: str = ""
    error: str | None = None


def phase_to_agent_request(
    request: PhaseRequest,
    *,
    workspace_root: Path,
    allowed_writes: Sequence[str] = (),
    timeout_seconds: float = 3600.0,
    reconnect_session_id: str | None = None,
) -> AgentRequest:
    """v1 ``PhaseRequest`` → graph ``AgentRequest``（v1 缺失的字段取保守默认）。"""
    return AgentRequest(
        target=f"skill:{request.skill or request.phase_id}",
        node_id=request.phase_id,
        change_id=request.change_id,
        workspace_root=workspace_root,
        allowed_writes=tuple(allowed_writes),
        prompt=request.prompt,
        timeout_seconds=timeout_seconds,
        reconnect_session_id=reconnect_session_id,
    )


def agent_to_phase_result(result: AgentResult, *, output: str = "") -> PhaseResult:
    """graph ``AgentResult`` → v1 ``PhaseResult``；stdout 只能由 v1 路径自身保留。"""
    return PhaseResult(ok=result.ok, output=output, error=result.error)


@runtime_checkable
class Adapter(Protocol):
    def run_phase(self, request: PhaseRequest) -> PhaseResult:
        """Dispatch one phase to an Agent; never raises for an agent-side
        failure — return PhaseResult(ok=False, error=...) instead."""
        ...
