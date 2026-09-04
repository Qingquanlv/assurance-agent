from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, Protocol, TypeAlias, TypeVar

from pydantic import BaseModel, Field

from graph_engine.attempts.context import AuthorizedAttemptScope
from graph_engine.attempts.resolutions import (
    IndeterminateTaskResult,
    PendingTaskResult,
    PermanentTaskFailure,
    RejectedTaskResult,
)
from graph_engine.canonical import JSONValue, canonical_digest
from graph_engine.identifiers import validate_qualified_id
from graph_engine.plugin_api import EffectIntent, FrozenModel, ResourceClaims, ResourceClaimTemplate


InputT = TypeVar("InputT", bound=BaseModel)
OutputT = TypeVar("OutputT", bound=BaseModel)
_InputT_contra = TypeVar("_InputT_contra", bound=BaseModel, contravariant=True)


class AttemptRetryPolicy(FrozenModel):
    max_attempts: int = Field(ge=1)


class AttemptTimeoutPolicy(FrozenModel):
    seconds: float = Field(gt=0)


class TerminalReceiptRef(FrozenModel):
    identity_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    receipt_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class ExecutedAttemptResult(FrozenModel, Generic[OutputT]):
    output: OutputT
    effects: tuple[EffectIntent, ...] = ()
    source_terminal_receipt: TerminalReceiptRef | None = None


ExecutorResolution: TypeAlias = (
    RejectedTaskResult | PermanentTaskFailure | PendingTaskResult | IndeterminateTaskResult
)
ExecutorStepResult: TypeAlias = ExecutedAttemptResult[OutputT] | ExecutorResolution


class AttemptExecutor(Protocol[_InputT_contra, OutputT]):
    async def execute(
        self,
        validated_input: _InputT_contra,
        scope: AuthorizedAttemptScope,
    ) -> ExecutorStepResult[OutputT]: ...


def _model_symbol(model: type[BaseModel]) -> str:
    return f"{model.__module__}.{model.__qualname__}"


def _schema_digest(model: type[BaseModel]) -> str:
    schema: JSONValue = model.model_json_schema()
    return canonical_digest(schema)


def _resources_projection(resources: ResourceClaims | ResourceClaimTemplate) -> JSONValue:
    kind = "template" if isinstance(resources, ResourceClaimTemplate) else "claims"
    dumped = resources.model_dump(mode="json")
    return {"kind": kind, **dumped}


@dataclass(frozen=True, slots=True)
class TaskAttemptContract(Generic[InputT, OutputT]):
    contract_id: str
    owner_id: str
    handler_id: str
    input_model: type[InputT]
    output_model: type[OutputT]
    resources: ResourceClaims | ResourceClaimTemplate
    retry: AttemptRetryPolicy
    timeout: AttemptTimeoutPolicy
    validators: tuple[str, ...]

    def __post_init__(self) -> None:
        validate_qualified_id(self.contract_id)
        validate_qualified_id(self.owner_id)
        validate_qualified_id(self.handler_id)
        if not isinstance(self.validators, tuple):
            raise TypeError("validators must be an explicit tuple")
        for validator_id in self.validators:
            validate_qualified_id(validator_id)

    def canonical_projection(self) -> dict[str, JSONValue]:
        return {
            "contract_id": self.contract_id,
            "owner_id": self.owner_id,
            "handler_id": self.handler_id,
            "input_model": _model_symbol(self.input_model),
            "output_model": _model_symbol(self.output_model),
            "input_schema_digest": _schema_digest(self.input_model),
            "output_schema_digest": _schema_digest(self.output_model),
            "resources": _resources_projection(self.resources),
            "retry": self.retry.model_dump(mode="json"),
            "timeout": self.timeout.model_dump(mode="json"),
            "validators": list(self.validators),
        }


@dataclass(frozen=True, slots=True)
class ResolvedAttemptContract(Generic[InputT, OutputT]):
    contract: TaskAttemptContract[InputT, OutputT]
    executor: AttemptExecutor[InputT, OutputT]
    contract_digest: str

    def canonical_projection(self) -> dict[str, JSONValue]:
        return self.contract.canonical_projection()


def resolve_contract(
    contract: TaskAttemptContract[InputT, OutputT],
    *,
    executor: AttemptExecutor[InputT, OutputT],
) -> ResolvedAttemptContract[InputT, OutputT]:
    projection = contract.canonical_projection()
    return ResolvedAttemptContract(
        contract=contract,
        executor=executor,
        contract_digest=canonical_digest(projection),
    )


__all__ = [
    "AttemptExecutor",
    "AttemptRetryPolicy",
    "AttemptTimeoutPolicy",
    "AuthorizedAttemptScope",
    "ExecutedAttemptResult",
    "ExecutorResolution",
    "ExecutorStepResult",
    "ResolvedAttemptContract",
    "TaskAttemptContract",
    "TerminalReceiptRef",
    "resolve_contract",
]
