from __future__ import annotations

from typing import Literal, Self

import yaml
from packaging.version import InvalidVersion, Version
from pydantic import Field, field_validator, model_validator

from graph_engine.graph.output_projection import OutputProjectionDef
from graph_engine.graph.schema import (
    FrozenModel,
    GraphDef,
    RetryPolicyDef,
    TimeoutPolicyDef,
)
from graph_engine.identifiers import IdentifierError, validate_qualified_id


class WorkflowImportDef(FrozenModel):
    owner_id: str
    module_id: str
    export: str

    @field_validator("owner_id", "module_id")
    @classmethod
    def _validate_qualified_ids(cls, value: str) -> str:
        return _require_qualified_id(value)

    @field_validator("export")
    @classmethod
    def _validate_export_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("import export must be non-empty")
        return value


class WorkflowExportDef(FrozenModel):
    graph: str
    input_schema: str
    output_schema: str
    output_projection: OutputProjectionDef

    @field_validator("graph")
    @classmethod
    def _validate_graph_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("export graph must be non-empty")
        return value

    @field_validator("input_schema", "output_schema")
    @classmethod
    def _validate_schema_ids(cls, value: str) -> str:
        return _require_qualified_id(value)


class CapabilitySlotDef(FrozenModel):
    contract_id: str

    @field_validator("contract_id")
    @classmethod
    def _validate_contract_id(cls, value: str) -> str:
        return _require_qualified_id(value)


class WorkflowModuleDef(FrozenModel):
    schema_version: Literal["1"]
    role: Literal["product", "feature"]
    owner_id: str
    module_id: str
    module_version: str
    name: str | None = None
    entrypoints: dict[str, str] = Field(default_factory=dict)
    imports: dict[str, WorkflowImportDef] = Field(default_factory=dict)
    exports: dict[str, WorkflowExportDef] = Field(default_factory=dict)
    capability_slots: dict[str, CapabilitySlotDef] = Field(default_factory=dict)
    schemas: tuple[str, ...] = ()
    resources: tuple[str, ...] = ()
    effects: tuple[str, ...] = ()
    retry: dict[str, RetryPolicyDef]
    timeout: dict[str, TimeoutPolicyDef]
    graphs: dict[str, GraphDef]

    @field_validator("owner_id", "module_id")
    @classmethod
    def _validate_module_ids(cls, value: str) -> str:
        return _require_qualified_id(value)

    @field_validator("module_version")
    @classmethod
    def _normalize_module_version(cls, value: str) -> str:
        try:
            return str(Version(value))
        except InvalidVersion as error:
            raise ValueError(f"invalid module version: {value!r}") from error

    @field_validator("schemas", "resources", "effects")
    @classmethod
    def _validate_registry_references(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized: list[str] = []
        for value in values:
            try:
                normalized.append(validate_qualified_id(value))
            except IdentifierError as error:
                raise ValueError(f"invalid workflow registry reference: {value!r}") from error
        if len(set(normalized)) != len(normalized):
            raise ValueError("workflow registry references must be unique")
        return tuple(sorted(normalized))

    @model_validator(mode="after")
    def _validate_role_and_references(self) -> Self:
        if self.role == "feature":
            if "name" in self.model_fields_set:
                raise ValueError("feature module does not accept name")
            if self.entrypoints:
                raise ValueError("feature module does not accept entrypoints")
            if not self.exports:
                raise ValueError("feature module requires at least one export")
        else:
            if not (self.name and self.name.strip()):
                raise ValueError("product module requires a non-empty name")
            if not self.entrypoints:
                raise ValueError("product module requires entrypoints")
            for entrypoint, graph_id in self.entrypoints.items():
                if graph_id not in self.graphs:
                    raise ValueError(f"entrypoint {entrypoint} references unknown graph {graph_id}")

        for export_name, export in self.exports.items():
            if export.graph not in self.graphs:
                raise ValueError(f"export {export_name} references unknown graph {export.graph}")

        for graph in self.graphs.values():
            for node in graph.nodes.values():
                if node.capability_slot is not None and node.capability_slot not in self.capability_slots:
                    raise ValueError(f"undeclared capability slot {node.capability_slot}")
                if node.graph_import is not None and node.graph_import not in self.imports:
                    raise ValueError(f"undeclared graph import {node.graph_import}")
        return self


def parse_workflow_module(text: bytes | str) -> WorkflowModuleDef:
    decoded = _decode_module_text(text)
    try:
        node = yaml.compose(decoded, Loader=yaml.SafeLoader)
        if node is not None:
            _reject_duplicate_yaml_keys(node, set())
        raw = yaml.safe_load(decoded)
    except yaml.YAMLError as error:
        raise ValueError("workflow module is not safe YAML") from error
    return WorkflowModuleDef.model_validate(
        raw,
        by_alias=True,  # pyright: ignore[reportCallIssue]
        by_name=False,  # pyright: ignore[reportCallIssue]
    )


def _decode_module_text(text: bytes | str) -> str:
    if isinstance(text, str):
        return text
    try:
        return text.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("workflow module is not UTF-8 text") from error


def _reject_duplicate_yaml_keys(node: yaml.nodes.Node, seen: set[int]) -> None:
    identity = id(node)
    if identity in seen:
        return
    seen.add(identity)
    if isinstance(node, yaml.nodes.MappingNode):
        keys: set[tuple[str, str]] = set()
        for key, value in node.value:
            key_identity = (key.tag, getattr(key, "value", ""))
            if key_identity in keys:
                raise ValueError(f"duplicate YAML key: {key_identity[1]}")
            keys.add(key_identity)
            _reject_duplicate_yaml_keys(key, seen)
            _reject_duplicate_yaml_keys(value, seen)
        return
    if isinstance(node, yaml.nodes.SequenceNode):
        for item in node.value:
            _reject_duplicate_yaml_keys(item, seen)


def _require_qualified_id(value: str) -> str:
    try:
        return validate_qualified_id(value)
    except IdentifierError as error:
        raise ValueError(f"invalid qualified identifier: {value!r}") from error


__all__ = [
    "CapabilitySlotDef",
    "WorkflowExportDef",
    "WorkflowImportDef",
    "WorkflowModuleDef",
    "parse_workflow_module",
]
