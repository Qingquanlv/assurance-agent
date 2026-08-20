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
    DEPENDENCY = "dependency"
    ENTRYPOINT = "entrypoint"
    PROVIDER = "provider"
    QUARANTINE = "quarantine"


class ModuleClassification(str, Enum):
    REGULAR = "regular"
    NAMESPACE = "namespace"
    EXTENSION = "extension"


@dataclass(frozen=True, slots=True)
class ModuleImportPlan:
    module_name: str
    roles: tuple[ModuleRole, ...]
    classification: ModuleClassification
    physical_origin: Path | None
    physical_sha256: str | None
    namespace_locations: tuple[Path, ...]
    standard_loader: str
    standard_is_package: bool
    initial_module: ModuleType | None
    source_digest: str
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
            return next(entry for entry in self.modules if entry.module_name == module_name)
        except StopIteration as error:
            raise KeyError(module_name) from error


ModuleAuthority = Callable[[ModuleImportPlan, ModuleType], bool]
_MISSING = object()


class ImportPlanSession(AbstractContextManager["ImportPlanSession"]):
    """Serialized standard-import proof collector for one authenticated binding."""

    def __init__(
        self,
        source: ProviderSource,
        snapshot: SourceSnapshot,
        initial_modules: Mapping[str, ModuleType],
    ) -> None:
        self._source = source
        self._snapshot = snapshot
        self._initial_modules = dict(initial_modules)
        self._recorded: dict[str, ModuleType] = {}
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
    def recorded_module_names(self) -> tuple[str, ...]:
        return tuple(sorted(self._recorded))

    @property
    def post_quarantine_initial_modules(self) -> Mapping[str, ModuleType]:
        """Initial module authority after this plan's quarantine boundary."""
        return {
            name: module for name, module in self._initial_modules.items() if name not in self._quarantined
        }

    def quarantine(self, plan: ImportProvenancePlan) -> None:
        self._require_plan(plan)
        for entry in plan.modules:
            if ModuleRole.QUARANTINE not in entry.roles or entry.validate:
                continue
            current = sys.modules.get(entry.module_name)
            if current is not entry.initial_module or not isinstance(current, ModuleType):
                raise SourceSnapshotError(
                    f"quarantined provider module changed before binding: {entry.module_name}"
                )
            parent_name, separator, child_name = entry.module_name.rpartition(".")
            parent = sys.modules.get(parent_name) if separator else None
            prior_attribute = _MISSING
            if isinstance(parent, ModuleType):
                prior_attribute = parent.__dict__.get(child_name, _MISSING)
                if prior_attribute is current:
                    parent.__dict__.pop(child_name, None)
            self._quarantined[entry.module_name] = (
                current,
                parent if isinstance(parent, ModuleType) else None,
                child_name,
                prior_attribute,
            )
            del sys.modules[entry.module_name]

    def restore_unconsumed_quarantine(self, plan: ImportProvenancePlan) -> None:
        self._require_plan(plan)
        active_names = {entry.module_name for entry in plan.modules if entry.validate}
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
        for entry in plan.modules:
            if not entry.preload:
                continue
            current = sys.modules.get(entry.module_name)
            if entry.initial_module is not None:
                if current is not entry.initial_module or not authority(entry, entry.initial_module):
                    raise SourceSnapshotError(
                        "preloaded provider module is not platform-authenticated or has a different "
                        f"authenticated source: {entry.module_name}"
                    )
                continue
            if current is None:
                try:
                    import_module(entry.module_name)
                except Exception as error:
                    raise SourceSnapshotError(
                        f"cannot standard-import planned provider module: {entry.module_name}"
                    ) from error
                current = sys.modules.get(entry.module_name)
            if not isinstance(current, ModuleType) or self._recorded.get(entry.module_name) is not current:
                raise SourceSnapshotError(
                    f"provider module was not created by the planned standard import: {entry.module_name}"
                )

    def validate(
        self,
        plan: ImportProvenancePlan,
        authority: ModuleAuthority,
    ) -> tuple[tuple[ModuleImportPlan, ModuleType], ...]:
        self._require_plan(plan)
        planned_names = {entry.module_name for entry in plan.modules if entry.validate}
        unexpected = set(self._recorded).difference(planned_names)
        if unexpected:
            raise SourceSnapshotError(
                f"standard import loaded source modules outside the canonical plan: {sorted(unexpected)!r}"
            )
        validated: list[tuple[ModuleImportPlan, ModuleType]] = []
        for entry in plan.modules:
            if not entry.validate:
                continue
            current = sys.modules.get(entry.module_name)
            if not isinstance(current, ModuleType):
                raise SourceSnapshotError(f"planned provider module is unavailable: {entry.module_name}")
            if entry.initial_module is not None:
                if current is not entry.initial_module or not authority(entry, current):
                    raise SourceSnapshotError(
                        "preloaded provider module is not platform-authenticated or has a different "
                        f"authenticated source: {entry.module_name}"
                    )
            elif self._recorded.get(entry.module_name) is not current:
                raise SourceSnapshotError(
                    f"provider module lacks planned standard-import authority: {entry.module_name}"
                )
            _validate_imported_module(entry, current, self._snapshot)
            validated.append((entry, current))
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
    provider_module: str | None = None,
    dependency_modules: tuple[str, ...] = (),
    quarantine_modules: tuple[str, ...] = (),
    initial_modules: Mapping[str, ModuleType] | None = None,
) -> ImportProvenancePlan:
    if (
        entrypoint.group != source.entrypoint_group
        or entrypoint.name != source.entrypoint_name
        or entrypoint.value != source.entrypoint_value
    ):
        raise SourceSnapshotError("entry point disagrees with authenticated provider source")
    entrypoint_module = entrypoint.module
    roles_by_module: dict[str, set[ModuleRole]] = {}
    _add_lineage(roles_by_module, entrypoint_module, ModuleRole.ENTRYPOINT)
    if provider_module is not None:
        _add_lineage(roles_by_module, provider_module, ModuleRole.PROVIDER)
    for dependency_module in dependency_modules:
        _add_lineage(roles_by_module, dependency_module, ModuleRole.DEPENDENCY)
    for quarantine_module in quarantine_modules:
        roles_by_module.setdefault(quarantine_module, set()).add(ModuleRole.QUARANTINE)
    modules = tuple(
        _module_plan(
            module_name,
            roles_by_module[module_name],
            source,
            snapshot,
            sys.modules if initial_modules is None else initial_modules,
        )
        for module_name in sorted(
            roles_by_module,
            key=lambda name: (name.count("."), name),
        )
    )
    return ImportProvenancePlan(
        source_digest=snapshot.digest,
        entrypoint_value=entrypoint.value,
        modules=modules,
    )


