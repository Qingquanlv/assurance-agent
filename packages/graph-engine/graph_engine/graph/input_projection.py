from __future__ import annotations

import unicodedata
from collections.abc import Mapping
from typing import Annotated, Literal, Self, cast

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, TypeAdapter, model_validator

from graph_engine.errors import GraphEngineError
from graph_engine.frozen_json import FrozenJSONValue, thaw_json

_PROJECTION_MODEL_CONFIG = ConfigDict(
    frozen=True,
    extra="forbid",
    allow_inf_nan=False,
    populate_by_name=True,
)


class _ProjectionModel(BaseModel):
    model_config = _PROJECTION_MODEL_CONFIG


class InputProjectionError(GraphEngineError):
    """Raised when a declared input projection cannot be evaluated."""


def _validate_canonical_json_pointer(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("JSON pointer must be a string")
    if value == "":
        return value
    if not value.startswith("/"):
        raise ValueError("JSON pointer must be empty or start with '/'")
    if value.endswith("/"):
        raise ValueError("JSON pointer must not end with '/'")
    for token in value.split("/")[1:]:
        if not token:
            raise ValueError("JSON pointer must not contain empty reference tokens")
        index = 0
        while index < len(token):
            tilde = token.find("~", index)
            if tilde == -1:
                break
            if tilde == len(token) - 1:
                raise ValueError("JSON pointer contains an invalid escape sequence")
            escaped = token[tilde + 1]
            if escaped not in {"0", "1"}:
                raise ValueError("JSON pointer contains an invalid escape sequence")
            index = tilde + 2
    return value


JSONPointer = Annotated[str, BeforeValidator(_validate_canonical_json_pointer)]


def _decode_pointer_token(token: str) -> str:
    return token.replace("~1", "/").replace("~0", "~")


def resolve_json_pointer(document: object, pointer: str) -> object:
    if pointer == "":
        return document
    current = document
    for token in pointer.split("/")[1:]:
        decoded = _decode_pointer_token(token)
        if isinstance(current, Mapping):
            mapping = cast(Mapping[str, object], current)
            if decoded not in mapping:
                raise InputProjectionError(f"missing pointer {pointer!r}")
            current = mapping[decoded]
            continue
        if isinstance(current, list | tuple):
            if not decoded.isdecimal() or decoded != str(int(decoded)):
                raise InputProjectionError(f"missing pointer {pointer!r}")
            index = int(decoded)
            if index < 0 or index >= len(current):
                raise InputProjectionError(f"missing pointer {pointer!r}")
            current = current[index]
            continue
        raise InputProjectionError(f"missing pointer {pointer!r}")
    return current


class LiteralProjection(_ProjectionModel):
    type: Literal["literal"] = "literal"
    value: FrozenJSONValue


class RootPointerProjection(_ProjectionModel):
    type: Literal["root_pointer"] = "root_pointer"
    pointer: JSONPointer


class ConfigPointerProjection(_ProjectionModel):
    type: Literal["config_pointer"] = "config_pointer"
    pointer: JSONPointer


class PredecessorPointerProjection(_ProjectionModel):
    type: Literal["predecessor_pointer"] = "predecessor_pointer"
    predecessor: str
    pointer: JSONPointer


class PredecessorValueProjection(_ProjectionModel):
    type: Literal["predecessor"] = "predecessor"
    predecessor: str


class AllPredecessorTokensProjection(_ProjectionModel):
    type: Literal["all_predecessor_tokens"] = "all_predecessor_tokens"


class ObjectProjection(_ProjectionModel):
    type: Literal["object"] = "object"
    fields: dict[str, "InputProjectionDef"]

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
    items: tuple["InputProjectionDef", ...]


InputProjectionDef = Annotated[
    LiteralProjection
    | RootPointerProjection
    | ConfigPointerProjection
    | PredecessorPointerProjection
    | PredecessorValueProjection
    | AllPredecessorTokensProjection
    | ObjectProjection
    | TupleProjection,
    Field(discriminator="type"),
]

_INPUT_PROJECTION_ADAPTER: TypeAdapter[InputProjectionDef] = TypeAdapter(InputProjectionDef)


def project_task_input(
    projection: InputProjectionDef,
    *,
    root_input: FrozenJSONValue,
    node_config: FrozenJSONValue,
    predecessor_tokens: Mapping[str, FrozenJSONValue],
) -> FrozenJSONValue:
    return _evaluate_projection(projection, root_input, node_config, predecessor_tokens)


def _evaluate_projection(
    projection: InputProjectionDef,
    root_input: object,
    node_config: object,
    predecessor_tokens: Mapping[str, object],
) -> object:
    if isinstance(projection, LiteralProjection):
        return thaw_json(projection.value)
    if isinstance(projection, RootPointerProjection):
        return thaw_json(resolve_json_pointer(root_input, projection.pointer))
    if isinstance(projection, ConfigPointerProjection):
        return thaw_json(resolve_json_pointer(node_config, projection.pointer))
    if isinstance(projection, PredecessorPointerProjection):
        token = _require_predecessor_token(predecessor_tokens, projection.predecessor)
        return thaw_json(resolve_json_pointer(token, projection.pointer))
    if isinstance(projection, PredecessorValueProjection):
        return thaw_json(_require_predecessor_token(predecessor_tokens, projection.predecessor))
    if isinstance(projection, AllPredecessorTokensProjection):
        return tuple(
            thaw_json(predecessor_tokens[predecessor])
            for predecessor in sorted(predecessor_tokens)
        )
    if isinstance(projection, ObjectProjection):
        return {
            key: _evaluate_projection(item, root_input, node_config, predecessor_tokens)
            for key, item in sorted(projection.fields.items())
        }
    if isinstance(projection, TupleProjection):
        return tuple(
            _evaluate_projection(item, root_input, node_config, predecessor_tokens)
            for item in projection.items
        )
    raise InputProjectionError(f"unsupported projection operator {type(projection).__name__}")


def _require_predecessor_token(predecessor_tokens: Mapping[str, object], predecessor: str) -> object:
    if predecessor not in predecessor_tokens:
        raise InputProjectionError(f"missing predecessor token {predecessor!r}")
    return predecessor_tokens[predecessor]


def validate_input_projection_compile(
    projection: InputProjectionDef | Mapping[str, object],
    *,
    node_kind: Literal["task", "gate", "join", "subgraph", "interrupt", "end"],
    join_kind: Literal["all", "any"] | None,
    direct_predecessors: frozenset[str],
    location: str,
) -> None:

    if isinstance(projection, Mapping):
        _detect_raw_projection_cycle(projection, seen=set(), location=location)
    parsed = _INPUT_PROJECTION_ADAPTER.validate_python(projection)
    _validate_projection_tree(
        parsed,
        node_kind=node_kind,
        join_kind=join_kind,
        direct_predecessors=direct_predecessors,
        location=location,
        seen=set(),
    )


def _detect_raw_projection_cycle(value: object, *, seen: set[int], location: str) -> None:
    from graph_engine.graph.compiler import CompileError

    if not isinstance(value, Mapping):
        return
    identity = id(value)
    if identity in seen:
        raise CompileError(f"input projection cycle at {location}")
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
    projection: InputProjectionDef,
    *,
    node_kind: Literal["task", "gate", "join", "subgraph", "interrupt", "end"],
    join_kind: Literal["all", "any"] | None,
    direct_predecessors: frozenset[str],
    location: str,
    seen: set[int],
) -> None:
    from graph_engine.graph.compiler import CompileError

    identity = id(projection)
    if identity in seen:
        raise CompileError(f"input projection cycle at {location}")
    seen.add(identity)

    if isinstance(projection, PredecessorPointerProjection | PredecessorValueProjection):
        if projection.predecessor not in direct_predecessors:
            raise CompileError(
                f"non-direct predecessor {projection.predecessor!r} in input projection at {location}"
            )
    elif isinstance(projection, AllPredecessorTokensProjection):
        if node_kind != "join" or join_kind != "all":
            raise CompileError(
                f"all_predecessor_tokens is allowed only on all joins at {location}"
            )
        if len(direct_predecessors) < 2:
            raise CompileError(
                f"all_predecessor_tokens requires at least two direct predecessors at {location}"
            )

    nested: tuple[InputProjectionDef, ...]
    if isinstance(projection, ObjectProjection):
        nested = tuple(projection.fields.values())
    elif isinstance(projection, TupleProjection):
        nested = projection.items
    else:
        nested = ()

    for item in nested:
        _validate_projection_tree(
            item,
            node_kind=node_kind,
            join_kind=join_kind,
            direct_predecessors=direct_predecessors,
            location=location,
            seen=seen,
        )


__all__ = [
    "AllPredecessorTokensProjection",
    "ConfigPointerProjection",
    "InputProjectionDef",
    "InputProjectionError",
    "JSONPointer",
    "LiteralProjection",
    "ObjectProjection",
    "PredecessorPointerProjection",
    "PredecessorValueProjection",
    "RootPointerProjection",
    "TupleProjection",
    "project_task_input",
    "resolve_json_pointer",
    "validate_input_projection_compile",
]
