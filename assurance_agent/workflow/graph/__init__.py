"""schema v2 graph 运行时包（Plan → Execute → Update）。

GraphRuntime 是唯一的 workflow 引擎（v1 phase/loop engine 已在 canonical-schema
切换时移除）。本包负责图拓扑的编译、规划、调度与节点执行，并单向依赖
``workflow.orchestration`` 复用其 gate 裁决 / DSL / 人工决策等规则语义
（``orchestration`` 从不反向 import 本包，以守层边界）。

包根只导出 GraphRuntime 与公开命令/结果模型，以及 ``CompiledWorkflow``。
schema 模型请直接从 ``assurance_agent.workflow.graph.schema_v2`` 导入。
"""

from assurance_agent.workflow.graph.models import (
    CompiledWorkflow,
    GraphStatus,
    ImportResult,
    ResumeCommand,
    RunResult,
    RuntimeContext,
)
from assurance_agent.workflow.graph.runtime import (
    GraphDefinitionChanged,
    GraphIntegrityError,
    GraphRuntime,
    GraphRuntimeError,
)

__all__ = [
    "CompiledWorkflow",
    "GraphDefinitionChanged",
    "GraphIntegrityError",
    "GraphRuntime",
    "GraphRuntimeError",
    "GraphStatus",
    "ImportResult",
    "ResumeCommand",
    "RunResult",
    "RuntimeContext",
]
