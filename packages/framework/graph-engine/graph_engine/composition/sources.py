from __future__ import annotations

import _imp
import base64
from collections.abc import Mapping
import configparser
from contextlib import contextmanager
import csv
from dataclasses import dataclass, field, replace
import errno
from email.parser import BytesParser
from email.policy import compat32
import hashlib
import inspect
from importlib import metadata
import json
import os
from pathlib import Path
import stat
import sys
from threading import RLock
from types import FunctionType, MethodType, ModuleType
from typing import Iterator, Literal, NoReturn, Protocol, TypeAlias, cast

from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version
from pydantic import ValidationError, field_validator, model_validator

from graph_engine.composition.models import (
    AuthenticatedContribution,
    ContributionAuthority,
    ExecutableAuthority,
    ExecutableBindingMode,
    ExecutableKind,
    ExecutableModuleProvenance,
    ExecutableProvenance,
    ProductManifest,
    SourceKey,
    SourceRole,
    SourceFile,
    SourceIdentity,
    SourceKind,
    SourceSnapshot,
    _validate_canonical_relative_path,
)
from graph_engine.composition.import_plan import (
    ImportPlanSession,
    ImportProvenancePlan,
    ModuleImportPlan,
    ModuleProvenance,
    _validate_imported_module,
    active_import_provenance_plan,
    build_import_provenance_plan,
    extend_import_plan_with_quarantine,
    extend_import_provenance_plan,
    rebind_import_provenance_plan,
)
from graph_engine.composition.source_fs import (
    DeclaredTreePolicy,
    SourceSnapshotError,
    capture_declared_tree,
)
from graph_engine.plugin_api import (
    FrozenModel,
    PluginContribution,
    PluginDescriptor,
    PluginProvider,
    ProviderSource,
    RegistryPorts,
    validate_contribution,
)


class WheelProductSource(FrozenModel):
    kind: Literal["wheel_product"] = "wheel_product"
    distribution: str
    entrypoint_group: Literal["graph_engine.products"] = "graph_engine.products"
    entrypoint_name: str
    declaration_path: str

    @field_validator("distribution")
    @classmethod
    def _normalize_distribution(cls, value: str) -> str:
        return _normalized_distribution_name(value)

    @field_validator("declaration_path")
    @classmethod
    def _validate_declaration_path(cls, value: str) -> str:
        return _canonical_declaration_path(value)


class WheelPluginSource(FrozenModel):
    kind: Literal["wheel_plugin"] = "wheel_plugin"
    distribution: str
    entrypoint_group: Literal["graph_engine.plugins"] = "graph_engine.plugins"
    entrypoint_name: str
    declaration_path: str

    @field_validator("distribution")
    @classmethod
    def _normalize_distribution(cls, value: str) -> str:
        return _normalized_distribution_name(value)

    @field_validator("declaration_path")
    @classmethod
    def _validate_declaration_path(cls, value: str) -> str:
        return _canonical_declaration_path(value)


class EditableWheelProductSource(FrozenModel):
    kind: Literal["editable_product"] = "editable_product"
    distribution: str
    entrypoint_group: Literal["graph_engine.products"] = "graph_engine.products"
    entrypoint_name: str
    declaration_path: str
    source_root: Path
    source_files: tuple[str, ...]

    @field_validator("distribution")
    @classmethod
    def _normalize_distribution(cls, value: str) -> str:
        return _normalized_distribution_name(value)

    @field_validator("declaration_path")
    @classmethod
    def _validate_declaration_path(cls, value: str) -> str:
        return _canonical_declaration_path(value)

    @model_validator(mode="after")
    def _require_declared_declaration(self) -> EditableWheelProductSource:
        if self.declaration_path not in self.source_files:
            raise ValueError("editable declaration path must be present in source_files")
        return self


class EditableWheelPluginSource(FrozenModel):
    kind: Literal["editable_plugin"] = "editable_plugin"
    distribution: str
    entrypoint_group: Literal["graph_engine.plugins"] = "graph_engine.plugins"
    entrypoint_name: str
    declaration_path: str
    source_root: Path
    source_files: tuple[str, ...]

    @field_validator("distribution")
    @classmethod
    def _normalize_distribution(cls, value: str) -> str:
        return _normalized_distribution_name(value)

    @field_validator("declaration_path")
    @classmethod
    def _validate_declaration_path(cls, value: str) -> str:
        return _canonical_declaration_path(value)

    @model_validator(mode="after")
    def _require_declared_declaration(self) -> EditableWheelPluginSource:
        if self.declaration_path not in self.source_files:
            raise ValueError("editable declaration path must be present in source_files")
        return self


class WheelProductDeclaration(FrozenModel):
    schema_version: Literal["1"]
    kind: Literal["product"]
    source: ProviderSource
    manifest: ProductManifest

    @model_validator(mode="after")
    def _validate_manifest_source(self) -> WheelProductDeclaration:
        if self.manifest.source != self.source:
            raise ValueError("product manifest source must equal declaration source")
        return self


class WheelPluginDeclaration(FrozenModel):
    schema_version: Literal["1"]
    kind: Literal["plugin"]
    source: ProviderSource
    descriptor: PluginDescriptor

    @model_validator(mode="after")
    def _validate_descriptor_source(self) -> WheelPluginDeclaration:
        if self.descriptor.source != self.source:
            raise ValueError("plugin descriptor source must equal declaration source")
        return self


WheelSource: TypeAlias = (
    WheelProductSource | WheelPluginSource | EditableWheelProductSource | EditableWheelPluginSource
)
WheelDeclaration: TypeAlias = WheelProductDeclaration | WheelPluginDeclaration


class WheelProductProvider(Protocol):
    def manifest(self) -> ProductManifest: ...


WheelProvider: TypeAlias = WheelProductProvider | PluginProvider
_EntryState: TypeAlias = tuple[int, int, int, int, int, int]
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_DIRECTORY_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | _NOFOLLOW
_FILE_FLAGS = os.O_RDONLY | _NOFOLLOW | _NONBLOCK
_MISSING_MODULE = object()
_MAX_RETAINED_DIRECTORIES = 32


@dataclass(frozen=True, slots=True)
class _ResolvedWheelSnapshot:
    snapshot: SourceSnapshot
    entrypoint: metadata.EntryPoint
    declaration: WheelDeclaration


@dataclass(slots=True, eq=False)
class AuthenticatedProviderBinding:
    """Exact provider object whose every live call runs inside source authentication."""

    _provider: WheelProvider
    declaration: object
    _source: WheelSource
    _snapshot: SourceSnapshot
    _metadata_provider: MetadataProvider
    _cache: _AuthenticatedBindingCache
    _cache_key: tuple[str, str, str, str]
    _import_plan: ImportProvenancePlan
    _issued_contributions: tuple[ContributionAuthority, ...] = ()

    @property
    def import_plan(self) -> ImportProvenancePlan:
        return self._import_plan

    def manifest(self) -> ProductManifest:
        return cast(ProductManifest, _authenticated_provider_call(self, "manifest"))

    def descriptor(self) -> PluginDescriptor:
        return cast(PluginDescriptor, _authenticated_provider_call(self, "descriptor"))

    def contribute(self, ports: RegistryPorts) -> PluginContribution:
        return self.authenticated_contribute(ports).contribution

    def authenticated_contribute(self, ports: RegistryPorts) -> AuthenticatedContribution:
        return cast(AuthenticatedContribution, _authenticated_provider_call(self, "contribute", ports))

    def authenticate_executable(
        self,
        authority: ContributionAuthority,
        executable: object,
        provenance: ExecutableProvenance,
    ) -> None:
        _authenticate_bound_executable(self, authority, executable, provenance)

    def authenticate_contribution(self, authority: ContributionAuthority) -> None:
        """Require an exact contribution generation issued by this live binding."""

        with _serialized_imports():
            if self._cache.bindings.get(self._cache_key) is not self:
                raise SourceSnapshotError("provider binding is not owned by the current RegistryPlatform")
            resolved = _resolve_wheel_snapshot(self._source, self._metadata_provider)
            if resolved.snapshot != self._snapshot:
                raise SourceSnapshotError("wheel source changed before contribution authentication")
            if authority.provider_binding is not self or not any(
                candidate is authority for candidate in self._issued_contributions
            ):
                raise SourceSnapshotError(
                    "contribution authority was not issued by this authenticated provider binding"
                )