def rebind_import_provenance_plan(
    plan: ImportProvenancePlan,
    initial_modules: Mapping[str, ModuleType],
    authority: ModuleAuthority,
) -> ImportProvenancePlan:
    modules: list[ModuleImportPlan] = []
    for entry in plan.modules:
        initial = initial_modules.get(entry.module_name)
        if initial is not None and not isinstance(initial, ModuleType):
            raise SourceSnapshotError(f"preloaded provider module is not a module: {entry.module_name}")
        if entry.validate and (not isinstance(initial, ModuleType) or not authority(entry, initial)):
            raise SourceSnapshotError(
                f"cached provider module is not platform-authenticated: {entry.module_name}"
            )
        modules.append(replace(entry, initial_module=initial))
    return replace(plan, modules=tuple(modules))


def extend_import_plan_with_quarantine(
    plan: ImportProvenancePlan,
    source: ProviderSource,
    snapshot: SourceSnapshot,
    initial_modules: Mapping[str, ModuleType],
    authority: ModuleAuthority,
) -> ImportProvenancePlan:
    planned_names = {entry.module_name for entry in plan.modules}
    quarantine_names: list[str] = []
    for module_name, module in sorted(initial_modules.items()):
        if module_name in planned_names or not isinstance(module, ModuleType):
            continue
        try:
            entry = _module_plan(
                module_name,
                {ModuleRole.QUARANTINE},
                source,
                snapshot,
                initial_modules,
            )
        except SourceSnapshotError:
            continue
        if not authority(entry, module):
            quarantine_names.append(module_name)
    if not quarantine_names:
        return plan
    roles_by_name = {entry.module_name: set(entry.roles) for entry in plan.modules}
    for module_name in quarantine_names:
        roles_by_name[module_name] = {ModuleRole.QUARANTINE}
    modules = tuple(
        _module_plan(name, roles_by_name[name], source, snapshot, initial_modules)
        for name in sorted(roles_by_name, key=lambda item: (item.count("."), item))
    )
    return replace(plan, modules=modules)


