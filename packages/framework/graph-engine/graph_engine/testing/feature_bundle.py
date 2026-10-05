"""Compile a feature bundle the way a wheel test used to receive it."""

from __future__ import annotations

from dataclasses import fields
from typing import Any

from graph_engine.flow import BoundFlow


def compile_bundle(bundle: object, *, outcome_field: str = "status") -> Any:
    """Compile every bound flow on a feature bundle."""
    values: dict[str, object] = {}
    for field in fields(bundle):  # type: ignore[arg-type]
        value = getattr(bundle, field.name)
        if isinstance(value, BoundFlow):
            values[field.name] = value.compile(outcome_field=outcome_field)
        else:
            values[field.name] = value
    return type(bundle)(**values)


__all__ = ["compile_bundle"]
