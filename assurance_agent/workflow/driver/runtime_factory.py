"""Shared GraphRuntime construction for CLI, detached launch, eval, and tests.

``assemble_graph_runtime`` is the single Runtime assembly composition root.
Production ``build_graph_runtime`` loads/compiles the schema then calls it;
tests pass an inline ``CompiledWorkflow`` into the same function. The only
substitution point for node execution is ``build_node_runner`` (or the
default path via ``adapter`` + ``operations``).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from assurance_agent.change_location import resolve_change
from assurance_agent.config import load_config
from assurance_agent.workflow.driver.operations_catalog import default_operations
from assurance_agent.workflow.graph.agent_api import AgentInvoker
from assurance_agent.workflow.graph.checkpoint import CheckpointStore
from assurance_agent.workflow.graph.compiler import compile_workflow
from assurance_agent.workflow.graph.contracts import ExecutionContractCatalog, load_execution_contracts
from assurance_agent.workflow.graph.leases import Clock, SystemClock
from assurance_agent.workflow.graph.model_routing import ModelRouter
from assurance_agent.workflow.graph.models import CompiledWorkflow, ExecutableTask, RuntimeContext, TaskResult
from assurance_agent.workflow.graph.project_locks import ProjectResourceLockManager
from assurance_agent.workflow.graph.runtime import GraphDefinitionChanged, GraphRuntime
from assurance_agent.workflow.graph.scheduler import Scheduler
from assurance_agent.workflow.graph.schema_v2 import load_workflow_v2
from assurance_agent.workflow.graph.task_runner import NodeRunner, build_default_node_runner
from assurance_agent.workflow.graph.workspace import TaskWorkspace, TreeStore, WorkspaceBackend

RunChildFn = Callable[[ExecutableTask, str, TaskWorkspace, RuntimeContext], TaskResult]
NodeRunnerBuilder = Callable[[TreeStore, RunChildFn], NodeRunner]
OperationFn = Callable[[ExecutableTask, TaskWorkspace, RuntimeContext], TaskResult]


@dataclass(frozen=True)
class RuntimeBundle:
    runtime: GraphRuntime
    compiled: CompiledWorkflow


def runtime_context_for(
    project_root: Path,
    change_id: str,
    params: dict[str, object],
    parent_session_id: str | None = None,
) -> RuntimeContext:
    loc = resolve_change(project_root, change_id)
    return RuntimeContext(
        project_root=project_root,
        repo_root=project_root,
        change_dir=loc.path,
        change_id=change_id,
        params=params,
        parent_session_id=parent_session_id,
    )


def assemble_graph_runtime(
    *,
    project_root: Path,
    change_dir: Path,
    compiled: CompiledWorkflow,
    contracts: ExecutionContractCatalog,
    adapter: AgentInvoker | None = None,
    operations: Mapping[str, OperationFn] | None = None,
    build_node_runner: NodeRunnerBuilder | None = None,
    clock: Clock | None = None,
    object_store: TreeStore | None = None,
    model_router: ModelRouter | None = None,
    adapter_name: str | None = None,
    cli_model_override: str | None = None,
) -> GraphRuntime:
    """Wire TreeStore / CheckpointStore / NodeRunner / Scheduler / GraphRuntime.

    Provide either ``build_node_runner`` (escape hatch) or ``adapter`` (default
    path, with ``operations`` defaulting to the driver catalog). ``run_child``
    for subgraph dispatch is always closed over the assembled runtime.
    """
    if build_node_runner is None and adapter is None:
        raise ValueError("assemble_graph_runtime requires adapter or build_node_runner")

    runtime_clock = clock or SystemClock()
    store = object_store if object_store is not None else TreeStore(change_dir)
    checkpoints = CheckpointStore(change_dir)
    workspaces = WorkspaceBackend(change_dir)
    project_locks = ProjectResourceLockManager(project_root, clock=runtime_clock)
    holder: dict[str, GraphRuntime] = {}

    def run_child(
        task: ExecutableTask,
        graph_id: str,
        workspace: TaskWorkspace,
        context: RuntimeContext,
    ) -> TaskResult:
        return holder["rt"].run_child(task, graph_id, workspace, context)

    if build_node_runner is not None:
        runner = build_node_runner(store, run_child)
    else:
        assert adapter is not None  # narrowed above
        runner = build_default_node_runner(
            adapter,
            store,
            contracts,
            compiled=compiled,
            operations=dict(operations) if operations is not None else default_operations(),
            run_child=run_child,
            model_router=model_router,
            adapter_name=adapter_name,
            cli_model_override=cli_model_override,
        )

    state_defs: dict = {}
    for graph in compiled.schema.graphs.values():
        state_defs.update(dict(graph.state))

    scheduler = Scheduler(
        checkpoints=checkpoints,
        object_store=store,
        clock=runtime_clock,
        workspace_backend=workspaces,
        node_runner=runner,
        max_parallel_tasks=compiled.schema.policies.scheduler.max_parallel_tasks,
        contracts=contracts,
        state_defs=state_defs,
        project_lock_manager=project_locks,
    )

    def resolve_pinned(digest: str) -> CompiledWorkflow:
        if digest != compiled.digest:
            raise GraphDefinitionChanged(f"requested graph digest {digest} does not match {compiled.digest}")
        return compiled

    runtime = GraphRuntime(
        checkpoint_store=checkpoints,
        object_store=store,
        workspace_backend=workspaces,
        contracts=contracts,
        node_runner=runner,
        scheduler=scheduler,
        schema_resolver=resolve_pinned,
        clock=runtime_clock,
    )
    holder["rt"] = runtime
    return runtime


def build_graph_runtime(
    *,
    project_root: Path,
    change_id: str,
    adapter: AgentInvoker,
    explicit_schema: Path | None = None,
    clock: Clock | None = None,
    adapter_name: str | None = None,
    cli_model_override: str | None = None,
) -> RuntimeBundle:
    loc = resolve_change(project_root, change_id)
    schema = load_workflow_v2(project_root, explicit_schema)
    contracts = load_execution_contracts(project_root)
    compiled = compile_workflow(schema, contracts)

    model_router: ModelRouter | None = None
    if adapter_name == "opencode":
        config = load_config(project_root)
        model_router = ModelRouter(config.execution.model_routing)
        model_router.validate_compiled(
            compiled,
            adapter=adapter_name,
            cli_override=cli_model_override,
        )

    runtime = assemble_graph_runtime(
        project_root=project_root,
        change_dir=loc.path,
        compiled=compiled,
        contracts=contracts,
        adapter=adapter,
        clock=clock,
        model_router=model_router,
        adapter_name=adapter_name,
        cli_model_override=cli_model_override,
    )
    return RuntimeBundle(runtime=runtime, compiled=compiled)
