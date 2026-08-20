from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass, replace
from enum import Enum
import hashlib
from importlib import import_module, metadata
from importlib.abc import Loader, MetaPathFinder
from importlib.machinery import (
    EXTENSION_SUFFIXES,
    ExtensionFileLoader,
    ModuleSpec,
    NamespaceLoader,
    PathFinder,
    SourceFileLoader,
)
from pathlib import Path
import sys
from types import ModuleType
from typing import Any, cast

from graph_engine.composition.models import SourceSnapshot
from graph_engine.composition.source_fs import SourceSnapshotError
from graph_engine.plugin_api import ProviderSource


class ModuleRole(str, Enum):
    ANCESTOR = "ancestor"
    AUTHORIZED = "authorized"
    DEPENDENCY = "dependency"
    ENTRYPOINT = "entrypoint"
    PROVIDER = "provider"
    QUARANTINE = "quarantine"


class StandardLoader(str, Enum):
    SOURCE = "source"
    NAMESPACE = "namespace"
    EXTENSION = "extension"


@dataclass(frozen=True, slots=True)
class ModuleProvenance:
    """One canonical source/import proof consumed throughout a binding lifetime."""

    standard_loader: StandardLoader
    standard_is_package: bool
    canonical_origin: Path | None
    canonical_locations: tuple[Path, ...]
    authenticated_locations: tuple[Path, ...]
    physical_sha256: str | None
    source_digest: str

    def authenticates_same_module(self, other: ModuleProvenance) -> bool:
        """Compare physical standard-import authority across selected source identities."""
        return (
            self.standard_loader is other.standard_loader
            and self.standard_is_package == other.standard_is_package
            and self.canonical_origin == other.canonical_origin
            and self.canonical_locations == other.canonical_locations
            and self.physical_sha256 == other.physical_sha256
        )


@dataclass(frozen=True, slots=True)
class ModuleImportPlan:
    module_name: str
    roles: tuple[ModuleRole, ...]
    provenance: ModuleProvenance
    initial_module: ModuleType | None
    preload: bool = True
    validate: bool = True
    commit: bool = True
    rollback: bool = True


@dataclass(frozen=True, slots=True)
class ImportProvenancePlan:
    source_digest: str
    entrypoint_value: str
    modules: tuple[ModuleImportPlan, ...]

    def module(self, module_name: str) -> ModuleImportPlan:
        try:
            return next(item for item in self.modules if item.module_name == module_name)
        except StopIteration as error:
            raise KeyError(module_name) from error


ModuleAuthority = Callable[[ModuleImportPlan, ModuleType], bool]
_MISSING = object()


class _ModuleOutsideAuthenticatedSource(SourceSnapshotError):
    """A standard import name that the selected source does not claim."""