def active_import_provenance_plan(plan: ImportProvenancePlan) -> ImportProvenancePlan:
    return replace(plan, modules=tuple(entry for entry in plan.modules if entry.validate))


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


def _module_plan(
    module_name: str,
    roles: set[ModuleRole],
    source: ProviderSource,
    snapshot: SourceSnapshot,
    initial_modules: Mapping[str, ModuleType],
) -> ModuleImportPlan:
    classification, origin, locations = _classify_module(
        module_name,
        source.import_roots,
        snapshot,
    )
    initial = initial_modules.get(module_name)
    if initial is not None and not isinstance(initial, ModuleType):
        raise SourceSnapshotError(f"preloaded provider module is not a module: {module_name}")
    physical_sha256 = None
    if origin is not None:
        relative_origin = origin.relative_to(snapshot.identity.root).as_posix()
        physical_sha256 = next(item.sha256 for item in snapshot.files if item.path == relative_origin)
    return ModuleImportPlan(
        module_name=module_name,
        roles=tuple(sorted(roles, key=lambda role: role.value)),
        classification=classification,
        physical_origin=origin,
        physical_sha256=physical_sha256,
        namespace_locations=locations,
        standard_loader=classification.value,
        standard_is_package=(
            classification is ModuleClassification.NAMESPACE
            or (origin is not None and _is_package_initializer(origin.name))
        ),
        initial_module=initial,
        source_digest=snapshot.digest,
        preload=roles != {ModuleRole.QUARANTINE},
        validate=roles != {ModuleRole.QUARANTINE},
        commit=roles != {ModuleRole.QUARANTINE},
    )


def _classify_module(
    module_name: str,
    import_roots: tuple[str, ...],
    snapshot: SourceSnapshot,
) -> tuple[ModuleClassification, Path | None, tuple[Path, ...]]:
    source_paths = {source_file.path for source_file in snapshot.files}
    module_path = module_name.replace(".", "/")
    origins: list[tuple[ModuleClassification, str]] = []
    namespace_paths: list[str] = []
    for import_root in import_roots:
        prefix = f"{import_root}/" if import_root else ""
        relative_module = f"{prefix}{module_path}"
        python_candidates = (
            f"{relative_module}.py",
            f"{relative_module}/__init__.py",
        )
        origins.extend(
            (ModuleClassification.REGULAR, candidate)
            for candidate in python_candidates
            if candidate in source_paths
        )
        extension_candidates = tuple(
            candidate
            for suffix in EXTENSION_SUFFIXES
            for candidate in (
                f"{relative_module}{suffix}",
                f"{relative_module}/__init__{suffix}",
            )
        )
        origins.extend(
            (ModuleClassification.EXTENSION, candidate)
            for candidate in extension_candidates
            if candidate in source_paths
        )
        if any(path.startswith(f"{relative_module}/") for path in source_paths):
            namespace_paths.append(relative_module)
    if len(origins) > 1:
        raise SourceSnapshotError(f"provider module has ambiguous authenticated origins: {module_name}")
    if origins:
        classification, relative_origin = origins[0]
        return classification, snapshot.identity.root / relative_origin, ()
    if namespace_paths:
        return (
            ModuleClassification.NAMESPACE,
            None,
            tuple(snapshot.identity.root / path for path in sorted(set(namespace_paths))),
        )
    raise SourceSnapshotError(f"provider module is absent from authenticated import roots: {module_name}")


