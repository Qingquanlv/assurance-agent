"""Framework control channel and the state schemas a compiled flow uses."""

from __future__ import annotations

import types
from collections.abc import Mapping
from typing import Annotated, Any, TypedDict, Union, get_args, get_origin, get_type_hints

from pydantic import BaseModel

from graph_engine.stategraph.checkpoint_bridge import (
    CHECKPOINT_MARKERS_STATE_KEY,
    replace_checkpoint_marker_batch,
)
from graph_engine.flow.errors import FlowCheckError
from graph_engine.stategraph.ledger import merge_artifact_ledger

RESERVED_CHANNELS = frozenset(
    {
        "artifact_ledger",
        "attempt_failure",
        "flow_control",
        "flow_outcome",
        CHECKPOINT_MARKERS_STATE_KEY,
    }
)
ROUTE_SENTINEL = "__flow_route__"
_TABLES = frozenset({"loops", "gates", "results", "failures", "terminal", "gate_failures"})


def merge_flow_control(left: object, right: object) -> dict[str, Any]:
    """Merge ``loops``, ``gates``, ``results``, and ``failures`` key by key.

    The right-hand write wins for a shared key. ``public_receipts`` appends.
    Other control fields follow the same replacement rule.
    """
    base = _control_mapping(left)
    incoming = _control_mapping(right)
    merged = dict(base)
    for name, value in incoming.items():
        if name in _TABLES and isinstance(value, Mapping):
            previous = merged.get(name)
            table = dict(previous) if isinstance(previous, Mapping) else {}
            table.update(dict(value))
            merged[name] = table
            continue
        if name == "public_receipts" and isinstance(value, list):
            previous = merged.get(name)
            earlier = list(previous) if isinstance(previous, list) else []
            merged[name] = [*earlier, *value]
            continue
        merged[name] = value
    return merged


def control_table(state: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    raw = state.get("flow_control")
    if not isinstance(raw, Mapping):
        return {}
    table = raw.get(name)
    if not isinstance(table, Mapping):
        return {}
    return table


def read_round(table: Mapping[str, Any], key: str) -> int:
    if key not in table:
        return 0
    value = table[key]
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"loop {key} round must be a non-negative integer")
    return value


def attach_control(update: dict[str, Any], **tables: Mapping[str, Any]) -> None:
    delta = {name: dict(table) for name, table in tables.items() if table}
    if not delta:
        return
    current = update.get("flow_control")
    if isinstance(current, Mapping):
        update["flow_control"] = merge_flow_control(current, delta)
        return
    update["flow_control"] = delta


def dig(value: object, path: str) -> object:
    current = value
    for part in path.split("."):
        if isinstance(current, Mapping) and part in current:
            current = current[part]
            continue
        if isinstance(current, BaseModel) and part in type(current).model_fields:
            current = getattr(current, part)
            continue
        raise KeyError(path)
    return current


def project_input(schema: type[BaseModel], state: Mapping[str, Any]) -> dict[str, Any]:
    return {name: state[name] for name in schema.model_fields if name in state}


def validate_input(schema: type[BaseModel], state: Mapping[str, Any]) -> None:
    schema.model_validate(project_input(schema, state))


def model_annotations(schema: type[BaseModel]) -> dict[str, Any]:
    try:
        hints = get_type_hints(schema, include_extras=True)
    except Exception:
        hints = {name: field.annotation for name, field in schema.model_fields.items()}
    return {name: hints.get(name, field.annotation) for name, field in schema.model_fields.items()}


def assert_model_path(schema: type[BaseModel], path: str, *, what: str) -> None:
    parts = [part for part in path.split(".") if part]
    if not parts or len(parts) != path.count(".") + 1:
        raise FlowCheckError(f"{what} {path!r} is not a field path")
    current: Any = schema
    for index, part in enumerate(parts):
        fields = _annotation_map(current)
        if fields is None:
            return
        if part not in fields:
            raise FlowCheckError(f"{what} {path} is not on the model")
        if index == len(parts) - 1:
            return
        current = unwrap_optional(fields[part])


def literal_strings(annotation: Any) -> frozenset[str] | None:
    """Return the strings of a bare ``Literal``. ``Literal | None`` is not bare."""
    origin = get_origin(unwrap_annotated(annotation))
    if origin is not _literal_origin():
        return None
    values = get_args(unwrap_annotated(annotation))
    if not values or any(not isinstance(item, str) for item in values):
        return None
    return frozenset(values)


def optional_literal(annotation: Any) -> bool:
    inner = unwrap_annotated(annotation)
    origin = get_origin(inner)
    if origin is not Union and origin is not types.UnionType:
        return False
    args = get_args(inner)
    if type(None) not in args:
        return False
    remaining = [item for item in args if item is not type(None)]
    return len(remaining) == 1 and literal_strings(remaining[0]) is not None


def unwrap_optional(annotation: Any) -> Any:
    inner = unwrap_annotated(annotation)
    origin = get_origin(inner)
    if origin is Union or origin is types.UnionType:
        remaining = [item for item in get_args(inner) if item is not type(None)]
        if len(remaining) == 1:
            return unwrap_annotated(remaining[0])
    return inner


