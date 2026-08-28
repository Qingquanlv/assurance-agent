from __future__ import annotations

import math
from collections.abc import Mapping
from types import MappingProxyType
from typing import Annotated, Any, cast

from pydantic import BeforeValidator, JsonValue, PlainSerializer


def freeze_json(value: object) -> object:
    """Copy JSON-compatible data into canonical immutable containers."""

    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("JSON numbers must be finite")
        return value
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("JSON object keys must be strings")
        items = cast(Mapping[str, object], value)
        return MappingProxyType({key: freeze_json(item) for key, item in sorted(items.items())})
    if isinstance(value, list | tuple):
        return tuple(freeze_json(item) for item in value)
    raise TypeError(f"value is not JSON-compatible: {type(value).__name__}")


def thaw_json(value: object) -> Any:
    """Copy frozen JSON containers back into ordinary JSON containers."""

    if isinstance(value, Mapping):
        return {key: thaw_json(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [thaw_json(item) for item in value]
    return value


FrozenJSONValue = Annotated[
    object,
    BeforeValidator(freeze_json),
    PlainSerializer(thaw_json, return_type=JsonValue),
]

__all__ = ["FrozenJSONValue", "freeze_json", "thaw_json"]