class ImportPlanSession(AbstractContextManager["ImportPlanSession"]):
    """Serialized standard-import proof collector for one authenticated call."""

    def __init__(
        self,
        source: ProviderSource,
        snapshot: SourceSnapshot,
        initial_modules: Mapping[str, ModuleType],
    ) -> None:
        self._source = source
        self._snapshot = snapshot
        self._initial_modules = dict(initial_modules)
        self._recorded: dict[str, tuple[ModuleType, ModuleProvenance]] = {}
        self._finder = _RecordingFinder(source, snapshot, self._recorded)
        self._entered = False
        self._prior_dont_write_bytecode: bool | None = None
        self._quarantined: dict[str, tuple[ModuleType, ModuleType | None, str, object]] = {}

    def __enter__(self) -> ImportPlanSession:
        if self._entered:
            raise SourceSnapshotError("import plan session cannot be entered twice")
        self._entered = True
        self._prior_dont_write_bytecode = sys.dont_write_bytecode
        sys.dont_write_bytecode = True
        sys.meta_path.insert(0, self._finder)
        return self

    def __exit__(self, *_exc: object) -> None:
        if self._finder in sys.meta_path:
            sys.meta_path.remove(self._finder)
        if self._prior_dont_write_bytecode is not None:
            sys.dont_write_bytecode = self._prior_dont_write_bytecode
            self._prior_dont_write_bytecode = None
        self._entered = False

    @property
    def recorded_provenances(self) -> tuple[tuple[str, ModuleProvenance], ...]:
        return tuple((name, self._recorded[name][1]) for name in sorted(self._recorded))

    @property
    def post_quarantine_initial_modules(self) -> Mapping[str, ModuleType]:
        return {
            name: module for name, module in self._initial_modules.items() if name not in self._quarantined
        }

    def quarantine(self, plan: ImportProvenancePlan) -> None:
        self._require_plan(plan)
        for item in plan.modules:
            if ModuleRole.QUARANTINE not in item.roles or item.validate:
                continue
            current = sys.modules.get(item.module_name)
            if current is not item.initial_module or not isinstance(current, ModuleType):
                raise SourceSnapshotError(
                    f"quarantined provider module changed before binding: {item.module_name}"
                )
            parent_name, separator, child_name = item.module_name.rpartition(".")
            parent = sys.modules.get(parent_name) if separator else None
            prior_attribute = _MISSING
            if isinstance(parent, ModuleType):
                prior_attribute = parent.__dict__.get(child_name, _MISSING)
                if prior_attribute is current:
                    parent.__dict__.pop(child_name, None)
            self._quarantined[item.module_name] = (
                current,
                parent if isinstance(parent, ModuleType) else None,
                child_name,
                prior_attribute,
            )
            del sys.modules[item.module_name]

    def restore_unconsumed_quarantine(self, plan: ImportProvenancePlan) -> None:
        self._require_plan(plan)
        active_names = {item.module_name for item in plan.modules if item.validate}
        for module_name in sorted(self._quarantined, key=lambda name: (name.count("."), name)):
            if module_name in active_names:
                continue
            module, parent, child_name, prior_attribute = self._quarantined[module_name]
            if module_name in sys.modules:
                raise SourceSnapshotError(
                    f"unconsumed quarantined module was unexpectedly imported: {module_name}"
                )
            sys.modules[module_name] = module
            if parent is not None:
                if prior_attribute is _MISSING:
                    parent.__dict__.pop(child_name, None)
                else:
                    parent.__dict__[child_name] = prior_attribute

    def preload(
        self,
        plan: ImportProvenancePlan,
        authority: ModuleAuthority,
    ) -> None:
        self._require_plan(plan)
        self._finder.expect(plan)
        for item in plan.modules:
            if not item.preload:
                continue
            current = sys.modules.get(item.module_name)
            if item.initial_module is not None:
                if current is not item.initial_module or not authority(item, item.initial_module):
                    raise SourceSnapshotError(
                        "preloaded provider module is not platform-authenticated or has a different "
                        f"authenticated source: {item.module_name}"
                    )
                continue
            if current is None:
                try:
                    import_module(item.module_name)
                except Exception as error:
                    raise SourceSnapshotError(
                        f"cannot standard-import planned provider module: {item.module_name}"
                    ) from error
                current = sys.modules.get(item.module_name)
            recorded = self._recorded.get(item.module_name)
            if (
                not isinstance(current, ModuleType)
                or recorded is None
                or recorded[0] is not current
                or recorded[1] != item.provenance
            ):
                raise SourceSnapshotError(
                    f"provider module was not created by the planned standard import: {item.module_name}"
                )

    def validate(
        self,
        plan: ImportProvenancePlan,
        authority: ModuleAuthority,
    ) -> tuple[tuple[ModuleImportPlan, ModuleType], ...]:
        self._require_plan(plan)
        self._finder.expect(plan)
        planned_names = {item.module_name for item in plan.modules if item.validate}
        unexpected = set(self._recorded).difference(planned_names)
        if unexpected:
            raise SourceSnapshotError(
                f"standard import loaded source modules outside the canonical plan: {sorted(unexpected)!r}"
            )
        validated: list[tuple[ModuleImportPlan, ModuleType]] = []
        for item in plan.modules:
            if not item.validate:
                continue
            current = sys.modules.get(item.module_name)
            if not isinstance(current, ModuleType):
                raise SourceSnapshotError(f"planned provider module is unavailable: {item.module_name}")
            if item.initial_module is not None:
                if current is not item.initial_module or not authority(item, current):
                    raise SourceSnapshotError(
                        "preloaded provider module is not platform-authenticated or has a different "
                        f"authenticated source: {item.module_name}"
                    )
            else:
                recorded = self._recorded.get(item.module_name)
                if recorded is None or recorded != (current, item.provenance):
                    raise SourceSnapshotError(
                        f"provider module lacks planned standard-import authority: {item.module_name}"
                    )
            _validate_imported_module(item, current)
            validated.append((item, current))
        return tuple(validated)

    def _require_plan(self, plan: ImportProvenancePlan) -> None:
        if not self._entered:
            raise SourceSnapshotError("import plan session is not active")
        if (
            plan.source_digest != self._snapshot.digest
            or plan.entrypoint_value != self._source.entrypoint_value
        ):
            raise SourceSnapshotError("import plan disagrees with authenticated provider source")


