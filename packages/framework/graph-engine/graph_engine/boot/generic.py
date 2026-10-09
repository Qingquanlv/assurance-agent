from __future__ import annotations

import importlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, cast

from langgraph.checkpoint.memory import InMemorySaver

from graph_engine.application import (
    AssuranceApplication,
    FixedExecutionFactory,
    InvocationBoundExecutionFactory,
)
from graph_engine.attempts.contracts import ResolvedAttemptContract, TaskAttemptContract
from graph_engine.attempts.kernel import AssuranceAttemptKernel
from graph_engine.attempts.resource_arbiter import ResourceArbiter
from graph_engine.boot.boot import BootRequest, GraphEngineBoot, RuntimePorts
from graph_engine.boot.graph_revision import BootArtifact, FeatureFactoryRef
from graph_engine.boot.source_authentication import (
    AuthenticatedFactory,
    AuthenticatedFactorySource,
    ProductFactoryRef,
    authenticate_factory_ref,
)
from graph_engine.canonical import JSONValue, canonical_digest
from graph_engine.composition.lock import ProductLock
from graph_engine.composition.models import FrozenComposition, SourceKey, SourceRole
from graph_engine.persistence.attempt_checkpoint import MemoryAttemptCheckpointStore
from graph_engine.persistence.resource_authorization import MemoryResourceAuthorizationStore
from graph_engine.persistence.runner_lease import LocalInvocationRunnerLease
from graph_engine.attempts.workspace import TaskWorkspaceProvider, TaskWorkspaceStore
from graph_engine.plugin_api import PluginDescriptor, ResourceClaims, WorkspaceProvider


class MemoryCheckpointer(InMemorySaver):
    backend_id = "memory"


@dataclass(frozen=True, slots=True)
class CatalogContractResolver:
    data: Mapping[str, TaskAttemptContract[Any, Any]]
    executors: Mapping[str, ResolvedAttemptContract[Any, Any]]

    def resolve_data_contracts(self) -> Mapping[str, TaskAttemptContract[Any, Any]]:
        return MappingProxyType(dict(self.data))

    def resolve_executors(self) -> Mapping[str, ResolvedAttemptContract[Any, Any]]:
        return MappingProxyType(dict(self.executors))


class _UnusedSecrets:
    def resolve(self, handle: str) -> bytes:
        raise RuntimeError(f"secret resolver is unused: {handle}")


class _FactoryAuthenticator:
    def authenticate(
        self,
        ref: FeatureFactoryRef | ProductFactoryRef,
        sources: Mapping[str, object],
    ) -> AuthenticatedFactory:
        return authenticate_factory_ref(ref, sources)


def factory_source_from_composition(composition: FrozenComposition) -> AuthenticatedFactorySource:
    manifest = composition.manifest
    if manifest.graph_factory_symbol is None or manifest.source is None:
        raise TypeError("composition is not a graph factory product")
    entry = composition.registries.sources.entries.get(SourceKey(SourceRole.PRODUCT, manifest.product_id))
    if entry is None:
        raise ValueError("composition has no product source snapshot")
    return AuthenticatedFactorySource(
        owner_id=manifest.product_id,
        snapshot=entry.snapshot,
        provider_source=manifest.source,
        source_files=tuple(item.path for item in entry.snapshot.files),
    )


def boot_factory_product(
    composition: FrozenComposition,
    *,
    workspace: WorkspaceProvider,
    contract_resolver: CatalogContractResolver,
    checkpointer: MemoryCheckpointer | None = None,
) -> tuple[BootArtifact, AssuranceAttemptKernel]:
    if not isinstance(composition.lock, ProductLock):
        raise TypeError("factory composition lock must be ProductLock v3")
    symbol = composition.manifest.graph_factory_symbol
    if symbol is None:
        raise TypeError("composition manifest has no graph factory symbol")
    checkpoints = MemoryAttemptCheckpointStore()
    kernel = AssuranceAttemptKernel(
        checkpoints=checkpoints,
        arbiter=ResourceArbiter(MemoryResourceAuthorizationStore()),
        workspace=workspace,
        graph_revision="0" * 64,
    )
    saver = MemoryCheckpointer() if checkpointer is None else checkpointer
    ports = RuntimePorts(
        attempt_kernel=kernel,
        secret_resolver=_UnusedSecrets(),
        workspace_provider=workspace,
    )
    boot = GraphEngineBoot(
        authenticator=_FactoryAuthenticator(),
        contract_resolver=contract_resolver,
    )
    artifact = boot.boot(
        BootRequest(
            product_factory=ProductFactoryRef(composition.manifest.product_id, symbol),
            feature_factories=(),
            sources={composition.manifest.product_id: factory_source_from_composition(composition)},
            product_lock=composition.lock,
        ),
        saver,
        ports,
    )
    object.__setattr__(kernel, "graph_revision", artifact.manifest.revision.revision_id)
    return artifact, kernel


