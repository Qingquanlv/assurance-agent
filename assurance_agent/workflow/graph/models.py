"""schema v2 编译产物模型：冻结拓扑 + 稳定 digest；ledger 投影模型。

GraphRuntime 只接受 compiled model，不直接解释原始 YAML。compiled 拓扑一律使用
tuple（而非 list），使 digest 之后的执行顺序不可被调用方原地修改。
投影模型（TaskProjection/GraphProjection 等）是 strict ledger 的纯函数输出，
frozen + extra="forbid"，绝不反向覆盖 ledger。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from assurance_agent.workflow.core.graph_types import ErrorKind
from assurance_agent.workflow.graph.contracts import ResourceClaims
from assurance_agent.workflow.graph.schema_v2 import (
    EdgeDef,
    NodeDef,
    RouteDef,
    WorkflowSchemaV2,
)
from assurance_agent.workflow.orchestration.dsl import Expr


class CompiledNode(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    graph_id: str
    node_id: str
    declaration_index: int
    topology_rank: int
    definition: NodeDef
    incoming: tuple[EdgeDef, ...]
    outgoing: tuple[EdgeDef, ...]
    routes: tuple[RouteDef, ...]


class CompiledGraph(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)
    graph_id: str
    max_supersteps: int
    declaration_order: tuple[str, ...]
    nodes: dict[str, CompiledNode]
    sccs: tuple[tuple[str, ...], ...]
    artifact_symbols: dict[str, str]
    # 保守资源 footprint：全图 node claim 的并集（graph:<id> 递归展开）；
    # 无 catalog 编译时不可推导，为 global:exclusive。
    resource_footprint: ResourceClaims = Field(default_factory=ResourceClaims)


class CompiledEntrypoint(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    name: str
    graph_id: str
    allow_expr: Expr | None
    param_overrides: dict[str, object]


class CompiledWorkflow(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)
    # plan 指定的公开字段名；遮蔽 BaseModel.schema() 方法属有意为之。
    schema: WorkflowSchemaV2  # type: ignore[reportIncompatibleMethodOverride]
    digest: str
    entrypoints: dict[str, CompiledEntrypoint]
    graphs: dict[str, CompiledGraph]
    contract_digests: dict[str, str] = Field(default_factory=dict)


# ---- ledger 投影模型（strict events 的唯一权威视图）----


TaskStatus = Literal["pending", "running", "succeeded", "failed", "abandoned", "interrupted"]


class TaskProjection(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    task_id: str
    node_id: str
    status: TaskStatus
    attempts_used: int = 0
    latest_attempt_id: str | None = None
    write_set_id: str | None = None
    outputs_sha256: dict[str, str] = Field(default_factory=dict)
    gate_report: dict[str, object] | None = None
    state_updates: dict[str, object] = Field(default_factory=dict)
    error_kind: ErrorKind | None = None
    next_retry_at: str | None = None


class InterruptProjection(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    interrupt_id: str
    checkpoint_ns: str
    node_id: str
    checkpoint: str
    actions: tuple[str, ...]
    audited_reads_sha256: dict[str, str]
    artifact_view: str | None = None
    resolved_action: str | None = None


class GraphProjection(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    invocation_id: str
    entrypoint: str
    checkpoint_ns: str
    parent_invocation_id: str | None = None
    parent_task_id: str | None = None
    structural_path: str
    graph_digest: str
    contract_digests: dict[str, str]
    params: dict[str, object]
    root_tree_id: str
    current_tree_id: str
    latest_checkpoint_id: str | None = None
    event_seq: int = 0
    supersteps: int = 0
    state_values: dict[str, object] = Field(default_factory=dict)
    tasks: dict[str, TaskProjection] = Field(default_factory=dict)
    budgets: dict[str, int] = Field(default_factory=dict)
    fan_out_expansions: dict[str, dict[str, object]] = Field(default_factory=dict)
    interrupts: dict[str, InterruptProjection] = Field(default_factory=dict)
    terminal: Literal["completed", "stopped", "failed"] | None = None
    terminal_reason: str | None = None


class WorkflowStateProjection(BaseModel):
    """``workflow-state.yaml`` 兼容视图：仅人工/报告用途，运行时决策从不读它。

    每个字段都只能从 ``GraphProjection`` 派生（``project_workflow_state``）。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    invocation_id: str
    entrypoint: str
    terminal: Literal["completed", "stopped", "failed"] | None = None
    terminal_reason: str | None = None
    latest_checkpoint_id: str | None = None
    event_seq: int = 0
    # node 摘要：node_id -> 该 node 产生的 task_id（稳定排序）。
    nodes: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    tasks: dict[str, TaskProjection] = Field(default_factory=dict)
    pending_interrupts: tuple[InterruptProjection, ...] = ()
    budgets: dict[str, int] = Field(default_factory=dict)