def build_import_provenance_plan(
    source: ProviderSource,
    snapshot: SourceSnapshot,
    entrypoint: metadata.EntryPoint,
    *,
    initial_modules: Mapping[str, ModuleType] | None = None,
) -> ImportProvenancePlan:
    if (
        entrypoint.group != source.entrypoint_group
        or entrypoint.name != source.entrypoint_name
        or entrypoint.value != source.entrypoint_value
    ):
        raise SourceSnapshotError("entry point disagrees with authenticated provider source")
    roles_by_module: dict[str, set[ModuleRole]] = {}
    _add_lineage(roles_by_module, entrypoint.module, ModuleRole.ENTRYPOINT)
    return _plan_from_roles(
        source,
        snapshot,
        entrypoint.value,
        roles_by_module,
        initial_modules=sys.modules if initial_modules is None else initial_modules,
    )


def extend_import_provenance_plan(
    plan: ImportProvenancePlan,
    source: ProviderSource,
    snapshot: SourceSnapshot,
    initial_modules: Mapping[str, ModuleType],
    *,
    provider_module: str | None = None,
    dependency_provenances: tuple[tuple[str, ModuleProvenance], ...] = (),
    authorized_modules: tuple[str, ...] = (),
) -> ImportProvenancePlan:
    _validate_plan_identity(plan, source, snapshot)
    roles_by_module = {item.module_name: set(item.roles) for item in plan.modules}
    provenances = {item.module_name: item.provenance for item in plan.modules}
    if provider_module is not None:
        _add_lineage(roles_by_module, provider_module, ModuleRole.PROVIDER)
    for module_name, provenance in dependency_provenances:
        _add_lineage(roles_by_module, module_name, ModuleRole.DEPENDENCY)
        prior = provenances.get(module_name)
        if prior is not None and prior != provenance:
            raise SourceSnapshotError(f"recorded module provenance changed: {module_name}")
        provenances[module_name] = provenance
    for module_name in authorized_modules:
        _add_lineage(roles_by_module, module_name, ModuleRole.AUTHORIZED)
    return _plan_from_roles(
        source,
        snapshot,
        plan.entrypoint_value,
        roles_by_module,
        initial_modules=initial_modules,
        provenances=provenances,
    )


def rebind_import_provenance_plan(
    plan: ImportProvenancePlan,
    initial_modules: Mapping[str, ModuleType],
    authority: ModuleAuthority,
) -> ImportProvenancePlan:
    modules: list[ModuleImportPlan] = []
    for item in plan.modules:
        initial = initial_modules.get(item.module_name)
        if initial is not None and not isinstance(initial, ModuleType):
            raise SourceSnapshotError(f"preloaded provider module is not a module: {item.module_name}")
        rebound = replace(item, initial_module=initial)
        if rebound.validate and (not isinstance(initial, ModuleType) or not authority(rebound, initial)):
            raise SourceSnapshotError(
                f"cached provider module is not platform-authenticated: {item.module_name}"
            )
        modules.append(rebound)
    return replace(plan, modules=tuple(modules))