def factory_application(
    artifact: BootArtifact,
    *,
    kernel: AssuranceAttemptKernel,
    workspace: WorkspaceProvider,
    lease_root: Any,
) -> tuple[AssuranceApplication, InvocationBoundExecutionFactory]:
    Path(lease_root).mkdir(parents=True, exist_ok=True)
    application = AssuranceApplication(
        lease=LocalInvocationRunnerLease(Path(lease_root)),
        owner_id="graph-engine-generic",
    )
    return application, FixedExecutionFactory(
        artifact=artifact,
        attempt_kernel=kernel,
        secret_resolver=_UnusedSecrets(),
        workspace_provider=workspace,
    )


async def invocation_values(
    artifact: BootArtifact, invocation_id: str, entrypoint: str
) -> Mapping[str, object]:
    graph = artifact.entrypoints[entrypoint]
    snapshot = await graph.aget_state({"configurable": {"thread_id": invocation_id}})
    values = getattr(snapshot, "values", {})
    if not isinstance(values, Mapping):
        return {}
    return values


async def run_factory_product(
    artifact: BootArtifact,
    *,
    kernel: AssuranceAttemptKernel,
    workspace: WorkspaceProvider,
    entrypoint: str,
    invocation_id: str,
    graph_input: Mapping[str, object],
    lease_root: Any,
) -> object:
    application, factory = factory_application(
        artifact, kernel=kernel, workspace=workspace, lease_root=lease_root
    )
    return await application.start_and_run(
        invocation_id=invocation_id,
        entrypoint=entrypoint,
        graph_input=cast(Mapping[str, JSONValue], dict(graph_input)),
        execution_factory=factory,
    )


def workspace_binding_roots(root: Path) -> tuple[Path, Path, Path]:
    project_root = root.parent / f".{root.name}-project"
    attempts_root = root.parent / f".{root.name}-attempts"
    receipts_root = root.parent / f".{root.name}-receipts"
    for path in (project_root, attempts_root, receipts_root):
        path.mkdir(exist_ok=True)
    return project_root, attempts_root, receipts_root


def workspace_provider_for(root: Path) -> tuple[TaskWorkspaceProvider, Path]:
    project_root, attempts_root, receipts_root = workspace_binding_roots(root)
    store = TaskWorkspaceStore(project_root, attempts_root, receipts_root)
    return TaskWorkspaceProvider(store), project_root


def load_plugin_class(descriptor: PluginDescriptor) -> type[Any] | None:
    source = descriptor.source
    if source is None or ":" not in source.entrypoint_value:
        return None
    module_name, attribute = source.entrypoint_value.split(":", 1)
    return getattr(importlib.import_module(module_name), attribute)


def contract_resolver_from_plugins(
    descriptors: Sequence[PluginDescriptor],
    workspace: WorkspaceProvider,
    *,
    fail_first: bool = False,
) -> CatalogContractResolver:
    data: dict[str, TaskAttemptContract[Any, Any]] = {}
    executors: dict[str, ResolvedAttemptContract[Any, Any]] = {}
    for descriptor in descriptors:
        plugin_cls = load_plugin_class(descriptor)
        if plugin_cls is None:
            continue
        published = getattr(plugin_cls, "published_attempt_contracts", None)
        bind = getattr(plugin_cls, "bind_attempt_executors", None)
        if callable(published):
            for contract in cast(Iterable[TaskAttemptContract[Any, Any]], published()):
                data[contract.contract_id] = contract
        if callable(bind):
            try:
                resolved = bind(workspace, fail_first=fail_first)
            except TypeError:
                resolved = bind(workspace)
            for contract_id, item in cast(Mapping[str, ResolvedAttemptContract[Any, Any]], resolved).items():
                executors[str(contract_id)] = item
    return CatalogContractResolver(data, executors)


def claims_for(contract: TaskAttemptContract[Any, Any]) -> ResourceClaims:
    resources = contract.resources
    if isinstance(resources, ResourceClaims):
        return resources
    return ResourceClaims(writes=())


def entrypoint_digest(name: str, side: str) -> str:
    return canonical_digest({"entrypoint": name, "side": side})


__all__ = [
    "CatalogContractResolver",
    "MemoryCheckpointer",
    "boot_factory_product",
    "claims_for",
    "contract_resolver_from_plugins",
    "entrypoint_digest",
    "factory_application",
    "factory_source_from_composition",
    "invocation_values",
    "load_plugin_class",
    "run_factory_product",
    "workspace_binding_roots",
    "workspace_provider_for",
]
