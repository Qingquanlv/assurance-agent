from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, cast

from graph_engine.attempts import ResolvedAttemptContract
from graph_engine.boot.graph_revision import EntrypointGraphContract, GraphRevision
from graph_engine.canonical import JSONValue, canonical_json_bytes
from graph_engine.composition import (
    CapabilityBindingEntry,
    ConfigTreePluginSource,
    EditableWheelProductSource,
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
from graph_engine.composition.lock import ProductLock
from graph_engine.frozen_json import thaw_json
from graph_engine.plugin_api import FrozenModel, ProviderSource

from assurance_product.agent_contracts import AGENT_EXECUTION_CONTRACTS
from assurance_product.change_workspace import ChangeWorkspace
from assurance_product.feature_set import CAPABILITIES, CAPABILITY_OWNERS
from assurance_product.models import (
    CONFIGURATION_PLUGIN_ID,
    CONFIGURATION_PLUGIN_VERSION,
    ENGINE_API,
    PLUGIN_ID,
    PLUGIN_VERSION,
    PRODUCT_ENTRYPOINTS,
    all_binding_ids,
)
from assurance_product.source_catalog import (
    editable_distribution_root,
    editable_source_files,
    installed_plugin_source,
    product_source_catalog,
)

_PRODUCT_VERSION = "0.3.0"
_MANIFEST_PRODUCT_ID = "assurance.product"
_CAPABILITY_PLUGIN_IDS: tuple[str, ...] = CAPABILITY_OWNERS
_RUNTIME_PLUGIN_ID = "runtime.opencode"
_PRODUCT_ENTRYPOINT = "assurance-opencode"
_DECLARATION_FILENAME = "product-declaration-opencode.json"
_DECLARATION_PATH = f"assurance_product/{_DECLARATION_FILENAME}"
_PROVIDER_SYMBOL = "AssuranceOpenCodeProductProvider"
_PLUGIN_VERSIONS: dict[str, str] = {
    **{pin.owner_id: f"=={pin.version}" for pin in CAPABILITIES},
    _RUNTIME_PLUGIN_ID: "==0.1.0",
    PLUGIN_ID: f"=={PLUGIN_VERSION}",
    CONFIGURATION_PLUGIN_ID: f"=={CONFIGURATION_PLUGIN_VERSION}",
}
_PRODUCT_FACTORY_SYMBOL = "assurance_product.graphs.factory:build_product_graphs"
_ENTRYPOINTS = {name: name for name in sorted(PRODUCT_ENTRYPOINTS)}


def prepare_change_workspace(project_root: Path, change_id: str) -> ChangeWorkspace:
    return ChangeWorkspace.prepare(Path(project_root).resolve(), change_id)


def reopen_change_workspace(project_root: Path, change_id: str) -> ChangeWorkspace:
    workspace = ChangeWorkspace.open(Path(project_root).resolve(), change_id)
    workspace.initialize()
    return workspace


_PREPARE_DATA_FIELDS = frozenset(
    {"agent_profile", "execution", "request_policy_digest", "request_config_digest"}
)
_PREPARE_EXECUTION_FIELDS = frozenset(
    {"provider_model", "worker_profile", "permission_profile_digest", "limits"}
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
    product_entrypoint: Literal["assurance-opencode"]
    deployment_source: WheelPluginSource
    configuration_tree: ConfigTreePluginSource


def _product_source() -> ProviderSource:
    return ProviderSource(
        distribution="assurance-product",
        version=_PRODUCT_VERSION,
        entrypoint_group="graph_engine.products",
        entrypoint_name=_PRODUCT_ENTRYPOINT,
        entrypoint_value=f"assurance_product.product:{_PROVIDER_SYMBOL}",
        declaration_path=_DECLARATION_PATH,
        import_roots=("", "assurance_product"),
    )


def _wheel_product_source() -> WheelProductSource | EditableWheelProductSource:
    root = editable_distribution_root("assurance-product")
    if root is None:
        return WheelProductSource(
            distribution="assurance-product",
            entrypoint_name=_PRODUCT_ENTRYPOINT,
            declaration_path=_DECLARATION_PATH,
        )
    files = editable_source_files(root)
    if _DECLARATION_PATH not in files:
        raise AssuranceCompositionError(
            f"editable assurance-product is missing declaration {_DECLARATION_PATH}"
        )
    return EditableWheelProductSource(
        distribution="assurance-product",
        entrypoint_name=_PRODUCT_ENTRYPOINT,
        declaration_path=_DECLARATION_PATH,
        source_root=root,
        source_files=files,
    )


def _required_plugin_ids() -> tuple[str, ...]:
    return tuple(
        sorted(
            (
                *_CAPABILITY_PLUGIN_IDS,
                _RUNTIME_PLUGIN_ID,
                PLUGIN_ID,
                CONFIGURATION_PLUGIN_ID,
            )
        )
    )


def _build_manifest(
    config_plugin_paths: tuple[str, ...] = (),
) -> ProductManifest:
    return ProductManifest(
        schema_version="1",
        source=_product_source(),
        product_id=_MANIFEST_PRODUCT_ID,
        product_version=_PRODUCT_VERSION,
        engine_api=ENGINE_API,
        plugins=tuple(
            PluginRequirement(plugin_id=plugin_id, version_specifier=_PLUGIN_VERSIONS[plugin_id])
            for plugin_id in _required_plugin_ids()
        ),
        entrypoints=dict(_ENTRYPOINTS),
        configuration={},
        config_plugin_paths=config_plugin_paths,
        graph_factory_symbol=_PRODUCT_FACTORY_SYMBOL,
    )


def product_declaration_document(
    config_plugin_paths: tuple[str, ...] = (),
) -> dict[str, JSONValue]:
    source = _product_source().model_dump(mode="json")
    manifest = _build_manifest(config_plugin_paths).model_dump(mode="json", exclude_none=True)
    manifest.pop("workflow", None)
    manifest.pop("workflow_resource_id", None)
    manifest.pop("workflow_module", None)
    manifest.pop("workflow_module_resources", None)
    manifest.pop("workflow_slot_bindings", None)
    return {
        "schema_version": "1",
        "kind": "product",
        "source": source,
        "manifest": manifest,
    }


def _load_declared_manifest() -> ProductManifest:
    path = Path(__file__).resolve().parent / _DECLARATION_FILENAME
    document = json.loads(path.read_bytes())
    declaration = WheelProductDeclaration.model_validate(document)
    return declaration.manifest


class AssuranceOpenCodeProductProvider:
    @staticmethod
    def manifest() -> ProductManifest:
        return _load_declared_manifest()


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


def _assignment_data_is_closed(value: object) -> bool:
    data = thaw_json(value)
    if not isinstance(data, Mapping):
        return False
    if not _PREPARE_DATA_FIELDS.issubset(data):
        return False
    execution = data.get("execution")
    return isinstance(execution, Mapping) and _PREPARE_EXECUTION_FIELDS.issubset(execution)


def _authenticate_assurance_composition(
    composition: FrozenComposition,
) -> FrozenComposition:
    expected_plugins = set(_required_plugin_ids())
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

    if composition.manifest.graph_factory_symbol != _PRODUCT_FACTORY_SYMBOL:
        raise AssuranceCompositionError("product graph factory symbol drifted")
    if not isinstance(composition.lock, ProductLock) or composition.lock.schema_version != "3":
        raise AssuranceCompositionError("composition lock is not ProductLock v3")

    bindings = _capability_bindings(composition)
    if set(bindings) != expected_bindings:
        raise AssuranceCompositionError("composition binding set is not the exact semantic contract catalog")
    if _lock_binding_ids(composition) != expected_bindings:
        raise AssuranceCompositionError("lock binding projection is not the exact semantic contract catalog")
    if set(bindings) != set(AGENT_EXECUTION_CONTRACTS):
        raise AssuranceCompositionError("composition bindings drifted from semantic Agent contracts")

    source = composition.manifest.source
    if source is None or source.entrypoint_name != _PRODUCT_ENTRYPOINT:
        raise AssuranceCompositionError("product entry-point coordinate is not the OpenCode product")
    if source.distribution != "assurance-product" or source.version != _PRODUCT_VERSION:
        raise AssuranceCompositionError("product distribution identity drifted")
    if source.declaration_path != _DECLARATION_PATH:
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
        or identity.entrypoint_name != _PRODUCT_ENTRYPOINT
        or identity.declaration_path != _DECLARATION_PATH
        or not identity.import_roots
    ):
        raise AssuranceCompositionError("product snapshot identity drifted")
    if composition.lock.product.source.kind not in {
        SourceKind.WHEEL_PRODUCT,
        SourceKind.EDITABLE_PRODUCT,
    }:
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

    runtime_execute = f"{_RUNTIME_PLUGIN_ID}.execute"
    expected_secrets = ("opencode.token",)
    for contract_id, contract in AGENT_EXECUTION_CONTRACTS.items():
        binding = bindings[contract_id]
        if binding.contract_id != contract.contract_id:
            raise AssuranceCompositionError(f"semantic binding contract drifted: {contract_id}")
        if binding.target_capability_id != runtime_execute:
            raise AssuranceCompositionError(f"semantic binding target drifted: {contract_id}")
        if not _assignment_data_is_closed(binding.data):
            raise AssuranceCompositionError(f"semantic binding is not a closed assignment: {contract_id}")
        if tuple(binding.secret_handles) != expected_secrets:
            raise AssuranceCompositionError(f"semantic binding secret handles drifted: {contract_id}")
        for resource_id in binding.resource_ids:
            if resource_id not in composition.registries.resources.entries:
                raise AssuranceCompositionError(f"binding resource is not registered: {resource_id}")
    if composition.lock.engine_api != ENGINE_API:
        raise AssuranceCompositionError("product lock engine API drifted")
    return composition


def _with_semantic_attempt_contracts(
    composition: FrozenComposition,
    contracts: Mapping[str, ResolvedAttemptContract[Any, Any]],
) -> AssuranceComposition:
    return AssuranceComposition(
        manifest=composition.manifest,
        descriptors=composition.descriptors,
        registries=composition.registries,
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

    if request.product_entrypoint != _PRODUCT_ENTRYPOINT:
        raise AssuranceCompositionError("product entry-point is not the OpenCode product")
    if not request.configuration_tree.path.is_dir():
        raise AssuranceCompositionError("configuration tree is not an explicit existing path")
    composition = RegistryPlatform().resolve(
        ResolutionRequest(
            product=_wheel_product_source(),
            plugins=(
                *(installed_plugin_source(source) for source in product_source_catalog()),
                request.deployment_source,
                request.configuration_tree,
            ),
        )
    )
    composition = _authenticate_assurance_composition(composition)
    return _with_semantic_attempt_contracts(
        composition,
        boot_semantic_attempt_contracts(composition),
    )


def product_lock_from_composition(composition: FrozenComposition) -> ProductLock:
    lock = composition.lock
    if isinstance(lock, ProductLock):
        return lock
    raise AssuranceCompositionError("composition lock is not ProductLock v3")


def reject_organization_overrides(organization_root: Path | None) -> None:
    from graph_engine.boot.boot import OrganizationOverrideError, _reject_organization_overrides

    try:
        _reject_organization_overrides(organization_root)
    except OrganizationOverrideError:
        raise
    except Exception as error:
        raise OrganizationOverrideError(str(error)) from error


def _revision_sources(
    composition: FrozenComposition,
    feature_graph_factories: tuple[Any, ...],
) -> tuple[dict[str, str], tuple[str, ...]]:
    sources = composition.registries.sources.entries
    wheel_source_digests: dict[str, str] = {}
    product_key = SourceKey(SourceRole.PRODUCT, composition.manifest.product_id)
    if product_key in sources:
        wheel_source_digests["assurance.product"] = sources[product_key].snapshot.digest
    for owner in _CAPABILITY_PLUGIN_IDS:
        key = SourceKey(SourceRole.PLUGIN, owner)
        if key in sources:
            wheel_source_digests[owner] = sources[key].snapshot.digest
    factory_symbols = (
        "assurance_product.graphs.factory:build_product_graphs",
        *(ref.symbol for ref in feature_graph_factories),
    )
    return wheel_source_digests, factory_symbols


def _revision_from_sources(
    product_lock: ProductLock,
    wheel_source_digests: Mapping[str, str],
    factory_symbols: tuple[str, ...],
    entrypoint_contracts: Mapping[str, EntrypointGraphContract],
    checkpoint_contract_version: str,
    package_version: Callable[[str], str],
) -> GraphRevision:
    return GraphRevision.build(
        product_lock_digest=product_lock.digest,
        wheel_source_digests=wheel_source_digests,
        factory_symbols=factory_symbols,
        state_schema_versions={
            name: contract.state_schema_version for name, contract in entrypoint_contracts.items()
        },
        langgraph_version=package_version("langgraph"),
        checkpoint_contract_version=checkpoint_contract_version,
    )


def product_graph_revision(
    composition: FrozenComposition,
    product_lock: ProductLock,
) -> GraphRevision:
    from importlib import metadata

    from assurance_product.graph_factories import FEATURE_GRAPH_FACTORIES
    from assurance_product.graphs.revisions import ENTRYPOINT_CONTRACTS
    from graph_engine.boot.boot import CHECKPOINT_CONTRACT_VERSION

    wheel_source_digests, factory_symbols = _revision_sources(composition, FEATURE_GRAPH_FACTORIES)
    return _revision_from_sources(
        product_lock,
        wheel_source_digests,
        factory_symbols,
        ENTRYPOINT_CONTRACTS,
        CHECKPOINT_CONTRACT_VERSION,
        metadata.version,
    )


def product_graph_manifest(
    composition: FrozenComposition,
    product_lock: ProductLock,
):
    from importlib import metadata

    from assurance_product.graph_factories import FEATURE_GRAPH_FACTORIES
    from assurance_product.graphs.revisions import ENTRYPOINT_CONTRACTS
    from graph_engine.boot.boot import CHECKPOINT_CONTRACT_VERSION
    from graph_engine.boot.graph_revision import GraphBuildManifest
    from graph_engine.canonical import canonical_digest

    wheel_source_digests, factory_symbols = _revision_sources(composition, FEATURE_GRAPH_FACTORIES)
    contracts = getattr(composition, "semantic_attempt_contracts", {})
    attempt_contract_digests = {
        contract_id: canonical_digest(contract.canonical_projection())
        for contract_id, contract in cast(Mapping[str, Any], contracts).items()
        if hasattr(contract, "canonical_projection")
    }
    revision = _revision_from_sources(
        product_lock,
        wheel_source_digests,
        factory_symbols,
        ENTRYPOINT_CONTRACTS,
        CHECKPOINT_CONTRACT_VERSION,
        metadata.version,
    )
    return GraphBuildManifest(
        revision=revision,
        entrypoint_contract_digests={
            name: canonical_digest(contract.canonical_projection())
            for name, contract in ENTRYPOINT_CONTRACTS.items()
        },
        attempt_contract_digests=attempt_contract_digests,
    )


def write_committed_product_declarations(package_root: Path | None = None) -> Path:
    root = package_root or Path(__file__).resolve().parent
    path = root / _DECLARATION_FILENAME
    path.write_bytes(canonical_json_bytes(product_declaration_document()) + b"\n")
    return path
