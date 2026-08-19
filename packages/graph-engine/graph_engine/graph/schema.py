from __future__ import annotations

from typing import TYPE_CHECKING, Literal, Self

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


class NodeDef(FrozenModel):
    kind: Literal["task", "gate", "join", "subgraph", "interrupt", "end"]
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
        required = {
            "task": ("capability", "retry", "timeout"),
            "subgraph": ("graph",),
            "join": ("join",),
            "gate": ("expression",),
            "interrupt": ("reason", "actions"),
            "end": (),
        }[self.kind]
        for field_name in required:
            value = getattr(self, field_name)
            if value is None or value == () or (isinstance(value, str) and not value.strip()):
                raise ValueError(f"{self.kind} node requires non-empty {field_name}")

        allowed = {
            "task": {"capability", "input", "retry", "timeout", "resources", "validators"},
            "subgraph": {"graph", "input", "resources"},
            "join": {"join"},
            "gate": {"expression"},
            "interrupt": {"reason", "actions"},
            "end": set(),
        }[self.kind]
        supplied_payload = self.model_fields_set - {"kind"}
        for field_name in sorted(supplied_payload - allowed):
            raise ValueError(f"{self.kind} node does not accept {field_name}")

        if self.kind == "interrupt":
            if any(not action.strip() for action in self.actions):
                raise ValueError("interrupt actions must be non-empty")
            if len(set(self.actions)) != len(self.actions):
                raise ValueError("interrupt actions must be unique")
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
