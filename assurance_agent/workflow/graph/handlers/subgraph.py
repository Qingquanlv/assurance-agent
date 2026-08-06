"""named subgraph handler：在父 task 私有 workspace 上驱动 child GraphRuntime。

``SubgraphHandler`` 不自己理解拓扑；它调用注入的 ``run_child``。child 的
superstep 只更新父 task workspace 内的 tree lineage 与 child ledger namespace；
child 完成后把累计 delta 冻成父 task 的 write-set。child 失败/中断时原样上抛
``TaskResult``，不把半成品文件 flatten 进父图。
"""

from __future__ import annotations

from collections.abc import Callable

from assurance_agent.workflow.graph.models import (
    ExecutableTask,
    RuntimeContext,
    TaskResult,
)
from assurance_agent.workflow.graph.handlers.operation import link_host_runtime_dirs
from assurance_agent.workflow.graph.workspace import TaskWorkspace

RunChild = Callable[
    [ExecutableTask, str, TaskWorkspace, RuntimeContext],
    TaskResult,
]


class SubgraphHandler:
    def __init__(self, run_child: RunChild) -> None:
        self._run_child = run_child

    def execute(
        self,
        task: ExecutableTask,
        workspace: TaskWorkspace,
        context: RuntimeContext,
    ) -> TaskResult:
        from assurance_agent.workflow.graph.task_runner import task_failure

        prefix, _, graph_id = task.target.partition(":")
        if prefix != "graph" or not graph_id:
            return task_failure(
                "contract",
                f"subgraph handler requires target graph:<id>, got {task.target!r}",
            )
        # Child invocation metadata points at this private workspace. Reattach
        # excluded runtime/config roots before handing it off so deeper agent
        # nodes can inherit .opencode (including newly added bounded agents).
        link_host_runtime_dirs(workspace, context)
        return self._run_child(task, graph_id, workspace, context)


__all__ = ["RunChild", "SubgraphHandler"]