def extend_import_plan_with_quarantine(
    plan: ImportProvenancePlan,
    source: ProviderSource,
    snapshot: SourceSnapshot,
    initial_modules: Mapping[str, ModuleType],
) -> ImportProvenancePlan:
    _validate_plan_identity(plan, source, snapshot)
    planned_names = {item.module_name for item in plan.modules}
    modules = list(plan.modules)
    provenances = {item.module_name: item.provenance for item in plan.modules}
    for module_name, module in sorted(initial_modules.items()):
        if module_name in planned_names or not isinstance(module, ModuleType):
            continue
        try:
            provenance = _resolve_module_lineage_provenance(
                module_name,
                source,
                snapshot,
                provenances,
            )
        except _ModuleOutsideAuthenticatedSource:
            continue
        modules.append(
            _module_item(
                module_name,
                {ModuleRole.QUARANTINE},
                provenance,
                initial_modules,
            )
        )
        provenances[module_name] = provenance
    return replace(plan, modules=_canonical_items(modules))


def active_import_provenance_plan(plan: ImportProvenancePlan) -> ImportProvenancePlan:
    return replace(plan, modules=tuple(item for item in plan.modules if item.validate))


def _validate_plan_identity(
    plan: ImportProvenancePlan,
    source: ProviderSource,
    snapshot: SourceSnapshot,
) -> None:
    if plan.source_digest != snapshot.digest or plan.entrypoint_value != source.entrypoint_value:
        raise SourceSnapshotError("import plan disagrees with authenticated provider source")


def _plan_from_roles(
    source: ProviderSource,
    snapshot: SourceSnapshot,
    entrypoint_value: str,
    roles_by_module: Mapping[str, set[ModuleRole]],
    *,
    initial_modules: Mapping[str, ModuleType],
    provenances: Mapping[str, ModuleProvenance] | None = None,
) -> ImportProvenancePlan:
    resolved = {} if provenances is None else dict(provenances)
    items: list[ModuleImportPlan] = []
    for module_name in sorted(roles_by_module, key=lambda name: (name.count("."), name)):
        provenance = resolved.get(module_name)
        if provenance is None:
            provenance = _resolve_module_provenance(
                module_name,
                source,
                snapshot,
                resolved,
            )[0]
            resolved[module_name] = provenance
        elif provenance.source_digest != snapshot.digest:
            raise SourceSnapshotError(f"planned module has a different source digest: {module_name}")
        items.append(
            _module_item(
                module_name,
                roles_by_module[module_name],
                provenance,
                initial_modules,
            )
        )
    return ImportProvenancePlan(
        source_digest=snapshot.digest,
        entrypoint_value=entrypoint_value,
        modules=tuple(items),
    )


def _canonical_items(items: list[ModuleImportPlan]) -> tuple[ModuleImportPlan, ...]:
    return tuple(sorted(items, key=lambda item: (item.module_name.count("."), item.module_name)))


def _module_item(
    module_name: str,
    roles: set[ModuleRole],
    provenance: ModuleProvenance,
    initial_modules: Mapping[str, ModuleType],
) -> ModuleImportPlan:
    initial = initial_modules.get(module_name)
    if initial is not None and not isinstance(initial, ModuleType):
        raise SourceSnapshotError(f"preloaded provider module is not a module: {module_name}")
    quarantine_only = roles == {ModuleRole.QUARANTINE}
    return ModuleImportPlan(
        module_name=module_name,
        roles=tuple(sorted(roles, key=lambda role: role.value)),
        provenance=provenance,
        initial_module=initial,
        preload=not quarantine_only,
        validate=not quarantine_only,
        commit=not quarantine_only,
    )


