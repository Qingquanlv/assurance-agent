"""Shared GraphRuntime construction for CLI, detached launch, eval, and tests."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from assurance_agent.change_location import resolve_change
from assurance_agent.workflow.graph.agent_api import AgentInvoker
from assurance_agent.workflow.graph.checkpoint import CheckpointStore
from assurance_agent.workflow.graph.compiler import compile_workflow
from assurance_agent.workflow.graph.contracts import load_execution_contracts
from assurance_agent.workflow.graph.leases import Clock, SystemClock
from assurance_agent.workflow.graph.models import CompiledWorkflow, RuntimeContext
from assurance_agent.workflow.graph.runtime import GraphDefinitionChanged, GraphRuntime
from assurance_agent.workflow.graph.scheduler import Scheduler
from assurance_agent.workflow.graph.schema_v2 import load_workflow_v2
from assurance_agent.workflow.graph.task_runner import build_default_node_runner
from assurance_agent.workflow.graph.workspace import TreeStore, WorkspaceBackend


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


def build_graph_runtime(
    *,
    project_root: Path,
    change_id: str,
    adapter: AgentInvoker,
    explicit_schema: Path | None = None,
    clock: Clock | None = None,
) -> RuntimeBundle:
    loc = resolve_change(project_root, change_id)
    schema = load_workflow_v2(project_root, explicit_schema)
    contracts = load_execution_contracts(project_root)
    compiled = compile_workflow(schema, contracts)
    runtime_clock = clock or SystemClock()

    def resolve_pinned(digest: str) -> CompiledWorkflow:
        if digest != compiled.digest:
            raise GraphDefinitionChanged(
                f"requested graph digest {digest} does not match {compiled.digest}"
            )
        return compiled

    object_store = TreeStore(loc.path)
    checkpoints = CheckpointStore(loc.path)
    workspaces = WorkspaceBackend(loc.path)
    holder: dict[str, GraphRuntime] = {}

    def run_child(task, graph_id, workspace, context):  # noqa: ANN001, ANN202
        return holder["rt"].run_child(task, graph_id, workspace, context)

    runner = build_default_node_runner(
        adapter,
        object_store,
        contracts,
        compiled=compiled,
        run_child=run_child,
    )
    state_defs: dict = {}
    for graph in compiled.schema.graphs.values():
        state_defs.update(dict(graph.state))
    scheduler = Scheduler(
        checkpoints=checkpoints,
        object_store=object_store,
        clock=runtime_clock,
        workspace_backend=workspaces,
        node_runner=runner,
        max_parallel_tasks=compiled.schema.policies.scheduler.max_parallel_tasks,
        contracts=contracts,
        state_defs=state_defs,
    )
    runtime = GraphRuntime(
        checkpoint_store=checkpoints,
        object_store=object_store,
        workspace_backend=workspaces,
        contracts=contracts,
        node_runner=runner,
        scheduler=scheduler,
        schema_resolver=resolve_pinned,
        clock=runtime_clock,
    )
    holder["rt"] = runtime
    return RuntimeBundle(runtime=runtime, compiled=compiled)
