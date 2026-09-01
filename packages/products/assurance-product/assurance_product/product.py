from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, cast

import yaml

from graph_engine.attempts import ResolvedAttemptContract
from graph_engine.canonical import JSONValue, canonical_json_bytes
from graph_engine.composition import (
    CapabilityBindingEntry,
    ConfigTreePluginSource,
    FrozenComposition,
    PluginRequirement,
    ProductManifest,
    RegistryPlatform,
    ResolutionRequest,
    SourceKey,
    SourceKind,
    SourceRole,
    WheelPluginSource,
    WheelProductSource,
)
from graph_engine.composition.sources import WheelProductDeclaration
from graph_engine.composition.lock import ProductLock, build_invocation_lock
from graph_engine.composition.workflow_assembler import assemble_product_workflow
from graph_engine.frozen_json import thaw_json
from graph_engine.graph.compiler import CompiledWorkflow, compile_workflow
from graph_engine.graph.module_schema import WorkflowModuleDef, parse_workflow_module
from graph_engine.plugin_api import FrozenModel, ProviderSource

from assurance_product.agent_contracts import (
    bind_agent_execution_contracts,
    product_workflow_module_requirements,
    product_workflow_slot_bindings,
)
from assurance_product.change_workspace import ChangeWorkspace
from assurance_product.models import (
    CONFIGURATION_PLUGIN_ID,
    CONFIGURATION_PLUGIN_VERSION,
    ENGINE_API,
    PLUGIN_ID,
    PLUGIN_VERSION,
    PREPARE_IDS,
    AdapterName,
    CursorBindingV1,
    OpenCodeBindingV1,
    adapter_secret_handles,
    alias_ids_for_prepare,
    all_binding_ids,
)
from assurance_product.source_catalog import (
    adapter_for_entrypoint,
    product_source_catalog,
    wheel_plugin_source,
)

_PRODUCT_VERSION = "0.2.0"
_MANIFEST_PRODUCT_ID = "assurance.product"
_CAPABILITY_PLUGIN_IDS: tuple[str, ...] = (
    "assurance.intake",
    "assurance.generation",
    "assurance.execution",
    "assurance.healing",
    "assurance.quality",
    "assurance.improvement",
)
_RUNTIME_PLUGIN_IDS: dict[AdapterName, str] = {
    "opencode": "runtime.opencode",
    "cursor": "runtime.cursor",
}
_PLUGIN_VERSIONS: dict[str, str] = {
    **{plugin_id: "==0.2.0" for plugin_id in _CAPABILITY_PLUGIN_IDS},
    **{plugin_id: "==0.1.0" for plugin_id in _RUNTIME_PLUGIN_IDS.values()},
    PLUGIN_ID: f"=={PLUGIN_VERSION}",
    CONFIGURATION_PLUGIN_ID: f"=={CONFIGURATION_PLUGIN_VERSION}",
}
_WORKFLOW_DIR = Path(__file__).resolve().parent / "resources" / "workflow"
_PRODUCT_MODULE_PATH = _WORKFLOW_DIR / "main.yaml"


def prepare_change_workspace(project_root: Path, change_id: str) -> ChangeWorkspace:
    return ChangeWorkspace.prepare(Path(project_root).resolve(), change_id)


def reopen_change_workspace(project_root: Path, change_id: str) -> ChangeWorkspace:
    workspace = ChangeWorkspace.open(Path(project_root).resolve(), change_id)
    workspace.initialize()
    return workspace


def load_product_workflow_module() -> WorkflowModuleDef:
    return parse_workflow_module(_PRODUCT_MODULE_PATH.read_text(encoding="utf-8"))