def _add_lineage(
    roles_by_module: dict[str, set[ModuleRole]],
    leaf_module: str,
    leaf_role: ModuleRole,
) -> None:
    parts = leaf_module.split(".")
    if not parts or any(not part.isidentifier() for part in parts):
        raise SourceSnapshotError(f"provider module name is not canonical: {leaf_module!r}")
    for index in range(1, len(parts) + 1):
        module_name = ".".join(parts[:index])
        role = leaf_role if index == len(parts) else ModuleRole.ANCESTOR
        roles_by_module.setdefault(module_name, set()).add(role)


def _resolve_module_provenance(
    module_name: str,
    source: ProviderSource,
    snapshot: SourceSnapshot,
    known: Mapping[str, ModuleProvenance],
    runtime_parent_locations: tuple[Path, ...] | None = None,
) -> tuple[ModuleProvenance, ModuleSpec]:
    expected_loader, authenticated_origin, authenticated_locations = _authenticated_module_shape(
        module_name,
        source.import_roots,
        snapshot,
    )
    parent_name, separator, _leaf = module_name.rpartition(".")
    if runtime_parent_locations is not None:
        search_path = runtime_parent_locations
    elif separator:
        parent = known.get(parent_name)
        if parent is None or not parent.standard_is_package:
            raise SourceSnapshotError(f"planned provider parent is not a package: {parent_name}")
        search_path = parent.canonical_locations
    else:
        search_path = _complete_top_level_search_path(source, snapshot)
    spec = _find_standard_spec(
        module_name,
        search_path,
        runtime_parent=runtime_parent_locations is not None,
    )
    if spec is None:
        raise SourceSnapshotError(f"planned module lacks a standard import spec: {module_name}")
    standard_loader = _standard_loader(spec)
    standard_is_package = spec.submodule_search_locations is not None
    canonical_origin = Path(spec.origin).resolve(strict=True) if isinstance(spec.origin, str) else None
    if spec.submodule_search_locations is None:
        canonical_locations = ()
    elif spec.origin is None:
        leaf = module_name.rpartition(".")[2]
        canonical_locations = tuple(
            candidate.resolve(strict=True) for parent in search_path if (candidate := parent / leaf).is_dir()
        )
    else:
        canonical_locations = tuple(
            Path(location).resolve(strict=True) for location in spec.submodule_search_locations
        )
    if standard_loader is not expected_loader:
        raise SourceSnapshotError(f"planned module loader disagrees with authenticated source: {module_name}")
    if expected_loader is StandardLoader.NAMESPACE:
        expected_locations = tuple(location.resolve(strict=True) for location in authenticated_locations)
        if not set(expected_locations).issubset(canonical_locations):
            raise SourceSnapshotError(
                f"planned namespace omits an authenticated source location: {module_name}"
            )
        physical_sha256 = None
    else:
        if authenticated_origin is None or canonical_origin != authenticated_origin.resolve(strict=True):
            raise SourceSnapshotError(
                f"planned module origin disagrees with authenticated source: {module_name}"
            )
        relative_origin = authenticated_origin.relative_to(snapshot.identity.root).as_posix()
        source_file = next((item for item in snapshot.files if item.path == relative_origin), None)
        if source_file is None:
            raise SourceSnapshotError(f"planned provider module is absent from snapshot: {module_name}")
        physical_sha256 = source_file.sha256
        expected_locations = (
            (authenticated_origin.parent.resolve(strict=True),) if standard_is_package else ()
        )
    provenance = ModuleProvenance(
        standard_loader=standard_loader,
        standard_is_package=standard_is_package,
        canonical_origin=canonical_origin,
        canonical_locations=canonical_locations,
        authenticated_locations=expected_locations,
        physical_sha256=physical_sha256,
        source_digest=snapshot.digest,
    )
    return provenance, spec


def _resolve_module_lineage_provenance(
    module_name: str,
    source: ProviderSource,
    snapshot: SourceSnapshot,
    known: Mapping[str, ModuleProvenance],
) -> ModuleProvenance:
    resolved = dict(known)
    parts = module_name.split(".")
    for index in range(1, len(parts) + 1):
        lineage_name = ".".join(parts[:index])
        if lineage_name not in resolved:
            resolved[lineage_name] = _resolve_module_provenance(
                lineage_name,
                source,
                snapshot,
                resolved,
            )[0]
    return resolved[module_name]


