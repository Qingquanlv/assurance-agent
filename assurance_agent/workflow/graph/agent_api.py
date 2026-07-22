"""graph 拥有的 agent 调用 seam：AgentRequest/AgentResult/AgentInvoker。

driver adapter（headless/opencode）import 并实现 ``AgentInvoker``；graph 包只
依赖本模块，绝不 import driver（保持 ``driver → graph`` 单向依赖）。request
携带 task 私有 workspace 的物化 root 与 contract 授权写范围，adapter 不得
读取 canonical workspace 路径。
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from assurance_agent.workflow.core.graph_types import ErrorKind


class AgentRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    target: str
    node_id: str
    change_id: str
    workspace_root: Path
    allowed_writes: tuple[str, ...]
    prompt: str
    timeout_seconds: float
    reconnect_session_id: str | None = None


class AgentResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    ok: bool
    error_kind: ErrorKind | None = None
    error: str | None = None
    session_id: str | None = None


class AgentInvoker(Protocol):
    def invoke(self, request: AgentRequest) -> AgentResult:
        raise NotImplementedError


def build_node_prompt(
    skill: str,
    node_id: str,
    change_id: str,
    *,
    allowed_writes: Sequence[str],
    item: str | None = None,
) -> str:
    """v2 node prompt：列出 contract 授权写范围，不再宣称只能写 change 目录。

    graph 拥有本构造函数；``driver.phase_prompt.build_phase_prompt`` 的 v2 分支
    委托到这里（driver → graph），v1 分支保留原 Scheme E 文案。
    """
    allowed = ", ".join(sorted(allowed_writes)) or "(none)"
    return (
        f"Call skill(name='{skill}'). Operate strictly on change_id='{change_id}'. "
        f"Authorized write paths: {allowed}. Produce only node {node_id}'s declared outputs. "
        "Do not run aa gate/status, edit workflow-state.yaml, or access coordinator runtime files."
        + (f" Fan-out item: {item}." if item is not None else "")
    )