class _RecordingFinder(MetaPathFinder):
    def __init__(
        self,
        source: ProviderSource,
        snapshot: SourceSnapshot,
        recorded: dict[str, ModuleType],
    ) -> None:
        self._source = source
        self._snapshot = snapshot
        self._recorded = recorded

    def find_spec(
        self,
        fullname: str,
        _path: object = None,
        _target: ModuleType | None = None,
    ) -> ModuleSpec | None:
        try:
            classification, origin, locations = _classify_module(
                fullname,
                self._source.import_roots,
                self._snapshot,
            )
        except SourceSnapshotError:
            return None
        standard = _standard_spec(fullname, classification, origin, locations, self._source, self._snapshot)
        loader = cast(Loader, standard.loader)
        wrapper = _RecordingLoader(fullname, standard, loader, self._recorded)
        wrapped = ModuleSpec(
            fullname,
            wrapper,
            origin=standard.origin,
            is_package=standard.submodule_search_locations is not None,
        )
        wrapped.cached = standard.cached
        wrapped.has_location = standard.has_location
        if standard.submodule_search_locations is not None:
            wrapped.submodule_search_locations = list(standard.submodule_search_locations)
        return wrapped


class _RecordingLoader(Loader):
    def __init__(
        self,
        module_name: str,
        standard_spec: ModuleSpec,
        loader: Loader,
        recorded: dict[str, ModuleType],
    ) -> None:
        self._module_name = module_name
        self._standard_spec = standard_spec
        self._loader = loader
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
        if self._standard_spec.submodule_search_locations is not None:
            module.__path__ = self._standard_spec.submodule_search_locations
        method(module)
        self._recorded[self._module_name] = module


def _standard_spec(
    module_name: str,
    classification: ModuleClassification,
    origin: Path | None,
    locations: tuple[Path, ...],
    source: ProviderSource,
    snapshot: SourceSnapshot,
) -> ModuleSpec:
    search_path = _standard_search_path(module_name, source, snapshot)
    spec = PathFinder.find_spec(module_name, [str(path) for path in search_path])
    if classification is ModuleClassification.NAMESPACE:
        if spec is None or spec.origin is not None or spec.submodule_search_locations is None:
            raise SourceSnapshotError(f"planned namespace lacks a standard import spec: {module_name}")
        resolved = tuple(
            sorted(Path(location).resolve(strict=True) for location in spec.submodule_search_locations)
        )
        if resolved != tuple(sorted(location.resolve(strict=True) for location in locations)):
            raise SourceSnapshotError(f"planned namespace search locations disagree: {module_name}")
        resolved_strings = [str(location) for location in resolved]
        loader = NamespaceLoader(module_name, resolved_strings, cast(Any, PathFinder))
        standard = ModuleSpec(module_name, loader, origin=None, is_package=True)
        standard.submodule_search_locations = resolved_strings
        return standard
    if spec is None or not isinstance(spec.origin, str) or spec.loader is None or origin is None:
        raise SourceSnapshotError(f"planned module lacks a standard import spec: {module_name}")
    if Path(spec.origin).resolve(strict=True) != origin.resolve(strict=True):
        raise SourceSnapshotError(f"planned module origin disagrees with standard import: {module_name}")
    return spec


def _standard_search_path(
    module_name: str,
    source: ProviderSource,
    snapshot: SourceSnapshot,
) -> tuple[Path, ...]:
    parent_name, separator, _leaf = module_name.rpartition(".")
    if not separator:
        return tuple(
            snapshot.identity.root / import_root if import_root else snapshot.identity.root
            for import_root in source.import_roots
        )
    classification, origin, locations = _classify_module(
        parent_name,
        source.import_roots,
        snapshot,
    )
    if classification is ModuleClassification.NAMESPACE:
        return locations
    if origin is None or not _is_package_initializer(origin.name):
        raise SourceSnapshotError(f"planned provider parent is not a package: {parent_name}")
    return (origin.parent,)


