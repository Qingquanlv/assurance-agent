"""Canonical recoverable manifest for runtime_commit_safety/v1."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal

from assurance_agent.workflow.graph.durable_effects import (
    COMMIT_SAFETY_INVENTORY as _DURABLE_INVENTORY,
)
from assurance_agent.workflow.graph.durable_effects import (
    KNOWN_DURABLE_EFFECT_KINDS,
)
from assurance_agent.workflow.graph.effect_retry import (
    COMMIT_SAFETY_INVENTORY as _RETRY_INVENTORY,
)
from assurance_agent.workflow.graph.precommit import (
    COMMIT_SAFETY_INVENTORY as _PRECOMMIT_INVENTORY,
)
from assurance_agent.workflow.graph.precommit import (
    KNOWN_PRECOMMIT_VALIDATORS,
)
from assurance_agent.workflow.healing.effects import (
    COMMIT_SAFETY_INVENTORY as _HEALING_INVENTORY,
)
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

SEMANTICS_ID = "runtime_commit_safety/v1"

InventoryRole = Literal["validator", "effect", "helper", "model", "protocol"]

_REQUIRED_CONSUMERS: frozenset[str] = frozenset(
    {
        *KNOWN_PRECOMMIT_VALIDATORS,
        *KNOWN_DURABLE_EFFECT_KINDS,
        "effect_retry_sidecar",
        "root_terminal_fence.guard",
        "root_terminal_fence.reject_if_terminal",
        "root_terminal_fence.prepare_terminal",
        "root_terminal_fence.commit_terminal",
        "root_terminal_fence.abort_prepared",
        "precommit_validator_registry",
        "precommit_validation_context",
        "candidate_validation_receipt",
        "validate_candidate",
        "verify_candidate_receipt",
        "validator_semantics_digest",
        "diff_safety",
        "evidence_path_resolver",
        "generated_entries_mapping",
        "generated_files_registry",
        "product_code_roots",
        "durable_effect_registry",
        "durable_effect_intent",
        "durable_effect_acknowledgement",
        "durable_effect_context",
        "payload_sha256",
        "reconciler_semantics_digest",
        "derive_effect_id",
        "build_intent",
        "validate_acknowledgement",
        "reconcile_effect",
        "evidence_export",
        "evidence_export_root_slice",
        "evidence_export_manifest",
        "evidence_export_base_tree_roots",
    }
)


@dataclass(frozen=True, slots=True)
class InventorySpec:
    qualified_name: str
    role: InventoryRole
    consumer_id: str
    semantic_version: str = "1"


def _specs_from_tuples(rows: tuple[tuple[str, str, str], ...]) -> tuple[InventorySpec, ...]:
    return tuple(
        InventorySpec(
            qualified_name=qualified_name,
            role=role,  # type: ignore[arg-type]
            consumer_id=consumer_id,
        )
        for qualified_name, role, consumer_id in rows
    )


# Evidence-export consumers (Task 17). Declared here to avoid import cycles with
# assurance_agent.eval.evidence_export → workspace → runtime_commit_safety.
_EVIDENCE_EXPORT_INVENTORY: tuple[tuple[str, str, str], ...] = (
    (
        "assurance_agent.eval.evidence_export.export_root_execution_closure",
        "helper",
        "evidence_export",
    ),
    (
        "assurance_agent.eval.evidence_export.select_root_event_slice",
        "helper",
        "evidence_export_root_slice",
    ),
    (
        "assurance_agent.eval.evidence_export.verify_export_manifest_closure",
        "helper",
        "evidence_export_manifest",
    ),
    (
        "assurance_agent.workflow.graph.evidence_paths.verify_write_set_base_tree_roots",
        "helper",
        "evidence_export_base_tree_roots",
    ),
)

_COMMIT_SAFETY_INVENTORY: tuple[InventorySpec, ...] = (
    *_specs_from_tuples(_PRECOMMIT_INVENTORY),
    *_specs_from_tuples(_DURABLE_INVENTORY),
    *_specs_from_tuples(_RETRY_INVENTORY),
    *_specs_from_tuples(_HEALING_INVENTORY),
    *_specs_from_tuples(_EVIDENCE_EXPORT_INVENTORY),
)


@dataclass(frozen=True, slots=True)
class CommitSafetyDependency:
    qualified_name: str
    kind: SymbolKind
    role: InventoryRole
    consumer_id: str
    semantic_version: str
    source_digest: str


@dataclass(frozen=True, slots=True)
class RuntimeCommitSafetyManifest:
    semantics_id: str
    schema_version: str
    runtime_versions: dict[str, str]
    dependencies: tuple[CommitSafetyDependency, ...]
    consumers: tuple[str, ...]
    semantic_digest: str
    object_digest: str
    canonical_bytes: bytes


def build_runtime_commit_safety_manifest(
    *,
    source_overrides: Mapping[str, str] | None = None,
    constant_overrides: Mapping[str, object] | None = None,
    source_digest_overrides: Mapping[str, str] | None = None,
) -> RuntimeCommitSafetyManifest:
    _validate_closed_inventory(_COMMIT_SAFETY_INVENTORY)
    overrides = dict(source_overrides or {})
    constants = dict(constant_overrides or {})
    digest_overrides = dict(source_digest_overrides or {})
    dependencies: list[CommitSafetyDependency] = []
    for spec in sorted(_COMMIT_SAFETY_INVENTORY, key=lambda item: item.qualified_name):
        obj = resolve_qualified_object(spec.qualified_name)
        if spec.qualified_name in digest_overrides:
            source_digest = digest_overrides[spec.qualified_name]
        elif spec.qualified_name in constants:
            source_digest = normalized_value_digest(constants[spec.qualified_name])
        elif spec.qualified_name in overrides:
            source_digest = normalized_ast_digest(overrides[spec.qualified_name])
        else:
            source_digest = implementation_digest_for(obj)
        dependencies.append(
            CommitSafetyDependency(
                qualified_name=spec.qualified_name,
                kind=_symbol_kind(obj),
                role=spec.role,
                consumer_id=spec.consumer_id,
                semantic_version=spec.semantic_version,
                source_digest=source_digest,
            )
        )
    runtime_versions = resolve_runtime_versions()
    consumers = tuple(sorted({item.consumer_id for item in dependencies}))
    dep_payload: list[dict[str, object]] = [
        {
            "consumer_id": item.consumer_id,
            "implementation_digest": item.source_digest,
            "kind": item.kind,
            "qualified_name": item.qualified_name,
            "role": item.role,
            "semantic_version": item.semantic_version,
        }
        for item in dependencies
    ]
    semantic_digest = aggregate_semantic_digest(
        [
            {
                "implementation_digest": item["implementation_digest"],
                "qualified_name": item["qualified_name"],
                "semantic_version": item["semantic_version"],
            }
            for item in dep_payload
        ],
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
    return RuntimeCommitSafetyManifest(
        semantics_id=SEMANTICS_ID,
        schema_version=MANIFEST_SCHEMA_VERSION,
        runtime_versions=runtime_versions,
        dependencies=tuple(dependencies),
        consumers=consumers,
        semantic_digest=semantic_digest,
        object_digest=hashlib.sha256(canonical).hexdigest(),
        canonical_bytes=canonical,
    )


@lru_cache(maxsize=1)
def commit_safety_semantics_digest() -> str:
    return build_runtime_commit_safety_manifest().semantic_digest


@lru_cache(maxsize=1)
def commit_safety_semantics_bytes() -> bytes:
    return build_runtime_commit_safety_manifest().canonical_bytes


@lru_cache(maxsize=1)
def commit_safety_semantics_object_digest() -> str:
    return build_runtime_commit_safety_manifest().object_digest


def _validate_closed_inventory(inventory: tuple[InventorySpec, ...]) -> None:
    qualified_names = [item.qualified_name for item in inventory]
    if len(qualified_names) != len(set(qualified_names)):
        raise ValueError("commit-safety inventory lists a dependency more than once")
    consumers = {item.consumer_id for item in inventory}
    missing = _REQUIRED_CONSUMERS - consumers
    if missing:
        raise ValueError(f"listed-but-unconsumed commit-safety consumers: {sorted(missing)}")
    extra = consumers - _REQUIRED_CONSUMERS
    if extra:
        raise ValueError(f"unregistered commit-safety consumers: {sorted(extra)}")

    validator_constants = [
        item for item in inventory if item.role == "validator"
    ]
    effect_constants = [item for item in inventory if item.role == "effect"]
    seen_validators: set[str] = set()
    for item in validator_constants:
        value = resolve_qualified_object(item.qualified_name)
        if not isinstance(value, str) or value not in KNOWN_PRECOMMIT_VALIDATORS:
            raise ValueError(f"unregistered precommit validator inventory member: {item.qualified_name}")
        if value in seen_validators:
            raise ValueError(f"validator listed more than once: {value}")
        seen_validators.add(value)
        if item.consumer_id != value:
            raise ValueError(f"validator consumer_id mismatch for {value}")
    if seen_validators != set(KNOWN_PRECOMMIT_VALIDATORS):
        raise ValueError("commit-safety inventory missing registered precommit validators")

    seen_effects: set[str] = set()
    for item in effect_constants:
        value = resolve_qualified_object(item.qualified_name)
        if not isinstance(value, str) or value not in KNOWN_DURABLE_EFFECT_KINDS:
            raise ValueError(f"unregistered durable effect inventory member: {item.qualified_name}")
        if value in seen_effects:
            raise ValueError(f"effect kind listed more than once: {value}")
        seen_effects.add(value)
        if item.consumer_id != value:
            raise ValueError(f"effect consumer_id mismatch for {value}")
    if seen_effects != set(KNOWN_DURABLE_EFFECT_KINDS):
        raise ValueError("commit-safety inventory missing registered durable effect kinds")


def _symbol_kind(obj: object) -> SymbolKind:
    import inspect

    if inspect.isfunction(obj) or inspect.ismethod(obj):
        return "function"
    if inspect.isclass(obj):
        return "class"
    return "constant"


__all__ = [
    "SEMANTICS_ID",
    "InventorySpec",
    "CommitSafetyDependency",
    "RuntimeCommitSafetyManifest",
    "build_runtime_commit_safety_manifest",
    "commit_safety_semantics_bytes",
    "commit_safety_semantics_digest",
    "commit_safety_semantics_object_digest",
]
