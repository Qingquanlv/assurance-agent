from __future__ import annotations

import base64
import configparser
import csv
from dataclasses import dataclass
from email.parser import BytesParser
from email.policy import compat32
import hashlib
from importlib import metadata
import os
from pathlib import Path
import stat
import sys
from types import ModuleType
from typing import Literal, Protocol, TypeAlias, cast

from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version
from pydantic import field_validator

from graph_engine.composition.models import SourceFile, SourceIdentity, SourceKind, SourceSnapshot
from graph_engine.composition.source_fs import (
    DeclaredTreePolicy,
    SourceSnapshotError,
    capture_declared_tree,
)
from graph_engine.plugin_api import FrozenModel, PluginDescriptor, PluginProvider
from graph_engine.product import ProductManifest, ProductProvider


class WheelProductSource(FrozenModel):
    kind: Literal["wheel_product"] = "wheel_product"
    distribution: str
    entrypoint_group: Literal["graph_engine.products"] = "graph_engine.products"
    entrypoint_name: str

    @field_validator("distribution")
    @classmethod
    def _normalize_distribution(cls, value: str) -> str:
        return _normalized_distribution_name(value)


class WheelPluginSource(FrozenModel):
    kind: Literal["wheel_plugin"] = "wheel_plugin"
    distribution: str
    entrypoint_group: Literal["graph_engine.plugins"] = "graph_engine.plugins"
    entrypoint_name: str

    @field_validator("distribution")
    @classmethod
    def _normalize_distribution(cls, value: str) -> str:
        return _normalized_distribution_name(value)


class EditableWheelPluginSource(FrozenModel):
    kind: Literal["editable_plugin"] = "editable_plugin"
    distribution: str
    entrypoint_group: Literal["graph_engine.plugins"] = "graph_engine.plugins"
    entrypoint_name: str
    source_root: Path
    source_files: tuple[str, ...]

    @field_validator("distribution")
    @classmethod
    def _normalize_distribution(cls, value: str) -> str:
        return _normalized_distribution_name(value)


WheelSource: TypeAlias = WheelProductSource | WheelPluginSource | EditableWheelPluginSource
WheelProvider: TypeAlias = ProductProvider | PluginProvider
_EntryState: TypeAlias = tuple[int, int, int, int, int, int]
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_DIRECTORY_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | _NOFOLLOW
_FILE_FLAGS = os.O_RDONLY | _NOFOLLOW | _NONBLOCK


@dataclass(frozen=True, slots=True)
class _ResolvedWheelSnapshot:
    snapshot: SourceSnapshot
    entrypoint: metadata.EntryPoint


@dataclass(frozen=True, slots=True)
class _LoadedSnapshottedEntrypoint:
    provider: WheelProvider
    declaration: object


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
    if isinstance(source, EditableWheelPluginSource):
        tree = capture_declared_tree(
            source.source_root,
            source.source_files,
            DeclaredTreePolicy.editable(),
        )
        identity = SourceIdentity(
            kind=kind,
            root=tree.identity.root,
            distribution=source.distribution,
            version=version,
            entrypoint_group=entrypoint.group,
            entrypoint_name=entrypoint.name,
        )
        return _ResolvedWheelSnapshot(
            snapshot=SourceSnapshot.from_identity(identity, tree.files),
            entrypoint=entrypoint,
        )

    root, files = _capture_installed_distribution(
        distribution,
        source,
        entrypoint,
        version,
    )
    identity = SourceIdentity(
        kind=kind,
        root=root,
        distribution=source.distribution,
        version=version,
        entrypoint_group=entrypoint.group,
        entrypoint_name=entrypoint.name,
    )
    return _ResolvedWheelSnapshot(
        snapshot=SourceSnapshot.from_identity(identity, files),
        entrypoint=entrypoint,
    )


def load_snapshotted_entrypoint(
    source: WheelSource,
    snapshot: SourceSnapshot,
    metadata_provider: MetadataProvider = metadata,
) -> WheelProvider:
    return _load_snapshotted_entrypoint_binding(source, snapshot, metadata_provider).provider