_PRODUCT_MODULE = load_product_workflow_module()
_ENTRYPOINTS = dict(_PRODUCT_MODULE.entrypoints)
_PROVIDERS: dict[AdapterName, str] = {
    "opencode": "AssuranceOpenCodeProductProvider",
    "cursor": "AssuranceCursorProductProvider",
}
_PREPARE_DATA_FIELDS = frozenset(
    {"agent_profile", "execution", "request_policy_digest", "request_config_digest"}
)
_PREPARE_EXECUTION_FIELDS = frozenset(
    {"provider_model", "worker_profile", "permission_profile_digest", "limits"}
)
_FORBIDDEN_GRAPH_PREFIXES = (
    "runtime.",
    "test.",
)
_FEATURE_CAPABILITY_PREFIXES = (
    "assurance.intake.",
    "assurance.generation.",
    "assurance.execution.",
    "assurance.quality.",
    "assurance.healing.",
    "assurance.improvement.",
)
_TEST_ONLY_MARKERS = (".test.",)
_INVENTORY_RELATIVE = Path(
    "packages/products/assurance-product/assurance_product/resources/graph-inventory.yaml"
)


class GraphAuditResult(FrozenModel):
    unreachable_nodes: tuple[str, ...]
    dead_ends: tuple[str, ...]
    forbidden_direct_targets: tuple[str, ...]
    missing_bindings: tuple[str, ...]
    uninventoried_nodes: tuple[str, ...]


class AssuranceCompositionError(ValueError):
    """Raised when an Assurance composition is incomplete, extra, or forged."""


@dataclass(frozen=True, slots=True)
class AssuranceComposition(FrozenComposition):
    semantic_attempt_contracts: Mapping[str, ResolvedAttemptContract[Any, Any]]


class AssuranceCompositionRequest(FrozenModel):
    product_entrypoint: Literal["assurance-opencode", "assurance-cursor"]
    deployment_source: WheelPluginSource
    configuration_tree: ConfigTreePluginSource


def _declaration_filename(adapter: AdapterName) -> str:
    return f"product-declaration-{adapter}.json"


def _declaration_path(adapter: AdapterName) -> str:
    return f"assurance_product/{_declaration_filename(adapter)}"


def _product_source(adapter: AdapterName) -> ProviderSource:
    return ProviderSource(
        distribution="assurance-product",
        version=_PRODUCT_VERSION,
        entrypoint_group="graph_engine.products",
        entrypoint_name=f"assurance-{adapter}",
        entrypoint_value=f"assurance_product.product:{_PROVIDERS[adapter]}",
        declaration_path=_declaration_path(adapter),
        import_roots=("",),
    )


def _wheel_product_source(adapter: AdapterName, entrypoint: str) -> WheelProductSource:
    return WheelProductSource(
        distribution="assurance-product",
        entrypoint_name=entrypoint,
        declaration_path=_declaration_path(adapter),
    )


def _required_plugin_ids(adapter: AdapterName) -> tuple[str, ...]:
    return tuple(
        sorted(
            (
                *_CAPABILITY_PLUGIN_IDS,
                _RUNTIME_PLUGIN_IDS[adapter],
                PLUGIN_ID,
                CONFIGURATION_PLUGIN_ID,
            )
        )
    )


def _build_manifest(
    adapter: AdapterName,
    config_plugin_paths: tuple[str, ...] = (),
) -> ProductManifest:
    return ProductManifest(
        schema_version="1",
        source=_product_source(adapter),
        product_id=_MANIFEST_PRODUCT_ID,
        product_version=_PRODUCT_VERSION,
        engine_api=ENGINE_API,
        plugins=tuple(
            PluginRequirement(plugin_id=plugin_id, version_specifier=_PLUGIN_VERSIONS[plugin_id])
            for plugin_id in _required_plugin_ids(adapter)
        ),
        entrypoints=dict(_ENTRYPOINTS),
        configuration={},
        config_plugin_paths=config_plugin_paths,
        workflow_module=_PRODUCT_MODULE,
        workflow_module_resources=product_workflow_module_requirements(),
        workflow_slot_bindings=product_workflow_slot_bindings(),
    )


