from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Generic, TypeVar

from pydantic import BaseModel

from graph_engine.attempts import AttemptRetryPolicy, AttemptTimeoutPolicy, TaskAttemptContract
from graph_engine.identifiers import validate_qualified_id
from graph_engine.plugin_api import ResourceClaimTemplate, ResourceClaims

from agent_runtime_contracts.schema import canonical_digest, result_schema_from_model

RAW_AGENT_CONTRACT_SCHEMA_VERSION = "raw-agent-contract-v1"


InputT = TypeVar("InputT", bound=BaseModel)
AgentResultT = TypeVar("AgentResultT", bound=BaseModel)
OutputT = TypeVar("OutputT", bound=BaseModel)


def _model_symbol(model: type[BaseModel]) -> str:
    return f"{model.__module__}.{model.__qualname__}"


@dataclass(frozen=True, slots=True)
class AgentExecutionContract(Generic[InputT, AgentResultT, OutputT]):
    contract_id: str
    owner_id: str
    prepare_handler_id: str
    finalize_handler_id: str
    skill_id: str
    agent_profile: str
    input_model: type[InputT]
    agent_result_model: type[AgentResultT]
    output_model: type[OutputT]
    resources: ResourceClaims | ResourceClaimTemplate
    retry: AttemptRetryPolicy
    timeout: AttemptTimeoutPolicy
    validators: tuple[str, ...]

    def __post_init__(self) -> None:
        validate_qualified_id(self.contract_id)
        validate_qualified_id(self.owner_id)
        validate_qualified_id(self.prepare_handler_id)
        validate_qualified_id(self.finalize_handler_id)
        if not self.skill_id.strip() or self.skill_id != self.skill_id.strip():
            raise ValueError("skill_id must be a nonempty canonical token")
        if not self.agent_profile.strip() or self.agent_profile != self.agent_profile.strip():
            raise ValueError("agent_profile must be a nonempty canonical token")
        if not isinstance(self.validators, tuple):
            raise TypeError("validators must be an explicit tuple")
        for validator_id in self.validators:
            validate_qualified_id(validator_id)

    def agent_result_schema_document(self) -> object:
        return result_schema_from_model(self.agent_result_model)

    def agent_result_schema_digest(self) -> str:
        return canonical_digest(self.agent_result_schema_document())

    def canonical_projection(self) -> dict[str, object]:
        return {
            "schema_version": RAW_AGENT_CONTRACT_SCHEMA_VERSION,
            "contract_id": self.contract_id,
            "owner_id": self.owner_id,
            "prepare_handler_id": self.prepare_handler_id,
            "finalize_handler_id": self.finalize_handler_id,
            "skill_id": self.skill_id,
            "agent_profile": self.agent_profile,
            "input_model": _model_symbol(self.input_model),
            "agent_result_model": _model_symbol(self.agent_result_model),
            "output_model": _model_symbol(self.output_model),
            "agent_result_schema_digest": self.agent_result_schema_digest(),
            "resources": self.resources.model_dump(mode="json"),
            "retry": self.retry.model_dump(mode="json"),
            "timeout": self.timeout.model_dump(mode="json"),
            "validators": list(self.validators),
        }

    def to_task_contract(self) -> TaskAttemptContract[InputT, OutputT]:
        return TaskAttemptContract(
            contract_id=self.contract_id,
            owner_id=self.owner_id,
            handler_id=self.prepare_handler_id,
            input_model=self.input_model,
            output_model=self.output_model,
            resources=self.resources,
            retry=self.retry,
            timeout=self.timeout,
            validators=self.validators,
        )


def _feature_and_base(contract: AgentExecutionContract[Any, Any, Any]) -> tuple[str, str]:
    body = contract.contract_id.removeprefix("assurance.").removesuffix(".v1")
    feature, marker, base = body.partition(".agent.")
    if marker != ".agent." or not feature or not base:
        raise ValueError(f"invalid agent job contract id: {contract.contract_id!r}")
    return feature, base


def expand_agent_job_slots(
    catalogs: Sequence[Mapping[str, AgentExecutionContract[Any, Any, Any]]],
) -> Mapping[str, AgentExecutionContract[Any, Any, Any]]:
    expanded: dict[str, AgentExecutionContract[Any, Any, Any]] = {}
    for catalog in catalogs:
        for base, contract in catalog.items():
            feature, contract_base = _feature_and_base(contract)
            if contract_base != base:
                raise ValueError(f"agent job catalog key drifted: {base!r} vs {contract.contract_id!r}")
            for phase in ("prepare", "execute", "finalize"):
                alias = f"assurance.product.agent.{feature}.{base}.{phase}"
                if alias in expanded:
                    raise ValueError(f"duplicate agent job alias: {alias}")
                expanded[alias] = contract
    return MappingProxyType(expanded)


__all__ = [
    "AgentExecutionContract",
    "RAW_AGENT_CONTRACT_SCHEMA_VERSION",
    "expand_agent_job_slots",
]