def _find_standard_spec(
    module_name: str,
    search_path: tuple[Path, ...],
    *,
    runtime_parent: bool,
) -> ModuleSpec | None:
    """Resolve one standard spec without requiring an unexecuted parent in sys.modules."""
    _parent_name, separator, leaf_name = module_name.rpartition(".")
    lookup_name = module_name if not separator or runtime_parent else leaf_name
    raw = PathFinder.find_spec(lookup_name, [str(path) for path in search_path])
    if raw is None:
        return None
    loader_kind = _standard_loader(raw)
    is_package = raw.submodule_search_locations is not None
    if loader_kind is StandardLoader.NAMESPACE:
        locations = [
            str(candidate.resolve(strict=True))
            for parent in search_path
            if (candidate := parent / leaf_name).is_dir()
        ]
        normalized = ModuleSpec(module_name, None, origin=None, is_package=True)
        normalized.submodule_search_locations = locations
        return normalized
    if not isinstance(raw.origin, str):  # pragma: no cover - loader contract invariant.
        raise SourceSnapshotError(f"standard provider module lacks an origin: {module_name}")
    loader: Loader
    if loader_kind is StandardLoader.EXTENSION:
        loader = ExtensionFileLoader(module_name, raw.origin)
    else:
        loader = SourceFileLoader(module_name, raw.origin)
    normalized = ModuleSpec(
        module_name,
        loader,
        origin=raw.origin,
        is_package=is_package,
    )
    normalized.cached = raw.cached
    normalized.has_location = raw.has_location
    if is_package:
        normalized.submodule_search_locations = [
            str(Path(location).resolve(strict=True)) for location in cast(Any, raw.submodule_search_locations)
        ]
    return normalized


def _authenticated_module_shape(
    module_name: str,
    import_roots: tuple[str, ...],
    snapshot: SourceSnapshot,
) -> tuple[StandardLoader, Path | None, tuple[Path, ...]]:
    source_paths = {source_file.path for source_file in snapshot.files}
    module_path = module_name.replace(".", "/")
    origins: list[tuple[StandardLoader, str]] = []
    namespace_paths: list[str] = []
    for import_root in import_roots:
        prefix = f"{import_root}/" if import_root else ""
        relative_module = f"{prefix}{module_path}"
        for candidate in (f"{relative_module}.py", f"{relative_module}/__init__.py"):
            if candidate in source_paths:
                origins.append((StandardLoader.SOURCE, candidate))
        for suffix in EXTENSION_SUFFIXES:
            for candidate in (
                f"{relative_module}{suffix}",
                f"{relative_module}/__init__{suffix}",
            ):
                if candidate in source_paths:
                    origins.append((StandardLoader.EXTENSION, candidate))
        if any(path.startswith(f"{relative_module}/") for path in source_paths):
            namespace_paths.append(relative_module)
    if len(origins) > 1:
        raise SourceSnapshotError(f"provider module has ambiguous authenticated origins: {module_name}")
    if origins:
        loader, relative_origin = origins[0]
        return loader, snapshot.identity.root / relative_origin, ()
    if namespace_paths:
        return (
            StandardLoader.NAMESPACE,
            None,
            tuple(snapshot.identity.root / path for path in sorted(set(namespace_paths))),
        )
    raise _ModuleOutsideAuthenticatedSource(
        f"provider module is absent from authenticated import roots: {module_name}"
    )


def _complete_top_level_search_path(
    source: ProviderSource,
    snapshot: SourceSnapshot,
) -> tuple[Path, ...]:
    standard: list[Path] = []
    for raw_path in sys.path:
        try:
            path = Path(raw_path or ".").resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if path.is_dir() and path not in standard:
            standard.append(path)
    selected = [
        (snapshot.identity.root / import_root if import_root else snapshot.identity.root).resolve(strict=True)
        for import_root in source.import_roots
    ]
    missing = [path for path in selected if path not in standard]
    return tuple((*missing, *standard))


