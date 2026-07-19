"""schema v2 编译产物模型：冻结拓扑 + 稳定 digest。

GraphRuntime 只接受 compiled model，不直接解释原始 YAML。compiled 拓扑一律使用
tuple（而非 list），使 digest 之后的执行顺序不可被调用方原地修改。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

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
