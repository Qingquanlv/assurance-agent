from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from importlib import import_module, metadata
from pathlib import Path
import sys
from types import ModuleType

from graph_engine.boot.graph_revision import FeatureFactoryRef
from graph_engine.composition.import_plan import (
    ImportPlanSession,
    ImportProvenancePlan,
    ModuleImportPlan,
    build_import_provenance_plan,
    extend_import_plan_with_quarantine,
    extend_import_provenance_plan,
)
from graph_engine.composition.models import (
    SourceSnapshot,
    _module_belongs_to_import_roots,
)
from graph_engine.composition.source_fs import DeclaredTreePolicy, SourceSnapshotError, capture_declared_tree
from graph_engine.composition.sources import (
    _capture_parent_attributes,
    _preloaded_physical_authority,
    _restore_import_transaction,
    _serialized_imports,
)
from graph_engine.errors import GraphEngineError
from graph_engine.plugin_api import ProviderSource


class FactoryAuthenticationError(GraphEngineError):
    """Raised when a graph factory symbol cannot be authenticated."""


@dataclass(frozen=True, slots=True)
class ProductFactoryRef:
    product_id: str
    symbol: str

    def __post_init__(self) -> None:
        if not self.product_id:
            raise ValueError("product factory product id must be nonempty")
        if not self.symbol:
            raise ValueError("product factory symbol must be nonempty")


@dataclass(frozen=True, slots=True)
class AuthenticatedFactorySource:
    owner_id: str
    snapshot: SourceSnapshot
    provider_source: ProviderSource
    source_files: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AuthenticatedFactory:
    owner_id: str
    symbol: str
    factory: object
    source_digest: str


FactoryRef = FeatureFactoryRef | ProductFactoryRef


