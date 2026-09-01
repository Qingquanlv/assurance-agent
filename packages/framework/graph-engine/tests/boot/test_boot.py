from __future__ import annotations

import importlib.util
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest
from pydantic import BaseModel

from assurance_product.graph_factories import FEATURE_GRAPH_FACTORIES
from graph_engine.attempts.contracts import (
    AttemptRetryPolicy,
    AttemptTimeoutPolicy,
    TaskAttemptContract,
)
from graph_engine.attempts.keys import BusinessActivation
from graph_engine.boot.boot import (
    BootRequest,
    ContractOwnershipError,
    ContractResolverPort,
    GraphBuildContext,
    GraphEngineBoot,
    RuntimePorts,
    SourceAuthenticator,
)
from graph_engine.boot.graph_revision import FeatureFactoryRef
from graph_engine.boot.source_authentication import (
    AuthenticatedFactorySource,
    ProductFactoryRef,
    authenticate_factory_ref,
)
from graph_engine.canonical import canonical_digest
from graph_engine.composition.lock import ProductLock
from graph_engine.composition.source_fs import DeclaredTreePolicy, capture_declared_tree
from graph_engine.persistence.anchored_checkpointer import AnchoredCheckpointer
from graph_engine.plugin_api import ProviderSource, ResourceClaims
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import StateGraph


EXPECTED_ENTRYPOINTS = {
    "archive",
    "case",
    "execute",
    "full",
    "improvement-apply",
    "improvement-evaluate",
    "improvement-export",
    "improvement-review",
    "improvement-rollback",
    "intake",
    "issue-analyze",
    "issue-reconcile",
    "issue-review",
    "retro",
}

PRODUCT_FACTORY = ProductFactoryRef(
    "assurance.product",
    "assurance_product.graphs.factory:build_product_graphs",
)

_ENTRYPOINT_NAMES = tuple(sorted(EXPECTED_ENTRYPOINTS))

_PRODUCT_FACTORY_SOURCE = f"""\
from typing import TypedDict

from graph_engine.boot.graph_revision import EntrypointGraphContract
from graph_engine.canonical import canonical_digest
from langgraph.graph import END, START, StateGraph

ENTRYPOINTS = {list(_ENTRYPOINT_NAMES)!r}


class RootState(TypedDict):
    marker: str


def _noop(state: RootState) -> RootState:
    return state


def build_product_graphs(context, features=None):
    entrypoints = {{}}
    contracts = {{}}
    for name in ENTRYPOINTS:
        builder = StateGraph(RootState)
        builder.add_node("noop", _noop)
        builder.add_edge(START, "noop")
        builder.add_edge("noop", END)
        entrypoints[name] = context.compile_root(builder)
        contracts[name] = EntrypointGraphContract(
            name=name,
            input_model="graph_engine.boot.tests.Input",
            output_model="graph_engine.boot.tests.Output",
            state_model="graph_engine.boot.tests.State",
            input_schema_digest=canonical_digest({{"entrypoint": name, "side": "input"}}),
            output_schema_digest=canonical_digest({{"entrypoint": name, "side": "output"}}),
            state_schema_digest=canonical_digest({{"entrypoint": name, "side": "state"}}),
            state_schema_version="1",
            recursion_limit=2048,
        )
    return type("ProductGraphs", (), {{"entrypoints": entrypoints, "contracts": contracts}})()
"""

_HOSTILE_PRODUCT_FACTORY_SOURCE = """\
from pathlib import Path

def build_product_graphs(context, features=None):
    return context.read_source(Path("/tmp/assurance-sut-unapproved.py"))
"""

_INCOMPLETE_PRODUCT_FACTORY_SOURCE = """\
from typing import TypedDict

from graph_engine.boot.graph_revision import EntrypointGraphContract
from graph_engine.canonical import canonical_digest
from langgraph.graph import END, START, StateGraph

ENTRYPOINTS = ["archive", "case", "execute", "full"]


class RootState(TypedDict):
    marker: str


def _noop(state: RootState) -> RootState:
    return state


def build_product_graphs(context, features=None):
    entrypoints = {}
    contracts = {}
    for name in ENTRYPOINTS:
        contracts[name] = EntrypointGraphContract(
            name=name,
            input_model="graph_engine.boot.tests.Input",
            output_model="graph_engine.boot.tests.Output",
            state_model="graph_engine.boot.tests.State",
            input_schema_digest=canonical_digest({"entrypoint": name, "side": "input"}),
            output_schema_digest=canonical_digest({"entrypoint": name, "side": "output"}),
            state_schema_digest=canonical_digest({"entrypoint": name, "side": "state"}),
            state_schema_version="1",
            recursion_limit=2048,
        )
        if name == "full":
            continue
        builder = StateGraph(RootState)
        builder.add_node("noop", _noop)
        builder.add_edge(START, "noop")
        builder.add_edge("noop", END)
        entrypoints[name] = context.compile_root(builder)
    return type("ProductGraphs", (), {"entrypoints": entrypoints, "contracts": contracts})()
"""