@dataclass(slots=True)
class _AuthenticatedBindingCache:
    """Platform-owned authority for modules imported after source authentication."""

    bindings: dict[tuple[str, str, str, str], AuthenticatedProviderBinding] = field(default_factory=dict)
    modules: dict[str, _AuthenticatedModule] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class _AuthenticatedModule:
    module: ModuleType
    provenances: frozenset[ModuleProvenance]


@dataclass(frozen=True, slots=True)
class _ParentPackageState:
    parent_name: str
    parent: ModuleType
    attributes: dict[str, object]


@dataclass(frozen=True, slots=True)
class _ParentAttributeSnapshot:
    packages: tuple[_ParentPackageState, ...]


_MODULE_IMPORT_LOCK = RLock()


@contextmanager
def _serialized_imports() -> Iterator[None]:
    with _MODULE_IMPORT_LOCK:
        _imp.acquire_lock()
        try:
            yield
        finally:
            _imp.release_lock()


class _EntryPointConfigParser(configparser.ConfigParser):
    def optionxform(self, optionstr: str) -> str:
        return optionstr


class MetadataProvider(Protocol):
    def distribution(self, distribution_name: str, /) -> metadata.Distribution: ...


def snapshot_wheel_source(
    source: WheelSource,
    metadata_provider: MetadataProvider = metadata,
) -> SourceSnapshot:
    return _resolve_wheel_snapshot(source, metadata_provider).snapshot


def _resolve_wheel_snapshot(
    source: WheelSource,
    metadata_provider: MetadataProvider,
) -> _ResolvedWheelSnapshot:
    distribution = _selected_distribution(source, metadata_provider)
    entrypoint = _selected_entrypoint(distribution, source)
    version = _normalized_version(distribution.version, "distribution version")
    kind = SourceKind(source.kind)
    if isinstance(source, EditableWheelProductSource | EditableWheelPluginSource):
        policy = (
            DeclaredTreePolicy.editable_product()
            if isinstance(source, EditableWheelProductSource)
            else DeclaredTreePolicy.editable()
        )
        tree = capture_declared_tree(
            source.source_root,
            source.source_files,
            policy,
        )
        declaration = _parse_wheel_declaration(
            source,
            tree.files,
            entrypoint.value,
            version,
        )
        identity = SourceIdentity(
            kind=kind,
            root=tree.identity.root,
            distribution=source.distribution,
            version=version,
            entrypoint_group=entrypoint.group,
            entrypoint_name=entrypoint.name,
            entrypoint_value=entrypoint.value,
            declaration_path=source.declaration_path,
            import_roots=declaration.source.import_roots,
        )
        identity = _identity_with_declaration(identity, declaration)
        snapshot = SourceSnapshot.from_identity(identity, tree.files)
        return _ResolvedWheelSnapshot(
            snapshot=snapshot,
            entrypoint=entrypoint,
            declaration=declaration,
        )

    root, files = _capture_installed_distribution(
        distribution,
        source,
        entrypoint,
        version,
    )
    declaration = _parse_wheel_declaration(
        source,
        files,
        entrypoint.value,
        version,
    )
    identity = SourceIdentity(
        kind=kind,
        root=root,
        distribution=source.distribution,
        version=version,
        entrypoint_group=entrypoint.group,
        entrypoint_name=entrypoint.name,
        entrypoint_value=entrypoint.value,
        declaration_path=source.declaration_path,
        import_roots=declaration.source.import_roots,
    )
    identity = _identity_with_declaration(identity, declaration)
    snapshot = SourceSnapshot.from_identity(identity, files)
    return _ResolvedWheelSnapshot(
        snapshot=snapshot,
        entrypoint=entrypoint,
        declaration=declaration,
    )


def _identity_with_declaration(
    identity: SourceIdentity,
    declaration: WheelDeclaration,
) -> SourceIdentity:
    if isinstance(declaration, WheelProductDeclaration):
        return replace(
            identity,
            import_roots=declaration.source.import_roots,
            product_id=declaration.manifest.product_id,
            product_version=declaration.manifest.product_version,
        )
    return replace(
        identity,
        import_roots=declaration.source.import_roots,
        plugin_id=declaration.descriptor.plugin_id,
        plugin_version=declaration.descriptor.plugin_version,
    )


def load_snapshotted_entrypoint(
    source: WheelSource,
    snapshot: SourceSnapshot,
    metadata_provider: MetadataProvider = metadata,
) -> WheelProvider:
    return cast(
        WheelProvider,
        _load_snapshotted_entrypoint_binding(
            source,
            snapshot,
            metadata_provider,
            _AuthenticatedBindingCache(),
        ),
    )


def _load_snapshotted_entrypoint_binding(
    source: WheelSource,
    snapshot: SourceSnapshot,
    metadata_provider: MetadataProvider = metadata,
    binding_cache: _AuthenticatedBindingCache | None = None,
) -> AuthenticatedProviderBinding:
    cache = binding_cache or _AuthenticatedBindingCache()
    _validate_snapshot_matches_source(source, snapshot)
    resolved = _resolve_wheel_snapshot(source, metadata_provider)
    if resolved.snapshot != snapshot:
        raise SourceSnapshotError("wheel source changed after snapshot")
    cache_key = (
        snapshot.digest,
        resolved.entrypoint.group,
        resolved.entrypoint.name,
        resolved.entrypoint.value,
    )
    with _serialized_imports():
        cached = cache.bindings.get(cache_key)
        if cached is not None:
            if cached.declaration != _static_declaration_value(resolved.declaration):
                raise SourceSnapshotError("cached provider declaration changed")
            _call_binding_declaration(cached)
            return cached

        before_modules = dict(sys.modules)
        provider_source = resolved.declaration.source
        import_plan = build_import_provenance_plan(
            provider_source,
            snapshot,
            resolved.entrypoint,
            initial_modules=before_modules,
        )
        import_plan = _extend_plan_with_cache_authority(
            import_plan,
            provider_source,
            snapshot,
            before_modules,
            cache,
        )
        import_plan = _extend_plan_with_preloaded_authority(
            import_plan,
            provider_source,
            snapshot,
            before_modules,
            cache,
        )
        import_plan = extend_import_plan_with_quarantine(
            import_plan,
            provider_source,
            snapshot,
            before_modules,
        )
        parent_attributes = _capture_parent_attributes(before_modules)
        try:
            with ImportPlanSession(provider_source, snapshot, before_modules) as session:
                session.quarantine(import_plan)
                session.preload(import_plan, _fresh_module_authority(cache))
                try:
                    loaded = resolved.entrypoint.load()
                except Exception as error:
                    raise SourceSnapshotError("cannot load snapshotted entry point") from error
                import_plan = extend_import_provenance_plan(
                    import_plan,
                    provider_source,
                    snapshot,
                    session.post_quarantine_initial_modules,
                    provider_module=_provider_module_name(loaded),
                    dependency_provenances=session.recorded_provenances,
                )
                session.preload(import_plan, _fresh_module_authority(cache))
                live_declaration = _invoke_provider_method(
                    loaded,
                    _declaration_method_name(source),
                    _declaration_kind(source),
                )
                import_plan = extend_import_provenance_plan(
                    import_plan,
                    provider_source,
                    snapshot,
                    session.post_quarantine_initial_modules,
                    dependency_provenances=session.recorded_provenances,
                )
                session.preload(import_plan, _fresh_module_authority(cache))
                authenticated_modules = session.validate(import_plan, _fresh_module_authority(cache))
                after_load = _resolve_wheel_snapshot(source, metadata_provider)
                if after_load.snapshot != snapshot:
                    raise SourceSnapshotError("wheel source changed while loading its entry point")
                declaration = _validate_provider_result(
                    source,
                    resolved.declaration,
                    _declaration_method_name(source),
                    live_declaration,
                )
                session.restore_unconsumed_quarantine(import_plan)
            _commit_authenticated_plan(cache, authenticated_modules)
            binding = AuthenticatedProviderBinding(
                _provider=cast(WheelProvider, loaded),
                declaration=declaration,
                _source=source,
                _snapshot=snapshot,
                _metadata_provider=metadata_provider,
                _cache=cache,
                _cache_key=cache_key,
                _import_plan=active_import_provenance_plan(import_plan),
            )
            cache.bindings[cache_key] = binding
            return binding
        except BaseException as primary_error:
            _restore_import_transaction(
                before_modules,
                parent_attributes,
                import_plan,
                primary_error,
            )
            raise


