from __future__ import annotations

import math
from collections.abc import Mapping
from types import MappingProxyType
from typing import Annotated, Any, NoReturn, cast

from pydantic import BeforeValidator, JsonValue, PlainSerializer


class FrozenJSONDict(dict[str, object]):
    """A JSON object that preserves dict compatibility while denying mutation."""

    def _deny_mutation(self, *_args: object, **_kwargs: object) -> NoReturn:
        raise TypeError("frozen JSON object cannot be mutated")

    __setitem__ = _deny_mutation
    __delitem__ = _deny_mutation
    __ior__ = _deny_mutation
    clear = _deny_mutation
    pop = _deny_mutation
    popitem = _deny_mutation
    setdefault = _deny_mutation  # pyright: ignore[reportAssignmentType]
    update = _deny_mutation  # pyright: ignore[reportAssignmentType]


class FrozenJSONList(list[object]):
    """A JSON array that preserves list compatibility while denying mutation."""

    def _deny_mutation(self, *_args: object, **_kwargs: object) -> NoReturn:
        raise TypeError("frozen JSON array cannot be mutated")

    __setitem__ = _deny_mutation
    __delitem__ = _deny_mutation
    __iadd__ = _deny_mutation
    __imul__ = _deny_mutation
    append = _deny_mutation
    clear = _deny_mutation
    extend = _deny_mutation
    insert = _deny_mutation
    pop = _deny_mutation
    remove = _deny_mutation
    reverse = _deny_mutation
    sort = _deny_mutation  # pyright: ignore[reportAssignmentType]


def freeze_json(value: object) -> object:
    """Copy JSON-compatible data into canonical immutable containers."""

    return _freeze_json(value, compatible_containers=False)


def freeze_json_containers(value: object) -> object:
    """Copy JSON data into immutable dict/list-compatible containers."""

    return _freeze_json(value, compatible_containers=True)


def _freeze_json(value: object, *, compatible_containers: bool) -> object:

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
        frozen = {
            key: _freeze_json(item, compatible_containers=compatible_containers)
            for key, item in sorted(items.items())
        }
        if compatible_containers:
            return FrozenJSONDict(frozen)
        return MappingProxyType(frozen)
    if isinstance(value, list | tuple):
        frozen_items = [_freeze_json(item, compatible_containers=compatible_containers) for item in value]
        if compatible_containers:
            return FrozenJSONList(frozen_items)
        return tuple(frozen_items)
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

FrozenJSONContainerValue = Annotated[
    object,
    BeforeValidator(freeze_json_containers),
    PlainSerializer(thaw_json, return_type=JsonValue),
]


__all__ = [
    "FrozenJSONDict",
    "FrozenJSONList",
    "FrozenJSONContainerValue",
    "FrozenJSONValue",
    "freeze_json",
    "freeze_json_containers",
    "thaw_json",
]
