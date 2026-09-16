"""Shared handler errors and outcome helpers."""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from graph_engine.plugin_api import TaskOutcome


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
    return TaskOutcome.failed("invalid_input", str(error), retryable=True)


def failed_output(message: str) -> TaskOutcome:
    return TaskOutcome.failed("invalid_output", message, retryable=True)
