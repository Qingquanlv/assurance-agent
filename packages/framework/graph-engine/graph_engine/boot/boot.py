from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from importlib import metadata
import inspect
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol

from langgraph.graph import StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Checkpointer

from graph_engine.application.runtime_context import (
    AttemptKernelPort,
    SecretResolverPort,
    WorkspaceProviderPort,
)
from graph_engine.attempts.contracts import ResolvedAttemptContract, TaskAttemptContract
from graph_engine.attempts.kernel import AssuranceAttemptKernel
from graph_engine.attempts.node_factory import AttemptNodeFactory
from graph_engine.boot.graph_revision import (
    BootArtifact,
    EntrypointGraphContract,
    FeatureFactoryRef,
    GraphBuildManifest,
    GraphRevision,
)
from graph_engine.boot.source_authentication import (
    AuthenticatedFactory,
    AuthenticatedFactorySource,
    ProductFactoryRef,
)
from graph_engine.canonical import canonical_digest
from graph_engine.composition.lock import ProductLock
from graph_engine.composition.source_fs import recapture_source_snapshot
from graph_engine.errors import GraphEngineError


CHECKPOINT_CONTRACT_VERSION = "1"

_REJECTED_AA_NAMES = frozenset(
    {
        "execution-contracts.yaml",
        "execution-contracts.yml",
        "factory.py",
        "module.yaml",
        "module.yml",
        "topology.yaml",
        "topology.yml",
        "workflow-schema.yaml",
        "workflow-schema.yml",
    }
)


class ContractOwnershipError(GraphEngineError):
    """Raised when Feature graph code names a contract it does not own."""


class OrganizationOverrideError(GraphEngineError):
    """Raised when `.aa/` tries to supply topology, factory, or module code."""


class FactorySourcePolicyError(GraphEngineError):
    """Raised when a factory reads a source outside authenticated wheels."""


class BootValidationError(GraphEngineError):
    """Raised when offline or runtime Boot cannot authenticate a graph build."""


class SourceAuthenticator(Protocol):
    def authenticate(
        self,
        ref: FeatureFactoryRef | ProductFactoryRef,
        sources: Mapping[str, object],
    ) -> AuthenticatedFactory: ...


class ContractResolverPort(Protocol):
    def resolve_data_contracts(self) -> Mapping[str, TaskAttemptContract[Any, Any]]: ...

    def resolve_executors(self) -> Mapping[str, ResolvedAttemptContract[Any, Any]]: ...


class CapabilityBuildContext(Protocol):
    owner_id: str

    def attempt(
        self,
        contract_id: str,
        *,
        semantic_node_id: str,
        activation: object,
        select: object,
        publish: object,
    ) -> object: ...

    def compile_subgraph(self, builder: StateGraph[Any]) -> CompiledStateGraph: ...


class GraphBuildContext(Protocol):
    def for_capability(self, owner_id: str) -> CapabilityBuildContext: ...

    def compile_root(self, builder: StateGraph[Any]) -> CompiledStateGraph: ...

    def compile_subgraph(self, builder: StateGraph[Any]) -> CompiledStateGraph: ...


@dataclass(frozen=True, slots=True)
class RuntimePorts:
    attempt_kernel: AttemptKernelPort
    secret_resolver: SecretResolverPort
    workspace_provider: WorkspaceProviderPort


@dataclass(frozen=True, slots=True)
class BootRequest:
    product_factory: ProductFactoryRef
    feature_factories: tuple[FeatureFactoryRef, ...]
    sources: Mapping[str, object]
    product_lock: ProductLock
    organization_root: Path | None = None
    expected_manifest: GraphBuildManifest | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.product_factory, ProductFactoryRef):
            raise TypeError("product factory must be a ProductFactoryRef")
        if not isinstance(self.product_lock, ProductLock):
            raise TypeError("product lock must be a ProductLock")
        refs = tuple(self.feature_factories)
        owners = tuple(ref.owner_id for ref in refs)
        if any(not owner for owner in owners):
            raise ValueError("feature factory owner id must be nonempty")
        if len(owners) != len(set(owners)):
            raise ValueError("feature factory owners must be unique")
        object.__setattr__(self, "feature_factories", refs)
        object.__setattr__(self, "sources", MappingProxyType(dict(self.sources)))


@dataclass(frozen=True, slots=True)
class BoundAttemptNode:
    contract_id: str
    semantic_node_id: str
    owner_id: str


@dataclass(frozen=True, slots=True)
class _AssembledGraphs:
    manifest: GraphBuildManifest
    entrypoints: Mapping[str, CompiledStateGraph]