def authenticated_editable_source(
    *,
    owner_id: str,
    root: Path,
    import_roots: tuple[str, ...],
) -> AuthenticatedFactorySource:
    if not owner_id or not import_roots:
        raise FactoryAuthenticationError("authenticated editable source requires owner and import roots")
    package = import_roots[0]
    attribute = f"build_{owner_id.rsplit('.', 1)[-1]}_graphs"
    files = {
        f"{package}/{package}/__init__.py": b"",
        f"{package}/{package}/graphs/__init__.py": b"",
        f"{package}/{package}/graphs/factory.py": (
            f"def {attribute}() -> dict[str, str]:\n"
            f"    return {{'owner_id': {owner_id!r}}}\n"
            "\n"
            "def _private() -> None:\n"
            "    return None\n"
        ).encode("utf-8"),
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


def authenticate_factory_ref(
    ref: FactoryRef,
    sources: Mapping[str, object],
) -> AuthenticatedFactory:
    owner_id = ref.owner_id if isinstance(ref, FeatureFactoryRef) else ref.product_id
    raw_source = sources.get(owner_id)
    if not isinstance(raw_source, AuthenticatedFactorySource) or raw_source.owner_id != owner_id:
        raise FactoryAuthenticationError(f"no authenticated source for factory owner: {owner_id}")
    module_name, attribute = _split_public_symbol(ref.symbol)
    if not _module_belongs_to_import_roots(module_name, raw_source.provider_source.import_roots):
        raise FactoryAuthenticationError(
            f"factory module is outside the authenticated owner import roots: {module_name}"
        )
    provider_source = raw_source.provider_source.model_copy(update={"entrypoint_value": ref.symbol})
    try:
        factory = _import_authenticated_factory(
            provider_source,
            raw_source.snapshot,
            raw_source.source_files,
            module_name,
            attribute,
        )
    except FactoryAuthenticationError:
        raise
    except (SourceSnapshotError, ValueError, TypeError) as error:
        raise FactoryAuthenticationError(str(error)) from error
    return AuthenticatedFactory(
        owner_id=owner_id,
        symbol=ref.symbol,
        factory=factory,
        source_digest=raw_source.snapshot.digest,
    )


def _split_public_symbol(symbol: str) -> tuple[str, str]:
    if symbol.count(":") != 1:
        raise FactoryAuthenticationError("factory symbol must be module:attribute")
    module_name, attribute = symbol.split(":")
    if (
        not module_name
        or module_name.startswith(".")
        or any(not part.isidentifier() for part in module_name.split("."))
    ):
        raise FactoryAuthenticationError("factory module is not a public absolute import")
    if not attribute.isidentifier() or attribute.startswith("_"):
        raise FactoryAuthenticationError("factory attribute must be a public name")
    return module_name, attribute


def _import_authenticated_factory(
    provider_source: ProviderSource,
    snapshot: SourceSnapshot,
    source_files: tuple[str, ...],
    module_name: str,
    attribute: str,
) -> object:
    entrypoint = metadata.EntryPoint(
        name=provider_source.entrypoint_name,
        value=provider_source.entrypoint_value,
        group=provider_source.entrypoint_group,
    )
    with _serialized_imports():
        before_modules = dict(sys.modules)
        parent_attributes = _capture_parent_attributes(before_modules)
        import_plan = build_import_provenance_plan(
            provider_source,
            snapshot,
            entrypoint,
            initial_modules=before_modules,
        )
        _evict_mismatched_preloads(import_plan)
        import_plan = build_import_provenance_plan(
            provider_source,
            snapshot,
            entrypoint,
            initial_modules=dict(sys.modules),
        )
        import_plan = extend_import_plan_with_quarantine(
            import_plan,
            provider_source,
            snapshot,
            dict(sys.modules),
        )
        try:
            with ImportPlanSession(provider_source, snapshot, dict(sys.modules)) as session:
                session.quarantine(import_plan)
                session.preload(import_plan, _factory_module_authority)
                loaded = import_module(module_name)
                import_plan = extend_import_provenance_plan(
                    import_plan,
                    provider_source,
                    snapshot,
                    session.post_quarantine_initial_modules,
                    provider_module=_factory_module_name(loaded),
                    dependency_provenances=session.recorded_provenances,
                )
                session.preload(import_plan, _factory_module_authority)
                session.validate(import_plan, _factory_module_authority)
                _revalidate_source_bytes(snapshot, source_files)
                session.restore_unconsumed_quarantine(import_plan)
            factory = getattr(loaded, attribute, None)
            if not callable(factory) or getattr(factory, "__name__", None) != attribute:
                raise FactoryAuthenticationError(
                    f"authenticated factory attribute is not the public callable {attribute}"
                )
        except BaseException as primary_error:
            _restore_import_transaction(
                before_modules,
                parent_attributes,
                import_plan,
                primary_error,
            )
            raise
        _restore_import_transaction(
            before_modules,
            parent_attributes,
            import_plan,
            FactoryAuthenticationError("factory import session complete"),
        )
        return factory


def _evict_mismatched_preloads(plan: ImportProvenancePlan) -> None:
    for item in plan.modules:
        live = sys.modules.get(item.module_name)
        if not isinstance(live, ModuleType):
            continue
        if _factory_module_authority(item, live):
            continue
        sys.modules.pop(item.module_name, None)


def _factory_module_authority(item: ModuleImportPlan, module: ModuleType) -> bool:
    return _preloaded_physical_authority(item, module)


def _factory_module_name(loaded: object) -> str:
    if isinstance(loaded, ModuleType):
        return loaded.__name__
    module_name = getattr(loaded, "__module__", None)
    if not isinstance(module_name, str) or not module_name:
        raise FactoryAuthenticationError("loaded factory has no verifiable module origin")
    return module_name


def _revalidate_source_bytes(snapshot: SourceSnapshot, source_files: tuple[str, ...]) -> None:
    policy = DeclaredTreePolicy(kind=snapshot.identity.kind)
    after = capture_declared_tree(snapshot.identity.root, source_files, policy)
    if after.digest != snapshot.digest:
        raise FactoryAuthenticationError("factory source changed while authenticating its symbol")
