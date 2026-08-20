from __future__ import annotations

import _imp
import base64
import configparser
from contextlib import contextmanager
import csv
from dataclasses import dataclass, field, replace
from email.parser import BytesParser
from email.policy import compat32
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import stat
import sys
from threading import RLock
from types import ModuleType
from typing import Iterator, Literal, NoReturn, Protocol, TypeAlias, cast

from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version
from pydantic import ValidationError, field_validator, model_validator

from graph_engine.composition.models import (
    ProductManifest,
    SourceFile,
    SourceIdentity,
    SourceKind,
    SourceSnapshot,
)
from graph_engine.composition.import_plan import (
    ImportPlanSession,
    ImportProvenancePlan,
    ModuleImportPlan,
    ModuleRole,
    active_import_provenance_plan,
    build_import_provenance_plan,
    extend_import_plan_with_quarantine,
    rebind_import_provenance_plan,
)
from graph_engine.composition.source_fs import (
    DeclaredTreePolicy,
    SourceSnapshotError,
    capture_declared_tree,
)
from graph_engine.plugin_api import FrozenModel, PluginDescriptor, PluginProvider, ProviderSource


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


@dataclass(frozen=True, slots=True)
class _ResolvedWheelSnapshot:
    snapshot: SourceSnapshot
    entrypoint: metadata.EntryPoint
    declaration: WheelDeclaration


@dataclass(frozen=True, slots=True)
class _LoadedSnapshottedEntrypoint:
    provider: WheelProvider
    declaration: object
    import_plan: ImportProvenancePlan


@dataclass(slots=True)
class _AuthenticatedBindingCache:
    """Platform-owned authority for modules imported after source authentication."""

    bindings: dict[tuple[str, str, str, str], _LoadedSnapshottedEntrypoint] = field(default_factory=dict)
    modules: dict[str, _AuthenticatedModule] = field(default_factory=dict)


@dataclass(slots=True)
class _AuthenticatedModule:
    module: ModuleType
    source_digests: set[str]
    provenance_keys: set[tuple[object, ...]]


@dataclass(frozen=True, slots=True)
class _ParentPackageState:
    parent_name: str
    parent: ModuleType
    attributes: dict[str, object]


@dataclass(frozen=True, slots=True)
class _ParentAttributeSnapshot:
    candidate_module_names: tuple[str, ...]
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
    return _load_snapshotted_entrypoint_binding(
        source,
        snapshot,
        metadata_provider,
        _AuthenticatedBindingCache(),
    ).provider