def _load_snapshotted_entrypoint_binding(
    source: WheelSource,
    snapshot: SourceSnapshot,
    metadata_provider: MetadataProvider = metadata,
) -> _LoadedSnapshottedEntrypoint:
    _validate_snapshot_matches_source(source, snapshot)
    resolved = _resolve_wheel_snapshot(source, metadata_provider)
    if resolved.snapshot != snapshot:
        raise SourceSnapshotError("wheel source changed after snapshot")
    _reject_preloaded_entrypoint_modules(resolved.entrypoint, snapshot)
    try:
        loaded = resolved.entrypoint.load()
    except Exception as error:
        raise SourceSnapshotError("cannot load snapshotted entry point") from error
    _verify_loaded_provider_provenance(loaded, resolved.entrypoint, snapshot)
    after_load = _resolve_wheel_snapshot(source, metadata_provider)
    if after_load.snapshot != snapshot:
        raise SourceSnapshotError("wheel source changed while loading its entry point")

    if isinstance(source, WheelProductSource):
        manifest = cast(ProductManifest, _call_descriptor(loaded, "manifest", "product"))
        if manifest.product_id != snapshot.identity.entrypoint_name:
            raise SourceSnapshotError("loaded product id does not match source identity")
        if _normalized_version(manifest.product_version, "product version") != snapshot.identity.version:
            raise SourceSnapshotError("loaded product version does not match source identity")
        return _LoadedSnapshottedEntrypoint(cast(ProductProvider, loaded), manifest)

    descriptor = cast(PluginDescriptor, _call_descriptor(loaded, "descriptor", "plugin"))
    if descriptor.plugin_id != snapshot.identity.entrypoint_name:
        raise SourceSnapshotError("loaded plugin id does not match source identity")
    if _normalized_version(descriptor.plugin_version, "plugin version") != snapshot.identity.version:
        raise SourceSnapshotError("loaded plugin version does not match source identity")
    return _LoadedSnapshottedEntrypoint(cast(PluginProvider, loaded), descriptor)


def _normalized_distribution_name(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("distribution name must be non-empty text")
    normalized = canonicalize_name(value)
    if not normalized:
        raise ValueError("distribution name must be non-empty text")
    return normalized


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
        SourceIdentity(kind=SourceKind.ENGINE, root=root),
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
    ):
        raise SourceSnapshotError("snapshot identity does not match requested wheel source")
    if isinstance(source, EditableWheelPluginSource) and identity.root != source.source_root.resolve():
        raise SourceSnapshotError("snapshot root does not match requested editable source")
    if isinstance(source, EditableWheelPluginSource):
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


def _reject_preloaded_entrypoint_modules(
    entrypoint: metadata.EntryPoint,
    snapshot: SourceSnapshot,
) -> None:
    parts = entrypoint.module.split(".")
    module_names = tuple(".".join(parts[:index]) for index in range(1, len(parts) + 1))
    if entrypoint.module in sys.modules:
        raise SourceSnapshotError(f"entry point module is already loaded: {entrypoint.module}")
    for module_name in module_names[:-1]:
        if module_name in sys.modules:
            _verify_snapshotted_module(module_name, snapshot)


def _verify_loaded_provider_provenance(
    provider: object,
    entrypoint: metadata.EntryPoint,
    snapshot: SourceSnapshot,
) -> None:
    provider_module = (
        provider.__name__ if isinstance(provider, ModuleType) else getattr(provider, "__module__", None)
    )
    if not isinstance(provider_module, str) or not provider_module:
        raise SourceSnapshotError("loaded provider has no verifiable module origin")
    for module_name in dict.fromkeys((entrypoint.module, provider_module)):
        _verify_snapshotted_module(module_name, snapshot)


def _verify_snapshotted_module(module_name: str, snapshot: SourceSnapshot) -> None:
    module = sys.modules.get(module_name)
    if not isinstance(module, ModuleType):
        raise SourceSnapshotError(f"loaded provider module is unavailable: {module_name}")
    origin = getattr(module, "__file__", None)
    if not isinstance(origin, str) or not origin:
        raise SourceSnapshotError(f"loaded provider module has no file origin: {module_name}")

    root = snapshot.identity.root
    try:
        relative_path = Path(origin).absolute().relative_to(root).as_posix()
    except ValueError as error:
        raise SourceSnapshotError(
            f"loaded provider module is outside the authenticated root: {module_name}"
        ) from error
    expected = next(
        (source_file for source_file in snapshot.files if source_file.path == relative_path), None
    )
    if expected is None:
        raise SourceSnapshotError(
            f"loaded provider module is not present in the authenticated snapshot: {module_name}"
        )

    root_fd = _open_physical_root(root)
    try:
        current, _state = _read_stable_installed_file(root_fd, relative_path)
    finally:
        os.close(root_fd)
    if current.sha256 != expected.sha256:
        raise SourceSnapshotError(f"loaded provider module hash changed: {module_name}")


def _call_descriptor(loaded: object, method_name: str, kind: str) -> object:
    method = getattr(loaded, method_name, None)
    if not callable(method):
        raise SourceSnapshotError(f"loaded {kind} provider has no callable {method_name}")
    try:
        return method()
    except Exception as error:
        raise SourceSnapshotError(f"loaded {kind} provider {method_name} failed") from error


__all__ = [
    "EditableWheelPluginSource",
    "MetadataProvider",
    "WheelPluginSource",
    "WheelProductSource",
    "load_snapshotted_entrypoint",
    "snapshot_wheel_source",
]