def _standard_loader(spec: ModuleSpec) -> StandardLoader:
    if spec.origin is None and spec.submodule_search_locations is not None:
        return StandardLoader.NAMESPACE
    if isinstance(spec.loader, ExtensionFileLoader):
        return StandardLoader.EXTENSION
    if isinstance(spec.loader, SourceFileLoader):
        return StandardLoader.SOURCE
    raise SourceSnapshotError(f"provider module uses an unsupported standard loader: {spec.name}")


class _RecordingFinder(MetaPathFinder):
    def __init__(
        self,
        source: ProviderSource,
        snapshot: SourceSnapshot,
        recorded: dict[str, tuple[ModuleType, ModuleProvenance]],
    ) -> None:
        self._source = source
        self._snapshot = snapshot
        self._recorded = recorded
        self._expected: dict[str, ModuleProvenance] = {}

    def expect(self, plan: ImportProvenancePlan) -> None:
        for item in plan.modules:
            if not item.preload:
                continue
            prior = self._expected.get(item.module_name)
            if prior is not None and prior != item.provenance:
                raise SourceSnapshotError(
                    f"planned module provenance changed during call: {item.module_name}"
                )
            self._expected[item.module_name] = item.provenance

    def find_spec(
        self,
        fullname: str,
        path: object = None,
        _target: ModuleType | None = None,
    ) -> ModuleSpec | None:
        parent_locations = _runtime_locations(path)
        try:
            provenance, standard = _resolve_module_provenance(
                fullname,
                self._source,
                self._snapshot,
                self._expected,
                parent_locations,
            )
        except _ModuleOutsideAuthenticatedSource:
            return None
        expected = self._expected.get(fullname)
        if expected is not None and expected != provenance:
            raise SourceSnapshotError(f"planned standard import provenance changed: {fullname}")
        selected = provenance if expected is None else expected
        executable = _executable_standard_spec(fullname, standard, selected)
        loader = cast(Loader, executable.loader)
        wrapper = _RecordingLoader(fullname, executable, loader, selected, self._recorded)
        wrapped = ModuleSpec(
            fullname,
            wrapper,
            origin=executable.origin,
            is_package=selected.standard_is_package,
        )
        wrapped.cached = executable.cached
        wrapped.has_location = executable.has_location
        if selected.standard_is_package:
            wrapped.submodule_search_locations = [str(path) for path in selected.canonical_locations]
        return wrapped


def _runtime_locations(path: object) -> tuple[Path, ...] | None:
    if path is None:
        return None
    try:
        return tuple(Path(cast(str, item)).resolve(strict=True) for item in cast(Any, path))
    except (OSError, TypeError, RuntimeError) as error:
        raise SourceSnapshotError("provider parent has invalid standard search locations") from error


def _executable_standard_spec(
    module_name: str,
    standard: ModuleSpec,
    provenance: ModuleProvenance,
) -> ModuleSpec:
    if provenance.standard_loader is not StandardLoader.NAMESPACE:
        return standard
    locations = [str(location) for location in provenance.canonical_locations]
    loader = NamespaceLoader(module_name, locations, cast(Any, PathFinder))
    spec = ModuleSpec(module_name, loader, origin=None, is_package=True)
    spec.submodule_search_locations = locations
    return spec


class _RecordingLoader(Loader):
    def __init__(
        self,
        module_name: str,
        standard_spec: ModuleSpec,
        loader: Loader,
        provenance: ModuleProvenance,
        recorded: dict[str, tuple[ModuleType, ModuleProvenance]],
    ) -> None:
        self._module_name = module_name
        self._standard_spec = standard_spec
        self._loader = loader
        self._provenance = provenance
        self._recorded = recorded

    def create_module(self, spec: ModuleSpec) -> ModuleType | None:
        del spec
        method = getattr(self._loader, "create_module", None)
        if not callable(method):
            return None
        return cast(ModuleType | None, method(self._standard_spec))

    def exec_module(self, module: ModuleType) -> None:
        method = getattr(self._loader, "exec_module", None)
        if not callable(method):
            raise SourceSnapshotError(f"standard provider loader cannot execute: {self._module_name}")
        module.__loader__ = self._loader
        module.__spec__ = self._standard_spec
        if self._provenance.standard_is_package:
            locations = self._standard_spec.submodule_search_locations
            if locations is None:  # pragma: no cover - provenance/spec constructor invariant.
                raise SourceSnapshotError(
                    f"planned package lacks standard search locations: {self._module_name}"
                )
            module.__path__ = locations
        method(module)
        self._recorded[self._module_name] = (module, self._provenance)


