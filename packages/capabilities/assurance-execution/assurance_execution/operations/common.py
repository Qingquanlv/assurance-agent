"""Shared handler errors and input helpers."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from pydantic import ValidationError

from graph_engine.canonical import JSONValue, canonical_digest
from graph_engine.plugin_api import TaskOutcome

from assurance_execution.contracts.selection import ClosedMappingV1


class InputError(ValueError):
    """Malformed caller input or missing locked configuration."""


class OutputError(ValueError):
    """Model-authored semantic invalidity."""


def leafs_of(values: Iterable[str]) -> frozenset[str]:
    return frozenset(values)


def validate_input(model: type[Any], data: object) -> Any:
    try:
        return model.model_validate(data)
    except ValidationError as error:
        raise InputError(str(error)) from error


def failed_input(error: Exception) -> TaskOutcome:
    return TaskOutcome.failed("invalid_input", str(error), retryable=True)


def failed_output(message: str) -> TaskOutcome:
    return TaskOutcome.failed("invalid_output", message, retryable=True)


def mapping_digest(mapping: ClosedMappingV1) -> str:
    return canonical_digest(mapping.model_dump(mode="json"))


def json_digest(value: JSONValue) -> str:
    return canonical_digest(value)
