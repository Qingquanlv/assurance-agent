from __future__ import annotations

import unicodedata
from collections.abc import Mapping
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from graph_engine.errors import GraphEngineError
from graph_engine.frozen_json import FrozenJSONValue, thaw_json
from graph_engine.graph.input_projection import (
    InputProjectionError,
    JSONPointer,
    LiteralProjection,
    resolve_json_pointer,
)

_PROJECTION_MODEL_CONFIG = ConfigDict(
    frozen=True,
    extra="forbid",
    allow_inf_nan=False,
    populate_by_name=True,
)


class _ProjectionModel(BaseModel):
    model_config = _PROJECTION_MODEL_CONFIG


class OutputProjectionError(GraphEngineError):
    """Raised when a declared output projection cannot be evaluated."""


class ChildOutputPointerProjection(_ProjectionModel):
    type: Literal["child_output_pointer"] = "child_output_pointer"
    pointer: JSONPointer


class ObjectProjection(_ProjectionModel):
    type: Literal["object"] = "object"
    fields: dict[str, "OutputProjectionDef"]

    @model_validator(mode="after")
    def _validate_unique_normalized_field_names(self) -> Self:
        seen: dict[str, str] = {}
        for key in self.fields:
            normalized = unicodedata.normalize("NFC", key)
            if normalized in seen:
                raise ValueError("duplicate object projection field after normalization")
            seen[normalized] = key
        return self


class TupleProjection(_ProjectionModel):
    type: Literal["tuple"] = "tuple"
    items: tuple["OutputProjectionDef", ...]


OutputProjectionDef = Annotated[
    LiteralProjection | ChildOutputPointerProjection | ObjectProjection | TupleProjection,
    Field(discriminator="type"),
]

_OUTPUT_PROJECTION_ADAPTER: TypeAdapter[OutputProjectionDef] = TypeAdapter(OutputProjectionDef)


def parse_output_projection(value: object) -> OutputProjectionDef:
    return _OUTPUT_PROJECTION_ADAPTER.validate_python(value)


def project_subgraph_output(
    projection: OutputProjectionDef,
    *,
    child_output: FrozenJSONValue,
) -> FrozenJSONValue:
    return _evaluate_projection(projection, child_output)


def _evaluate_projection(projection: OutputProjectionDef, child_output: object) -> object:
    if isinstance(projection, LiteralProjection):
        return thaw_json(projection.value)
    if isinstance(projection, ChildOutputPointerProjection):
        return thaw_json(_resolve_child_pointer(child_output, projection.pointer))
    if isinstance(projection, ObjectProjection):
        return {
            key: _evaluate_projection(item, child_output) for key, item in sorted(projection.fields.items())
        }
    if isinstance(projection, TupleProjection):
        return tuple(_evaluate_projection(item, child_output) for item in projection.items)
    raise OutputProjectionError(f"unsupported projection operator {type(projection).__name__}")


def _resolve_child_pointer(document: object, pointer: str) -> object:
    try:
        return resolve_json_pointer(document, pointer)
    except InputProjectionError as error:
        raise OutputProjectionError(str(error)) from error


def validate_output_projection_compile(
    projection: OutputProjectionDef | Mapping[str, object],
    *,
    location: str,
) -> None:
    if isinstance(projection, Mapping):
        _detect_raw_projection_cycle(projection, seen=set(), location=location)
    parsed = parse_output_projection(projection)
    _validate_projection_tree(parsed, location=location, seen=set())


def _detect_raw_projection_cycle(value: object, *, seen: set[int], location: str) -> None:
    from graph_engine.graph.compiler import CompileError

    if not isinstance(value, Mapping):
        return
    identity = id(value)
    if identity in seen:
        raise CompileError(f"output projection cycle at {location}")
    seen.add(identity)
    fields = value.get("fields")
    if isinstance(fields, Mapping):
        for item in fields.values():
            _detect_raw_projection_cycle(item, seen=seen, location=location)
    items = value.get("items")
    if isinstance(items, list | tuple):
        for item in items:
            _detect_raw_projection_cycle(item, seen=seen, location=location)


def _validate_projection_tree(
    projection: OutputProjectionDef,
    *,
    location: str,
    seen: set[int],
) -> None:
    from graph_engine.graph.compiler import CompileError

    identity = id(projection)
    if identity in seen:
        raise CompileError(f"output projection cycle at {location}")
    seen.add(identity)

    nested: tuple[OutputProjectionDef, ...]
    if isinstance(projection, ObjectProjection):
        nested = tuple(projection.fields.values())
    elif isinstance(projection, TupleProjection):
        nested = projection.items
    else:
        nested = ()

    for item in nested:
        _validate_projection_tree(item, location=location, seen=seen)


__all__ = [
    "ChildOutputPointerProjection",
    "LiteralProjection",
    "ObjectProjection",
    "OutputProjectionDef",
    "OutputProjectionError",
    "TupleProjection",
    "parse_output_projection",
    "project_subgraph_output",
    "validate_output_projection_compile",
]