def _load_snapshotted_entrypoint_binding(
    source: WheelSource,
    snapshot: SourceSnapshot,
    metadata_provider: MetadataProvider = metadata,
    binding_cache: _AuthenticatedBindingCache | None = None,
) -> _LoadedSnapshottedEntrypoint:
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
            before_modules = dict(sys.modules)
            import_plan = rebind_import_provenance_plan(
                cached.import_plan,
                before_modules,
                _cached_module_authority(cache, snapshot.digest),
            )
            import_plan = extend_import_plan_with_quarantine(
                import_plan,
                resolved.declaration.source,
                snapshot,
                before_modules,
                _fresh_module_authority(cache),
            )
            parent_attributes = _capture_parent_attributes(before_modules, import_plan)
            try:
                with ImportPlanSession(
                    resolved.declaration.source,
                    snapshot,
                    before_modules,
                ) as session:
                    session.quarantine(import_plan)
                    session.preload(
                        import_plan,
                        _cached_module_authority(cache, snapshot.digest),
                    )
                    session.validate(
                        import_plan,
                        _cached_module_authority(cache, snapshot.digest),
                    )
                    _validate_live_declaration(source, cached.provider, resolved.declaration)
                    session.validate(
                        import_plan,
                        _cached_module_authority(cache, snapshot.digest),
                    )
                    session.restore_unconsumed_quarantine(import_plan)
                return cached
            except BaseException as primary_error:
                _restore_import_transaction(before_modules, parent_attributes, primary_error)
                raise

        before_modules = dict(sys.modules)
        provider_source = resolved.declaration.source
        import_plan = build_import_provenance_plan(
            provider_source,
            snapshot,
            resolved.entrypoint,
            initial_modules=before_modules,
        )
        import_plan = extend_import_plan_with_quarantine(
            import_plan,
            provider_source,
            snapshot,
            before_modules,
            _fresh_module_authority(cache),
        )
        parent_attributes = _capture_parent_attributes(
            before_modules,
            import_plan,
        )
        try:
            with ImportPlanSession(provider_source, snapshot, before_modules) as session:
                session.quarantine(import_plan)
                session.preload(import_plan, _fresh_module_authority(cache))
                try:
                    loaded = resolved.entrypoint.load()
                except Exception as error:
                    raise SourceSnapshotError("cannot load snapshotted entry point") from error
                import_plan = build_import_provenance_plan(
                    provider_source,
                    snapshot,
                    resolved.entrypoint,
                    provider_module=_provider_module_name(loaded),
                    dependency_modules=session.recorded_module_names,
                    quarantine_modules=_quarantine_module_names(import_plan),
                    initial_modules=session.post_quarantine_initial_modules,
                )
                parent_attributes = replace(
                    parent_attributes,
                    candidate_module_names=tuple(
                        entry.module_name for entry in import_plan.modules if entry.rollback
                    ),
                )
                session.preload(import_plan, _fresh_module_authority(cache))
                authenticated_modules = session.validate(
                    import_plan,
                    _fresh_module_authority(cache),
                )
                after_load = _resolve_wheel_snapshot(source, metadata_provider)
                if after_load.snapshot != snapshot:
                    raise SourceSnapshotError("wheel source changed while loading its entry point")
                declaration = _validate_live_declaration(source, loaded, resolved.declaration)
                authenticated_modules = session.validate(
                    import_plan,
                    _fresh_module_authority(cache),
                )
                session.restore_unconsumed_quarantine(import_plan)
            binding = _LoadedSnapshottedEntrypoint(
                cast(WheelProvider, loaded),
                declaration,
                active_import_provenance_plan(import_plan),
            )
            _commit_authenticated_plan(cache, authenticated_modules)
            cache.bindings[cache_key] = binding
            return binding
        except BaseException as primary_error:
            _restore_import_transaction(before_modules, parent_attributes, primary_error)
            raise


def _quarantine_module_names(plan: ImportProvenancePlan) -> tuple[str, ...]:
    return tuple(entry.module_name for entry in plan.modules if ModuleRole.QUARANTINE in entry.roles)


def _validate_live_declaration(
    source: WheelSource,
    loaded: object,
    static_declaration: WheelDeclaration,
) -> ProductManifest | PluginDescriptor:
    if isinstance(source, WheelProductSource | EditableWheelProductSource):
        manifest = _call_descriptor(loaded, "manifest", "product")
        if not isinstance(manifest, ProductManifest):
            raise SourceSnapshotError("loaded product provider returned an invalid manifest")
        declaration = cast(WheelProductDeclaration, static_declaration)
        if manifest != declaration.manifest:
            raise SourceSnapshotError("loaded product manifest disagrees with static declaration")
        return manifest

    descriptor = _call_descriptor(loaded, "descriptor", "plugin")
    if not isinstance(descriptor, PluginDescriptor):
        raise SourceSnapshotError("loaded plugin provider returned an invalid descriptor")
    declaration = cast(WheelPluginDeclaration, static_declaration)
    if descriptor != declaration.descriptor:
        raise SourceSnapshotError("loaded plugin descriptor disagrees with static declaration")
    return descriptor


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
        raise SourceSnapshotError("wheel declaration path is absent from the authenticated snapshot")
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
        try:
            record_file, record_state = _read_stable_installed_file(root_fd, record_relative)
        except SourceSnapshotError as error:
            raise SourceSnapshotError("installed distribution RECORD is missing or unreadable") from error
        rows = _parse_record(record_file.content)
        if record_relative not in {path for path, _hash, _size in rows}:
            raise SourceSnapshotError("installed distribution RECORD does not list itself")
        declared_paths = tuple(path for path, _hash, _size in rows)
        before_directories = _capture_directory_states(root_fd, declared_paths)

        files: list[SourceFile] = []
        captured_states: dict[str, _EntryState] = {}
        for relative_path, declared_hash, declared_size in rows:
            if _is_cache_file(relative_path):
                continue
            if relative_path == record_relative:
                source_file = record_file
                state = record_state
            else:
                source_file, state = _read_stable_installed_file(root_fd, relative_path)
            if declared_hash is not None:
                _validate_record_hash(relative_path, source_file.content, declared_hash)
            if declared_size is not None and len(source_file.content) != declared_size:
                raise SourceSnapshotError(f"RECORD size mismatch: {relative_path}")
            files.append(source_file)
            captured_states[relative_path] = state

        _snapshot_boundary("before_rescan", None)
        for relative_path, expected in captured_states.items():
            if _stat_installed_file(root_fd, relative_path) != expected:
                raise SourceSnapshotError("installed distribution changed while it was captured")
        if _capture_directory_states(root_fd, declared_paths) != before_directories:
            raise SourceSnapshotError("installed distribution directories changed while it was captured")
        rescanned_record, rescanned_state = _read_stable_installed_file(root_fd, record_relative)
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