def product_declaration_document(
    adapter: AdapterName,
    config_plugin_paths: tuple[str, ...] = (),
) -> dict[str, JSONValue]:
    source = _product_source(adapter).model_dump(mode="json")
    manifest = _build_manifest(adapter, config_plugin_paths).model_dump(mode="json", exclude_none=True)
    manifest.pop("workflow", None)
    manifest.pop("workflow_resource_id", None)
    if _PRODUCT_MODULE is not None:
        manifest["workflow_module"] = _PRODUCT_MODULE.model_dump(
            mode="json",
            by_alias=True,
            exclude_unset=True,
        )
    return {
        "schema_version": "1",
        "kind": "product",
        "source": source,
        "manifest": manifest,
    }


def _load_declared_manifest(adapter: AdapterName) -> ProductManifest:
    path = Path(__file__).resolve().parent / _declaration_filename(adapter)
    document = json.loads(path.read_bytes())
    declaration = WheelProductDeclaration.model_validate(document)
    return declaration.manifest


class AssuranceOpenCodeProductProvider:
    @staticmethod
    def manifest() -> ProductManifest:
        return _load_declared_manifest("opencode")


class AssuranceCursorProductProvider:
    @staticmethod
    def manifest() -> ProductManifest:
        return _load_declared_manifest("cursor")


def _capability_bindings(composition: FrozenComposition) -> dict[str, CapabilityBindingEntry]:
    return {
        key: value
        for key, value in composition.registries.capabilities.entries.items()
        if isinstance(value, CapabilityBindingEntry)
    }


def _lock_binding_ids(composition: FrozenComposition) -> set[str]:
    projection = thaw_json(composition.lock.capability_bindings)
    if not isinstance(projection, list):
        raise AssuranceCompositionError("lock binding projection is not a list")
    ids: set[str] = set()
    for item in projection:
        if not isinstance(item, Mapping):
            raise AssuranceCompositionError("lock binding projection entry is not a mapping")
        capability_id = item.get("capability_id")
        if not isinstance(capability_id, str):
            raise AssuranceCompositionError("lock binding projection is missing capability_id")
        ids.add(capability_id)
    return ids


def _graph_binding_ids(composition: FrozenComposition) -> set[str]:
    return {
        node.definition.capability
        for graph in composition.workflow.graphs.values()
        for node in graph.nodes.values()
        if node.definition.capability is not None
    }


def _prepare_data_is_assignment(value: object) -> bool:
    data = thaw_json(value)
    if not isinstance(data, Mapping):
        return False
    if not _PREPARE_DATA_FIELDS.issubset(data):
        return False
    execution = data.get("execution")
    return isinstance(execution, Mapping) and _PREPARE_EXECUTION_FIELDS.issubset(execution)


def _execute_secret_handles(adapter: AdapterName, data: object) -> tuple[str, ...]:
    raw = thaw_json(data)
    if adapter == "opencode":
        binding = OpenCodeBindingV1.model_validate(raw)
    else:
        binding = CursorBindingV1.model_validate(raw)
    return adapter_secret_handles(binding)


