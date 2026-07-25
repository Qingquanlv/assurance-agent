"""严格 ``schema_version: "2"`` graph schema 模型 + YAML 加载器。

v2 与 v1 不兼容：``graphs:`` 是唯一拓扑声明，``phases:``/``loops:`` 在加载期
即被拒绝；``schema_version`` 必须恰好是字符串 ``"2"``（先于 pydantic 校验，
防止 YAML 整数强转削弱版本边界）。所有 schema 模型冻结且禁止额外字段。
gate 定义复用 v1 的共享模型与 normalization。
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from assurance_agent import resources
from assurance_agent.exceptions import AaError
from assurance_agent.workflow.core.graph_types import ErrorKind
from assurance_agent.workflow.orchestration.schema import (
    GateDef,
    SchemaError,
    normalize_gates,
)

_ALLOWED_ROOT_KEYS = frozenset(
    {"schema_version", "name", "params", "entrypoints", "policies", "graphs", "gates"}
)
_V1_ROOT_KEYS = frozenset({"phases", "loops"})


class SchemaV2Error(AaError):
    """v2 schema 结构非法，或版本/拓扑边界（schema_version、v1 键）不被满足。"""


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ParamDef(_FrozenModel):
    type: Literal["enum", "list", "bool", "int", "str"]
    values: list[object] | None = None
    min_items: int | None = Field(default=None, ge=0)
    unique: bool = False
    default: object = None


class EntrypointDef(_FrozenModel):
    graph: str
    allow: str | None = None
    with_: dict[str, object] = Field(default_factory=dict, alias="with")


class BackoffDef(_FrozenModel):
    initial_seconds: float = Field(default=0, ge=0)
    multiplier: float = Field(default=1, ge=1)
    max_seconds: float = Field(default=0, ge=0)
    jitter: bool = False


class RetryPolicyDef(_FrozenModel):
    max_attempts: int = Field(ge=1, le=10)
    retry_on: list[ErrorKind] = Field(default_factory=list)
    backoff: BackoffDef = Field(default_factory=BackoffDef)


class TimeoutPolicyDef(_FrozenModel):
    run_seconds: float = Field(gt=0)
    heartbeat_seconds: float = Field(gt=0)


class SchedulerPolicyDef(_FrozenModel):
    max_parallel_tasks: int = Field(default=4, ge=1, le=64)
    conflict_order: list[Literal["topology", "declaration", "task_id"]] = Field(
        default_factory=lambda: ["topology", "declaration", "task_id"]
    )


class PoliciesDef(_FrozenModel):
    retry: dict[str, RetryPolicyDef] = Field(default_factory=dict)
    timeout: dict[str, TimeoutPolicyDef] = Field(default_factory=dict)
    scheduler: SchedulerPolicyDef = Field(default_factory=SchedulerPolicyDef)


class StateDef(_FrozenModel):
    type: Literal["list", "object", "str", "int", "bool"]
    default: object
    reducer: Literal["replace", "append", "merge_disjoint", "set_union"] = "replace"


class ResourceDef(_FrozenModel):
    reads: list[str] = Field(default_factory=list)
    writes: list[str] = Field(default_factory=list)
    exclusive: list[str] = Field(default_factory=list)


class JoinDef(_FrozenModel):
    sources: list[str]
    mode: Literal["all", "all_active", "any"]
    cancel_remaining: bool = False


class ReduceDef(_FrozenModel):
    into: str
    using: Literal["replace", "append", "merge_disjoint", "set_union"]


class FanOutDef(_FrozenModel):
    items: str
    item_as: str
    key: str
    max_items: int = Field(default=32, ge=1, le=128)
    completion: Literal["all"] = "all"
    reduce: ReduceDef | None = None


class BudgetUseDef(_FrozenModel):
    consume: str
    on: Literal["committed"]
    exhausted_to: str


class InterruptDef(_FrozenModel):
    reason: str
    checkpoint: str
    bind: Literal["audited_gate_read"]
    actions: list[Literal["fix_and_proceed", "accept_risk", "stop"]]


class EvidenceRef(_FrozenModel):
    node: str
    symbol: str
    task_key: str | None = None


class ExportDef(_FrozenModel):
    from_: str = Field(alias="from")
    output: str


class NodeDef(_FrozenModel):
    uses: str
    agent: str | None = None
    when: str | None = None
    outputs: list[str] = Field(default_factory=list)
    evidence: dict[str, EvidenceRef] = Field(default_factory=dict)
    gate: str | None = None
    retry: str | None = None
    timeout: str | None = None
    with_: dict[str, object] = Field(default_factory=dict, alias="with")
    resources: ResourceDef | None = None
    state_writes: dict[str, str] = Field(default_factory=dict)
    join: JoinDef | None = None
    fan_out: FanOutDef | None = None
    budget: BudgetUseDef | None = None
    interrupt: InterruptDef | None = None


class EdgeDef(_FrozenModel):
    from_: str = Field(alias="from")
    to: str
    when: str | None = None


class RouteDef(_FrozenModel):
    from_: str = Field(alias="from")
    select: str
    cases: dict[str, str]
    default: str | None = None


class BudgetDef(_FrozenModel):
    limit: str | int


class GraphDef(_FrozenModel):
    max_supersteps: int = Field(gt=0)
    state: dict[str, StateDef] = Field(default_factory=dict)
    budgets: dict[str, BudgetDef] = Field(default_factory=dict)
    nodes: dict[str, NodeDef]
    edges: list[EdgeDef] = Field(default_factory=list)
    routes: list[RouteDef] = Field(default_factory=list)
    exports: dict[str, ExportDef] = Field(default_factory=dict)


class WorkflowSchemaV2(_FrozenModel):
    schema_version: Literal["2"]
    name: str
    params: dict[str, ParamDef] = Field(default_factory=dict)
    entrypoints: dict[str, EntrypointDef]
    policies: PoliciesDef = Field(default_factory=PoliciesDef)
    graphs: dict[str, GraphDef]
    gates: dict[str, GateDef] = Field(default_factory=dict)


def parse_workflow_v2(yaml_text: str) -> WorkflowSchemaV2:
    try:
        doc = yaml.safe_load(yaml_text)
    except yaml.YAMLError as exc:
        raise SchemaV2Error(f"invalid workflow schema v2: {exc}") from exc
    if not isinstance(doc, dict):
        raise SchemaV2Error("schema root is not a mapping")

    v1_keys = sorted(str(k) for k in doc if k in _V1_ROOT_KEYS)
    if v1_keys:
        raise SchemaV2Error(
            "v1 topology keys " + ", ".join(v1_keys) + " do not exist in schema v2; declare graphs instead"
        )

    # 版本边界先于 pydantic：YAML 会把 2 解析成 int，不能靠 Literal["2"] 兜底。
    version = doc.get("schema_version")
    if not isinstance(version, str) or version != "2":
        raise SchemaV2Error('schema_version must be exactly "2"')

    unknown = sorted(str(k) for k in doc if k not in _ALLOWED_ROOT_KEYS)
    if unknown:
        raise SchemaV2Error("unknown root keys: " + ", ".join(unknown))

    try:
        gates = normalize_gates(doc.get("gates"))
        return WorkflowSchemaV2.model_validate({**doc, "gates": gates})
    except (SchemaError, ValidationError) as exc:
        raise SchemaV2Error(f"invalid workflow schema v2: {exc}") from exc


def load_workflow_v2(project_root: Path, explicit: Path | None = None) -> WorkflowSchemaV2:
    if explicit is not None:
        path = explicit if explicit.is_absolute() else project_root / explicit
        if not path.exists():
            raise SchemaV2Error(f"explicit schema not found: {path}")
        return parse_workflow_v2(path.read_text(encoding="utf-8"))
    for rel in (Path(".aa/workflow-schema.yaml"), Path("schemas/workflow-schema.yaml")):
        candidate = project_root / rel
        if candidate.exists():
            return parse_workflow_v2(candidate.read_text(encoding="utf-8"))
    return parse_workflow_v2(resources.read_text("schemas", "workflow-schema.yaml"))