def _validate_imported_module(item: ModuleImportPlan, module: ModuleType) -> None:
    provenance = item.provenance
    spec = getattr(module, "__spec__", None)
    if not isinstance(spec, ModuleSpec) or spec.name != item.module_name:
        raise SourceSnapshotError(
            f"planned standard import provenance has a nonstandard spec: {item.module_name}"
        )
    actual_loader = _standard_loader(spec)
    actual_is_package = spec.submodule_search_locations is not None
    actual_origin = Path(spec.origin).resolve(strict=True) if isinstance(spec.origin, str) else None
    actual_locations = (
        tuple(Path(location).resolve(strict=True) for location in spec.submodule_search_locations)
        if spec.submodule_search_locations is not None
        else ()
    )
    if (
        actual_loader is not provenance.standard_loader
        or actual_is_package != provenance.standard_is_package
        or actual_origin != provenance.canonical_origin
        or actual_locations != provenance.canonical_locations
    ):
        raise SourceSnapshotError(f"planned standard import provenance changed: {item.module_name}")
    if not set(provenance.authenticated_locations).issubset(provenance.canonical_locations):
        raise SourceSnapshotError(
            f"planned authenticated locations escape standard provenance: {item.module_name}"
        )
    if provenance.standard_loader is StandardLoader.NAMESPACE:
        if (
            module.__name__ != item.module_name
            or not isinstance(spec.loader, NamespaceLoader)
            or getattr(module, "__loader__", None) is not spec.loader
            or getattr(module, "__path__", None) is not spec.submodule_search_locations
            or getattr(module, "__package__", None) != item.module_name
            or provenance.physical_sha256 is not None
        ):
            raise SourceSnapshotError(f"planned standard import provenance changed: {item.module_name}")
        return
    if provenance.canonical_origin is None or provenance.physical_sha256 is None:
        raise SourceSnapshotError(
            f"planned standard import provenance lacks physical authority: {item.module_name}"
        )
    origin = getattr(module, "__file__", None)
    if (
        not isinstance(origin, str)
        or Path(origin).resolve(strict=True) != provenance.canonical_origin
        or module.__name__ != item.module_name
        or getattr(module, "__loader__", None) is not spec.loader
    ):
        raise SourceSnapshotError(f"planned standard import provenance changed: {item.module_name}")
    if provenance.standard_is_package:
        if (
            getattr(module, "__path__", None) is not spec.submodule_search_locations
            or getattr(module, "__package__", None) != item.module_name
        ):
            raise SourceSnapshotError(f"planned standard import provenance changed: {item.module_name}")
    elif getattr(module, "__package__", None) != item.module_name.rpartition(".")[0]:
        raise SourceSnapshotError(f"planned standard import provenance changed: {item.module_name}")
    if hashlib.sha256(provenance.canonical_origin.read_bytes()).hexdigest() != provenance.physical_sha256:
        raise SourceSnapshotError(f"planned standard import provenance hash changed: {item.module_name}")


__all__ = [
    "active_import_provenance_plan",
    "build_import_provenance_plan",
    "extend_import_plan_with_quarantine",
    "extend_import_provenance_plan",
    "ImportPlanSession",
    "ImportProvenancePlan",
    "ModuleImportPlan",
    "ModuleProvenance",
    "ModuleRole",
    "rebind_import_provenance_plan",
    "StandardLoader",
]