def _package_for(owner_id: str) -> str:
    return owner_id.replace(".", "_")


def _attribute_for(owner_id: str) -> str:
    return f"build_{owner_id.rsplit('.', 1)[-1]}_graphs"


def authenticated_factory_source(
    *,
    owner_id: str,
    root: Path,
    import_roots: tuple[str, ...],
    factory_source: str,
) -> AuthenticatedFactorySource:
    package = import_roots[0]
    files = {
        f"{package}/{package}/__init__.py": b"",
        f"{package}/{package}/graphs/__init__.py": b"",
        f"{package}/{package}/graphs/factory.py": factory_source.encode("utf-8"),
    }
    root.mkdir(parents=True, exist_ok=True)
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    policy = (
        DeclaredTreePolicy.editable_product()
        if owner_id.endswith(".product")
        else DeclaredTreePolicy.editable()
    )
    snapshot = capture_declared_tree(root, tuple(files), policy)
    group = "graph_engine.products" if owner_id.endswith(".product") else "graph_engine.plugins"
    attribute = _attribute_for(owner_id)
    provider_source = ProviderSource(
        distribution=owner_id.replace(".", "-"),
        version="1.0.0",
        entrypoint_group=group,
        entrypoint_name=owner_id,
        entrypoint_value=f"{package}.graphs.factory:{attribute}",
        declaration_path=f"{package}/{package}/__init__.py",
        import_roots=import_roots,
    )
    return AuthenticatedFactorySource(
        owner_id=owner_id,
        snapshot=snapshot,
        provider_source=provider_source,
        source_files=tuple(sorted(files)),
    )


def feature_factory_source(owner_id: str) -> str:
    return (
        f"def {_attribute_for(owner_id)}(context):\n"
        f"    scoped = context.for_capability({owner_id!r})\n"
        f"    return {{'owner_id': scoped.owner_id}}\n"
    )


def write_boot_sources(
    root: Path,
    *,
    product_factory_source: str = _PRODUCT_FACTORY_SOURCE,
) -> dict[str, AuthenticatedFactorySource]:
    sources: dict[str, AuthenticatedFactorySource] = {
        "assurance.product": authenticated_factory_source(
            owner_id="assurance.product",
            root=root / "product",
            import_roots=("assurance_product",),
            factory_source=product_factory_source,
        )
    }
    for ref in FEATURE_GRAPH_FACTORIES:
        package = _package_for(ref.owner_id)
        sources[ref.owner_id] = authenticated_factory_source(
            owner_id=ref.owner_id,
            root=root / package,
            import_roots=(package,),
            factory_source=feature_factory_source(ref.owner_id),
        )
    return sources