class EngineCapabilityBuildContext:
    def __init__(
        self,
        *,
        owner_id: str,
        contracts: Mapping[str, TaskAttemptContract[Any, Any]],
        approved_source_roots: tuple[Path, ...],
        attempt_factory: AttemptNodeFactory | None = None,
        resolved_contracts: Mapping[str, ResolvedAttemptContract[Any, Any]] | None = None,
    ) -> None:
        if not owner_id:
            raise ValueError("capability owner id must be nonempty")
        self.owner_id = owner_id
        self._contracts = contracts
        self._approved_source_roots = approved_source_roots
        self._attempt_factory = attempt_factory
        self._resolved_contracts = resolved_contracts or {}

    def attempt(
        self,
        contract_id: str,
        *,
        semantic_node_id: str,
        activation: object,
        select: object,
        publish: object,
    ) -> object:
        contract = self._contracts.get(contract_id)
        if contract is None or contract.owner_id != self.owner_id:
            raise ContractOwnershipError(f"contract {contract_id!r} is not owned by {self.owner_id!r}")
        if isinstance(contract, ResolvedAttemptContract):
            raise BootValidationError("resolved executor-bearing contracts cannot enter Feature graph code")
        if self._attempt_factory is None:
            del activation, select, publish
            return BoundAttemptNode(
                contract_id=contract.contract_id,
                semantic_node_id=semantic_node_id,
                owner_id=self.owner_id,
            )
        bound = self._resolved_contracts.get(contract_id, contract)
        return self._attempt_factory.attempt(
            bound,
            semantic_node_id=semantic_node_id,
            activation=activation,
            select=select,
            publish=publish,
        )

    def compile_subgraph(self, builder: StateGraph[Any]) -> CompiledStateGraph:
        return builder.compile(checkpointer=None)

    def read_source(self, path: Path) -> bytes:
        return _read_approved_source(path, self._approved_source_roots)

    def __getattr__(self, name: str) -> object:
        if name in {"compile_root", "checkpointer", "_checkpointer"}:
            raise FactorySourcePolicyError("feature context cannot access the root saver")
        raise AttributeError(name)


class EngineGraphBuildContext:
    """Product build context. Only ``compile_root`` installs the checkpointer."""

    product_context = True

    def __init__(
        self,
        *,
        contracts: Mapping[str, TaskAttemptContract[Any, Any]],
        checkpointer: Checkpointer,
        approved_source_roots: tuple[Path, ...],
        attempt_factory: AttemptNodeFactory | None = None,
        resolved_contracts: Mapping[str, ResolvedAttemptContract[Any, Any]] | None = None,
    ) -> None:
        self._contracts = contracts
        self._checkpointer = checkpointer
        self._approved_source_roots = approved_source_roots
        self._attempt_factory = attempt_factory
        self._resolved_contracts = resolved_contracts or {}

    def for_capability(self, owner_id: str) -> CapabilityBuildContext:
        return EngineCapabilityBuildContext(
            owner_id=owner_id,
            contracts=self._contracts,
            approved_source_roots=self._approved_source_roots,
            attempt_factory=self._attempt_factory,
            resolved_contracts=self._resolved_contracts,
        )

    def compile_root(self, builder: StateGraph[Any]) -> CompiledStateGraph:
        return builder.compile(checkpointer=self._checkpointer)

    def compile_subgraph(self, builder: StateGraph[Any]) -> CompiledStateGraph:
        return builder.compile(checkpointer=None)

    def read_source(self, path: Path) -> bytes:
        return _read_approved_source(path, self._approved_source_roots)


