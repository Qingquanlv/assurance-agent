"""handler registry 与内部 NodeRunner seam（设计 §5.2）。

``NodeRunner.execute(task, workspace, context)`` 按 target 精确分发到注册的
``TaskHandler``；``skill:`` / ``graph:`` namespace 注册 namespace 级默认 handler。
分发语义：

- 未注册且无 namespace 默认的 target → ``contract`` 失败（不执行任何 handler）；
- handler 抛出的任何异常归一化为 ``internal`` 失败，绝不逃出 runner；
- runner 不写 strict events、不冻结 write-set——ledger 与 write-set 的事务
  边界归 scheduler/runtime（Task 10/11）。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Protocol

from assurance_agent.workflow.core.graph_types import ErrorKind
from assurance_agent.workflow.graph.agent_api import AgentInvoker
from assurance_agent.workflow.graph.contracts import ExecutionContractCatalog
from assurance_agent.workflow.graph.model_routing import ModelRouter
from assurance_agent.workflow.graph.models import (
    CompiledWorkflow,
    ExecutableTask,
    RuntimeContext,
    TaskResult,
)
from assurance_agent.workflow.graph.workspace import TaskWorkspace, TreeStore


class TaskHandler(Protocol):
    def execute(
        self,
        task: ExecutableTask,
        workspace: TaskWorkspace,
        context: RuntimeContext,
    ) -> TaskResult:
        raise NotImplementedError


class NodeRunner(Protocol):
    def execute(
        self,
        task: ExecutableTask,
        workspace: TaskWorkspace,
        context: RuntimeContext,
    ) -> TaskResult:
        raise NotImplementedError


def task_with(task: ExecutableTask) -> Mapping[str, object]:
    """task input 中的 ``with`` 参数映射；缺失/损坏时按空映射（fail closed）。"""
    payload = task.input
    if isinstance(payload, Mapping):
        with_ = payload.get("with")
        if isinstance(with_, Mapping):
            return with_
    return {}


def task_failure(error_kind: ErrorKind, message: str) -> TaskResult:
    return TaskResult(status="failed", error_kind=error_kind, error=message)


class HandlerNodeRunner:
    """精确 target 注册表 + 可选 namespace 默认 handler 的分发器。"""

    def __init__(
        self,
        handlers: Mapping[str, TaskHandler],
        *,
        namespace_handlers: Mapping[str, TaskHandler] | None = None,
        compiled: CompiledWorkflow | None = None,
        object_store: TreeStore | None = None,
    ) -> None:
        self._handlers = dict(handlers)
        self._namespace_handlers = dict(namespace_handlers or {})
        self._compiled = compiled
        self._object_store = object_store

    def execute(
        self,
        task: ExecutableTask,
        workspace: TaskWorkspace,
        context: RuntimeContext,
    ) -> TaskResult:
        handler = self._handlers.get(task.target)
        if handler is None:
            namespace, _, _ = task.target.partition(":")
            handler = self._namespace_handlers.get(namespace)
        if handler is None:
            return task_failure("contract", f"unknown target: {task.target}")
        try:
            result = handler.execute(task, workspace, context)
        except Exception as exc:  # noqa: BLE001 - handler 异常绝不逃出 runner
            return task_failure("internal", f"{type(exc).__name__}: {exc}")
        if (
            self._compiled is not None
            and self._object_store is not None
            and result.status in ("succeeded", "stopped")
        ):
            from assurance_agent.workflow.graph.finalize import finalize_task_result

            return finalize_task_result(
                compiled=self._compiled,
                store=self._object_store,
                task=task,
                result=result,
                workspace=workspace,
                context=context,
            )
        return result


def build_default_node_runner(
    adapter: AgentInvoker,
    object_store: TreeStore,
    contracts: ExecutionContractCatalog,
    *,
    compiled: CompiledWorkflow,
    operations: Mapping[str, Callable[[ExecutableTask, TaskWorkspace, RuntimeContext], TaskResult]],
    run_child: Callable[[ExecutableTask, str, TaskWorkspace, RuntimeContext], TaskResult] | None = None,
    model_router: ModelRouter | None = None,
    adapter_name: str | None = None,
    cli_model_override: str | None = None,
) -> NodeRunner:
    """注册 canonical target handler：agent 桥、domain operation、builtin 与 subgraph。

    ``compiled`` 提供 node 定义（per-node contract claim 收窄、interrupt 配置）
    与 gate 定义；``contracts`` 提供 execution contract 默认 claim。
    ``operations`` 由 driver 组装根注入（内核不 import 领域注册表）。
    ``run_child`` 注入后注册 ``graph:`` namespace handler。
    成功路径统一经 ``finalize_task_result``：output 校验 → attached gate → 冻结报告。
    """
    from assurance_agent.workflow.graph.handlers.agent import AgentHandler
    from assurance_agent.workflow.graph.handlers.gate import GateHandler
    from assurance_agent.workflow.graph.handlers.interrupt import InterruptHandler
    from assurance_agent.workflow.graph.handlers.join import JoinHandler
    from assurance_agent.workflow.graph.handlers.operation import OperationHandler
    from assurance_agent.workflow.graph.handlers.subgraph import SubgraphHandler

    agent = AgentHandler(
        adapter,
        object_store,
        contracts=contracts,
        compiled=compiled,
        model_router=model_router,
        adapter_name=adapter_name,
        cli_model_override=cli_model_override,
    )
    operation = OperationHandler(operations)
    handlers: dict[str, TaskHandler] = {
        "builtin:join": JoinHandler(),
        "builtin:gate": GateHandler(compiled),
        "builtin:interrupt": InterruptHandler(compiled),
        **{target: operation for target in operations},
    }
    namespace_handlers: dict[str, TaskHandler] = {"skill": agent}
    if run_child is not None:
        namespace_handlers["graph"] = SubgraphHandler(run_child)
    return HandlerNodeRunner(
        handlers,
        namespace_handlers=namespace_handlers,
        compiled=compiled,
        object_store=object_store,
    )


__all__ = [
    "HandlerNodeRunner",
    "NodeRunner",
    "TaskHandler",
    "build_default_node_runner",
    "task_failure",
    "task_with",
]