def product_lock() -> ProductLock:
    path = Path(__file__).resolve().parents[1] / "composition" / "test_lock_model.py"
    spec = importlib.util.spec_from_file_location("graph_engine_lock_model_helpers", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module._product_lock()


def product_lock_with_configuration(configuration: Mapping[str, object]) -> ProductLock:
    lock = product_lock()
    payload = dict(configuration)
    return ProductLock.create(
        engine_api=lock.engine_api,
        engine=lock.engine,
        engine_digest=lock.engine_digest,
        product=lock.product,
        plugins=lock.plugins,
        dependency_order=lock.dependency_order,
        registry_projections=lock.registry_projections,
        registry_digests=lock.registry_digests,
        configuration=payload,
        configuration_digest=canonical_digest(payload),
        capability_bindings=lock.capability_bindings,
        capability_bindings_digest=lock.capability_bindings_digest,
    )


class ProbeInput(BaseModel):
    change_id: str


class ProbeOutput(BaseModel):
    status: str


def _contract(
    *,
    contract_id: str,
    owner_id: str,
    handler_id: str,
) -> TaskAttemptContract[ProbeInput, ProbeOutput]:
    return TaskAttemptContract(
        contract_id=contract_id,
        owner_id=owner_id,
        handler_id=handler_id,
        input_model=ProbeInput,
        output_model=ProbeOutput,
        resources=ResourceClaims(),
        retry=AttemptRetryPolicy(max_attempts=1),
        timeout=AttemptTimeoutPolicy(seconds=30),
        validators=(),
    )


class FakeContractResolver:
    def __init__(self, contracts: Mapping[str, TaskAttemptContract[Any, Any]] | None = None) -> None:
        catalog = (
            dict(contracts)
            if contracts is not None
            else {
                "assurance.intake.agent.prepare.v1": _contract(
                    contract_id="assurance.intake.agent.prepare.v1",
                    owner_id="assurance.intake",
                    handler_id="assurance.intake.prepare",
                ),
                "assurance.generation.agent.api.plan.v1": _contract(
                    contract_id="assurance.generation.agent.api.plan.v1",
                    owner_id="assurance.generation",
                    handler_id="assurance.generation.plan",
                ),
            }
        )
        self._contracts = catalog

    def resolve_data_contracts(self) -> Mapping[str, TaskAttemptContract[Any, Any]]:
        return MappingProxyType(self._contracts)

    def resolve_executors(self) -> Mapping[str, object]:
        return MappingProxyType({})


class FactoryAuthenticator:
    def authenticate(
        self,
        ref: FeatureFactoryRef | ProductFactoryRef,
        sources: Mapping[str, object],
    ) -> object:
        return authenticate_factory_ref(ref, sources)


class RecordingFactories:
    def __init__(self) -> None:
        self.observed_checkpointers: list[object] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        original = StateGraph.compile

        def tracked(graph: StateGraph[Any], checkpointer: object = None, **kwargs: object) -> object:
            self.observed_checkpointers.append(checkpointer)
            return original(graph, checkpointer=checkpointer, **kwargs)

        monkeypatch.setattr(StateGraph, "compile", tracked)


class _AnchoredSaver(InMemorySaver):
    backend_id = "memory"


def make_boot_request(
    tmp_path: Path,
    *,
    product_factory_source: str = _PRODUCT_FACTORY_SOURCE,
    lock: ProductLock | None = None,
    organization_root: Path | None = None,
) -> BootRequest:
    return BootRequest(
        product_factory=PRODUCT_FACTORY,
        feature_factories=FEATURE_GRAPH_FACTORIES,
        sources=write_boot_sources(tmp_path, product_factory_source=product_factory_source),
        product_lock=lock if lock is not None else product_lock(),
        organization_root=organization_root,
    )


def test_offline_compile_uses_no_saver_and_runtime_boot_matches_manifest(
    authenticator: SourceAuthenticator,
    resolver: ContractResolverPort,
    boot_request: BootRequest,
    factories: RecordingFactories,
    anchored_saver: AnchoredCheckpointer,
    ports: RuntimePorts,
) -> None:
    request = boot_request
    boot = GraphEngineBoot(authenticator=authenticator, contract_resolver=resolver)
    manifest = boot.compile_manifest(request)
    assert factories.observed_checkpointers == [None] * 14

    artifact = boot.boot(request, checkpointer=anchored_saver, runtime_ports=ports)
    assert artifact.manifest == manifest
    assert set(artifact.entrypoints) == EXPECTED_ENTRYPOINTS
    assert artifact.checkpointer_backend_id == anchored_saver.backend_id


def test_feature_context_rejects_foreign_contract(
    build_context: GraphBuildContext,
    select_probe: object,
    publish_probe: object,
) -> None:
    context = build_context.for_capability("assurance.intake")
    with pytest.raises(ContractOwnershipError):
        context.attempt(
            "assurance.generation.agent.api.plan.v1",
            semantic_node_id="intake.foreign-probe",
            activation=BusinessActivation.one_shot(),
            select=select_probe,
            publish=publish_probe,
        )


def test_extra_or_missing_roots_are_rejected(
    authenticator: SourceAuthenticator,
    resolver: ContractResolverPort,
    tmp_path: Path,
) -> None:
    from graph_engine.boot.boot import BootValidationError

    boot = GraphEngineBoot(authenticator=authenticator, contract_resolver=resolver)
    request = make_boot_request(tmp_path, product_factory_source=_INCOMPLETE_PRODUCT_FACTORY_SOURCE)
    with pytest.raises(BootValidationError, match="root"):
        boot.compile_manifest(request)


@pytest.fixture
def factories(monkeypatch: pytest.MonkeyPatch) -> RecordingFactories:
    recording = RecordingFactories()
    recording.install(monkeypatch)
    return recording


@pytest.fixture
def authenticator() -> SourceAuthenticator:
    return FactoryAuthenticator()


@pytest.fixture
def resolver() -> ContractResolverPort:
    return FakeContractResolver()


@pytest.fixture
def boot_request(tmp_path: Path) -> BootRequest:
    return make_boot_request(tmp_path)


@pytest.fixture
def anchored_saver() -> AnchoredCheckpointer:
    return _AnchoredSaver()  # type: ignore[return-value]


@pytest.fixture
def ports() -> RuntimePorts:
    return RuntimePorts(
        attempt_kernel=object(),
        secret_resolver=object(),
        workspace_provider=object(),
    )


@pytest.fixture
def build_context(resolver: ContractResolverPort) -> GraphBuildContext:
    from graph_engine.boot.boot import EngineGraphBuildContext

    return EngineGraphBuildContext(
        contracts=resolver.resolve_data_contracts(),
        checkpointer=None,
        approved_source_roots=(),
    )


@pytest.fixture
def select_probe() -> object:
    return object()


@pytest.fixture
def publish_probe() -> object:
    return object()
