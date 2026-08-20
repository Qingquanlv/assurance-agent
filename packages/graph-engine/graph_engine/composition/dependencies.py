from __future__ import annotations

from collections.abc import Mapping
import heapq

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from graph_engine import ENGINE_API_VERSION
from graph_engine.errors import GraphEngineError
from graph_engine.plugin_api import PluginDependency, PluginDescriptor


class DependencyConflict(GraphEngineError):
    """Raised when explicit plugin sources do not form one valid dependency closure."""


def resolve_dependency_order(
    descriptors: Mapping[str, PluginDescriptor],
    required_plugin_ids: tuple[str, ...],
) -> tuple[str, ...]:
    """Validate an already-selected source set and return its canonical dependency order.

    The mapping contains exactly one descriptor for each explicitly selected source.
    This function never discovers sources or chooses versions.
    """

    selected = _validate_exact_source_set(descriptors, required_plugin_ids)
    _validate_selected_versions(selected)
    return _canonical_topological_order(selected)


def _validate_exact_source_set(
    descriptors: Mapping[str, PluginDescriptor],
    required_plugin_ids: tuple[str, ...],
) -> dict[str, PluginDescriptor]:
    selected = dict(descriptors)
    _validate_selected_descriptors(selected)

    required = tuple(required_plugin_ids)
    duplicate_required = _duplicates(required)
    if duplicate_required:
        raise DependencyConflict(f"required plugin IDs repeat: {', '.join(duplicate_required)}")

    closure: set[str] = set()
    missing: dict[str, set[str]] = {}
    ready = list(sorted(required))
    heapq.heapify(ready)

    while ready:
        plugin_id = heapq.heappop(ready)
        if plugin_id in closure:
            continue
        descriptor = selected.get(plugin_id)
        if descriptor is None:
            missing.setdefault(plugin_id, set()).add("resolution request")
            continue
        closure.add(plugin_id)

        dependencies = _validated_dependencies(plugin_id, descriptor.dependencies)
        for dependency in dependencies:
            dependency_id = dependency.plugin_id
            if dependency_id not in selected:
                missing.setdefault(dependency_id, set()).add(plugin_id)
            elif dependency_id not in closure:
                heapq.heappush(ready, dependency_id)

    if missing:
        details = "; ".join(
            f"{plugin_id} (required by {', '.join(sorted(parents))})"
            for plugin_id, parents in sorted(missing.items())
        )
        raise DependencyConflict(f"missing selected plugin source: {details}")

    unexpected = tuple(sorted(set(selected).difference(closure)))
    if unexpected:
        raise DependencyConflict(f"unexpected selected plugin source: {', '.join(unexpected)}")

    return {plugin_id: selected[plugin_id] for plugin_id in sorted(closure)}


def _validate_selected_descriptors(selected: Mapping[str, PluginDescriptor]) -> None:
    for source_id, descriptor in sorted(selected.items()):
        if not isinstance(source_id, str):
            raise DependencyConflict(f"invalid selected plugin source ID: {source_id!r}")
        if not isinstance(descriptor, PluginDescriptor):
            raise DependencyConflict(f"selected source {source_id!r} does not contain a plugin descriptor")
        if descriptor.plugin_id != source_id:
            raise DependencyConflict(
                f"selected source {source_id!r} describes plugin {descriptor.plugin_id!r}"
            )


def _validated_dependencies(
    plugin_id: str,
    dependencies: tuple[PluginDependency, ...],
) -> tuple[PluginDependency, ...]:
    dependency_ids: list[str] = []
    for dependency in dependencies:
        if not isinstance(dependency, PluginDependency):
            raise DependencyConflict(f"plugin {plugin_id} has an invalid dependency declaration")
        if dependency.plugin_id == plugin_id:
            raise DependencyConflict(f"plugin {plugin_id} cannot depend on itself")
        dependency_ids.append(dependency.plugin_id)

    duplicates = _duplicates(tuple(dependency_ids))
    if duplicates:
        raise DependencyConflict(f"plugin {plugin_id} declares duplicate dependency: {', '.join(duplicates)}")
    return tuple(sorted(dependencies, key=lambda dependency: dependency.plugin_id))


def _validate_selected_versions(selected: Mapping[str, PluginDescriptor]) -> None:
    engine_version = _version(ENGINE_API_VERSION, "engine API")
    selected_versions = {
        plugin_id: _version(descriptor.plugin_version, f"selected plugin version for {plugin_id}")
        for plugin_id, descriptor in selected.items()
    }

    for plugin_id, descriptor in selected.items():
        if not _matches_engine_api(descriptor.engine_api, engine_version):
            raise DependencyConflict(
                f"plugin {plugin_id} requires engine API {descriptor.engine_api!r}, "
                f"but engine is {engine_version}"
            )

        for dependency in _validated_dependencies(plugin_id, descriptor.dependencies):
            try:
                constraint = SpecifierSet(dependency.version_specifier)
            except (InvalidSpecifier, TypeError) as error:
                raise DependencyConflict(
                    f"invalid dependency version constraint for {plugin_id} -> {dependency.plugin_id}: "
                    f"{dependency.version_specifier!r}"
                ) from error
            selected_version = selected_versions[dependency.plugin_id]
            if selected_version not in constraint:
                raise DependencyConflict(
                    f"plugin {plugin_id} requires {dependency.plugin_id}{dependency.version_specifier}, "
                    f"but selected {dependency.plugin_id}=={selected_version}"
                )


def _canonical_topological_order(selected: Mapping[str, PluginDescriptor]) -> tuple[str, ...]:
    dependencies = {
        plugin_id: tuple(
            dependency.plugin_id for dependency in _validated_dependencies(plugin_id, descriptor.dependencies)
        )
        for plugin_id, descriptor in selected.items()
    }
    dependents = {plugin_id: [] for plugin_id in selected}
    unresolved = {plugin_id: len(required) for plugin_id, required in dependencies.items()}
    for plugin_id, required in dependencies.items():
        for dependency_id in required:
            dependents[dependency_id].append(plugin_id)

    ready = [plugin_id for plugin_id, count in unresolved.items() if count == 0]
    heapq.heapify(ready)
    order: list[str] = []
    while ready:
        plugin_id = heapq.heappop(ready)
        order.append(plugin_id)
        for dependent_id in sorted(dependents[plugin_id]):
            unresolved[dependent_id] -= 1
            if unresolved[dependent_id] == 0:
                heapq.heappush(ready, dependent_id)

    if len(order) != len(selected):
        remaining = tuple(sorted(set(selected).difference(order)))
        raise DependencyConflict(f"dependency cycle among: {', '.join(remaining)}")
    return tuple(order)


def _matches_engine_api(requirement: str, engine_version: Version) -> bool:
    try:
        return Version(requirement) == engine_version
    except (InvalidVersion, TypeError):
        try:
            return engine_version in SpecifierSet(requirement)
        except (InvalidSpecifier, TypeError) as error:
            raise DependencyConflict(f"invalid engine API requirement: {requirement!r}") from error


def _version(value: object, kind: str) -> Version:
    try:
        return Version(value)  # type: ignore[arg-type]
    except (InvalidVersion, TypeError) as error:
        raise DependencyConflict(f"invalid {kind}: {value!r}") from error


def _duplicates(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(sorted({value for value in values if values.count(value) > 1}))


__all__ = ["DependencyConflict", "resolve_dependency_order"]
