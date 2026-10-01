"""Endpoint-style entry helpers for Agent op prepare and finalize modules."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, TypeVar

from pydantic import BaseModel

from graph_engine.plugin_api import TaskContext, TaskOutcome

from agent_runtime_contracts.ops.binding import AgentBindingDataV1, validate_binding
from agent_runtime_contracts.ops.errors import (
    InputError,
    OutputError,
    failed_input,
    failed_output,
    validate_model,
)
from agent_runtime_contracts.ops.request import prepared_outcome
from agent_runtime_contracts.wire.models import AgentRunRequest

BusinessT = TypeVar("BusinessT", bound=BaseModel)
FinalizeT = TypeVar("FinalizeT", bound=BaseModel)


class _Request(Protocol):
    @property
    def input(self) -> object: ...

    @property
    def binding_data(self) -> object: ...


def run_prepare(
    request: _Request,
    context: TaskContext,
    *,
    input_model: type[BusinessT],
    build: Callable[[BusinessT, AgentBindingDataV1, TaskContext], AgentRunRequest],
    input_errors: tuple[type[Exception], ...] = (InputError,),
    validate: Callable[[type[BusinessT], object], BusinessT] = validate_model,
) -> TaskOutcome:
    try:
        business = validate(input_model, request.input)
        binding = validate_binding(request.binding_data)
        return prepared_outcome(build(business, binding, context))
    except input_errors as error:
        return failed_input(error)


def run_finalize(
    request: _Request,
    context: TaskContext,
    *,
    input_model: type[FinalizeT],
    commit: Callable[[FinalizeT, TaskContext], TaskOutcome],
    validate: Callable[[type[FinalizeT], object], FinalizeT] = validate_model,
) -> TaskOutcome:
    try:
        return commit(validate(input_model, request.input), context)
    except InputError as error:
        return failed_input(error)
    except OutputError as error:
        return failed_output(str(error))
