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
from typing import Any, Protocol

from pydantic import BaseModel

from assurance_kernel.change_location import resolve_change
from assurance_kernel.config import load_config
from assurance_kernel.workflow.driver.capability_catalog import (
    current_operations,
    ensure_default_catalog,
)
from assurance_kernel.workflow.graph.capability_state import CapabilityCatalogError
from assurance_kernel.workflow.graph.agent_api import AgentInvoker
from assurance_kernel.workflow.graph.checkpoint import CheckpointStore
from assurance_kernel.workflow.graph.compiler import (
    PinnedDefinitionRequest,
    compile_loaded_workflow,
)
from assurance_kernel.workflow.graph.contracts import ExecutionContractCatalog, load_execution_contracts
from assurance_kernel.workflow.graph.definition_pinning import request_for_compiled
from assurance_kernel.workflow.graph.ingest_catalog import (
    IngestArtifactCatalog,
    model_schema_digest,
    resolve_model,
    validate_catalog_runtime,
)
from assurance_kernel.workflow.graph.leases import Clock, SystemClock
from assurance_kernel.workflow.graph.model_routing import ModelRouter
from assurance_kernel.workflow.graph.models import (
    CompiledWorkflow,
    ExecutableTask,
    RuntimeContext,
    TaskResult,
)
from assurance_kernel.workflow.graph.project_locks import ProjectResourceLockManager
from assurance_kernel.workflow.graph.runtime import (
    GraphDefinitionChanged,
    GraphRuntime,
    assert_live_semantic_compatibility,
)
from assurance_kernel.workflow.graph.scheduler import Scheduler
from assurance_kernel.workflow.graph.schema_v2 import load_workflow_v2_with_origin
from assurance_kernel.workflow.graph.task_runner import NodeRunner, build_default_node_runner
from assurance_kernel.workflow.graph.workspace import TaskWorkspace, TreeStore, WorkspaceBackend

RunChildFn = Callable[[ExecutableTask, str, TaskWorkspace, RuntimeContext], TaskResult]
NodeRunnerBuilder = Callable[[TreeStore, RunChildFn], NodeRunner]
OperationFn = Callable[[ExecutableTask, TaskWorkspace, RuntimeContext], TaskResult]


@dataclass(frozen=True, slots=True)
class ResolvedExecutionBundle:
    request: PinnedDefinitionRequest
    compiled: CompiledWorkflow
    contracts: ExecutionContractCatalog
    ingest_catalog: IngestArtifactCatalog
    model_map: Mapping[str, type[BaseModel]]
    node_runner: NodeRunner
    scheduler: Scheduler


class DefinitionResolver(Protocol):
    """Keyword-capable resolver; avoids Callable[[T], R] positional-only mismatch."""

    def __call__(self, request: PinnedDefinitionRequest) -> ResolvedExecutionBundle: ...


@dataclass(frozen=True, slots=True)
class RuntimeBundle:
    runtime: GraphRuntime
    compiled: CompiledWorkflow
    resolved: ResolvedExecutionBundle | None = None


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
        host_project_root=project_root,
    )


def request_from_projection(projection: Any) -> PinnedDefinitionRequest:
    return PinnedDefinitionRequest(
        graph_digest=projection.graph_digest,
        ingest_catalog_digest=projection.ingest_catalog_digest,
        contract_digests=tuple(sorted(projection.contract_digests.items())),
        event_schema_version=projection.event_schema_version,
        gate_semantics_digest=projection.gate_semantics_digest,
        assurance_profile_digest=projection.assurance_profile_digest,
        gate_semantics_object_id=getattr(projection, "gate_semantics_object_id", ""),
        topology_safety_semantics_object_id=getattr(projection, "topology_safety_semantics_object_id", ""),
        topology_safety_semantics_digest=getattr(projection, "topology_safety_semantics_digest", ""),
        commit_safety_semantics_object_id=getattr(projection, "commit_safety_semantics_object_id", ""),
        commit_safety_semantics_digest=getattr(projection, "commit_safety_semantics_digest", ""),
    )


