from __future__ import annotations

from pydantic import BaseModel

from graph_engine.attempts.context import AttemptExecutionContext, AuthorizedAttemptScope
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


class EmptyInput(BaseModel):
    pass


class SeedOutput(BaseModel):
    route: str = "both"


class LeftOutput(BaseModel):
    left: bool = True


class ChildOutput(BaseModel):
    child: bool = True


class CombineOutput(BaseModel):
    combined: bool = True


def _contract(
    contract_id: str,
    *,
    output_model: type[BaseModel],
    writes: tuple[str, ...] = (),
    max_attempts: int,
) -> TaskAttemptContract[EmptyInput, BaseModel]:
    return TaskAttemptContract(
        contract_id=contract_id,
        owner_id="toy.b",
        handler_id=contract_id,
        input_model=EmptyInput,
        output_model=output_model,
        resources=ResourceClaims(writes=writes),
        retry=AttemptRetryPolicy(max_attempts=max_attempts),
        timeout=AttemptTimeoutPolicy(seconds=5),
        validators=(),
    )


SEED_CONTRACT = _contract("toy.b.seed", output_model=SeedOutput, max_attempts=1)
LEFT_CONTRACT = _contract("toy.b.left", output_model=LeftOutput, writes=("left.txt",), max_attempts=2)
CHILD_CONTRACT = _contract("toy.b.child", output_model=ChildOutput, writes=("child.txt",), max_attempts=1)
COMBINE_CONTRACT = _contract("toy.b.combine", output_model=CombineOutput, max_attempts=1)
CONTRACTS = tuple(
    sorted(
        (SEED_CONTRACT, LEFT_CONTRACT, CHILD_CONTRACT, COMBINE_CONTRACT),
        key=lambda item: item.contract_id,
    )
)
CONTRACT_REFS = tuple(
    AttemptContractRef(contract_id=item.contract_id, digest=canonical_digest(item.canonical_projection()))
    for item in CONTRACTS
)


def _attempt_key(context: AttemptExecutionContext | AuthorizedAttemptScope) -> AttemptKey:
    execution = context.execution if isinstance(context, AuthorizedAttemptScope) else context
    return execution.attempt_key


class SeedExecutor:
    def __init__(self, workspace: WorkspaceProvider) -> None:
        self._workspace = workspace

    async def execute(
        self, validated_input: EmptyInput, context: AttemptExecutionContext | AuthorizedAttemptScope
    ) -> ExecutedAttemptResult[SeedOutput]:
        del validated_input
        await self._workspace.open_or_create(_attempt_key(context), SEED_CONTRACT.resources)
        return ExecutedAttemptResult(output=SeedOutput())


class LeftExecutor:
    def __init__(self, workspace: WorkspaceProvider) -> None:
        self._workspace = workspace
        self.executions = 0

    async def execute(
        self,
        validated_input: EmptyInput,
        context: AttemptExecutionContext | AuthorizedAttemptScope,
    ) -> ExecutedAttemptResult[LeftOutput] | PermanentTaskFailure:
        del validated_input
        self.executions += 1
        if self.executions == 1:
            return PermanentTaskFailure(kind="transient", message="retry the left branch")
        binding = await self._workspace.open_or_create(_attempt_key(context), LEFT_CONTRACT.resources)
        (binding.write_root / "left.txt").write_text("left\n", encoding="utf-8")
        return ExecutedAttemptResult(output=LeftOutput())


class ChildExecutor:
    def __init__(self, workspace: WorkspaceProvider) -> None:
        self._workspace = workspace

    async def execute(
        self, validated_input: EmptyInput, context: AttemptExecutionContext | AuthorizedAttemptScope
    ) -> ExecutedAttemptResult[ChildOutput]:
        del validated_input
        binding = await self._workspace.open_or_create(_attempt_key(context), CHILD_CONTRACT.resources)
        (binding.write_root / "child.txt").write_text("child\n", encoding="utf-8")
        return ExecutedAttemptResult(output=ChildOutput())


class CombineExecutor:
    def __init__(self, workspace: WorkspaceProvider) -> None:
        self._workspace = workspace

    async def execute(
        self, validated_input: EmptyInput, context: AttemptExecutionContext | AuthorizedAttemptScope
    ) -> ExecutedAttemptResult[CombineOutput]:
        del validated_input
        await self._workspace.open_or_create(_attempt_key(context), COMBINE_CONTRACT.resources)
        return ExecutedAttemptResult(output=CombineOutput())


_EXECUTORS = {
    SEED_CONTRACT.contract_id: SeedExecutor,
    LEFT_CONTRACT.contract_id: LeftExecutor,
    CHILD_CONTRACT.contract_id: ChildExecutor,
    COMBINE_CONTRACT.contract_id: CombineExecutor,
}


def bind_executors(workspace: WorkspaceProvider) -> dict[str, ResolvedAttemptContract[EmptyInput, BaseModel]]:
    by_id = {item.contract_id: item for item in CONTRACTS}
    return {
        contract_id: resolve_contract(by_id[contract_id], executor=executor_cls(workspace))
        for contract_id, executor_cls in _EXECUTORS.items()
    }
