"""Convention-driven authoring kit for runtime adapter plugins.

Every runtime adapter (OpenCode, Cursor, ...) ships the same provider shape: one
``runtime.<name>.execute`` task handler plus the provider-neutral
``runtime.<name>.request`` / ``runtime.<name>.result`` schemas. The two shipped
adapters previously hand-wrote byte-identical descriptors, contributions, and
schema documents that differed only by the adapter name. This kit collapses each
adapter to a single :class:`RuntimeAdapterSpec`; the descriptor is derived from
the same data the contribution realizes, so the two can never disagree.

The kit lives in the shared contracts wheel (which both adapters already depend
on) and depends only on the engine's plugin API. It stays adapter-independent:
it holds the common request/result schema shape and the ``runtime.<name>.*`` id
convention, and knows nothing about any concrete adapter.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import ClassVar

from graph_engine.plugin_api import (
    PluginContribution,
    PluginDescriptor,
    ProviderSource,
    RegistryPorts,
    SchemaContribution,
    TaskHandler,
)

from agent_runtime_contracts.runtime_binding import AgentRuntimeCapabilities
from agent_runtime_contracts.schema import canonical_json_bytes

_SCHEMA_MEDIA_TYPE = "application/schema+json"


class StructuredOutputCapabilityError(ValueError):
    """Raised when a contract requires provider schema the adapter does not advertise."""


def negotiate_provider_schema(*, required: bool, capabilities: AgentRuntimeCapabilities) -> None:
    if required and not capabilities.provider_schema:
        raise StructuredOutputCapabilityError(
            "adapter does not advertise provider-enforced structured output"
        )


RUNTIME_REQUEST_SCHEMA = canonical_json_bytes(
    {
        "additionalProperties": False,
        "properties": {
            "instructions": {
                "items": {"additionalProperties": False, "type": "object"},
                "type": "array",
            },
            "schema_version": {"const": "1", "type": "string"},
        },
        "required": ["instructions", "schema_version"],
        "type": "object",
    }
)
RUNTIME_RESULT_SCHEMA = canonical_json_bytes(
    {
        "additionalProperties": False,
        "properties": {
            "adapter_id": {"minLength": 1, "type": "string"},
            "result_digest": {"maxLength": 64, "minLength": 64, "type": "string"},
            "schema_version": {"const": "1", "type": "string"},
            "structured_result": {"additionalProperties": False, "type": "object"},
        },
        "required": ["adapter_id", "result_digest", "schema_version", "structured_result"],
        "type": "object",
    }
)


@dataclass(frozen=True, slots=True)
class RuntimeAdapterSpec:
    """Everything a runtime adapter declares, in one place.

    ``handler`` is a factory because a fresh handler is bound per contribution,
    matching the historical behavior where ``contribute`` constructed a new
    handler each call.
    """

    name: str
    version: str
    engine_api: str
    source: ProviderSource
    handler: Callable[[], TaskHandler]
    capabilities: AgentRuntimeCapabilities = field(
        default_factory=lambda: AgentRuntimeCapabilities(provider_schema=False)
    )

    @property
    def plugin_id(self) -> str:
        return f"runtime.{self.name}"

    @property
    def handler_id(self) -> str:
        return f"runtime.{self.name}.execute"

    @property
    def request_schema_id(self) -> str:
        return f"runtime.{self.name}.request"

    @property
    def result_schema_id(self) -> str:
        return f"runtime.{self.name}.result"

    def descriptor(self) -> PluginDescriptor:
        return PluginDescriptor(
            schema_version="1",
            source=self.source,
            plugin_id=self.plugin_id,
            plugin_version=self.version,
            engine_api=self.engine_api,
            task_handlers=(self.handler_id,),
            commit_validators=(),
            schemas=(self.request_schema_id, self.result_schema_id),
        )

    def contribution(self, ports: RegistryPorts) -> PluginContribution:
        if ports.engine_api != self.engine_api:
            raise ValueError(f"unsupported engine API: {ports.engine_api!r}")
        return PluginContribution(
            task_handlers={self.handler_id: self.handler()},
            schemas=(
                SchemaContribution(self.request_schema_id, _SCHEMA_MEDIA_TYPE, RUNTIME_REQUEST_SCHEMA),
                SchemaContribution(self.result_schema_id, _SCHEMA_MEDIA_TYPE, RUNTIME_RESULT_SCHEMA),
            ),
        )


class RuntimeAdapterPlugin:
    """Base for an adapter wheel's entry-point provider.

    A wheel subclasses this and sets ``spec``. The engine loads the subclass and
    calls ``descriptor()`` / ``contribute()`` on it (class- or instance-bound);
    both are classmethods so either style works.
    """

    spec: ClassVar[RuntimeAdapterSpec]

    @classmethod
    def descriptor(cls) -> PluginDescriptor:
        return cls.spec.descriptor()

    @classmethod
    def contribute(cls, ports: RegistryPorts) -> PluginContribution:
        return cls.spec.contribution(ports)


__all__ = [
    "RUNTIME_REQUEST_SCHEMA",
    "RUNTIME_RESULT_SCHEMA",
    "RuntimeAdapterPlugin",
    "RuntimeAdapterSpec",
    "StructuredOutputCapabilityError",
    "negotiate_provider_schema",
]
