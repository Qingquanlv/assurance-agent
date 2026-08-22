"""Shared handler errors and outcome builders."""

from __future__ import annotations

from typing import Any, cast

from pydantic import ValidationError

from graph_engine.canonical import JSONValue
from graph_engine.plugin_api import EffectIntent, TaskOutcome


class InputError(ValueError):
    """Malformed caller input or missing locked configuration."""


class OutputError(ValueError):
    """Model-authored semantic invalidity."""


def validate_input(model: type[Any], data: object) -> Any:
    try:
        return model.model_validate(data)
    except ValidationError as error:
        raise InputError(str(error)) from error


def failed_input(error: Exception) -> TaskOutcome:
    return TaskOutcome.failed("invalid_input", str(error), retryable=False)


def failed_output(message: str) -> TaskOutcome:
    return TaskOutcome.failed("invalid_output", message, retryable=True)


def as_json(payload: object) -> JSONValue:
    return cast(JSONValue, payload)


def succeeded(
    payload: dict[str, object],
    *,
    effects: tuple[EffectIntent, ...] = (),
) -> TaskOutcome:
    return TaskOutcome.succeeded(as_json(payload), effects=effects)