def validate_ingest_model_map(catalog: IngestArtifactCatalog) -> dict[str, type[BaseModel]]:
    """Validate every executable model against the catalog's pinned schema digests."""
    model_map: dict[str, type[BaseModel]] = {}
    for symbol, spec in catalog.artifacts.items():
        if spec.kind != "file_ingest" or not spec.model:
            continue
        model = resolve_model(spec.model)
        computed = model_schema_digest(spec.model)
        if spec.model_schema_digest and spec.model_schema_digest != computed:
            raise GraphDefinitionChanged(
                f"ingest model schema digest mismatch for {symbol!r} "
                f"(model {spec.model!r}): pinned {spec.model_schema_digest!r} != {computed!r}"
            )
        model_map[spec.model] = model
    return model_map


def one_definition_resolver(
    *,
    compiled: CompiledWorkflow,
    contracts: ExecutionContractCatalog,
    ingest_catalog: IngestArtifactCatalog,
    node_runner: NodeRunner,
    scheduler: Scheduler,
    event_schema_version: int = 6,
    enforce_live_semantics: bool = True,
) -> DefinitionResolver:
    """Synthetic resolver that serves exactly one prebuilt execution bundle."""
    model_map = validate_ingest_model_map(ingest_catalog)
    request = request_for_compiled(compiled, event_schema_version=event_schema_version)
    bundle = ResolvedExecutionBundle(
        request=request,
        compiled=compiled,
        contracts=contracts,
        ingest_catalog=ingest_catalog,
        model_map=model_map,
        node_runner=node_runner,
        scheduler=scheduler,
    )
    by_graph = {compiled.digest: bundle}

    def resolve(request: PinnedDefinitionRequest) -> ResolvedExecutionBundle:
        if enforce_live_semantics:
            assert_live_semantic_compatibility(request)
        hit = by_graph.get(request.graph_digest)
        if hit is None:
            raise GraphDefinitionChanged(f"unknown graph digest {request.graph_digest}")
        if (
            request.ingest_catalog_digest
            and request.ingest_catalog_digest != hit.compiled.ingest_catalog_digest
        ):
            raise GraphDefinitionChanged("graph_definition_changed: ingest catalog digest drifted")
        if dict(request.contract_digests) and dict(request.contract_digests) != hit.compiled.contract_digests:
            raise GraphDefinitionChanged("graph_definition_changed: contract digests drifted")
        return hit

    return resolve


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
    ingest_catalog: IngestArtifactCatalog | None = None,
    model_map: Mapping[str, type[BaseModel]] | None = None,
) -> GraphRuntime:
    """Wire TreeStore / CheckpointStore / NodeRunner / Scheduler / GraphRuntime.

    Provide either ``build_node_runner`` (escape hatch) or ``adapter`` (default
    path, with ``operations`` defaulting to the driver catalog). Operation
    catalog membership is enforced for every assembled graph, including custom
    runners. ``run_child`` for subgraph dispatch is always closed over the
    assembled runtime.
    """
    if build_node_runner is None and adapter is None:
        raise ValueError("assemble_graph_runtime requires adapter or build_node_runner")

    ensure_default_catalog()
    runtime_clock = clock or SystemClock()
    store = object_store if object_store is not None else TreeStore(change_dir)
    checkpoints = CheckpointStore(change_dir)
    workspaces = WorkspaceBackend(change_dir)
    project_locks = ProjectResourceLockManager(project_root, clock=runtime_clock)
    resolved_catalog = ingest_catalog if ingest_catalog is not None else validate_catalog_runtime()
    resolved_models = (
        dict(model_map) if model_map is not None else validate_ingest_model_map(resolved_catalog)
    )
    ops = dict(operations) if operations is not None else current_operations()
    missing = sorted(
        {
            node.uses
            for graph in compiled.schema.graphs.values()
            for node in graph.nodes.values()
            if node.uses.startswith("operation:") and node.uses not in ops
        }
    )
    if missing:
        raise CapabilityCatalogError("unregistered operations: " + ", ".join(missing))
    holder: dict[str, GraphRuntime] = {}

    def run_child(
        task: ExecutableTask,
        graph_id: str,
        workspace: TaskWorkspace,
        context: RuntimeContext,
    ) -> TaskResult:
        return holder["rt"].run_child(task, graph_id, workspace, context)

    def _services_for(
        resolved_compiled: CompiledWorkflow,
        resolved_contracts: ExecutionContractCatalog,
        resolved_ingest: IngestArtifactCatalog,
        resolved_model_map: Mapping[str, type[BaseModel]],
    ) -> tuple[NodeRunner, Scheduler]:
        if build_node_runner is not None:
            runner = build_node_runner(store, run_child)
        else:
            assert adapter is not None  # narrowed above
            runner = build_default_node_runner(
                adapter,
                store,
                resolved_contracts,
                compiled=resolved_compiled,
                operations=ops,
                run_child=run_child,
                model_router=model_router,
                adapter_name=adapter_name,
                cli_model_override=cli_model_override,
                ingest_catalog=resolved_ingest,
                model_map=resolved_model_map,
            )
        state_defs: dict = {}
        for graph in resolved_compiled.schema.graphs.values():
            state_defs.update(dict(graph.state))
        scheduler = Scheduler(
            checkpoints=checkpoints,
            object_store=store,
            clock=runtime_clock,
            workspace_backend=workspaces,
            node_runner=runner,
            max_parallel_tasks=resolved_compiled.schema.policies.scheduler.max_parallel_tasks,
            contracts=resolved_contracts,
            state_defs=state_defs,
            project_lock_manager=project_locks,
        )
        return runner, scheduler

    current_runner, current_scheduler = _services_for(compiled, contracts, resolved_catalog, resolved_models)
    current_request = request_for_compiled(compiled, event_schema_version=6)
    current_bundle = ResolvedExecutionBundle(
        request=current_request,
        compiled=compiled,
        contracts=contracts,
        ingest_catalog=resolved_catalog,
        model_map=resolved_models,
        node_runner=current_runner,
        scheduler=current_scheduler,
    )
    cache: dict[PinnedDefinitionRequest, ResolvedExecutionBundle] = {current_request: current_bundle}

    if build_node_runner is not None:
        # Escape hatch: single prebuilt runner; no historical pin reload.
        resolve_definition: DefinitionResolver = one_definition_resolver(
            compiled=compiled,
            contracts=contracts,
            ingest_catalog=resolved_catalog,
            node_runner=current_runner,
            scheduler=current_scheduler,
        )
    else:

        def _resolve_definition(request: PinnedDefinitionRequest) -> ResolvedExecutionBundle:
            assert_live_semantic_compatibility(request)
            cached = cache.get(request)
            if cached is not None:
                return cached

            same_identities = (
                request.graph_digest == compiled.digest
                and request.ingest_catalog_digest == compiled.ingest_catalog_digest
                and dict(request.contract_digests) == compiled.contract_digests
            )
            if same_identities:
                bundle = ResolvedExecutionBundle(
                    request=request,
                    compiled=compiled,
                    contracts=contracts,
                    ingest_catalog=resolved_catalog,
                    model_map=resolved_models,
                    node_runner=current_runner,
                    scheduler=current_scheduler,
                )
                cache[request] = bundle
                return bundle

            raise GraphDefinitionChanged(
                "graph_definition_changed: live compiled identity does not match "
                "the invocation pin; start a new invocation"
            )

        resolve_definition = _resolve_definition

    runtime = GraphRuntime(
        checkpoint_store=checkpoints,
        object_store=store,
        workspace_backend=workspaces,
        definition_resolver=resolve_definition,
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
    explicit_contracts: Path | None = None,
    clock: Clock | None = None,
    adapter_name: str | None = None,
    cli_model_override: str | None = None,
) -> RuntimeBundle:
    loc = resolve_change(project_root, change_id)
    loaded = load_workflow_v2_with_origin(project_root, explicit_schema)
    contracts = load_execution_contracts(project_root, explicit_contracts)
    compiled = compile_loaded_workflow(loaded, contracts)

    model_router: ModelRouter | None = None
    if adapter_name == "opencode":
        config = load_config(project_root)
        model_router = ModelRouter(config.execution.model_routing)
        model_router.validate_compiled(
            compiled,
            adapter=adapter_name,
            cli_override=cli_model_override,
        )

    ingest_catalog = validate_catalog_runtime()
    model_map = validate_ingest_model_map(ingest_catalog)
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
        ingest_catalog=ingest_catalog,
        model_map=model_map,
    )
    # Expose the live definition bundle so test/runtime seams can reach the
    # scheduler/node_runner without rebuilding the graph (four-layer crash
    # injection, barrier runners, etc.).
    resolved = runtime._definition_resolver(  # noqa: SLF001 — composition-root seam
        request_for_compiled(compiled, event_schema_version=6)
    )
    return RuntimeBundle(runtime=runtime, compiled=compiled, resolved=resolved)
