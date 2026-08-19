from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Literal, Self, cast

import yaml
from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from graph_engine.plugin_api import FailureKind, ResourceClaims

if TYPE_CHECKING:
    from graph_engine.canonical import JSONValue
else:
    JSONValue = JsonValue

_FROZEN_MODEL_CONFIG = ConfigDict(
    frozen=True,
    extra="forbid",
    allow_inf_nan=False,
    populate_by_name=True,
)


class FrozenModel(BaseModel):
    model_config = _FROZEN_MODEL_CONFIG


class RetryPolicyDef(FrozenModel):
    max_attempts: int = Field(ge=1)
    retry_on: tuple[FailureKind, ...] = ()


class TimeoutPolicyDef(FrozenModel):
    run_seconds: float = Field(gt=0)


NodeKind = Literal["task", "gate", "join", "subgraph", "interrupt", "end"]

_REQUIRED_NODE_FIELDS: dict[NodeKind, tuple[str, ...]] = {
    "task": ("capability", "retry", "timeout"),
    "subgraph": ("graph",),
    "join": ("join",),
    "gate": ("expression",),
    "interrupt": ("reason", "actions"),
    "end": (),
}

_ALLOWED_NODE_FIELDS: dict[NodeKind, frozenset[str]] = {
    "task": frozenset({"capability", "input", "retry", "timeout", "resources", "validators"}),
    "subgraph": frozenset({"graph", "input", "resources"}),
    "join": frozenset({"join"}),
    "gate": frozenset({"expression"}),
    "interrupt": frozenset({"reason", "actions"}),
    "end": frozenset(),
}


def validate_node_shape(
    kind: NodeKind,
    supplied_fields: set[str],
    values: Mapping[str, object],
) -> None:
    """Apply the authoritative kind-specific field-presence contract."""
    for field_name in _REQUIRED_NODE_FIELDS[kind]:
        value = values[field_name]
        if value is None or value == () or (isinstance(value, str) and not value.strip()):
            raise ValueError(f"{kind} node requires non-empty {field_name}")

    supplied_payload = supplied_fields - {"kind"}
    for field_name in sorted(supplied_payload - _ALLOWED_NODE_FIELDS[kind]):
        raise ValueError(f"{kind} node does not accept {field_name}")

    if kind == "interrupt":
        actions = cast(tuple[str, ...], values["actions"])
        if any(not action.strip() for action in actions):
            raise ValueError("interrupt actions must be non-empty")
        if len(set(actions)) != len(actions):
            raise ValueError("interrupt actions must be unique")


class NodeDef(FrozenModel):
    kind: NodeKind
    capability: str | None = None
    graph: str | None = None
    join: Literal["all", "any"] | None = None
    expression: str | None = None
    reason: str | None = None
    actions: tuple[str, ...] = ()
    input: dict[str, JSONValue] = Field(default_factory=dict)
    retry: str | None = None
    timeout: str | None = None
    resources: ResourceClaims = Field(default_factory=ResourceClaims)
    validators: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate_kind_shape(self) -> Self:
        validate_node_shape(self.kind, self.model_fields_set, self.__dict__)
        return self


class EdgeDef(FrozenModel):
    from_: str = Field(alias="from")
    to: str


class GraphDef(FrozenModel):
    max_activations: int = Field(ge=1, le=10_000)
    start: str
    nodes: dict[str, NodeDef]
    edges: tuple[EdgeDef, ...]


class WorkflowDef(FrozenModel):
    name: str
    entrypoints: dict[str, str]
    retry: dict[str, RetryPolicyDef]
    timeout: dict[str, TimeoutPolicyDef]
    graphs: dict[str, GraphDef]


def parse_workflow(text: str) -> WorkflowDef:
    raw = yaml.safe_load(text)
    return WorkflowDef.model_validate(raw, by_alias=True, by_name=False)


__all__ = [
    "EdgeDef",
    "GraphDef",
    "NodeDef",
    "RetryPolicyDef",
    "TimeoutPolicyDef",
    "WorkflowDef",
    "parse_workflow",
]