def unwrap_annotated(annotation: Any) -> Any:
    if get_origin(annotation) is Annotated:
        return get_args(annotation)[0]
    return annotation


def select_literals(annotation: Any) -> frozenset[str] | None:
    inner = unwrap_optional(annotation)
    origin = get_origin(inner)
    if origin not in (list, tuple):
        return None
    args = get_args(inner)
    if not args:
        return None
    element = args[0]
    if origin is tuple and len(args) == 2 and args[1] is Ellipsis:
        element = args[0]
    elif origin is tuple and any(item is not args[0] for item in args):
        return None
    return literal_strings(element)


_PUBLIC_CHANNELS = ("output", "status", "receipts", "terminal")


def build_schemas(
    flow_name: str,
    input_schema: type[BaseModel],
    *,
    output_mode: str,
    outcome_field: str | None = None,
    public: bool = False,
    controls: Mapping[str, Any] | None = None,
) -> tuple[type, type, type, frozenset[str]]:
    fields = model_annotations(input_schema)
    control_fields = {} if controls is None else controls
    for name in fields:
        if name in RESERVED_CHANNELS or name.startswith("__"):
            raise FlowCheckError(f"input {name} collides with a state channel")
    for name in control_fields:
        if name in RESERVED_CHANNELS or name in fields or name.startswith("__"):
            raise FlowCheckError(f"control {name} collides with a state channel")
    if public:
        for name in _PUBLIC_CHANNELS:
            if name in RESERVED_CHANNELS or name in fields or name in control_fields or name == outcome_field:
                raise FlowCheckError(f"public field {name} collides with a state channel")
    state: dict[str, Any] = dict(fields)
    for name, annotation in control_fields.items():
        state[name] = annotation
    state["artifact_ledger"] = Annotated[dict[str, list[dict[str, str]]], merge_artifact_ledger]
    state["attempt_failure"] = dict[str, Any]
    state["flow_control"] = Annotated[dict[str, Any], merge_flow_control]
    state["flow_outcome"] = str
    if outcome_field is not None:
        state[outcome_field] = str
    if public:
        state["output"] = dict[str, Any]
        state["status"] = str
        state["receipts"] = list[dict[str, str]]
        state["terminal"] = dict[str, str]
    state[CHECKPOINT_MARKERS_STATE_KEY] = Annotated[list[Any], replace_checkpoint_marker_batch]
    stem = "Flow_" + "".join(char if char.isalnum() else "_" for char in flow_name)
    # Dynamic fields cannot satisfy TypedDict's constructor overload.
    state_type = TypedDict(f"{stem}_state", state, total=False)  # noqa: UP013  # pyright: ignore[reportArgumentType]
    input_fields: dict[str, Any] = {name: fields[name] for name in fields}
    input_fields["artifact_ledger"] = state["artifact_ledger"]
    input_fields["flow_control"] = state["flow_control"]
    input_type = TypedDict(f"{stem}_input", input_fields, total=False)  # noqa: UP013  # pyright: ignore[reportArgumentType]
    if output_mode == "branch":
        output_fields = {
            "artifact_ledger": state["artifact_ledger"],
            "flow_control": state["flow_control"],
        }
    elif output_mode == "root":
        # A mounted root must not write the parent's input channels back.
        output_fields = {
            "flow_outcome": state["flow_outcome"],
            "artifact_ledger": state["artifact_ledger"],
            "attempt_failure": state["attempt_failure"],
            "flow_control": state["flow_control"],
            CHECKPOINT_MARKERS_STATE_KEY: state[CHECKPOINT_MARKERS_STATE_KEY],
        }
        for name, annotation in control_fields.items():
            output_fields[name] = annotation
        if outcome_field is not None:
            output_fields[outcome_field] = str
        if public:
            output_fields["output"] = state["output"]
            output_fields["status"] = state["status"]
            output_fields["receipts"] = state["receipts"]
            output_fields["terminal"] = state["terminal"]
    else:
        output_fields = dict(state)
    output_type = TypedDict(f"{stem}_output", output_fields, total=False)  # noqa: UP013  # pyright: ignore[reportArgumentType]
    return state_type, input_type, output_type, frozenset(state)


def _control_mapping(value: object) -> Mapping[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return value
    raise TypeError("flow_control must be a mapping")


def _annotation_map(schema: Any) -> dict[str, Any] | None:
    if isinstance(schema, type) and issubclass(schema, BaseModel):
        return model_annotations(schema)
    origin = get_origin(schema)
    if origin is dict or origin is Mapping:
        return None
    return None


def _literal_origin() -> Any:
    from typing import Literal

    return Literal


__all__ = [
    "RESERVED_CHANNELS",
    "ROUTE_SENTINEL",
    "assert_model_path",
    "attach_control",
    "build_schemas",
    "control_table",
    "dig",
    "literal_strings",
    "merge_flow_control",
    "model_annotations",
    "optional_literal",
    "project_input",
    "read_round",
    "select_literals",
    "unwrap_optional",
    "validate_input",
]
