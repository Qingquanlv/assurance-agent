"""Error vocabulary shared by capability prepare and finalize handlers."""

from __future__ import annotations

from typing import TypeVar

from pydantic import BaseModel, ValidationError

from graph_engine.plugin_api import TaskOutcome

ModelT = TypeVar("ModelT", bound=BaseModel)


class InputError(ValueError):
    """Caller input or locked configuration is malformed."""


class OutputError(ValueError):
    """The Agent produced semantically invalid output."""


def validate_model(model: type[ModelT], data: object) -> ModelT:
    try:
        return model.model_validate(data)
    except ValidationError as error:
        raise InputError(str(error)) from error


def validate_output(model: type[ModelT], data: object) -> ModelT:
    try:
        return model.model_validate(data)
    except ValidationError as error:
        raise OutputError(str(error)) from error


def failed_input(error: Exception) -> TaskOutcome:
    return TaskOutcome.failed("invalid_input", str(error), retryable=True)


def failed_output(message: str) -> TaskOutcome:
    return TaskOutcome.failed("invalid_output", message, retryable=True)
