"""builtin join result handler。

planner 只在 join 声明的 sources 满足 mode 语义后激活 join task（``all`` /
``all_active`` / ``any``，见 ``planner._decide_join``）；join task 被执行即代表
planner satisfaction 已成立，handler 不做任何等待，直接返回 succeeded。
"""

from __future__ import annotations

from assurance_kernel.workflow.graph.models import ExecutableTask, RuntimeContext, TaskResult
from assurance_kernel.workflow.graph.workspace import TaskWorkspace


class JoinHandler:
    def execute(
        self,
        task: ExecutableTask,
        workspace: TaskWorkspace,
        context: RuntimeContext,
    ) -> TaskResult:
        return TaskResult(status="succeeded")


__all__ = ["JoinHandler"]
