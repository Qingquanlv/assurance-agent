"""Shared GraphRuntime construction for CLI, detached launch, eval, and tests."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from assurance_agent.change_location import resolve_change
from assurance_agent.workflow.graph.agent_api import AgentInvoker
from assurance_agent.workflow.graph.checkpoint import CheckpointStore
from assurance_agent.workflow.graph.compiler import (
    PinnedDefinitionRequest,
    compile_packaged_workflow,
    compile_workflow,
)
from assurance_agent.workflow.graph.contracts import ExecutionContractCatalog, load_execution_contracts
from assurance_agent.workflow.graph.definition_pinning import (
    load_pinned_execution_definition,
    request_for_compiled,
)
from assurance_agent.workflow.graph.ingest_catalog import (
    IngestArtifactCatalog,
    model_schema_digest,
    resolve_model,
    validate_catalog_runtime,
)
from assurance_agent.workflow.graph.leases import Clock, SystemClock
from assurance_agent.workflow.graph.models import CompiledWorkflow, RuntimeContext
from assurance_agent.workflow.graph.project_locks import ProjectResourceLockManager
from assurance_agent.workflow.graph.runtime import (
    GraphDefinitionChanged,
    GraphRuntime,
    assert_live_semantic_compatibility,
)
from assurance_agent.workflow.graph.scheduler import Scheduler
from assurance_agent.workflow.graph.schema_v2 import load_workflow_v2_with_origin
from assurance_agent.workflow.graph.task_runner import NodeRunner, build_default_node_runner
from assurance_agent.workflow.graph.workspace import TreeStore, WorkspaceBackend


@dataclass(frozen=True, slots=True)
class ResolvedExecutionBundle:
    request: PinnedDefinitionRequest
    compiled: CompiledWorkflow
    contracts: ExecutionContractCatalog
    ingest_catalog: IngestArtifactCatalog
    model_map: Mapping[str, type[BaseModel]]
    node_runner: NodeRunner
    scheduler: Scheduler


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
    )


def request_from_projection(projection: Any) -> PinnedDefinitionRequest:
    return PinnedDefinitionRequest(
        graph_digest=projection.graph_digest,
        ingest_catalog_digest=projection.ingest_catalog_digest,
        contract_digests=tuple(sorted(projection.contract_digests.items())),
        event_schema_version=projection.event_schema_version,
        gate_semantics_digest=projection.gate_semantics_digest,
        assurance_profile_digest=projection.assurance_profile_digest,
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
    event_schema_version: int = 5,
    enforce_live_semantics: bool = True,
) -> Callable[[PinnedDefinitionRequest], ResolvedExecutionBundle]:
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

    def resolve(req: PinnedDefinitionRequest) -> ResolvedExecutionBundle:
        if enforce_live_semantics:
            assert_live_semantic_compatibility(req)
        hit = by_graph.get(req.graph_digest)
        if hit is None:
            raise GraphDefinitionChanged(f"unknown graph digest {req.graph_digest}")
        if req.ingest_catalog_digest and req.ingest_catalog_digest != hit.compiled.ingest_catalog_digest:
            raise GraphDefinitionChanged("graph_definition_changed: ingest catalog digest drifted")
        if dict(req.contract_digests) and dict(req.contract_digests) != hit.compiled.contract_digests:
            raise GraphDefinitionChanged("graph_definition_changed: contract digests drifted")
        return hit

    return resolve


def build_graph_runtime(
    *,
    project_root: Path,
    change_id: str,
    adapter: AgentInvoker,
    explicit_schema: Path | None = None,
    clock: Clock | None = None,
) -> RuntimeBundle:
    loc = resolve_change(project_root, change_id)
    loaded = load_workflow_v2_with_origin(project_root, explicit_schema)
    contracts = load_execution_contracts(project_root)
    if loaded.origin == "packaged":
        compiled = compile_packaged_workflow(loaded.schema, contracts)
    else:
        compiled = compile_workflow(loaded.schema, contracts)
    runtime_clock = clock or SystemClock()
    ingest_catalog = validate_catalog_runtime()
    model_map = validate_ingest_model_map(ingest_catalog)

    object_store = TreeStore(loc.path)
    checkpoints = CheckpointStore(loc.path)
    workspaces = WorkspaceBackend(loc.path)
    project_locks = ProjectResourceLockManager(project_root, clock=runtime_clock)
    holder: dict[str, GraphRuntime] = {}
    cache: dict[PinnedDefinitionRequest, ResolvedExecutionBundle] = {}

    def run_child(task, graph_id, workspace, context):  # noqa: ANN001, ANN202
        return holder["rt"].run_child(task, graph_id, workspace, context)

    def _services_for(
        resolved_compiled: CompiledWorkflow,
        resolved_contracts: ExecutionContractCatalog,
        resolved_catalog: IngestArtifactCatalog,
        resolved_models: Mapping[str, type[BaseModel]],
    ) -> tuple[NodeRunner, Scheduler]:
        runner = build_default_node_runner(
            adapter,
            object_store,
            resolved_contracts,
            compiled=resolved_compiled,
            run_child=run_child,
            ingest_catalog=resolved_catalog,
            model_map=resolved_models,
        )
        state_defs: dict = {}
        for graph in resolved_compiled.schema.graphs.values():
            state_defs.update(dict(graph.state))
        scheduler = Scheduler(
            checkpoints=checkpoints,
            object_store=object_store,
            clock=runtime_clock,
            workspace_backend=workspaces,
            node_runner=runner,
            max_parallel_tasks=resolved_compiled.schema.policies.scheduler.max_parallel_tasks,
            contracts=resolved_contracts,
            state_defs=state_defs,
            project_lock_manager=project_locks,
        )
        return runner, scheduler

    current_runner, current_scheduler = _services_for(compiled, contracts, ingest_catalog, model_map)
    current_request = request_for_compiled(compiled, event_schema_version=5)
    current_bundle = ResolvedExecutionBundle(
        request=current_request,
        compiled=compiled,
        contracts=contracts,
        ingest_catalog=ingest_catalog,
        model_map=model_map,
        node_runner=current_runner,
        scheduler=current_scheduler,
    )
    cache[current_request] = current_bundle

    def resolve_definition(request: PinnedDefinitionRequest) -> ResolvedExecutionBundle:
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
                ingest_catalog=ingest_catalog,
                model_map=model_map,
                node_runner=current_runner,
                scheduler=current_scheduler,
            )
            cache[request] = bundle
            return bundle

        pinned = load_pinned_execution_definition(loc.path, request)
        pinned_models = validate_ingest_model_map(pinned.ingest_catalog)
        runner, scheduler = _services_for(
            pinned.compiled, pinned.contracts, pinned.ingest_catalog, pinned_models
        )
        bundle = ResolvedExecutionBundle(
            request=request,
            compiled=pinned.compiled,
            contracts=pinned.contracts,
            ingest_catalog=pinned.ingest_catalog,
            model_map=pinned_models,
            node_runner=runner,
            scheduler=scheduler,
        )
        cache[request] = bundle
        return bundle

    runtime = GraphRuntime(
        checkpoint_store=checkpoints,
        object_store=object_store,
        workspace_backend=workspaces,
        definition_resolver=resolve_definition,
        clock=runtime_clock,
    )
    holder["rt"] = runtime
    return RuntimeBundle(runtime=runtime, compiled=compiled, resolved=current_bundle)
