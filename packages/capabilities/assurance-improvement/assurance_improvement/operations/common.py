"""Shared input normalization and outcome builders."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TypeVar, cast

from pydantic import BaseModel

from agent_runtime_contracts.ops import validate_model
from graph_engine.canonical import JSONValue
from graph_engine.plugin_api import TaskOutcome

ModelT = TypeVar("ModelT", bound=BaseModel)


def _wire_value(value: object) -> object:
    """Normalize imported wheel models to the JSON value contract."""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return {str(key): _wire_value(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_wire_value(item) for item in value]
    return value


def validate_input(model: type[ModelT], data: object) -> ModelT:
    return validate_model(model, _wire_value(data))


def as_json(payload: object) -> JSONValue:
    return cast(JSONValue, payload)


def succeeded(payload: dict[str, object]) -> TaskOutcome:
    return TaskOutcome.succeeded(as_json(payload))
