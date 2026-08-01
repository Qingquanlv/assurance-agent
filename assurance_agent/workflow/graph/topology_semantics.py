"""Canonical recoverable manifest for historical_topology_safety/v1."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass

from assurance_agent.workflow.orchestration.gate_semantics import (
    MANIFEST_SCHEMA_VERSION,
    SymbolKind,
    aggregate_semantic_digest,
    canonical_descriptor_bytes,
    implementation_digest_for,
    normalized_ast_digest,
    normalized_value_digest,
    resolve_qualified_object,
    resolve_runtime_versions,
)

SEMANTICS_ID = "historical_topology_safety/v1"

# Inventory constants live beside the discovery/classifier/CFG authorities they pin.
_TOPOLOGY_DEPENDENCIES: tuple[str, ...] = (
    "assurance_agent.workflow.graph.historical_roles.DiscoveredHistoricalAssuranceRoles",
    "assurance_agent.workflow.graph.historical_roles.DiscoveredHistoricalLayerRoles",
    "assurance_agent.workflow.graph.historical_roles.discover_historical_assurance_roles",
    "assurance_agent.workflow.graph.historical_roles.layer_roles_or_none",
    "assurance_agent.workflow.graph.historical_topology_v6.V6_SEMANTICS_ID",
    "assurance_agent.workflow.graph.historical_topology_v6.WIRING_STATUSES",
    "assurance_agent.workflow.graph.historical_topology_v6.classify_historical_layer_topology_v6",
    "assurance_agent.workflow.graph.topology_analysis.build_cfg",
    "assurance_agent.workflow.graph.topology_analysis.can_reach",
    "assurance_agent.workflow.graph.topology_analysis.dominates",
    "assurance_agent.workflow.graph.topology_analysis.expressions_truth_equivalent",
    "assurance_agent.workflow.graph.topology_analysis.layer_selection_domain",
    "assurance_agent.workflow.graph.topology_analysis.paths_exist_avoiding",
    "assurance_agent.workflow.graph.topology_analysis.reachable_from",
)

_TOPOLOGY_CONSUMERS: tuple[str, ...] = (
    "cfg",
    "discovery",
    "dominance",
    "runtime_versions",
    "status",
    "truth_table",
)

_SEMANTIC_VERSIONS: dict[str, str] = dict.fromkeys(_TOPOLOGY_DEPENDENCIES, "1")


@dataclass(frozen=True, slots=True)
class TopologyDependency:
    qualified_name: str
    kind: SymbolKind
    semantic_version: str
    source_digest: str


@dataclass(frozen=True, slots=True)
class TopologySemanticsManifest:
    semantics_id: str
    schema_version: str
    runtime_versions: dict[str, str]
    dependencies: tuple[TopologyDependency, ...]
    consumers: tuple[str, ...]
    semantic_digest: str
    object_digest: str
    canonical_bytes: bytes

    @classmethod
    def from_canonical_bytes(cls, data: bytes) -> TopologySemanticsManifest:
        payload = json.loads(data.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("topology semantics descriptor must be a JSON object")
        dependencies = tuple(
            TopologyDependency(
                qualified_name=str(item["qualified_name"]),
                kind=item["kind"],  # type: ignore[arg-type]
                semantic_version=str(item["semantic_version"]),
                source_digest=str(item["implementation_digest"]),
            )
            for item in payload["dependencies"]
        )
        runtime_versions = {str(k): str(v) for k, v in dict(payload["runtime_versions"]).items()}
        consumers = tuple(str(item) for item in payload["consumers"])
        semantic_digest = str(payload["semantic_digest"])
        canonical = canonical_descriptor_bytes(
            semantics_id=str(payload["semantics_id"]),
            schema_version=str(payload["schema_version"]),
            runtime_versions=runtime_versions,
            dependencies=[
                {
                    "implementation_digest": dep.source_digest,
                    "kind": dep.kind,
                    "qualified_name": dep.qualified_name,
                    "semantic_version": dep.semantic_version,
                }
                for dep in dependencies
            ],
            consumers=consumers,
            semantic_digest=semantic_digest,
        )
        if canonical != data:
            raise ValueError("topology semantics descriptor bytes are not canonical")
        return cls(
            semantics_id=str(payload["semantics_id"]),
            schema_version=str(payload["schema_version"]),
            runtime_versions=runtime_versions,
            dependencies=dependencies,
            consumers=consumers,
            semantic_digest=semantic_digest,
            object_digest=hashlib.sha256(canonical).hexdigest(),
            canonical_bytes=canonical,
        )


def build_topology_semantics_manifest(
    *,
    source_overrides: Mapping[str, str] | None = None,
    constant_overrides: Mapping[str, object] | None = None,
    source_digest_overrides: Mapping[str, str] | None = None,
) -> TopologySemanticsManifest:
    overrides = dict(source_overrides or {})
    constants = dict(constant_overrides or {})
    digest_overrides = dict(source_digest_overrides or {})
    dependencies: list[TopologyDependency] = []
    for qualified_name in sorted(_TOPOLOGY_DEPENDENCIES):
        obj = resolve_qualified_object(qualified_name)
        if qualified_name in digest_overrides:
            source_digest = digest_overrides[qualified_name]
        elif qualified_name in constants:
            source_digest = normalized_value_digest(constants[qualified_name])
        elif qualified_name in overrides:
            source_digest = normalized_ast_digest(overrides[qualified_name])
        else:
            source_digest = implementation_digest_for(obj)
        dependencies.append(
            TopologyDependency(
                qualified_name=qualified_name,
                kind=_symbol_kind(obj),
                semantic_version=_SEMANTIC_VERSIONS[qualified_name],
                source_digest=source_digest,
            )
        )
    runtime_versions = resolve_runtime_versions()
    consumers = tuple(sorted(_TOPOLOGY_CONSUMERS))
    dep_payload: list[dict[str, object]] = [
        {
            "implementation_digest": item.source_digest,
            "kind": item.kind,
            "qualified_name": item.qualified_name,
            "semantic_version": item.semantic_version,
        }
        for item in dependencies
    ]
    semantic_digest = aggregate_semantic_digest(
        dep_payload,
        runtime_versions,
        schema_version=MANIFEST_SCHEMA_VERSION,
    )
    canonical = canonical_descriptor_bytes(
        semantics_id=SEMANTICS_ID,
        schema_version=MANIFEST_SCHEMA_VERSION,
        runtime_versions=runtime_versions,
        dependencies=dep_payload,
        consumers=consumers,
        semantic_digest=semantic_digest,
    )
    return TopologySemanticsManifest(
        semantics_id=SEMANTICS_ID,
        schema_version=MANIFEST_SCHEMA_VERSION,
        runtime_versions=runtime_versions,
        dependencies=tuple(dependencies),
        consumers=consumers,
        semantic_digest=semantic_digest,
        object_digest=hashlib.sha256(canonical).hexdigest(),
        canonical_bytes=canonical,
    )


def topology_safety_semantics_digest() -> str:
    return build_topology_semantics_manifest().semantic_digest


def topology_safety_semantics_bytes() -> bytes:
    return build_topology_semantics_manifest().canonical_bytes


def topology_safety_semantics_object_digest() -> str:
    return build_topology_semantics_manifest().object_digest


def _symbol_kind(obj: object) -> SymbolKind:
    import inspect

    if inspect.isfunction(obj) or inspect.ismethod(obj):
        return "function"
    if inspect.isclass(obj):
        return "class"
    return "constant"


__all__ = [
    "SEMANTICS_ID",
    "TopologyDependency",
    "TopologySemanticsManifest",
    "build_topology_semantics_manifest",
    "topology_safety_semantics_bytes",
    "topology_safety_semantics_digest",
    "topology_safety_semantics_object_digest",
]
