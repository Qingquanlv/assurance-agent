"""graph 拥有的 agent 调用 seam：AgentRequest/AgentResult/AgentInvoker。

driver adapter（headless/opencode）import 并实现 ``AgentInvoker``；graph 包只
依赖本模块，绝不 import driver（保持 ``driver → graph`` 单向依赖）。request
携带 task 私有 workspace 的物化 root 与 contract 授权写范围，adapter 不得
读取 canonical workspace 路径。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from assurance_agent.workflow.core.graph_types import ErrorKind
from assurance_agent.workflow.skill_memory import load_skill_memory


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
    # Focused adapter agent/persona to execute this node (e.g. opencode aa-* role
    # agent). None lets the adapter fall back to its default. Routed by skill in
    # ``AgentHandler`` so each phase runs under a bounded QA worker rather than an
    # aggressive general coding preset.
    agent: str | None = None


class AgentResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    ok: bool
    error_kind: ErrorKind | None = None
    error: str | None = None
    session_id: str | None = None


class AgentInvoker(Protocol):
    def invoke(self, request: AgentRequest) -> AgentResult:
        raise NotImplementedError


_PRIOR_FAILURE_KINDS = frozenset({"invalid_output", "forbidden_write"})
_PRIOR_FAILURE_MAX_CHARS = 800
_OUTPUT_CONTRACT_REMINDERS = {
    "aa-api-plan-reviewer": (
        " OUTPUT CONTRACT: api-plan-review.json must include "
        "required_capabilities as a non-empty list of concrete leaf keys."
    ),
    "aa-e2e-plan-reviewer": (
        " OUTPUT CONTRACT: plan-review.json must include required_capabilities "
        "as a non-empty list of concrete leaf keys."
    ),
}


def build_node_prompt(
    skill: str,
    node_id: str,
    change_id: str,
    *,
    allowed_writes: Sequence[str],
    item: str | None = None,
    workspace_root: str | Path | None = None,
    memory_root: Path | None = None,
    prior_failure: str | None = None,
    prior_error_kind: ErrorKind | None = None,
    evidence: Mapping[str, object] | None = None,
) -> str:
    """v2 node prompt：列出 contract 授权写范围，不再宣称只能写 change 目录。

    graph 拥有本构造函数；``driver.phase_prompt.build_phase_prompt`` 的 v2 分支
    委托到这里（driver → graph），v1 分支保留原 Scheme E 文案。

    当 ``workspace_root`` 给定时，把 task 沙箱的**绝对** cwd 明确写进 prompt。
    沙箱物理嵌套在真实 repo 内、且 bash 常被收窄（``pwd``/``find`` 被拒），聪明
    模型无法自证 cwd 时会"向上发现" canonical ``.aa/config.yaml`` 并把相对产物
    路径解析到 canonical（越界写、被沙箱权限拒 → ``invalid_output``）。显式给出
    cwd 消除这种猜测：agent 直接以此为项目根落盘。

    Contract-violation retries (``invalid_output`` / ``forbidden_write``) append a
    prior-failure clause with the exact engine error so the agent can fix the
    declared artifact instead of blindly replaying.
    """
    allowed = ", ".join(sorted(allowed_writes)) or "(none)"
    cwd_clause = ""
    if workspace_root is not None:
        cwd_clause = (
            f" Your working directory is EXACTLY '{workspace_root}'. That directory "
            "IS this task's project root and already contains .aa/config.yaml; do "
            "NOT search parent directories for a different project root, and resolve "
            "every declared output under it (e.g. write "
            f"'{workspace_root}/qa/changes/{change_id}/<declared-output>')."
        )
    memory_clause = ""
    if memory_root is not None:
        memory = load_skill_memory(Path(memory_root), skill)
        if memory:
            memory_clause = (
                f" Active skill memory for {skill} (from frozen workspace snapshot):\n{memory}\n"
                "Apply these rules when producing declared outputs."
            )
    failure_clause = ""
    if prior_failure and prior_error_kind is not None and prior_error_kind in _PRIOR_FAILURE_KINDS:
        detail = prior_failure.strip()
        if len(detail) > _PRIOR_FAILURE_MAX_CHARS:
            detail = detail[:_PRIOR_FAILURE_MAX_CHARS] + "…"
        failure_clause = (
            f" PRIOR ATTEMPT FAILED ({prior_error_kind}) — do not repeat it:\n"
            f"{detail}\n"
            "Your previous attempt violated the artifact contract. Open the declared "
            "output you wrote, fix exactly the reported fields, rewrite it, and keep "
            "all other content unchanged."
        )
    evidence_clause = ""
    if evidence:
        rendered = json.dumps(evidence, sort_keys=True, ensure_ascii=False, indent=2)
        evidence_clause = (
            f" FROZEN UPSTREAM EVIDENCE (authoritative; do not re-derive from disk):\n{rendered}\n"
        )
    output_contract_clause = _OUTPUT_CONTRACT_REMINDERS.get(skill, "")
    return (
        f"Call skill(name='{skill}'). Operate strictly on change_id='{change_id}'. "
        f"Authorized write paths: {allowed}. Produce only node {node_id}'s declared outputs. "
        "Do not run aa gate/status, edit workflow-state.yaml, or access coordinator runtime files. "
        # Sandbox contract (hard): this task runs in an isolated workspace nested
        # inside the real repo. The current working directory IS this task's
        # project root — treat it as such. Use ONLY paths relative to the cwd.
        # Never search parent directories for another .aa/config.yaml, never pass
        # an absolute --project-dir, and never read/write via an absolute path that
        # leaves the cwd. Escaping the cwd writes into the real repo and fails
        # output validation (the runtime only sees files written inside this cwd).
        "SANDBOX CONTRACT: your current working directory IS this task's project "
        "root; use only paths relative to it. Never walk up to find another "
        ".aa/config.yaml, never pass an absolute --project-dir, and never read or "
        "write via an absolute path outside the working directory — doing so "
        "escapes the isolated workspace and fails output validation."
        + cwd_clause
        + memory_clause
        + evidence_clause
        + output_contract_clause
        + failure_clause
        + (f" Fan-out item: {item}." if item is not None else "")
    )