class GraphEngineBoot:
    def __init__(
        self,
        *,
        authenticator: SourceAuthenticator,
        contract_resolver: ContractResolverPort,
    ) -> None:
        self._authenticator = authenticator
        self._resolver = contract_resolver

    def compile_manifest(self, request: BootRequest) -> GraphBuildManifest:
        return self._assemble(request, checkpointer=None).manifest

    def boot(
        self,
        request: BootRequest,
        checkpointer: Checkpointer,
        runtime_ports: RuntimePorts,
    ) -> BootArtifact:
        if runtime_ports is None:
            raise BootValidationError("runtime ports are required")
        identity = getattr(checkpointer, "_identity", None)
        token = getattr(identity, "fencing_token", None)
        if token is not None and (not isinstance(token, int) or isinstance(token, bool) or token < 1):
            raise BootValidationError("fencing token")
        assembled = self._assemble(request, checkpointer=checkpointer, runtime_ports=runtime_ports)
        if request.expected_manifest is not None and assembled.manifest != request.expected_manifest:
            raise BootValidationError("revision mismatch")
        backend_id = getattr(checkpointer, "backend_id", "")
        if not isinstance(backend_id, str) or not backend_id:
            raise BootValidationError("checkpointer backend id must be nonempty")
        return BootArtifact(
            manifest=assembled.manifest,
            entrypoints=assembled.entrypoints,
            attempt_contracts=self._resolver.resolve_executors(),
            checkpointer_backend_id=backend_id,
        )

    def _assemble(
        self,
        request: BootRequest,
        *,
        checkpointer: Checkpointer,
        runtime_ports: RuntimePorts | None = None,
    ) -> _AssembledGraphs:
        _reject_organization_overrides(request.organization_root)
        refs = _factory_refs(request)
        sources = _authenticated_sources(request, refs)
        for source in sources.values():
            _assert_source_bytes(source)
        imported = {_owner(ref): self._authenticator.authenticate(ref, request.sources) for ref in refs}
        for source in sources.values():
            _assert_source_bytes(source)
        _assert_imported_source_digests(imported, sources)
        data_contracts = dict(self._resolver.resolve_data_contracts())
        _assert_data_only_contracts(data_contracts)
        approved_roots = tuple(source.snapshot.identity.root for source in sources.values())
        resolved_contracts: dict[str, ResolvedAttemptContract[Any, Any]] = {}
        kernel = None
        factory = None
        if runtime_ports is not None:
            kernel = runtime_ports.attempt_kernel
            if isinstance(kernel, AssuranceAttemptKernel):
                factory = bind_attempt_factory(kernel)
            for contract_id, resolved in self._resolver.resolve_executors().items():
                if isinstance(resolved, ResolvedAttemptContract):
                    resolved_contracts[str(contract_id)] = resolved
        context = EngineGraphBuildContext(
            contracts=data_contracts,
            checkpointer=checkpointer,
            approved_source_roots=approved_roots,
            attempt_factory=factory,
            resolved_contracts=resolved_contracts,
        )
        features: dict[str, object] = {}
        for ref in request.feature_factories:
            features[ref.owner_id] = _invoke_factory(imported[ref.owner_id].factory, context)
        product = _invoke_factory(
            imported[_owner(request.product_factory)].factory,
            context,
            features,
        )
        entrypoints, contracts = _product_bundle(product)
        _assert_root_parity(entrypoints, contracts)
        attempt_contract_digests = {
            contract_id: canonical_digest(contract.canonical_projection())
            for contract_id, contract in data_contracts.items()
        }
        entrypoint_contract_digests = {
            name: canonical_digest(contract.canonical_projection()) for name, contract in contracts.items()
        }
        revision = GraphRevision.build(
            product_lock_digest=request.product_lock.digest,
            wheel_source_digests={owner_id: sources[owner_id].snapshot.digest for owner_id in sources},
            factory_symbols=tuple(ref.symbol for ref in refs),
            state_schema_versions={
                name: contract.state_schema_version for name, contract in contracts.items()
            },
            langgraph_version=metadata.version("langgraph"),
            checkpoint_contract_version=CHECKPOINT_CONTRACT_VERSION,
        )
        manifest = GraphBuildManifest(
            revision=revision,
            entrypoint_contract_digests=entrypoint_contract_digests,
            attempt_contract_digests=attempt_contract_digests,
        )
        return _AssembledGraphs(manifest=manifest, entrypoints=entrypoints)


def _factory_refs(request: BootRequest) -> tuple[ProductFactoryRef | FeatureFactoryRef, ...]:
    return (request.product_factory, *request.feature_factories)


def _owner(ref: FeatureFactoryRef | ProductFactoryRef) -> str:
    return ref.owner_id if isinstance(ref, FeatureFactoryRef) else ref.product_id


def _authenticated_sources(
    request: BootRequest,
    refs: tuple[ProductFactoryRef | FeatureFactoryRef, ...],
) -> dict[str, AuthenticatedFactorySource]:
    sources: dict[str, AuthenticatedFactorySource] = {}
    for ref in refs:
        owner_id = _owner(ref)
        raw = request.sources.get(owner_id)
        if not isinstance(raw, AuthenticatedFactorySource) or raw.owner_id != owner_id:
            raise BootValidationError(f"no authenticated source for factory owner: {owner_id}")
        sources[owner_id] = raw
    return sources


def _assert_source_bytes(source: AuthenticatedFactorySource) -> None:
    rebuilt = recapture_source_snapshot(source.snapshot, source.source_files)
    if rebuilt.digest != source.snapshot.digest:
        raise BootValidationError("source drift")