def _authenticate_assurance_composition(
    composition: FrozenComposition,
    adapter: AdapterName,
) -> FrozenComposition:
    expected_plugins = set(_required_plugin_ids(adapter))
    expected_bindings = set(all_binding_ids())
    descriptor_ids = {descriptor.plugin_id for descriptor in composition.descriptors}
    manifest_ids = set(composition.manifest.required_plugin_ids)
    authority_owners = set(composition.contribution_authorities)
    if (
        descriptor_ids != expected_plugins
        or manifest_ids != expected_plugins
        or authority_owners != expected_plugins
    ):
        raise AssuranceCompositionError("composition plugin set is not the exact product closure")

    bindings = _capability_bindings(composition)
    if set(bindings) != expected_bindings or len(bindings) != 99:
        raise AssuranceCompositionError("composition binding set is not the exact 99 aliases")
    if _lock_binding_ids(composition) != expected_bindings:
        raise AssuranceCompositionError("lock binding projection is not the exact 99 aliases")
    graph_bindings = _graph_binding_ids(composition)
    agent_bindings = {capability for capability in graph_bindings if capability.startswith(f"{PLUGIN_ID}.")}
    feature_bindings = graph_bindings - agent_bindings
    slot_aliases = {item.capability_id for item in composition.manifest.workflow_slot_bindings}
    if slot_aliases != expected_bindings or len(slot_aliases) != 99:
        raise AssuranceCompositionError("workflow slot bindings are not the exact 99 aliases")
    if agent_bindings != expected_bindings:
        raise AssuranceCompositionError("graph bindings are not the exact frozen 99 aliases")
    if any(capability.startswith(_FORBIDDEN_GRAPH_PREFIXES) for capability in graph_bindings):
        raise AssuranceCompositionError("graph referenced a direct runtime or Phase 4 capability")
    if any(not capability.startswith(_FEATURE_CAPABILITY_PREFIXES) for capability in feature_bindings):
        raise AssuranceCompositionError("graph referenced a non-product agent alias")

    source = composition.manifest.source
    if source is None or source.entrypoint_name != f"assurance-{adapter}":
        raise AssuranceCompositionError("product entry-point coordinate does not match the selected adapter")
    if source.distribution != "assurance-product" or source.version != _PRODUCT_VERSION:
        raise AssuranceCompositionError("product distribution identity drifted")
    if source.declaration_path != _declaration_path(adapter):
        raise AssuranceCompositionError("product declaration path drifted")
    product_entry = composition.registries.sources.entries.get(
        SourceKey(SourceRole.PRODUCT, composition.manifest.product_id)
    )
    if product_entry is None:
        raise AssuranceCompositionError("product snapshot is missing")
    identity = product_entry.snapshot.identity
    if (
        identity.distribution != "assurance-product"
        or identity.version != _PRODUCT_VERSION
        or identity.entrypoint_name != f"assurance-{adapter}"
        or identity.declaration_path != _declaration_path(adapter)
        or not identity.import_roots
    ):
        raise AssuranceCompositionError("product snapshot identity drifted")
    if composition.lock.product.source.kind is not SourceKind.WHEEL_PRODUCT:
        raise AssuranceCompositionError("product source kind is not an installed wheel")
    declaration_file = next(
        (item for item in product_entry.snapshot.files if item.path == source.declaration_path),
        None,
    )
    if declaration_file is None:
        raise AssuranceCompositionError("product declaration bytes are missing from the snapshot")

    deployment = next(
        (descriptor for descriptor in composition.descriptors if descriptor.plugin_id == PLUGIN_ID),
        None,
    )
    if deployment is None or deployment.plugin_id != PLUGIN_ID or deployment.plugin_version != PLUGIN_VERSION:
        raise AssuranceCompositionError("deployment descriptor identity drifted")
    authority = composition.contribution_authorities.get(PLUGIN_ID)
    if authority is None:
        raise AssuranceCompositionError("deployment contribution authority is missing")
    contribution_binding_ids = {item.capability_id for item in authority.contribution.bindings}
    if contribution_binding_ids != expected_bindings:
        raise AssuranceCompositionError("deployment contribution binding set drifted")
    for resource in authority.contribution.resources:
        entry = composition.registries.resources.entries.get(resource.resource_id)
        if entry is None:
            raise AssuranceCompositionError(f"binding resource is not registered: {resource.resource_id}")
        if entry.sha256 != hashlib.sha256(entry.content).hexdigest():
            raise AssuranceCompositionError(f"resource digest drifted: {resource.resource_id}")

    runtime_execute = f"{_RUNTIME_PLUGIN_IDS[adapter]}.execute"
    for prepare_id in PREPARE_IDS:
        prepare_alias, execute_alias, finalize_alias = alias_ids_for_prepare(prepare_id)
        prepare = bindings[prepare_alias]
        execute = bindings[execute_alias]
        finalize = bindings[finalize_alias]
        resource_ids = (*prepare.resource_ids, *execute.resource_ids, *finalize.resource_ids)
        if prepare.target_capability_id != prepare_id:
            raise AssuranceCompositionError(f"prepare alias target drifted: {prepare_alias}")
        if execute.target_capability_id != runtime_execute:
            raise AssuranceCompositionError(f"execute alias target drifted: {execute_alias}")
        if finalize.target_capability_id != f"{prepare_id.removesuffix('.prepare')}.finalize":
            raise AssuranceCompositionError(f"finalize alias target drifted: {finalize_alias}")
        if not _prepare_data_is_assignment(prepare.data) or prepare.secret_handles != ():
            raise AssuranceCompositionError(
                f"prepare alias is not an AgentBindingDataV1 assignment: {prepare_alias}"
            )
        if execute.data is None:
            raise AssuranceCompositionError(f"execute alias is missing adapter binding data: {execute_alias}")
        if tuple(execute.secret_handles) != _execute_secret_handles(adapter, execute.data):
            raise AssuranceCompositionError(f"execute alias secret handles drifted: {execute_alias}")
        if finalize.data is not None or finalize.secret_handles != ():
            raise AssuranceCompositionError(f"finalize alias must be null with no secrets: {finalize_alias}")
        for resource_id in resource_ids:
            if resource_id not in composition.registries.resources.entries:
                raise AssuranceCompositionError(f"binding resource is not registered: {resource_id}")
    if composition.lock.engine_api != ENGINE_API:
        raise AssuranceCompositionError("invocation lock engine API drifted")
    return composition


