from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Generic, Protocol, TypeAlias, TypeVar

from pydantic import BaseModel, Field, model_serializer

from graph_engine.stategraph.ledger import InputBinding, LedgerArtifact, NamedWrite, ledger_key

from graph_engine.attempts.models.context import AuthorizedAttemptScope
from graph_engine.attempts.models.runtime_evidence import RUNTIME_EVIDENCE
from graph_engine.attempts.models.resolutions import (
    IndeterminateTaskResult,
    PendingTaskResult,
    PermanentTaskFailure,
    RejectedTaskResult,
)
from graph_engine.canonical import JSONValue, canonical_digest
from graph_engine.identifiers import validate_qualified_id
from graph_engine.plugin_api import FrozenModel, ResourceClaims, ResourceClaimTemplate


InputT = TypeVar("InputT", bound=BaseModel)
OutputT = TypeVar("OutputT", bound=BaseModel)
_InputT_contra = TypeVar("_InputT_contra", bound=BaseModel, contravariant=True)


class AttemptRetryPolicy(FrozenModel):
    max_attempts: int = Field(ge=1)
    interval_seconds: float = Field(default=0, ge=0, allow_inf_nan=False)
    # An invalid_output retry starts from a copy of the rejected attempt's write root.
    carry_invalid_output: bool = False

    @model_serializer(mode="wrap")
    def _serialize(self, handler: Any) -> Any:
        serialized = handler(self)
        if not self.carry_invalid_output:
            serialized.pop("carry_invalid_output", None)
        return serialized


class AttemptTimeoutPolicy(FrozenModel):
    seconds: float = Field(gt=0)


class TerminalReceiptRef(FrozenModel):
    identity_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    receipt_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class ExecutedAttemptResult(FrozenModel, Generic[OutputT]):
    output: OutputT
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
    writes: tuple[NamedWrite, ...] = ()
    bindings: tuple[InputBinding, ...] = ()
    capabilities: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        validate_qualified_id(self.contract_id)
        validate_qualified_id(self.owner_id)
        validate_qualified_id(self.handler_id)
        if not isinstance(self.validators, tuple):
            raise TypeError("validators must be an explicit tuple")
        for validator_id in self.validators:
            validate_qualified_id(validator_id)
        if not isinstance(self.capabilities, tuple):
            raise TypeError("capabilities must be an explicit tuple")
        unknown = [name for name in self.capabilities if name != RUNTIME_EVIDENCE]
        if unknown:
            raise ValueError(f"unknown task capability: {unknown[0]}")

    def canonical_projection(self) -> dict[str, JSONValue]:
        projection: dict[str, JSONValue] = {
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
        if self.capabilities:
            projection["capabilities"] = list(self.capabilities)
        return projection

    def ledger_namespace(self) -> str:
        return self.owner_id.rsplit(".", 1)[-1]

    def ledger_writes(self) -> tuple[NamedWrite, ...]:
        return self.writes

    def input_bindings(self) -> tuple[InputBinding, ...]:
        return self.bindings

    def artifact(self, name: str, *, slot: str | None = None) -> LedgerArtifact:
        """Handle for a named write. The ledger key is ``{namespace}.{name}``."""
        spec = next((item for item in self.writes if item.name == name), None)
        if spec is None:
            raise ValueError(f"{self.contract_id} does not write {name}")
        return LedgerArtifact(
            ledger_key=ledger_key(self.ledger_namespace(), name),
            slot=slot,
            many=spec.many,
        )


@dataclass(frozen=True, slots=True)
class ResolvedAttemptContract(Generic[InputT, OutputT]):
    contract: TaskAttemptContract[InputT, OutputT]
    executor: AttemptExecutor[InputT, OutputT]
    contract_digest: str
    validation_context: Mapping[str, object]

    def canonical_projection(self) -> dict[str, JSONValue]:
        return self.contract.canonical_projection()


def resolve_contract(
    contract: TaskAttemptContract[InputT, OutputT],
    *,
    executor: AttemptExecutor[InputT, OutputT],
    validation_context: Mapping[str, object] | None = None,
) -> ResolvedAttemptContract[InputT, OutputT]:
    projection = contract.canonical_projection()
    return ResolvedAttemptContract(
        contract=contract,
        executor=executor,
        contract_digest=canonical_digest(projection),
        validation_context=MappingProxyType(dict(validation_context or {})),
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