def _assert_imported_source_digests(
    imported: Mapping[str, AuthenticatedFactory],
    sources: Mapping[str, AuthenticatedFactorySource],
) -> None:
    for owner_id, authenticated in imported.items():
        expected = sources[owner_id].snapshot.digest
        if authenticated.source_digest != expected:
            raise BootValidationError("source drift")


def _assert_data_only_contracts(contracts: Mapping[str, object]) -> None:
    for contract in contracts.values():
        if isinstance(contract, ResolvedAttemptContract):
            raise BootValidationError("resolved executor-bearing contracts cannot enter Feature graph code")
        if not isinstance(contract, TaskAttemptContract):
            raise BootValidationError("contract closure must be data-only TaskAttemptContract values")


def _product_bundle(
    product: object,
) -> tuple[Mapping[str, CompiledStateGraph], Mapping[str, EntrypointGraphContract]]:
    entrypoints = getattr(product, "entrypoints", None)
    contracts = getattr(product, "contracts", None)
    if not isinstance(entrypoints, Mapping) or not isinstance(contracts, Mapping):
        raise BootValidationError("product factory must return entrypoints and contracts")
    typed_entrypoints = {str(name): graph for name, graph in entrypoints.items()}
    typed_contracts: dict[str, EntrypointGraphContract] = {}
    for name, contract in contracts.items():
        if not isinstance(contract, EntrypointGraphContract):
            raise BootValidationError("product factory contracts must be EntrypointGraphContract values")
        typed_contracts[str(name)] = contract
    return typed_entrypoints, typed_contracts


def _assert_root_parity(
    entrypoints: Mapping[str, CompiledStateGraph],
    contracts: Mapping[str, EntrypointGraphContract],
) -> None:
    extra = set(entrypoints) - set(contracts)
    missing = set(contracts) - set(entrypoints)
    if extra or missing:
        raise BootValidationError(f"extra or missing roots: extra={sorted(extra)} missing={sorted(missing)}")
    if not entrypoints:
        raise BootValidationError("product factory returned no roots")
    for name, contract in contracts.items():
        if contract.name != name:
            raise BootValidationError(f"entrypoint contract name drift: {name}")


def _invoke_factory(
    factory: object,
    context: GraphBuildContext,
    features: Mapping[str, object] | None = None,
) -> object:
    if not callable(factory):
        raise BootValidationError("authenticated factory is not callable")
    try:
        signature = inspect.signature(factory)
    except (TypeError, ValueError):
        return factory(context) if features is None else factory(context, features)
    accepts_features = "features" in signature.parameters
    positional = [
        parameter
        for parameter in signature.parameters.values()
        if parameter.kind in {inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD}
    ]
    if accepts_features:
        if not positional:
            return factory(context=context, features=features or {})
        return factory(context, features or {})
    if not positional:
        return factory(context=context)
    return factory(context)


def _reject_organization_overrides(root: Path | None) -> None:
    if root is None:
        return
    aa_root = root / ".aa"
    if not aa_root.is_dir():
        return
    for path in aa_root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(aa_root)
        if path.name in _REJECTED_AA_NAMES or "graphs" in relative.parts:
            raise OrganizationOverrideError(
                f"organization .aa/ may not override topology, factory, or module: {path}"
            )


def bind_attempt_factory(kernel: object | None, *, regenerate: bool = False) -> AttemptNodeFactory | None:
    if kernel is None:
        return None
    if isinstance(kernel, AssuranceAttemptKernel):
        return AttemptNodeFactory(checkpoints=kernel.checkpoints, kernel=kernel, regenerate=regenerate)
    raise BootValidationError("attempt kernel must expose its checkpoints")


def _read_approved_source(path: Path, approved_source_roots: tuple[Path, ...]) -> bytes:
    resolved = path.expanduser()
    if not resolved.is_absolute():
        resolved = resolved.resolve()
    else:
        resolved = resolved.resolve()
    for root in approved_source_roots:
        try:
            if resolved.is_relative_to(root.resolve()) and resolved.is_file():
                return resolved.read_bytes()
        except (OSError, ValueError):
            continue
    raise FactorySourcePolicyError(f"unapproved source read: {path}")


__all__ = [
    "BootRequest",
    "BootValidationError",
    "BoundAttemptNode",
    "CapabilityBuildContext",
    "ContractOwnershipError",
    "ContractResolverPort",
    "EngineCapabilityBuildContext",
    "EngineGraphBuildContext",
    "FactorySourcePolicyError",
    "GraphBuildContext",
    "GraphEngineBoot",
    "OrganizationOverrideError",
    "RuntimePorts",
    "SourceAuthenticator",
    "bind_attempt_factory",
]
