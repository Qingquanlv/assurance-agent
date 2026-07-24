"""agent adapter 桥 target handler（``skill:<name>``）。

把 skill node 翻译成 ``AgentRequest``：prompt 列出 contract 授权写范围，agent
进程在 task 私有 workspace 的物化 root 中运行。adapter 返回后 handler 用
``TreeStore.freeze_write_set`` 独立验证实际写入与声明 outputs：

- 授权范围外的实际写入 / 变化的 symlink / 路径逃逸 → ``forbidden_write``；
- 声明 output 缺失或无法冻结 → ``invalid_output``；
- adapter 失败 → 透传其 typed error kind（缺失时归一化为 ``internal``）。

handler 不写 strict events；write-set 的 ledger 持久化归 scheduler（Task 10）。
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from assurance_agent.workflow.core.graph_types import ErrorKind
from assurance_agent.workflow.graph.agent_api import AgentInvoker, AgentRequest, build_node_prompt
from assurance_agent.workflow.graph.contracts import (
    ExecutionContractCatalog,
    ResourceClaims,
    ResourcePath,
)
from assurance_agent.workflow.graph.models import (
    CompiledWorkflow,
    ExecutableTask,
    RuntimeContext,
    TaskResult,
)
from assurance_agent.workflow.graph.schema_v2 import NodeDef
from assurance_agent.workflow.graph.task_runner import task_failure
from assurance_agent.workflow.graph.handlers.operation import link_host_task_paths
from assurance_agent.workflow.graph.workspace import TaskWorkspace, TreeStore, WorkspaceError


class AgentHandler:
    def __init__(
        self,
        invoker: AgentInvoker,
        store: TreeStore,
        *,
        contracts: ExecutionContractCatalog,
        compiled: CompiledWorkflow,
    ) -> None:
        self._invoker = invoker
        self._store = store
        self._contracts = contracts
        self._compiled = compiled

    def execute(
        self,
        task: ExecutableTask,
        workspace: TaskWorkspace,
        context: RuntimeContext,
    ) -> TaskResult:
        node_def = self._node_def(task)
        if node_def is None:
            return task_failure(
                "contract",
                f"cannot resolve compiled node for task {task.task_id} "
                f"(graph '{task.graph_id}', node '{task.node_id}')",
            )
        claims = self._claims(task, node_def)
        outputs = self._outputs(task, node_def)
        allowed = tuple(_display_path(path) for path in claims.authorization_writes)
        skill = task.target.partition(":")[2]
        link_host_task_paths(workspace, context)
        prompt = build_node_prompt(
            skill,
            task.node_id,
            context.change_id,
            allowed_writes=allowed,
            item=_fan_out_item(task),
            workspace_root=str(Path(workspace.root)),
            memory_root=Path(workspace.project_root),
            prior_failure=task.prior_failure,
            prior_error_kind=task.prior_error_kind,
            evidence=task.resolved_evidence or None,
        )
        if skill == "aa-retro":
            rid = context.params.get("retro_id")
            if isinstance(rid, str) and rid.strip():
                from assurance_agent.retro.nightly.agent import build_retro_proposal_prompt
                prompt = prompt + " " + build_retro_proposal_prompt(rid)
        request = AgentRequest(
            target=task.target,
            node_id=task.node_id,
            change_id=context.change_id,
            workspace_root=Path(workspace.root),
            allowed_writes=allowed,
            prompt=prompt,
            timeout_seconds=task.timeout_policy.run_seconds,
            reconnect_session_id=context.parent_session_id,
            agent=agent_for_skill(skill),
        )
        result = self._invoker.invoke(request)
        if not result.ok:
            return task_failure(
                result.error_kind or "internal",
                result.error or f"agent invocation failed for {task.target}",
            )
        try:
            write_set = self._store.freeze_write_set(workspace, claims=claims, outputs=outputs)
        except WorkspaceError as exc:
            return task_failure(_freeze_error_kind(exc), str(exc))
        return TaskResult(
            status="succeeded",
            write_set_id=write_set.write_set_id,
            outputs_sha256=dict(write_set.outputs_sha256),
        )

    def _node_def(self, task: ExecutableTask) -> NodeDef | None:
        graph = self._compiled.graphs.get(task.graph_id)
        if graph is None:
            return None
        node = graph.nodes.get(task.node_id)
        return node.definition if node is not None else None

    def _claims(self, task: ExecutableTask, node_def: NodeDef) -> ResourceClaims:
        """per-node contract claim；fan-out child 按冻结展开的 resources 收窄授权。"""
        claims = self._contracts.claims_for(node_def)
        payload = task.input
        if isinstance(payload, Mapping):
            expanded = payload.get("resources")
            if isinstance(expanded, Mapping):
                writes = expanded.get("writes")
                if isinstance(writes, list) and writes:
                    narrowed = tuple(ResourcePath.parse(str(value)) for value in writes)
                    claims = ResourceClaims(
                        reads=claims.reads,
                        writes=claims.writes,
                        exclusive=claims.exclusive,
                        authorization_writes=narrowed,
                    )
        return claims

    @staticmethod
    def _outputs(task: ExecutableTask, node_def: NodeDef) -> tuple[str, ...]:
        """声明 outputs：fan-out child 用冻结展开值，否则用 node 定义原文。"""
        payload = task.input
        if isinstance(payload, Mapping):
            expanded = payload.get("outputs")
            if isinstance(expanded, list):
                return tuple(str(value) for value in expanded)
        return tuple(node_def.outputs)


def agent_for_skill(skill: str) -> str | None:
    """Route a phase skill to its bounded aa-* worker agent (opencode persona).

    Each aa-* agent declares the phases it serves (see ``.opencode/agents/*.md``)
    plus a restrictive permission floor and ``external_directory: deny``. Running
    a node under its matching worker keeps it focused on producing declared
    outputs (instead of an aggressive general coding preset that over-explores
    and never writes) and blocks writes/reads outside the task sandbox.

    Mapping is keyword-based so new sibling skills route correctly:
    - ``*codegen*``                     -> aa-test-author (tests/ + codegen/)
    - ``*reviewer*`` / ``*inspect*``    -> aa-reviewer   (review/ + inspect/)
    - ``*report*``                      -> aa-reporter   (report/)
    - ``*archive*``                     -> aa-archiver   (qa/cases + qa/archive)
    - explore / case-design / *-plan /
      *-fixer / fact-baseline /
      fix-proposal (the rest)           -> aa-doc-author (authoring/design/plan)

    Returns ``None`` for an unknown/empty skill so the adapter keeps its default.
    """
    if not skill:
        return None
    if "codegen" in skill:
        return "aa-test-author"
    if "reviewer" in skill or "inspect" in skill:
        return "aa-reviewer"
    if "report" in skill:
        return "aa-reporter"
    if "archive" in skill:
        return "aa-archiver"
    return "aa-doc-author"


def _display_path(path: ResourcePath) -> str:
    return f"{path.root}:{path.pattern}"


def _fan_out_item(task: ExecutableTask) -> str | None:
    payload = task.input
    if isinstance(payload, Mapping):
        fan_out = payload.get("fan_out")
        if isinstance(fan_out, Mapping):
            key = fan_out.get("task_key")
            if isinstance(key, str):
                return key
    return task.task_key


def _freeze_error_kind(exc: WorkspaceError) -> ErrorKind:
    """freeze 失败的 typed 归一化：outputs 校验 → invalid_output，其余写策略 → forbidden_write。"""
    if "declared output" in str(exc):
        return "invalid_output"
    return "forbidden_write"


__all__ = ["AgentHandler", "agent_for_skill"]
