from __future__ import annotations

from pydantic import BaseModel, Field

from graph_engine.attempts.context import AuthorizedAttemptScope
from graph_engine.attempts.keys import AttemptKey
from graph_engine.attempts.contracts import (
    AttemptRetryPolicy,
    AttemptTimeoutPolicy,
    ExecutedAttemptResult,
    ResolvedAttemptContract,
    TaskAttemptContract,
    resolve_contract,
)
from graph_engine.attempts.resolutions import PermanentTaskFailure
from graph_engine.canonical import canonical_digest
from graph_engine.plugin_api import AttemptContractRef, ResourceClaims, WorkspaceProvider


class GreetInput(BaseModel):
    name: str = Field(min_length=1)


class GreetOutput(BaseModel):
    message: str = Field(min_length=1)


GREET_CONTRACT = TaskAttemptContract(
    contract_id="toy.a.greet",
    owner_id="toy.a",
    handler_id="toy.a.greet",
    input_model=GreetInput,
    output_model=GreetOutput,
    resources=ResourceClaims(writes=("greeting.txt",)),
    retry=AttemptRetryPolicy(max_attempts=2),
    timeout=AttemptTimeoutPolicy(seconds=5),
    validators=(),
)
GREET_CONTRACT_REF = AttemptContractRef(
    contract_id=GREET_CONTRACT.contract_id,
    digest=canonical_digest(GREET_CONTRACT.canonical_projection()),
)


def _attempt_key(scope: AuthorizedAttemptScope) -> AttemptKey:
    return scope.execution.attempt_key


class GreetExecutor:
    def __init__(self, workspace: WorkspaceProvider, *, fail_first: bool = False) -> None:
        self._workspace = workspace
        self._fail_first = fail_first
        self.executions = 0

    async def execute(
        self,
        validated_input: GreetInput,
        scope: AuthorizedAttemptScope,
    ) -> ExecutedAttemptResult[GreetOutput] | PermanentTaskFailure:
        self.executions += 1
        if self._fail_first and self.executions == 1:
            return PermanentTaskFailure(kind="transient", message="retry the toy greeting")
        binding = await self._workspace.open_or_create(
            _attempt_key(scope),
            GREET_CONTRACT.resources,
        )
        message = f"hello {validated_input.name}"
        (binding.write_root / "greeting.txt").write_text(f"{message}\n", encoding="utf-8")
        return ExecutedAttemptResult(output=GreetOutput(message=message))


def greet_contract_ref() -> AttemptContractRef:
    return GREET_CONTRACT_REF


def bind_greet_executor(
    workspace: WorkspaceProvider,
    *,
    fail_first: bool = False,
) -> ResolvedAttemptContract[GreetInput, GreetOutput]:
    return resolve_contract(GREET_CONTRACT, executor=GreetExecutor(workspace, fail_first=fail_first))