def _apply_agent_execution_contracts(composition: FrozenComposition) -> FrozenComposition:
    descriptors = {descriptor.plugin_id: descriptor for descriptor in composition.descriptors}
    assembled = assemble_product_workflow(
        manifest=composition.manifest,
        descriptors=descriptors,
        registries=composition.registries,
    )
    workflow = compile_workflow(
        bind_agent_execution_contracts(assembled),
        composition.registries,
    )
    source_entries = composition.registries.sources.entries
    engine_source = source_entries[SourceKey(SourceRole.ENGINE, "graph.engine")]
    product_source = source_entries[SourceKey(SourceRole.PRODUCT, composition.manifest.product_id)]
    lock = build_invocation_lock(
        manifest=composition.manifest,
        product_snapshot=product_source.snapshot,
        descriptors=descriptors,
        dependency_order=composition.lock.dependency_order,
        registries=composition.registries,
        configuration=composition.configuration,
        workflow=workflow,
        engine_snapshot=engine_source.snapshot,
        contribution_authorities=composition.contribution_authorities,
    )
    return FrozenComposition.freeze(
        composition.manifest,
        composition.registries,
        workflow,
        lock,
        descriptors=composition.descriptors,
        configuration=composition.configuration,
        contribution_authorities=composition.contribution_authorities,
        providers=composition.providers,
        product_provider=composition.product_provider,
        declarative_sources=composition.declarative_sources,
    )


def _with_semantic_attempt_contracts(
    composition: FrozenComposition,
    contracts: Mapping[str, ResolvedAttemptContract[Any, Any]],
) -> AssuranceComposition:
    return AssuranceComposition(
        manifest=composition.manifest,
        descriptors=composition.descriptors,
        registries=composition.registries,
        workflow=composition.workflow,
        configuration=composition.configuration,
        contribution_authorities=composition.contribution_authorities,
        providers=composition.providers,
        product_provider=composition.product_provider,
        declarative_sources=composition.declarative_sources,
        lock=composition.lock,
        digest=composition.digest,
        semantic_attempt_contracts=MappingProxyType(dict(contracts)),
    )


def resolve_assurance_composition(request: AssuranceCompositionRequest) -> AssuranceComposition:
    from assurance_product.runtime_bindings import boot_semantic_attempt_contracts

    adapter = adapter_for_entrypoint(request.product_entrypoint)
    if not request.configuration_tree.path.is_dir():
        raise AssuranceCompositionError("configuration tree is not an explicit existing path")
    composition = RegistryPlatform().resolve(
        ResolutionRequest(
            product=_wheel_product_source(adapter, request.product_entrypoint),
            plugins=(
                *(wheel_plugin_source(source) for source in product_source_catalog(adapter)),
                request.deployment_source,
                request.configuration_tree,
            ),
        )
    )
    composition = _apply_agent_execution_contracts(composition)
    composition = _authenticate_assurance_composition(composition, adapter)
    return _with_semantic_attempt_contracts(
        composition,
        boot_semantic_attempt_contracts(composition),
    )


