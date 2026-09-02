from __future__ import annotations

import json

from pydantic import BaseModel, Field

from agent_runtime_contracts import AgentRunRequest
from graph_engine.attempts.context import AttemptExecutionContext
from graph_engine.attempts.contracts import (
    AttemptRetryPolicy,
    AttemptTimeoutPolicy,
    ResolvedAttemptContract,
    TaskAttemptContract,
    resolve_contract,
)
from graph_engine.canonical import canonical_digest
from graph_engine.plugin_api import AttemptContractRef, ResourceClaims, WorkspaceProvider

from agent_runtime_fixture import RUN_CAPABILITY_ID, assemble_request, fixture_config, fixture_resources


class RunOutput(BaseModel):
    artifact: str = Field(min_length=1)
    status: str = Field(min_length=1)


RUN_CONTRACT = TaskAttemptContract(
    contract_id=RUN_CAPABILITY_ID,
    owner_id="fixture.binding",
    handler_id=RUN_CAPABILITY_ID,
    input_model=AgentRunRequest,
    output_model=RunOutput,
    resources=ResourceClaims(writes=("result.json",)),
    retry=AttemptRetryPolicy(max_attempts=1),
    timeout=AttemptTimeoutPolicy(seconds=120),
    validators=(),
)
RUN_CONTRACT_REF = AttemptContractRef(
    contract_id=RUN_CONTRACT.contract_id,
    digest=canonical_digest(RUN_CONTRACT.canonical_projection()),
)


class RunExecutor:
    def __init__(self, workspace: WorkspaceProvider) -> None:
        self._workspace = workspace

    async def execute(
        self,
        validated_input: AgentRunRequest,
        context: AttemptExecutionContext,
    ) -> RunOutput:
        del validated_input
        binding = await self._workspace.open_or_create(context.attempt_key, RUN_CONTRACT.resources)
        payload = {"artifact": "result.json", "status": "ok"}
        (binding.write_root / "result.json").write_text(json.dumps(payload), encoding="utf-8")
        return RunOutput.model_validate(payload)


def frozen_run_request() -> AgentRunRequest:
    return assemble_request(fixture_resources(), fixture_config())


def bind_run_executor(workspace: WorkspaceProvider) -> ResolvedAttemptContract[AgentRunRequest, RunOutput]:
    return resolve_contract(RUN_CONTRACT, executor=RunExecutor(workspace))


def published_contracts() -> tuple[TaskAttemptContract[AgentRunRequest, RunOutput], ...]:
    return (RUN_CONTRACT,)