def _open_parent_at(root_fd: int, relative_path: str) -> tuple[int, str]:
    parts = relative_path.split("/")
    parent_fd = os.dup(root_fd)
    walked = ""
    try:
        for component in parts[:-1]:
            walked = f"{walked}/{component}" if walked else component
            enumerated = os.stat(component, dir_fd=parent_fd, follow_symlinks=False)
            child = os.open(component, _DIRECTORY_FLAGS, dir_fd=parent_fd)
            opened = os.fstat(child)
            if not stat.S_ISDIR(opened.st_mode) or _file_identity(opened) != _file_identity(enumerated):
                os.close(child)
                raise SourceSnapshotError(f"RECORD directory changed while opening: {walked}")
            os.close(parent_fd)
            parent_fd = child
        return parent_fd, parts[-1]
    except BaseException:
        os.close(parent_fd)
        raise


def _capture_directory_states(
    root_fd: int,
    relative_paths: tuple[str, ...],
) -> tuple[tuple[str, _EntryState], ...]:
    states: dict[str, _EntryState] = {"": _file_state(os.fstat(root_fd))}
    for relative_path in relative_paths:
        descriptor = os.dup(root_fd)
        walked = ""
        try:
            for component in relative_path.split("/")[:-1]:
                walked = f"{walked}/{component}" if walked else component
                enumerated = os.stat(component, dir_fd=descriptor, follow_symlinks=False)
                child = os.open(component, _DIRECTORY_FLAGS, dir_fd=descriptor)
                opened = os.fstat(child)
                if not stat.S_ISDIR(opened.st_mode) or _file_identity(opened) != _file_identity(enumerated):
                    os.close(child)
                    raise SourceSnapshotError(f"RECORD directory changed while opening: {walked}")
                os.close(descriptor)
                descriptor = child
                state = _file_state(opened)
                previous = states.setdefault(walked, state)
                if previous != state:
                    raise SourceSnapshotError(f"RECORD directory changed while enumerating: {walked}")
        except OSError as error:
            raise SourceSnapshotError(f"cannot capture RECORD directory state: {relative_path}") from error
        finally:
            os.close(descriptor)
    return tuple(sorted(states.items()))


def _read_stable_installed_file(
    root_fd: int,
    relative_path: str,
) -> tuple[SourceFile, _EntryState]:
    parent_fd, name = _open_parent_at(root_fd, relative_path)
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
        os.close(parent_fd)