def _workflow_node_ids(workflow: CompiledWorkflow) -> set[str]:
    return {f"{graph_id}/{node_id}" for graph_id, graph in workflow.graphs.items() for node_id in graph.nodes}


def _reachable_node_ids(workflow: CompiledWorkflow) -> set[str]:
    pending: list[tuple[str, str]] = []
    for graph_id in workflow.entrypoints.values():
        graph = workflow.graphs[graph_id]
        pending.append((graph_id, graph.start))
    seen: set[str] = set()
    while pending:
        graph_id, node_id = pending.pop()
        key = f"{graph_id}/{node_id}"
        if key in seen:
            continue
        seen.add(key)
        graph = workflow.graphs[graph_id]
        node = graph.nodes[node_id]
        target = node.definition.graph
        if target is not None:
            pending.append((target, workflow.graphs[target].start))
        for edge in node.outgoing:
            pending.append((graph_id, edge.to))
    return seen


def _is_forbidden_graph_target(capability: str) -> bool:
    if capability.startswith(_FORBIDDEN_GRAPH_PREFIXES):
        return True
    if capability.startswith("test.") or any(marker in capability for marker in _TEST_ONLY_MARKERS):
        return True
    if capability.startswith(f"{PLUGIN_ID}."):
        return False
    return not capability.startswith(_FEATURE_CAPABILITY_PREFIXES)


def _inventory_path() -> Path | None:
    candidates = (
        Path.cwd() / _INVENTORY_RELATIVE,
        Path(__file__).resolve().parents[4] / _INVENTORY_RELATIVE,
    )
    for path in candidates:
        if path.is_file():
            return path
    return None


def _load_inventory_nodes() -> set[str] | None:
    raw: str | None = None
    package = __package__
    if package is not None:
        packaged = files(package).joinpath("resources", "graph-inventory.yaml")
        try:
            raw = packaged.read_text(encoding="utf-8")
        except (FileNotFoundError, IsADirectoryError, OSError):
            raw = None
    if raw is None:
        path = _inventory_path()
        if path is not None:
            raw = path.read_text(encoding="utf-8")
    if raw is None:
        return None
    document = yaml.safe_load(raw)
    if not isinstance(document, Mapping):
        return None
    nodes = document.get("nodes")
    if not isinstance(nodes, list):
        return None
    return {node for node in nodes if isinstance(node, str)}


def audit_full_graph(workflow: CompiledWorkflow, composition: FrozenComposition) -> GraphAuditResult:
    all_nodes = _workflow_node_ids(workflow)
    reachable = _reachable_node_ids(workflow)
    inventoried = _load_inventory_nodes()
    capability_entries = composition.registries.capabilities.entries
    forbidden: list[str] = []
    missing: list[str] = []
    for graph in workflow.graphs.values():
        for node in graph.nodes.values():
            capability = node.definition.capability
            if capability is None:
                continue
            if _is_forbidden_graph_target(capability):
                forbidden.append(f"{graph.graph_id}/{node.node_id}:{capability}")
            if capability not in capability_entries:
                missing.append(f"{graph.graph_id}/{node.node_id}:{capability}")
    dead_ends = tuple(
        sorted(
            f"{graph.graph_id}/{node.node_id}"
            for graph in workflow.graphs.values()
            for node in graph.nodes.values()
            if node.definition.kind not in {"end", "interrupt"} and not node.outgoing
        )
    )
    return GraphAuditResult(
        unreachable_nodes=tuple(sorted(all_nodes - reachable)),
        dead_ends=dead_ends,
        forbidden_direct_targets=tuple(sorted(forbidden)),
        missing_bindings=tuple(sorted(missing)),
        uninventoried_nodes=tuple(sorted(all_nodes if inventoried is None else all_nodes - inventoried)),
    )