def _static_declaration_value(
    declaration: WheelDeclaration,
) -> ProductManifest | PluginDescriptor:
    if isinstance(declaration, WheelProductDeclaration):
        return declaration.manifest
    return declaration.descriptor


def _declaration_method_name(source: WheelSource) -> str:
    if isinstance(source, WheelProductSource | EditableWheelProductSource):
        return "manifest"
    return "descriptor"


def _declaration_kind(source: WheelSource) -> str:
    if isinstance(source, WheelProductSource | EditableWheelProductSource):
        return "product"
    return "plugin"


def _parse_wheel_declaration(
    source: WheelSource,
    files: tuple[SourceFile, ...],
    entrypoint_value: str,
    version: str,
) -> WheelDeclaration:
    declaration_file = next(
        (source_file for source_file in files if source_file.path == source.declaration_path),
        None,
    )
    if declaration_file is None:
        raise SourceSnapshotError(
            "wheel declaration path is absent from the authenticated snapshot: "
            f"{source.distribution} ({source.declaration_path})"
        )
    try:
        document = json.loads(
            declaration_file.content,
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise SourceSnapshotError("wheel declaration must be strict JSON") from error
    declaration_type: type[WheelProductDeclaration] | type[WheelPluginDeclaration]
    declaration_type = (
        WheelProductDeclaration
        if isinstance(source, WheelProductSource | EditableWheelProductSource)
        else WheelPluginDeclaration
    )
    try:
        declaration = declaration_type.model_validate(document)
    except ValidationError as error:
        raise SourceSnapshotError("wheel declaration violates its frozen schema") from error
    expected_coordinates = (
        source.distribution,
        version,
        source.entrypoint_group,
        source.entrypoint_name,
        entrypoint_value,
        source.declaration_path,
    )
    actual_coordinates = (
        declaration.source.distribution,
        declaration.source.version,
        declaration.source.entrypoint_group,
        declaration.source.entrypoint_name,
        declaration.source.entrypoint_value,
        declaration.source.declaration_path,
    )
    if actual_coordinates != expected_coordinates:
        raise SourceSnapshotError("wheel declaration source disagrees with selected source")
    return declaration


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> NoReturn:
    raise ValueError(f"non-finite JSON number: {value}")


def _normalized_distribution_name(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("distribution name must be non-empty text")
    normalized = canonicalize_name(value)
    if not normalized:
        raise ValueError("distribution name must be non-empty text")
    return normalized


def _canonical_declaration_path(value: str) -> str:
    try:
        return SourceFile.from_bytes(value, b"").path
    except (TypeError, ValueError) as error:
        raise ValueError("declaration path must be a canonical relative path") from error


def _normalized_version(value: str, kind: str) -> str:
    try:
        return str(Version(value))
    except InvalidVersion as error:
        raise SourceSnapshotError(f"invalid {kind}: {value!r}") from error


def _selected_distribution(
    source: WheelSource,
    metadata_provider: MetadataProvider,
) -> metadata.Distribution:
    try:
        distribution = metadata_provider.distribution(source.distribution)
    except metadata.PackageNotFoundError as error:
        raise SourceSnapshotError(f"installed distribution not found: {source.distribution}") from error
    try:
        installed_name = distribution.metadata["Name"]
    except KeyError:
        installed_name = None
    if not installed_name or canonicalize_name(installed_name) != source.distribution:
        raise SourceSnapshotError("selected distribution name mismatch")
    return distribution


def _selected_entrypoint(
    distribution: metadata.Distribution,
    source: WheelSource,
) -> metadata.EntryPoint:
    matches = tuple(
        entrypoint
        for entrypoint in distribution.entry_points
        if entrypoint.group == source.entrypoint_group and entrypoint.name == source.entrypoint_name
    )
    if len(matches) != 1:
        raise SourceSnapshotError("selected distribution must contain exactly one matching entry point")
    return matches[0]


def _capture_installed_distribution(
    distribution: metadata.Distribution,
    source: WheelProductSource | WheelPluginSource,
    entrypoint: metadata.EntryPoint,
    version: str,
) -> tuple[Path, tuple[SourceFile, ...]]:
    root, files, record_relative = _capture_installed_distribution_files(distribution)
    _validate_frozen_installed_metadata(
        files,
        record_relative,
        source,
        entrypoint,
        version,
    )
    return root, files


def _snapshot_installed_engine_distribution(
    distribution: metadata.Distribution,
) -> SourceSnapshot:
    """Capture an installed graph-engine wheel with its authenticated metadata."""

    version = _normalized_version(distribution.version, "engine distribution version")
    root, files, record_relative = _capture_installed_distribution_files(distribution)
    _validate_frozen_distribution_metadata(
        files,
        record_relative,
        distribution_name="graph-engine",
        version=version,
    )
    return SourceSnapshot.from_identity(
        SourceIdentity(
            kind=SourceKind.ENGINE,
            root=root,
            distribution="graph-engine",
            version=version,
            engine_installation="installed",
        ),
        files,
    )


def _capture_installed_distribution_files(
    distribution: metadata.Distribution,
) -> tuple[Path, tuple[SourceFile, ...], str]:
    root = Path(str(distribution.locate_file("")))
    record_relative = _record_relative_path(distribution, root)
    root_fd = _open_physical_root(root)
    try:
        with _directory_traversal(root_fd) as traversal:
            try:
                record_file, record_state = _read_stable_installed_file(traversal, record_relative)
            except SourceSnapshotError as error:
                raise SourceSnapshotError("installed distribution RECORD is missing or unreadable") from error
            rows = _parse_record(record_file.content)
            if record_relative not in {path for path, _hash, _size in rows}:
                raise SourceSnapshotError("installed distribution RECORD does not list itself")
            declared_paths = tuple(path for path, _hash, _size in rows)
            before_directories = _capture_directory_states(traversal, declared_paths)

            files: list[SourceFile] = []
            captured_states: dict[str, _EntryState] = {}
            for relative_path, declared_hash, declared_size in rows:
                if _is_cache_file(relative_path):
                    continue
                if relative_path == record_relative:
                    source_file = record_file
                    state = record_state
                else:
                    source_file, state = _read_stable_installed_file(traversal, relative_path)
                if declared_hash is not None:
                    _validate_record_hash(relative_path, source_file.content, declared_hash)
                if declared_size is not None and len(source_file.content) != declared_size:
                    raise SourceSnapshotError(f"RECORD size mismatch: {relative_path}")
                files.append(source_file)
                captured_states[relative_path] = state

            _snapshot_boundary("before_rescan", None)
            for relative_path, expected in captured_states.items():
                if _stat_installed_file(traversal, relative_path) != expected:
                    raise SourceSnapshotError("installed distribution changed while it was captured")
            if _capture_directory_states(traversal, declared_paths) != before_directories:
                raise SourceSnapshotError("installed distribution directories changed while it was captured")
            rescanned_record, rescanned_state = _read_stable_installed_file(traversal, record_relative)
            if rescanned_state != record_state or rescanned_record.content != record_file.content:
                raise SourceSnapshotError("installed distribution RECORD changed while it was captured")
            _snapshot_boundary("after_rescan", None)
            resolved_root = _resolve_stable_root(root, root_fd)
            return resolved_root, tuple(files), record_relative
    finally:
        os.close(root_fd)


def _record_relative_path(distribution: metadata.Distribution, root: Path) -> str:
    record = distribution.locate_file("RECORD")
    candidate = Path(str(record))
    if not candidate.is_file():
        dist_path = getattr(distribution, "_path", None)
        if dist_path is None:
            raise SourceSnapshotError("installed distribution has no RECORD")
        candidate = Path(dist_path) / "RECORD"
    try:
        return candidate.absolute().relative_to(root.absolute()).as_posix()
    except ValueError as error:
        raise SourceSnapshotError("installed distribution RECORD escapes its root") from error


def _open_physical_root(root: Path) -> int:
    if os.name != "posix" or _NOFOLLOW == 0:
        raise SourceSnapshotError("installed wheel capture requires POSIX no-follow descriptors")
    absolute = root.absolute()
    try:
        descriptor = os.open("/", _DIRECTORY_FLAGS)
    except OSError as error:
        raise SourceSnapshotError("cannot open installed distribution root") from error
    relative = ""
    for component in absolute.parts[1:]:
        relative = f"{relative}/{component}" if relative else component
        try:
            enumerated = os.stat(component, dir_fd=descriptor, follow_symlinks=False)
            child = os.open(component, _DIRECTORY_FLAGS, dir_fd=descriptor)
            opened = os.fstat(child)
        except OSError as error:
            os.close(descriptor)
            raise SourceSnapshotError(f"source root is not a no-follow directory: {relative}") from error
        if not stat.S_ISDIR(opened.st_mode) or _file_identity(opened) != _file_identity(enumerated):
            os.close(child)
            os.close(descriptor)
            raise SourceSnapshotError(f"source root changed while opening: {relative}")
        os.close(descriptor)
        descriptor = child
    return descriptor


@dataclass(frozen=True, slots=True)
class _RetainedDirectory:
    component: str
    prefix: str
    fd: int
    state: _EntryState


def _open_directory_component(
    parent_fd: int, component: str, prefix: str, enumerated: os.stat_result
) -> tuple[int, _EntryState]:
    child = os.open(component, _DIRECTORY_FLAGS, dir_fd=parent_fd)
    try:
        opened = os.fstat(child)
        if not stat.S_ISDIR(opened.st_mode) or _file_identity(opened) != _file_identity(enumerated):
            raise SourceSnapshotError(f"RECORD directory changed while opening: {prefix}")
        return child, _file_state(opened)
    except BaseException:
        try:
            os.close(child)
        except OSError:
            pass
        raise


def _open_parent_uncached(
    root_fd: int, relative_path: str
) -> tuple[int, str, tuple[tuple[str, _EntryState], ...]]:
    parts = relative_path.split("/")
    parent_fd = os.dup(root_fd)
    walked = ""
    states: list[tuple[str, _EntryState]] = []
    try:
        for component in parts[:-1]:
            walked = f"{walked}/{component}" if walked else component
            enumerated = os.stat(component, dir_fd=parent_fd, follow_symlinks=False)
            child, state = _open_directory_component(parent_fd, component, walked, enumerated)
            try:
                os.close(parent_fd)
            except BaseException:
                try:
                    os.close(child)
                except OSError:
                    pass
                raise
            parent_fd = child
            states.append((walked, state))
        return parent_fd, parts[-1], tuple(states)
    except BaseException:
        try:
            os.close(parent_fd)
        except OSError:
            pass
        raise


class _DirectoryTraversal:
    """Reuse only directory handles acquired by this one installed-wheel capture."""

    def __init__(self, root_fd: int) -> None:
        self.root_fd = root_fd
        self._stack: list[_RetainedDirectory] = []

    def close(self) -> None:
        self._close_suffix(0)

    def _close_suffix(self, length: int) -> None:
        first_error: OSError | None = None
        while len(self._stack) > length:
            retained = self._stack.pop()
            try:
                os.close(retained.fd)
            except OSError as error:
                if first_error is None:
                    first_error = error
        if first_error is not None:
            raise first_error

    def _cached_parent(self, relative_path: str) -> tuple[int, str, tuple[tuple[str, _EntryState], ...]]:
        parts = relative_path.split("/")
        parents = parts[:-1]
        common = 0
        while (
            common < min(len(parents), len(self._stack)) and parents[common] == self._stack[common].component
        ):
            common += 1
        self._close_suffix(common)

        parent_fd = self.root_fd
        walked = ""
        states: list[tuple[str, _EntryState]] = []
        for index, component in enumerate(parents):
            walked = f"{walked}/{component}" if walked else component
            enumerated = os.stat(component, dir_fd=parent_fd, follow_symlinks=False)
            if index < common:
                retained = self._stack[index]
                opened = os.fstat(retained.fd)
                if (
                    not stat.S_ISDIR(enumerated.st_mode)
                    or not stat.S_ISDIR(opened.st_mode)
                    or _file_state(enumerated) != retained.state
                    or _file_state(opened) != retained.state
                ):
                    raise SourceSnapshotError(f"RECORD directory changed while traversing: {walked}")
                parent_fd = retained.fd
                states.append((walked, retained.state))
            else:
                child, state = _open_directory_component(parent_fd, component, walked, enumerated)
                self._stack.append(_RetainedDirectory(component, walked, child, state))
                parent_fd = child
                states.append((walked, state))
        return parent_fd, parts[-1], tuple(states)

    @contextmanager
    def parent(
        self, relative_path: str, *, reserve_file_fd: bool = False
    ) -> Iterator[tuple[int, str, tuple[tuple[str, _EntryState], ...]]]:
        if relative_path.count("/") <= _MAX_RETAINED_DIRECTORIES:
            reserved_fd = -1
            try:
                if reserve_file_fd:
                    reserved_fd = os.dup(self.root_fd)
                cached = self._cached_parent(relative_path)
            except OSError as error:
                if reserved_fd >= 0:
                    try:
                        os.close(reserved_fd)
                    except OSError:
                        pass
                if error.errno not in (errno.EMFILE, errno.ENFILE):
                    raise
            except BaseException:
                if reserved_fd >= 0:
                    try:
                        os.close(reserved_fd)
                    except OSError:
                        pass
                raise
            else:
                if reserved_fd >= 0:
                    os.close(reserved_fd)
                yield cached
                return
        self.close()
        parent_fd, name, states = _open_parent_uncached(self.root_fd, relative_path)
        try:
            yield parent_fd, name, states
        except BaseException:
            try:
                os.close(parent_fd)
            except OSError:
                pass
            raise
        else:
            os.close(parent_fd)


@contextmanager
def _directory_traversal(root_fd: int) -> Iterator[_DirectoryTraversal]:
    traversal = _DirectoryTraversal(root_fd)
    try:
        yield traversal
    except BaseException:
        try:
            traversal.close()
        except OSError:
            pass
        raise
    else:
        traversal.close()


def _capture_directory_states(
    traversal: _DirectoryTraversal,
    relative_paths: tuple[str, ...],
) -> tuple[tuple[str, _EntryState], ...]:
    states: dict[str, _EntryState] = {"": _file_state(os.fstat(traversal.root_fd))}
    for relative_path in relative_paths:
        try:
            with traversal.parent(relative_path) as (_parent_fd, _name, visited):
                for walked, state in visited:
                    previous = states.setdefault(walked, state)
                    if previous != state:
                        raise SourceSnapshotError(f"RECORD directory changed while enumerating: {walked}")
        except OSError as error:
            raise SourceSnapshotError(f"cannot capture RECORD directory state: {relative_path}") from error
    return tuple(sorted(states.items()))


def _read_stable_installed_file(
    traversal: _DirectoryTraversal,
    relative_path: str,
) -> tuple[SourceFile, _EntryState]:
    with traversal.parent(relative_path, reserve_file_fd=True) as (parent_fd, name, _visited):
        return _read_stable_installed_file_at(parent_fd, name, relative_path)


def _read_stable_installed_file_at(
    parent_fd: int, name: str, relative_path: str
) -> tuple[SourceFile, _EntryState]:
    descriptor = -1
    try:
        try:
            enumerated = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError as error:
            raise SourceSnapshotError(f"missing RECORD file: {relative_path}") from error
        _snapshot_boundary("before_component_open", relative_path)
        try:
            descriptor = os.open(name, _FILE_FLAGS, dir_fd=parent_fd)
            opened = os.fstat(descriptor)
        except OSError as error:
            raise SourceSnapshotError(
                f"RECORD path is not a regular no-follow file: {relative_path}"
            ) from error
        if not stat.S_ISREG(opened.st_mode) or _file_identity(opened) != _file_identity(enumerated):
            raise SourceSnapshotError(f"RECORD file changed while opening: {relative_path}")

        _snapshot_boundary("before_read", relative_path)
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        _snapshot_boundary("before_final_stat", relative_path)
        final = os.fstat(descriptor)
        _snapshot_boundary("after_final_stat", relative_path)
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if _file_state(final) != _file_state(opened) or _file_identity(current) != _file_identity(opened):
            raise SourceSnapshotError(f"RECORD file changed while it was read: {relative_path}")
        return SourceFile.from_bytes(relative_path, b"".join(chunks)), _file_state(final)
    except OSError as error:
        raise SourceSnapshotError(f"cannot read RECORD file safely: {relative_path}") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _stat_installed_file(traversal: _DirectoryTraversal, relative_path: str) -> _EntryState:
    with traversal.parent(relative_path) as (parent_fd, name, _visited):
        try:
            status = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError as error:
            raise SourceSnapshotError(f"cannot rescan RECORD file: {relative_path}") from error
        if not stat.S_ISREG(status.st_mode):
            raise SourceSnapshotError(f"RECORD path is not a regular no-follow file: {relative_path}")
        return _file_state(status)


def _resolve_stable_root(root: Path, root_fd: int) -> Path:
    try:
        resolved = root.resolve(strict=True)
        named = os.stat(resolved, follow_symlinks=False)
        opened = os.fstat(root_fd)
    except OSError as error:
        raise SourceSnapshotError("cannot authenticate installed distribution root") from error
    if not stat.S_ISDIR(named.st_mode) or _file_identity(named) != _file_identity(opened):
        raise SourceSnapshotError("installed distribution root changed while it was captured")
    return resolved


def _file_identity(status: os.stat_result) -> tuple[int, int, int]:
    return status.st_dev, status.st_ino, status.st_mode


def _file_state(status: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        status.st_dev,
        status.st_ino,
        status.st_mode,
        status.st_size,
        status.st_mtime_ns,
        status.st_ctime_ns,
    )


def _snapshot_boundary(_phase: str, _relative_path: str | None) -> None:
    """Fault-injection seam for installed wheel capture tests."""


def _parse_record(record_bytes: bytes) -> tuple[tuple[str, str | None, int | None], ...]:
    try:
        text = record_bytes.decode("utf-8")
        rows = tuple(csv.reader(text.splitlines()))
    except (UnicodeDecodeError, csv.Error) as error:
        raise SourceSnapshotError("installed distribution RECORD is malformed") from error
    parsed: list[tuple[str, str | None, int | None]] = []
    seen: set[str] = set()
    for row in rows:
        if not row:
            continue
        if len(row) != 3:
            raise SourceSnapshotError("installed distribution RECORD is malformed")
        relative_path, declared_hash, declared_size = row
        if relative_path in seen:
            raise SourceSnapshotError(f"duplicate RECORD path: {relative_path}")
        seen.add(relative_path)
        if any(part == ".." for part in relative_path.split("/")):
            continue
        try:
            _validate_canonical_relative_path(relative_path)
        except (TypeError, ValueError) as error:
            raise SourceSnapshotError(f"unsafe RECORD path: {relative_path!r}") from error
        try:
            size = int(declared_size) if declared_size else None
        except ValueError as error:
            raise SourceSnapshotError(f"invalid RECORD size: {relative_path}") from error
        parsed.append((relative_path, declared_hash or None, size))
    return tuple(parsed)


def _validate_frozen_installed_metadata(
    files: tuple[SourceFile, ...] | list[SourceFile],
    record_relative: str,
    source: WheelProductSource | WheelPluginSource,
    entrypoint: metadata.EntryPoint,
    version: str,
) -> None:
    captured = _validate_frozen_distribution_metadata(
        files,
        record_relative,
        distribution_name=source.distribution,
        version=version,
    )
    dist_info = record_relative.rsplit("/", 1)[0]
    entrypoints_path = f"{dist_info}/entry_points.txt"
    if entrypoints_path not in captured:
        raise SourceSnapshotError("installed distribution authenticated metadata is incomplete")
    parser = _EntryPointConfigParser(interpolation=None, delimiters=("=",), strict=True)
    try:
        parser.read_string(captured[entrypoints_path].decode("utf-8"))
        matches = tuple(
            value.strip()
            for name, value in parser.items(source.entrypoint_group)
            if name.strip() == source.entrypoint_name
        )
    except (UnicodeDecodeError, configparser.Error, KeyError) as error:
        raise SourceSnapshotError("installed distribution entry point metadata is malformed") from error
    if matches != (entrypoint.value,):
        raise SourceSnapshotError("installed distribution entry point metadata disagrees with selection")


def _validate_frozen_distribution_metadata(
    files: tuple[SourceFile, ...] | list[SourceFile],
    record_relative: str,
    *,
    distribution_name: str,
    version: str,
) -> dict[str, bytes]:
    dist_info = record_relative.rsplit("/", 1)[0]
    metadata_path = f"{dist_info}/METADATA"
    captured = {source_file.path: source_file.content for source_file in files}
    if metadata_path not in captured:
        raise SourceSnapshotError("installed distribution authenticated metadata is incomplete")
    try:
        message = BytesParser(policy=compat32).parsebytes(captured[metadata_path])
        frozen_name = message["Name"]
        frozen_version = message["Version"]
    except Exception as error:
        raise SourceSnapshotError("installed distribution METADATA is malformed") from error
    if (
        not frozen_name
        or canonicalize_name(frozen_name) != canonicalize_name(distribution_name)
        or not frozen_version
        or _normalized_version(frozen_version, "frozen distribution version") != version
    ):
        raise SourceSnapshotError("installed distribution METADATA disagrees with source identity")
    return captured


def _validate_record_hash(relative_path: str, content: bytes, declared_hash: str) -> None:
    try:
        algorithm, encoded = declared_hash.split("=", 1)
        digest = hashlib.new(algorithm, content).digest()
        actual = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    except (ValueError, UnicodeError) as error:
        raise SourceSnapshotError(f"invalid RECORD hash: {relative_path}") from error
    if actual != encoded:
        raise SourceSnapshotError(f"RECORD hash mismatch: {relative_path}")


def _is_cache_file(relative_path: str) -> bool:
    parts = relative_path.split("/")
    return "__pycache__" in parts or relative_path.endswith((".pyc", ".pyo"))


def _validate_snapshot_matches_source(source: WheelSource, snapshot: SourceSnapshot) -> None:
    identity = snapshot.identity
    if (
        identity.kind != SourceKind(source.kind)
        or identity.distribution != source.distribution
        or identity.entrypoint_group != source.entrypoint_group
        or identity.entrypoint_name != source.entrypoint_name
        or identity.declaration_path != source.declaration_path
    ):
        raise SourceSnapshotError("snapshot identity does not match requested wheel source")
    if (
        isinstance(source, EditableWheelProductSource | EditableWheelPluginSource)
        and identity.root != source.source_root.resolve()
    ):
        raise SourceSnapshotError("snapshot root does not match requested editable source")
    if isinstance(source, EditableWheelProductSource | EditableWheelPluginSource):
        declared_paths = _canonical_declared_paths(source.source_files)
        snapshot_paths = tuple(source_file.path for source_file in snapshot.files)
        if declared_paths != snapshot_paths:
            raise SourceSnapshotError("snapshot file tuple does not match requested editable source")


def _canonical_declared_paths(files: tuple[str, ...]) -> tuple[str, ...]:
    paths: list[str] = []
    seen: set[str] = set()
    for path in files:
        try:
            canonical = SourceFile.from_bytes(path, b"").path
        except (TypeError, ValueError) as error:
            raise SourceSnapshotError(f"unsafe declared source path: {path!r}") from error
        if canonical in seen:
            raise SourceSnapshotError(f"duplicate declared source path: {canonical!r}")
        seen.add(canonical)
        paths.append(canonical)
    return tuple(sorted(paths))


def _fresh_module_authority(cache: _AuthenticatedBindingCache):
    def owns(item: ModuleImportPlan, module: ModuleType) -> bool:
        authenticated = cache.modules.get(item.module_name)
        if authenticated is not None:
            return authenticated.module is module and any(
                provenance.authenticates_same_module(item.provenance)
                for provenance in authenticated.provenances
            )
        return _preloaded_physical_authority(item, module)

    return owns


def _preloaded_physical_authority(item: ModuleImportPlan, module: ModuleType) -> bool:
    try:
        _validate_imported_module(item, module)
    except SourceSnapshotError:
        return False
    return item.provenance.physical_sha256 is not None


def _cached_module_authority(cache: _AuthenticatedBindingCache, source_digest: str):
    def owns(item: ModuleImportPlan, module: ModuleType) -> bool:
        authenticated = cache.modules.get(item.module_name)
        return (
            authenticated is not None
            and authenticated.module is module
            and item.provenance.source_digest == source_digest
            and item.provenance in authenticated.provenances
        )

    return owns


def _extend_plan_with_cache_authority(
    plan: ImportProvenancePlan,
    source: ProviderSource,
    snapshot: SourceSnapshot,
    initial_modules: Mapping[str, ModuleType],
    cache: _AuthenticatedBindingCache,
) -> ImportProvenancePlan:
    extended = plan
    for module_name, authenticated in sorted(cache.modules.items()):
        if initial_modules.get(module_name) is not authenticated.module:
            continue
        try:
            candidate = extend_import_provenance_plan(
                extended,
                source,
                snapshot,
                initial_modules,
                authorized_modules=(module_name,),
            )
        except SourceSnapshotError:
            continue
        if not _fresh_module_authority(cache)(candidate.module(module_name), authenticated.module):
            continue
        extended = candidate
    return extended


def _extend_plan_with_preloaded_authority(
    plan: ImportProvenancePlan,
    source: ProviderSource,
    snapshot: SourceSnapshot,
    initial_modules: Mapping[str, ModuleType],
    cache: _AuthenticatedBindingCache,
) -> ImportProvenancePlan:
    extended = plan
    planned = {item.module_name for item in plan.modules}
    if not any(
        item.validate
        and isinstance(initial_modules.get(item.module_name), ModuleType)
        and cache.modules.get(item.module_name) is None
        and _preloaded_physical_authority(item, initial_modules[item.module_name])
        for item in plan.modules
    ):
        return plan
    for module_name, module in sorted(initial_modules.items()):
        if module_name in planned or not isinstance(module, ModuleType):
            continue
        try:
            candidate = extend_import_provenance_plan(
                extended,
                source,
                snapshot,
                initial_modules,
                authorized_modules=(module_name,),
            )
        except SourceSnapshotError:
            continue
        if not _preloaded_physical_authority(candidate.module(module_name), module):
            continue
        extended = candidate
        planned = {candidate_item.module_name for candidate_item in extended.modules}
    return extended


def _commit_authenticated_plan(
    cache: _AuthenticatedBindingCache,
    authenticated_modules: tuple[tuple[ModuleImportPlan, ModuleType], ...],
) -> None:
    for item, module in authenticated_modules:
        if not item.commit:
            continue
        authenticated = cache.modules.get(item.module_name)
        if (
            authenticated is not None
            and authenticated.module is not module
            and any(
                prior.source_digest == item.provenance.source_digest for prior in authenticated.provenances
            )
        ):
            raise SourceSnapshotError(f"provider module changed before authority commit: {item.module_name}")
    for item, module in authenticated_modules:
        if not item.commit:
            continue
        authenticated = cache.modules.get(item.module_name)
        if authenticated is None or authenticated.module is not module:
            cache.modules[item.module_name] = _AuthenticatedModule(
                module,
                frozenset({item.provenance}),
            )
        elif authenticated.module is module:
            cache.modules[item.module_name] = _AuthenticatedModule(
                module,
                authenticated.provenances | {item.provenance},
            )


def _restore_modules(before: dict[str, ModuleType]) -> None:
    for module_name in tuple(sys.modules):
        if module_name not in before:
            del sys.modules[module_name]
    for module_name, module in before.items():
        if module_name not in sys.modules or sys.modules[module_name] is not module:
            sys.modules[module_name] = module


def _capture_parent_attributes(
    before_modules: dict[str, ModuleType],
) -> _ParentAttributeSnapshot:
    packages = tuple(
        _ParentPackageState(module_name, module, dict(module.__dict__))
        for module_name, module in sorted(before_modules.items())
        if isinstance(module, ModuleType) and getattr(module, "__path__", None) is not None
    )
    return _ParentAttributeSnapshot(packages=packages)


def _restore_import_transaction(
    before_modules: dict[str, ModuleType],
    parent_attributes: _ParentAttributeSnapshot,
    import_plan: ImportProvenancePlan,
    primary_error: BaseException,
) -> None:
    rollback_errors: list[BaseException] = []
    current_modules = dict(sys.modules)
    affected_module_names = {item.module_name for item in import_plan.modules if item.rollback}
    for module_name in set(before_modules).union(current_modules):
        before = before_modules[module_name] if module_name in before_modules else _MISSING_MODULE
        current = current_modules[module_name] if module_name in current_modules else _MISSING_MODULE
        if before is not current:
            affected_module_names.add(module_name)
    try:
        _restore_modules(before_modules)
    except BaseException as error:  # pragma: no cover - process-global mapping failure.
        rollback_errors.append(error)
    package_by_name = {state.parent_name: state for state in parent_attributes.packages}
    affected_attributes: set[tuple[str, str]] = set()
    for module_name in affected_module_names:
        parts = module_name.split(".")
        for index in range(1, len(parts)):
            affected_attributes.add((".".join(parts[:index]), parts[index]))
    for parent_name, child_name in sorted(
        affected_attributes,
        key=lambda item: (-item[0].count("."), item[0], item[1]),
    ):
        state = package_by_name.get(parent_name)
        if state is None:
            continue
        try:
            if child_name in state.attributes:
                state.parent.__dict__[child_name] = state.attributes[child_name]
            else:
                state.parent.__dict__.pop(child_name, None)
        except BaseException as error:  # pragma: no cover - native module dictionary failure.
            rollback_errors.append(error)
    if rollback_errors:
        indeterminate = SourceSnapshotError("provider import transaction rollback is indeterminate")
        indeterminate.add_note(
            f"primary provider import failure: {type(primary_error).__name__}: {primary_error}"
        )
        for rollback_error in rollback_errors:
            indeterminate.add_note(f"rollback failure: {type(rollback_error).__name__}: {rollback_error}")
        raise indeterminate from primary_error


def _provider_module_name(provider: object) -> str:
    provider_module = (
        provider.__name__ if isinstance(provider, ModuleType) else getattr(provider, "__module__", None)
    )
    if not isinstance(provider_module, str) or not provider_module:
        raise SourceSnapshotError("loaded provider has no verifiable module origin")
    return provider_module


def _invoke_provider_method(
    loaded: object,
    method_name: str,
    kind: str,
    *args: object,
) -> object:
    method = getattr(loaded, method_name, None)
    if not callable(method):
        raise SourceSnapshotError(f"loaded {kind} provider has no callable {method_name}")
    try:
        return method(*args)
    except Exception as error:
        raise SourceSnapshotError(f"loaded {kind} provider {method_name} failed") from error


def _validate_provider_result(
    source: WheelSource,
    static_declaration: WheelDeclaration,
    method_name: str,
    result: object,
) -> ProductManifest | PluginDescriptor | PluginContribution:
    if isinstance(source, WheelProductSource | EditableWheelProductSource):
        if method_name != "manifest" or not isinstance(result, ProductManifest):
            raise SourceSnapshotError("loaded product provider returned an invalid manifest")
        declaration = cast(WheelProductDeclaration, static_declaration)
        if result != declaration.manifest:
            raise SourceSnapshotError("loaded product manifest disagrees with static declaration")
        return result

    declaration = cast(WheelPluginDeclaration, static_declaration)
    if method_name == "descriptor":
        if not isinstance(result, PluginDescriptor):
            raise SourceSnapshotError("loaded plugin provider returned an invalid descriptor")
        if result != declaration.descriptor:
            raise SourceSnapshotError("loaded plugin descriptor disagrees with static declaration")
        return result
    if method_name == "contribute":
        if type(result) is not PluginContribution:
            raise SourceSnapshotError("loaded plugin provider returned an invalid contribution")
        validate_contribution(declaration.descriptor, result)
        return result
    raise SourceSnapshotError(f"unsupported authenticated plugin provider call: {method_name}")


def _authenticated_contribution(
    binding: AuthenticatedProviderBinding,
    contribution: PluginContribution,
    authenticated_modules: tuple[tuple[ModuleImportPlan, ModuleType], ...],
) -> AuthenticatedContribution:
    owner_id = cast(PluginDescriptor, binding.declaration).plugin_id
    source_key = SourceKey(SourceRole.PLUGIN, owner_id)
    authorities: list[ExecutableAuthority] = []
    for capability_id, handler in contribution.task_handlers.items():
        authorities.append(
            _executable_binding(
                binding,
                authenticated_modules,
                handler,
                ExecutableKind.TASK_HANDLER,
                capability_id,
                owner_id,
                source_key,
            )
        )
    for capability_id, validator in contribution.commit_validators.items():
        authorities.append(
            _executable_binding(
                binding,
                authenticated_modules,
                validator,
                ExecutableKind.COMMIT_VALIDATOR,
                capability_id,
                owner_id,
                source_key,
            )
        )
    ordered = tuple(
        sorted(authorities, key=lambda item: (item.provenance.registry_id, item.provenance.kind.value))
    )
    descriptor = cast(PluginDescriptor, binding.declaration)
    authority = ContributionAuthority(
        provider_binding=binding,
        descriptor=descriptor,
        owner_id=owner_id,
        source_key=source_key,
        source_digest=binding._snapshot.digest,
        contribution=contribution,
        authorities=ordered,
    )
    return AuthenticatedContribution(
        owner_id=owner_id,
        source_key=source_key,
        source_digest=binding._snapshot.digest,
        descriptor=descriptor,
        contribution=contribution,
        executables=tuple(item.provenance for item in ordered),
        authority=authority,
    )


def _executable_binding(
    binding: AuthenticatedProviderBinding,
    authenticated_modules: tuple[tuple[ModuleImportPlan, ModuleType], ...],
    executable: object,
    kind: ExecutableKind,
    registry_id: str,
    owner_id: str,
    source_key: SourceKey,
) -> ExecutableAuthority:
    function, bound_self, descriptor, callable_path, binding_mode = _resolve_executable_callable(
        executable,
        kind.slot,
        authenticated_modules,
        registry_id,
    )
    matches = tuple(
        (item, module) for item, module in authenticated_modules if module.__dict__ is function.__globals__
    )
    if len(matches) != 1:
        raise SourceSnapshotError(
            f"retained executable definition is outside its selected plugin source: {registry_id}"
        )
    item, module = matches[0]
    if sys.modules.get(item.module_name) is not module:
        raise SourceSnapshotError(
            f"retained executable module is not the exact imported object: {registry_id}"
        )
    module_provenance = _executable_module_projection(binding._snapshot, item)
    provenance = ExecutableProvenance.create(
        kind=kind,
        registry_id=registry_id,
        owner_id=owner_id,
        source_key=source_key,
        source_digest=binding._snapshot.digest,
        module=module_provenance,
        callable_path=callable_path,
        binding_mode=binding_mode,
    )
    return ExecutableAuthority(
        executable=executable,
        function=function,
        bound_self=bound_self,
        descriptor=descriptor,
        provenance=provenance,
    )


def _resolve_executable_callable(
    executable: object,
    slot: str,
    authenticated_modules: tuple[tuple[ModuleImportPlan, ModuleType], ...],
    registry_id: str,
) -> tuple[FunctionType, object | None, object, str, ExecutableBindingMode]:
    if isinstance(executable, ModuleType):
        if type(executable) is not ModuleType:
            raise SourceSnapshotError(
                f"retained module executable uses custom attribute dispatch: {registry_id}"
            )
    else:
        executable_type = type(executable)
        if inspect.getattr_static(executable_type, "__getattribute__") is not object.__getattribute__:
            raise SourceSnapshotError(f"retained executable uses custom attribute dispatch: {registry_id}")
        try:
            instance_attributes = vars(executable)
        except TypeError:
            instance_attributes = {}
        if slot in instance_attributes:
            raise SourceSnapshotError(f"retained executable shadows its execution slot: {registry_id}")
    method = getattr(executable, slot, None)
    if not callable(method):
        raise SourceSnapshotError(f"retained executable has no callable {slot}: {registry_id}")
    if isinstance(executable, ModuleType):
        descriptor = executable.__dict__.get(slot)
        if not isinstance(descriptor, FunctionType) or method is not descriptor:
            raise SourceSnapshotError(
                f"retained module executable has no stable function binding: {registry_id}"
            )
        _require_exact_authenticated_module(executable, authenticated_modules, registry_id)
        return (
            descriptor,
            None,
            descriptor,
            f"{_module_plan_name(executable, authenticated_modules)}:{slot}",
            ExecutableBindingMode.MODULE_FUNCTION,
        )

    executable_type = type(executable)
    try:
        descriptor = inspect.getattr_static(executable_type, slot)
    except AttributeError as error:
        raise SourceSnapshotError(
            f"retained executable has no static descriptor for {slot}: {registry_id}"
        ) from error
    declaring_type = next(
        (candidate for candidate in executable_type.__mro__ if candidate.__dict__.get(slot) is descriptor),
        None,
    )
    if declaring_type is None:
        raise SourceSnapshotError(f"retained executable descriptor is not statically owned: {registry_id}")
    if declaring_type is not executable_type:
        raise SourceSnapshotError(
            f"retained executable must own its execution descriptor directly: {registry_id}"
        )
    if isinstance(descriptor, FunctionType):
        if (
            not isinstance(method, MethodType)
            or method.__self__ is not executable
            or method.__func__ is not descriptor
        ):
            raise SourceSnapshotError(
                f"retained executable instance method binding is unstable: {registry_id}"
            )
        function = descriptor
        bound_self: object | None = executable
        mode = ExecutableBindingMode.INSTANCE_METHOD
    elif type(descriptor) is staticmethod:
        function = descriptor.__func__
        if not isinstance(function, FunctionType) or method is not function:
            raise SourceSnapshotError(f"retained executable static method binding is unstable: {registry_id}")
        bound_self = None
        mode = ExecutableBindingMode.STATIC_METHOD
    elif type(descriptor) is classmethod:
        function = descriptor.__func__
        if (
            not isinstance(function, FunctionType)
            or not isinstance(method, MethodType)
            or method.__self__ is not executable_type
            or method.__func__ is not function
        ):
            raise SourceSnapshotError(f"retained executable class method binding is unstable: {registry_id}")
        bound_self = executable_type
        mode = ExecutableBindingMode.CLASS_METHOD
    else:
        raise SourceSnapshotError(
            f"retained executable uses an unsupported dynamic descriptor: {registry_id}"
        )
    module_name, class_name = _class_binding(
        executable_type,
        function,
        slot,
        authenticated_modules,
        registry_id,
    )
    return (
        function,
        bound_self,
        descriptor,
        f"{module_name}:{class_name}.{slot}",
        mode,
    )


def _module_plan_name(
    module: ModuleType,
    authenticated_modules: tuple[tuple[ModuleImportPlan, ModuleType], ...],
) -> str:
    matches = tuple(item.module_name for item, candidate in authenticated_modules if candidate is module)
    if len(matches) != 1:
        raise SourceSnapshotError("retained executable module lacks unique plan membership")
    return matches[0]


def _require_exact_authenticated_module(
    module: ModuleType,
    authenticated_modules: tuple[tuple[ModuleImportPlan, ModuleType], ...],
    registry_id: str,
) -> None:
    module_name = _module_plan_name(module, authenticated_modules)
    if sys.modules.get(module_name) is not module:
        raise SourceSnapshotError(
            f"retained executable module is not the exact imported object: {registry_id}"
        )


def _class_binding(
    value: type[object],
    function: FunctionType,
    slot: str,
    authenticated_modules: tuple[tuple[ModuleImportPlan, ModuleType], ...],
    registry_id: str,
) -> tuple[str, str]:
    module_matches = tuple(
        (item.module_name, module)
        for item, module in authenticated_modules
        if module.__dict__ is function.__globals__
    )
    if len(module_matches) != 1:
        raise SourceSnapshotError(
            f"retained executable class lacks one authenticated defining module: {registry_id}"
        )
    module_name, module = module_matches[0]
    suffix = f".{slot}"
    code_qualname = function.__code__.co_qualname
    if not code_qualname.endswith(suffix):
        raise SourceSnapshotError(f"retained executable function lacks a canonical class path: {registry_id}")
    class_name = code_qualname[: -len(suffix)]
    segments = class_name.split(".")
    if any(not segment.isidentifier() for segment in segments) or "<locals>" in segments:
        raise SourceSnapshotError(
            f"retained executable class path is not statically resolvable: {registry_id}"
        )
    if type(value) is not type:
        raise SourceSnapshotError(f"retained executable class uses a custom metaclass: {registry_id}")
    actual_module = value.__module__
    actual_qualname = value.__qualname__
    if actual_module != module_name or actual_qualname != class_name:
        raise SourceSnapshotError(
            "retained executable class metadata disagrees with its defining code: "
            f"{registry_id}; {actual_module!r}:{actual_qualname!r} != {module_name!r}:{class_name!r}"
        )
    resolved: object = module
    for segment in segments:
        namespace = getattr(resolved, "__dict__", None)
        if not isinstance(namespace, Mapping) or segment not in namespace:
            raise SourceSnapshotError(
                f"retained executable class lacks its canonical static binding: {registry_id}"
            )
        resolved = namespace[segment]
    if resolved is not value or sys.modules.get(module_name) is not module:
        raise SourceSnapshotError(
            f"retained executable class binding is not the exact imported object: {registry_id}"
        )
    return module_name, class_name


def _executable_module_projection(
    snapshot: SourceSnapshot,
    item: ModuleImportPlan,
) -> ExecutableModuleProvenance:
    provenance = item.provenance
    if (
        provenance.source_digest != snapshot.digest
        or provenance.canonical_origin is None
        or provenance.physical_sha256 is None
    ):
        raise SourceSnapshotError(
            f"retained executable has no authenticated physical definition: {item.module_name}"
        )
    try:
        relative_origin = provenance.canonical_origin.relative_to(snapshot.identity.root).as_posix()
        authenticated_locations = tuple(
            sorted(
                location.relative_to(snapshot.identity.root).as_posix()
                for location in provenance.authenticated_locations
            )
        )
    except ValueError as error:
        raise SourceSnapshotError(
            f"retained executable provenance escapes its selected source: {item.module_name}"
        ) from error
    return ExecutableModuleProvenance(
        module_name=item.module_name,
        standard_loader=provenance.standard_loader,
        standard_is_package=provenance.standard_is_package,
        relative_origin=relative_origin,
        authenticated_locations=authenticated_locations,
        physical_sha256=provenance.physical_sha256,
        source_digest=provenance.source_digest,
    )


def _authenticate_bound_executable(
    binding: AuthenticatedProviderBinding,
    contribution_authority: ContributionAuthority,
    executable: object,
    expected: ExecutableProvenance,
) -> None:
    with _serialized_imports():
        if binding._cache.bindings.get(binding._cache_key) is not binding:
            raise SourceSnapshotError("provider binding is not owned by the current RegistryPlatform")
        resolved = _resolve_wheel_snapshot(binding._source, binding._metadata_provider)
        if resolved.snapshot != binding._snapshot:
            raise SourceSnapshotError("wheel source changed before executable authentication")
        if contribution_authority.provider_binding is not binding or not any(
            candidate is contribution_authority for candidate in binding._issued_contributions
        ):
            raise SourceSnapshotError("executable authority was not issued by this provider binding")
        try:
            authority = contribution_authority.authority(expected.kind, expected.registry_id)
        except KeyError as error:
            raise SourceSnapshotError(
                "executable is not a member of the authenticated contribution generation"
            ) from error
        if authority.executable is not executable or authority.provenance != expected:
            raise SourceSnapshotError("executable is not an exact member of the authenticated contribution")
        authenticated_modules: list[tuple[ModuleImportPlan, ModuleType]] = []
        for item in binding._import_plan.modules:
            cached = binding._cache.modules.get(item.module_name)
            current = sys.modules.get(item.module_name)
            if (
                cached is None
                or not isinstance(current, ModuleType)
                or cached.module is not current
                or item.provenance not in cached.provenances
            ):
                raise SourceSnapshotError("executable plan lacks current platform cache authority")
            authenticated_modules.append((item, current))
        actual = _executable_binding(
            binding,
            tuple(authenticated_modules),
            executable,
            expected.kind,
            expected.registry_id,
            expected.owner_id,
            expected.source_key,
        )
        if (
            actual.provenance != expected
            or actual.function is not authority.function
            or actual.bound_self is not authority.bound_self
            or actual.descriptor is not authority.descriptor
        ):
            raise SourceSnapshotError("executable implementation provenance drifted")


def _call_binding_declaration(binding: AuthenticatedProviderBinding) -> object:
    return _authenticated_provider_call(
        binding,
        _declaration_method_name(binding._source),
    )


def _authenticated_provider_call(
    binding: AuthenticatedProviderBinding,
    method_name: str,
    *args: object,
) -> object:
    with _serialized_imports():
        if binding._cache.bindings.get(binding._cache_key) is not binding:
            raise SourceSnapshotError("provider binding is not owned by the current RegistryPlatform")
        resolved = _resolve_wheel_snapshot(binding._source, binding._metadata_provider)
        if resolved.snapshot != binding._snapshot:
            raise SourceSnapshotError("wheel source changed before authenticated provider call")
        before_modules = dict(sys.modules)
        import_plan = rebind_import_provenance_plan(
            binding._import_plan,
            before_modules,
            _cached_module_authority(binding._cache, binding._snapshot.digest),
        )
        import_plan = _extend_plan_with_cache_authority(
            import_plan,
            resolved.declaration.source,
            binding._snapshot,
            before_modules,
            binding._cache,
        )
        import_plan = _extend_plan_with_preloaded_authority(
            import_plan,
            resolved.declaration.source,
            binding._snapshot,
            before_modules,
            binding._cache,
        )
        import_plan = extend_import_plan_with_quarantine(
            import_plan,
            resolved.declaration.source,
            binding._snapshot,
            before_modules,
        )
        parent_attributes = _capture_parent_attributes(before_modules)
        try:
            with ImportPlanSession(
                resolved.declaration.source,
                binding._snapshot,
                before_modules,
            ) as session:
                session.quarantine(import_plan)
                session.preload(import_plan, _fresh_module_authority(binding._cache))
                session.validate(import_plan, _fresh_module_authority(binding._cache))
                result = _invoke_provider_method(
                    binding._provider,
                    method_name,
                    _declaration_kind(binding._source),
                    *args,
                )
                import_plan = extend_import_provenance_plan(
                    import_plan,
                    resolved.declaration.source,
                    binding._snapshot,
                    session.post_quarantine_initial_modules,
                    dependency_provenances=session.recorded_provenances,
                )
                session.preload(import_plan, _fresh_module_authority(binding._cache))
                authenticated_modules = session.validate(
                    import_plan,
                    _fresh_module_authority(binding._cache),
                )
                after_call = _resolve_wheel_snapshot(binding._source, binding._metadata_provider)
                if after_call.snapshot != binding._snapshot:
                    raise SourceSnapshotError("wheel source changed during authenticated provider call")
                validated_result = _validate_provider_result(
                    binding._source,
                    resolved.declaration,
                    method_name,
                    result,
                )
                if method_name == "contribute":
                    validated_result = _authenticated_contribution(
                        binding,
                        cast(PluginContribution, validated_result),
                        authenticated_modules,
                    )
                session.restore_unconsumed_quarantine(import_plan)
            _commit_authenticated_plan(binding._cache, authenticated_modules)
            binding._import_plan = active_import_provenance_plan(import_plan)
            if isinstance(validated_result, AuthenticatedContribution):
                binding._issued_contributions = (
                    *binding._issued_contributions,
                    validated_result.authority,
                )
            return validated_result
        except BaseException as primary_error:
            _restore_import_transaction(
                before_modules,
                parent_attributes,
                import_plan,
                primary_error,
            )
            raise


__all__ = [
    "EditableWheelProductSource",
    "EditableWheelPluginSource",
    "MetadataProvider",
    "WheelPluginDeclaration",
    "WheelPluginSource",
    "WheelProductDeclaration",
    "WheelProductSource",
    "load_snapshotted_entrypoint",
    "snapshot_wheel_source",
]
