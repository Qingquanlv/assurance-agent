from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, TypeVar

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


def _sorted_unique_paths(paths: tuple[str, ...], *, kind: str) -> tuple[str, ...]:
    if not isinstance(paths, tuple):
        raise TypeError(f"{kind} must be an explicit tuple")
    if paths != tuple(sorted(paths)):
        raise ValueError(f"{kind} must be sorted")
    if len(set(paths)) != len(paths):
        raise ValueError(f"{kind} must be unique")
    for path in paths:
        if (
            not path
            or path != path.strip()
            or "\\" in path
            or any(part in {"", ".", ".."} for part in path.split("/"))
        ):
            raise ValueError(f"{kind} must contain canonical relative paths")
    return paths


@dataclass(frozen=True, slots=True)
class AgentPhaseWriteClaims:
    prepare: tuple[str, ...]
    runtime: tuple[str, ...]
    finalize: tuple[str, ...]

    def __post_init__(self) -> None:
        prepare = _sorted_unique_paths(self.prepare, kind="prepare write claims")
        runtime = _sorted_unique_paths(self.runtime, kind="runtime write claims")
        finalize = _sorted_unique_paths(self.finalize, kind="finalize write claims")
        # Prepare may seed files that runtime subsequently edits. Finalize-owned
        # outputs remain separate from both phases.
        if (set(prepare) | set(runtime)) & set(finalize):
            raise ValueError("finalize write claims must be disjoint from prepare and runtime")

    def as_projection(self) -> dict[str, list[str]]:
        return {
            "prepare": list(self.prepare),
            "runtime": list(self.runtime),
            "finalize": list(self.finalize),
        }


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
    phase_write_claims: AgentPhaseWriteClaims

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
        if not isinstance(self.phase_write_claims, AgentPhaseWriteClaims):
            raise TypeError("phase_write_claims must be AgentPhaseWriteClaims")
        claimed = set(self.phase_write_claims.prepare)
        claimed.update(self.phase_write_claims.runtime)
        claimed.update(self.phase_write_claims.finalize)
        writes = set(self.resources.writes)
        if not claimed <= writes:
            raise ValueError("phase write claims must be a subset of attempt write claims")

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
            "phase_write_claims": self.phase_write_claims.as_projection(),
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


__all__ = [
    "AgentExecutionContract",
    "AgentPhaseWriteClaims",
    "RAW_AGENT_CONTRACT_SCHEMA_VERSION",
]