def product_lock_from_composition(composition: FrozenComposition) -> ProductLock:
    lock = composition.lock
    return ProductLock.create(
        engine_api=lock.engine_api,
        engine=lock.engine,
        engine_digest=lock.engine_digest,
        product=lock.product,
        plugins=lock.plugins,
        dependency_order=lock.dependency_order,
        registry_projections=lock.registry_projections,
        registry_digests=lock.registry_digests,
        configuration=thaw_json(lock.configuration),
        configuration_digest=lock.configuration_digest,
        capability_bindings=thaw_json(lock.capability_bindings),
        capability_bindings_digest=lock.capability_bindings_digest,
    )


def reject_organization_overrides(organization_root: Path | None) -> None:
    from graph_engine.boot.boot import OrganizationOverrideError, _reject_organization_overrides

    try:
        _reject_organization_overrides(organization_root)
    except OrganizationOverrideError:
        raise
    except Exception as error:
        raise OrganizationOverrideError(str(error)) from error


def coexistence_graph_manifest(
    composition: FrozenComposition,
    product_lock: ProductLock,
):
    from importlib import metadata

    from assurance_product.graph_factories import FEATURE_GRAPH_FACTORIES
    from assurance_product.graphs.revisions import ENTRYPOINT_CONTRACTS
    from graph_engine.boot.boot import CHECKPOINT_CONTRACT_VERSION
    from graph_engine.boot.graph_revision import GraphBuildManifest, GraphRevision
    from graph_engine.canonical import canonical_digest
    from graph_engine.composition.models import SourceKey, SourceRole

    sources = composition.registries.sources.entries
    wheel_source_digests: dict[str, str] = {}
    product_key = SourceKey(SourceRole.PRODUCT, composition.manifest.product_id)
    if product_key in sources:
        wheel_source_digests["assurance.product"] = sources[product_key].snapshot.digest
    for owner in (
        "assurance.intake",
        "assurance.generation",
        "assurance.execution",
        "assurance.quality",
        "assurance.healing",
        "assurance.improvement",
    ):
        key = SourceKey(SourceRole.PLUGIN, owner)
        if key in sources:
            wheel_source_digests[owner] = sources[key].snapshot.digest
    factory_symbols = (
        "assurance_product.graphs.factory:build_product_graphs",
        *(ref.symbol for ref in FEATURE_GRAPH_FACTORIES),
    )
    attempt_contract_digests = {
        contract_id: canonical_digest(contract.canonical_projection())
        for contract_id, contract in composition.semantic_attempt_contracts.items()
        if hasattr(contract, "canonical_projection")
    }
    revision = GraphRevision.build(
        product_lock_digest=product_lock.digest,
        wheel_source_digests=wheel_source_digests,
        factory_symbols=factory_symbols,
        state_schema_versions={
            name: contract.state_schema_version for name, contract in ENTRYPOINT_CONTRACTS.items()
        },
        langgraph_version=metadata.version("langgraph"),
        checkpoint_contract_version=CHECKPOINT_CONTRACT_VERSION,
    )
    return GraphBuildManifest(
        revision=revision,
        entrypoint_contract_digests={
            name: canonical_digest(contract.canonical_projection())
            for name, contract in ENTRYPOINT_CONTRACTS.items()
        },
        attempt_contract_digests=attempt_contract_digests,
    )


def write_committed_product_declarations(package_root: Path | None = None) -> tuple[Path, Path]:
    root = package_root or Path(__file__).resolve().parent
    written: list[Path] = []
    for adapter in ("opencode", "cursor"):
        path = root / _declaration_filename(cast(AdapterName, adapter))
        path.write_bytes(
            canonical_json_bytes(product_declaration_document(cast(AdapterName, adapter))) + b"\n"
        )
        written.append(path)
    return (written[0], written[1])
