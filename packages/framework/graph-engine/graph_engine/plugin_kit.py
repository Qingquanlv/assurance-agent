"""Convention-driven authoring kit for capability plugins.

Capability wheels historically hand-wrote a ``descriptor()`` whose ID tuples had
to be kept in lock-step with the objects returned by ``contribute()``. That dual
declaration is a standing source of drift. This kit collapses each wheel to a
single declarative :class:`CapabilitySpec`; the descriptor is *derived* from the
same data the contribution realizes, so the two can never disagree.

The kit lives in the framework and depends only on the engine's plugin API. It
adds no auto-discovery: a wheel still names its handlers, validators, schemas,
and resources explicitly. It only removes the boilerplate that turned
those declarations into a ``PluginDescriptor`` and a ``PluginContribution``.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import ClassVar

from graph_engine.canonical import canonical_json_bytes
from graph_engine.plugin_api import (
    CommitValidator,
    PluginContribution,
    PluginDependency,
    PluginDescriptor,
    ProviderSource,
    RegistryPorts,
    ResourceContribution,
    SchemaContribution,
    TaskHandler,
)

_SCHEMA_MEDIA_TYPE = "application/schema+json"


def resource_media_type(path: str) -> str:
    """Classify a packaged resource path into its stable media type."""

    if path.endswith(".schema.json"):
        return _SCHEMA_MEDIA_TYPE
    if path.endswith(".json"):
        return "application/json"
    return "text/plain"


@dataclass(frozen=True, slots=True)
class CapabilitySpec:
    """Everything a capability wheel declares, in one place.

    ``task_handlers`` and ``commit_validators`` are stateless singletons and are
    supplied as ready mappings.
    """

    plugin_id: str
    version: str
    engine_api: str
    source: ProviderSource
    resource_bytes: Callable[[str], bytes]
    schema_files: Mapping[str, str]
    resource_files: Mapping[str, str]
    task_handlers: Mapping[str, TaskHandler]
    commit_validators: Mapping[str, CommitValidator] = field(default_factory=dict)
    dependencies: tuple[PluginDependency, ...] = ()

    def schema_contributions(self) -> tuple[SchemaContribution, ...]:
        return tuple(
            SchemaContribution(
                schema_id=schema_id,
                media_type=_SCHEMA_MEDIA_TYPE,
                content=canonical_json_bytes(json.loads(self.resource_bytes(self.schema_files[schema_id]))),
            )
            for schema_id in sorted(self.schema_files)
        )

    def resource_contributions(self) -> tuple[ResourceContribution, ...]:
        return tuple(
            ResourceContribution(
                resource_id=resource_id,
                media_type=resource_media_type(self.resource_files[resource_id]),
                content=self.resource_bytes(self.resource_files[resource_id]),
            )
            for resource_id in sorted(self.resource_files)
        )

    def descriptor(self) -> PluginDescriptor:
        return PluginDescriptor(
            schema_version="1",
            source=self.source,
            plugin_id=self.plugin_id,
            plugin_version=self.version,
            engine_api=self.engine_api,
            task_handlers=tuple(sorted(self.task_handlers)),
            commit_validators=tuple(sorted(self.commit_validators)),
            dependencies=self.dependencies,
            schemas=tuple(sorted(self.schema_files)),
            resources=tuple(sorted(self.resource_files)),
            bindings=(),
        )

    def contribution(self, ports: RegistryPorts) -> PluginContribution:
        if ports.engine_api != self.engine_api:
            raise ValueError(f"unsupported engine API: {ports.engine_api!r}")
        return PluginContribution(
            task_handlers=dict(self.task_handlers),
            commit_validators=dict(self.commit_validators),
            schemas=self.schema_contributions(),
            resources=self.resource_contributions(),
        )


class CapabilityPlugin:
    """Base for a wheel's entry-point provider.

    A wheel subclasses this and sets ``spec``. The engine loads the subclass and
    calls ``descriptor()`` / ``contribute()`` on it (class- or instance-bound);
    both are classmethods so either style works.
    """

    spec: ClassVar[CapabilitySpec]

    @classmethod
    def descriptor(cls) -> PluginDescriptor:
        return cls.spec.descriptor()

    @classmethod
    def contribute(cls, ports: RegistryPorts) -> PluginContribution:
        return cls.spec.contribution(ports)


__all__ = [
    "CapabilityPlugin",
    "CapabilitySpec",
    "resource_media_type",
]
