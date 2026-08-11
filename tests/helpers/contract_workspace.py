"""Run an operation under its packaged execution-contract write authorization.

Unlike bare unit fixtures that hand a fake workspace to domain callables, this
helper materializes a real ``TaskWorkspace``, executes the operation, then
``freeze_write_set`` against the target's ``authorization_writes``. Undeclared
writes raise ``WorkspaceError`` (the same fail-closed path as production
``forbidden_write``), so contract drift fails in CI instead of a live run.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from assurance_agent import resources
from assurance_agent.workflow.graph.contracts import (
    ExecutionContract,
    ResourceClaims,
    ResourcePath,
    parse_execution_contracts,
)
from assurance_agent.workflow.graph.handlers.operation import OperationFn
from assurance_agent.workflow.graph.models import ExecutableTask, RuntimeContext, TaskResult
from assurance_agent.workflow.graph.workspace import (
    TaskWorkspace,
    TreeStore,
    WorkspaceBackend,
    WorkspaceError,
    WriteSet,
)

_PACKAGED_CONTRACTS = parse_execution_contracts(resources.read_text("schemas", "execution-contracts.yaml"))


@dataclass(frozen=True)
class ContractRunResult:
    result: TaskResult
    workspace: TaskWorkspace
    write_set: WriteSet
    claims: ResourceClaims


def claims_for_target(target: str) -> ResourceClaims:
    """Static resource claims for a packaged ``operation:*`` / ``skill:*`` target."""
    contract = _PACKAGED_CONTRACTS.contracts.get(target)
    if contract is None:
        raise KeyError(f"no packaged execution contract for {target}")
    return _claims_from_contract(contract)


def _claims_from_contract(contract: ExecutionContract) -> ResourceClaims:
    return ResourceClaims(
        reads=tuple(ResourcePath.parse(v) for v in contract.reads),
        writes=tuple(ResourcePath.parse(v) for v in contract.writes),
        synchronized=tuple(ResourcePath.parse(v) for v in contract.synchronized),
        exclusive=contract.exclusive,
        authorization_writes=tuple(ResourcePath.parse(v) for v in contract.authorization_writes),
    )


def run_operation_under_contract(
    target: str,
    *,
    project_root: Path,
    change_id: str,
    operation: OperationFn | None = None,
    task: ExecutableTask | None = None,
    context: RuntimeContext | None = None,
    params: Mapping[str, object] | None = None,
    outputs: tuple[str, ...] = (),
) -> ContractRunResult:
    """Execute ``operation`` (or catalog entry for ``target``) then freeze under contract.

    ``project_root`` must already contain ``qa/changes/<change_id>/`` with any
    inputs the operation needs. The base tree is captured before execution so
    only post-operation diffs are authorized.
    """
    from assurance_agent.workflow.driver.operations_catalog import default_operations

    change_dir = project_root / "qa" / "changes" / change_id
    if not change_dir.is_dir():
        raise FileNotFoundError(f"change dir missing: {change_dir}")

    claims = claims_for_target(target)
    fn = operation if operation is not None else default_operations()[target]
    store = TreeStore(change_dir)
    base_tree_id = store.capture(project_root)
    workspace = WorkspaceBackend(change_dir).create(
        task_id=f"contract-{target.replace(':', '-')}",
        base_tree_id=base_tree_id,
        store=store,
        claims=claims,
    )
    runtime = context or RuntimeContext(
        project_root=project_root,
        repo_root=project_root,
        change_dir=change_dir,
        change_id=change_id,
        params=dict(params or {}),
    )
    executable = task or ExecutableTask.model_construct(
        task_id=workspace.task_id,
        node_id=target.removeprefix("operation:"),
        graph_id="contract-check",
        target=target,
        input={"with": {}, "context": {"change_id": change_id}},
    )
    result = fn(executable, workspace, runtime)
    write_set = store.freeze_write_set(workspace, claims=claims, outputs=outputs)
    return ContractRunResult(
        result=result,
        workspace=workspace,
        write_set=write_set,
        claims=claims,
    )


__all__ = [
    "ContractRunResult",
    "WorkspaceError",
    "claims_for_target",
    "run_operation_under_contract",
]