def _validate_imported_module(
    entry: ModuleImportPlan,
    module: ModuleType,
    snapshot: SourceSnapshot,
) -> None:
    spec = getattr(module, "__spec__", None)
    if not isinstance(spec, ModuleSpec) or spec.name != entry.module_name:
        raise SourceSnapshotError(f"planned provider module has a nonstandard spec: {entry.module_name}")
    if entry.classification is ModuleClassification.NAMESPACE:
        if (
            module.__name__ != entry.module_name
            or spec.origin is not None
            or not isinstance(spec.loader, NamespaceLoader)
            or spec.submodule_search_locations is None
            or getattr(module, "__loader__", None) is not spec.loader
            or getattr(module, "__path__", None) is not spec.submodule_search_locations
            or getattr(module, "__package__", None) != entry.module_name
        ):
            raise SourceSnapshotError(f"planned namespace has a nonstandard spec: {entry.module_name}")
        actual = tuple(
            sorted(Path(location).resolve(strict=True) for location in spec.submodule_search_locations)
        )
        expected = tuple(sorted(location.resolve(strict=True) for location in entry.namespace_locations))
        if actual != expected:
            raise SourceSnapshotError(f"planned namespace locations changed: {entry.module_name}")
        return
    origin = getattr(module, "__file__", None)
    if not isinstance(origin, str) or entry.physical_origin is None:
        raise SourceSnapshotError(f"planned provider module lacks a physical origin: {entry.module_name}")
    if Path(origin).resolve(strict=True) != entry.physical_origin.resolve(strict=True):
        raise SourceSnapshotError(f"planned provider module origin changed: {entry.module_name}")
    if (
        module.__name__ != entry.module_name
        or getattr(module, "__loader__", None) is not spec.loader
        or not isinstance(spec.origin, str)
        or Path(spec.origin).resolve(strict=True) != entry.physical_origin.resolve(strict=True)
    ):
        raise SourceSnapshotError(f"planned provider module has a nonstandard spec: {entry.module_name}")
    expected_loader = (
        ExtensionFileLoader if entry.classification is ModuleClassification.EXTENSION else SourceFileLoader
    )
    if not isinstance(spec.loader, expected_loader):
        raise SourceSnapshotError(f"planned provider module has a nonstandard loader: {entry.module_name}")
    is_package = _is_package_initializer(entry.physical_origin.name)
    if is_package:
        if (
            spec.submodule_search_locations is None
            or getattr(module, "__path__", None) is not spec.submodule_search_locations
            or getattr(module, "__package__", None) != entry.module_name
        ):
            raise SourceSnapshotError(f"planned provider package has a nonstandard spec: {entry.module_name}")
    elif (
        spec.submodule_search_locations is not None
        or getattr(module, "__package__", None) != entry.module_name.rpartition(".")[0]
    ):
        raise SourceSnapshotError(f"planned provider module has a nonstandard spec: {entry.module_name}")
    relative_path = entry.physical_origin.relative_to(snapshot.identity.root).as_posix()
    expected_file = next((item for item in snapshot.files if item.path == relative_path), None)
    if expected_file is None:
        raise SourceSnapshotError(f"planned provider module is absent from snapshot: {entry.module_name}")
    if hashlib.sha256(entry.physical_origin.read_bytes()).hexdigest() != expected_file.sha256:
        raise SourceSnapshotError(f"planned provider module hash changed: {entry.module_name}")


def _is_package_initializer(filename: str) -> bool:
    return filename == "__init__.py" or any(filename == f"__init__{suffix}" for suffix in EXTENSION_SUFFIXES)


__all__ = [
    "active_import_provenance_plan",
    "ImportPlanSession",
    "ImportProvenancePlan",
    "ModuleClassification",
    "ModuleImportPlan",
    "ModuleRole",
    "build_import_provenance_plan",
    "extend_import_plan_with_quarantine",
    "rebind_import_provenance_plan",
]