def _stat_installed_file(root_fd: int, relative_path: str) -> _EntryState:
    parent_fd, name = _open_parent_at(root_fd, relative_path)
    try:
        try:
            status = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError as error:
            raise SourceSnapshotError(f"cannot rescan RECORD file: {relative_path}") from error
        if not stat.S_ISREG(status.st_mode):
            raise SourceSnapshotError(f"RECORD path is not a regular no-follow file: {relative_path}")
        return _file_state(status)
    finally:
        os.close(parent_fd)


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
        try:
            SourceFile.from_bytes(relative_path, b"")
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
    def owns(entry: ModuleImportPlan, module: ModuleType) -> bool:
        authenticated = cache.modules.get(entry.module_name)
        return (
            authenticated is not None
            and authenticated.module is module
            and _module_provenance_key(entry) in authenticated.provenance_keys
        )

    return owns


def _cached_module_authority(cache: _AuthenticatedBindingCache, source_digest: str):
    def owns(entry: ModuleImportPlan, module: ModuleType) -> bool:
        authenticated = cache.modules.get(entry.module_name)
        return (
            authenticated is not None
            and authenticated.module is module
            and source_digest in authenticated.source_digests
            and _module_provenance_key(entry) in authenticated.provenance_keys
        )

    return owns


def _commit_authenticated_plan(
    cache: _AuthenticatedBindingCache,
    authenticated_modules: tuple[tuple[ModuleImportPlan, ModuleType], ...],
) -> None:
    for entry, module in authenticated_modules:
        if not entry.commit:
            continue
        authenticated = cache.modules.get(entry.module_name)
        if authenticated is not None and authenticated.module is not module:
            raise SourceSnapshotError(f"provider module changed before authority commit: {entry.module_name}")
    for entry, module in authenticated_modules:
        if not entry.commit:
            continue
        authenticated = cache.modules.get(entry.module_name)
        if authenticated is None:
            cache.modules[entry.module_name] = _AuthenticatedModule(
                module,
                {entry.source_digest},
                {_module_provenance_key(entry)},
            )
        elif authenticated.module is module:
            authenticated.source_digests.add(entry.source_digest)
            authenticated.provenance_keys.add(_module_provenance_key(entry))
        else:  # pragma: no cover - preflight and serialized import invariant.
            raise AssertionError("authenticated module changed after commit preflight")


def _restore_modules(before: dict[str, ModuleType]) -> None:
    for module_name in tuple(sys.modules):
        if module_name not in before:
            del sys.modules[module_name]
    for module_name, module in before.items():
        if sys.modules.get(module_name) is not module:
            sys.modules[module_name] = module


def _capture_parent_attributes(
    before_modules: dict[str, ModuleType],
    import_plan: ImportProvenancePlan,
) -> _ParentAttributeSnapshot:
    packages = tuple(
        _ParentPackageState(module_name, module, dict(module.__dict__))
        for module_name, module in sorted(before_modules.items())
        if isinstance(module, ModuleType) and getattr(module, "__path__", None) is not None
    )
    return _ParentAttributeSnapshot(
        candidate_module_names=tuple(entry.module_name for entry in import_plan.modules if entry.rollback),
        packages=packages,
    )


def _module_provenance_key(entry: ModuleImportPlan) -> tuple[object, ...]:
    return (
        entry.classification.value,
        str(entry.physical_origin) if entry.physical_origin is not None else None,
        entry.physical_sha256,
        tuple(str(location) for location in entry.namespace_locations),
    )


def _restore_import_transaction(
    before_modules: dict[str, ModuleType],
    parent_attributes: _ParentAttributeSnapshot,
    primary_error: BaseException,
) -> None:
    rollback_errors: list[BaseException] = []
    current_modules = dict(sys.modules)
    affected_module_names = set(parent_attributes.candidate_module_names)
    for module_name in set(before_modules).union(current_modules):
        if before_modules.get(module_name) is not current_modules.get(module_name):
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


def _call_descriptor(loaded: object, method_name: str, kind: str) -> object:
    method = getattr(loaded, method_name, None)
    if not callable(method):
        raise SourceSnapshotError(f"loaded {kind} provider has no callable {method_name}")
    try:
        return method()
    except Exception as error:
        raise SourceSnapshotError(f"loaded {kind} provider {method_name} failed") from error


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
