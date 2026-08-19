"""AST- and constant-bound manifest for recoverable plan-gate semantics."""

from __future__ import annotations

import ast
import hashlib
import importlib
import inspect
import json
import sys
import textwrap
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from importlib import metadata
from types import MappingProxyType
from typing import Literal

from assurance_kernel.workflow.core.product_hooks import current_product_hooks

SEMANTICS_ID = "plan_gate_semantics/v1"
MANIFEST_SCHEMA_VERSION = "1"

SymbolKind = Literal["function", "class", "constant"]

_REPLAY_SEMANTIC_DEPENDENCIES: tuple[str, ...] = (
    "assurance_agent.workflow.orchestration.gates.check_gate_in_view",
    "assurance_agent.workflow.orchestration.gates._evaluate_gate_def",
    "assurance_agent.workflow.orchestration.gates._evaluate_gate_def_base",
    "assurance_agent.workflow.orchestration.gates._view_scope",
    "assurance_agent.workflow.orchestration.gates._load_view_doc",
    "assurance_agent.workflow.orchestration.gates._audited_reads_sha256",
    "assurance_agent.workflow.orchestration.gates._gate_details",
    "assurance_agent.workflow.orchestration.gates.resolve_view_path",
    "assurance_agent.workflow.orchestration.gates.expand_gate_read_template",
    "assurance_agent.workflow.orchestration.gates._latest_graph_gate_decision",
    "assurance_agent.workflow.orchestration.gates._apply_gate_decision",
    "assurance_agent.workflow.orchestration.gates._decision_matches_source_epoch",
    "assurance_agent.workflow.orchestration.gates._checkpoint_matches_gate",
    "assurance_agent.workflow.orchestration.gates._current_gate_attempt_id",
    "assurance_agent.workflow.orchestration.gates.resolve_checkpoint_gate_id",
    "assurance_agent.workflow.orchestration.gates.CHECKPOINT_GATE_ALIASES",
    "assurance_agent.workflow.orchestration.dsl.parse_expression",
    "assurance_agent.workflow.orchestration.dsl.evaluate",
    "assurance_agent.workflow.orchestration.dsl._convert",
    "assurance_agent.workflow.orchestration.dsl._eval_boolop",
    "assurance_agent.workflow.orchestration.dsl._eval_compare",
    "assurance_agent.workflow.orchestration.dsl._eval_call",
    "assurance_agent.workflow.orchestration.dsl._to_bool",
    "assurance_agent.workflow.orchestration.dsl._typed_eq",
    "assurance_agent.workflow.orchestration.dsl.BUILTIN_ARITY",
    "assurance_agent.workflow.orchestration.dsl._KEYWORD_LITERALS",
    "assurance_agent.workflow.orchestration.dsl._COMPARE_OPS",
    "assurance_agent.workflow.orchestration.dsl.MAX_EXPR_LEN",
    "assurance_agent.workflow.orchestration.dsl.MAX_DEPTH",
    "assurance_agent.verification.gate_state.plan_assurance_state",
    "assurance_agent.verification.checks.registry.validate_plan_check_document",
    "assurance_agent.verification.profiles.get_layer_assurance_profile",
    "assurance_agent.knowledge.capabilities.capabilities_present",
    "assurance_agent.knowledge.capabilities.compute_missing_capabilities",
    "assurance_agent.knowledge.capabilities.plan_review_route",
    "assurance_agent.knowledge.capabilities.is_leaf_present",
    "assurance_agent.knowledge.capabilities._get_nested",
    "assurance_agent.workflow.core.audit_scope.is_audited_gate_read",
    "assurance_agent.artifacts.models.review.Review",
    "assurance_agent.artifacts.models.review.PlanReview",
    "assurance_agent.artifacts.models.review._CAPABILITY_GATED_REVIEW_TYPES",
    "assurance_agent.artifacts.models.review._PLAN_REVIEW_TYPES",
    "assurance_agent.artifacts.models.review._HUMAN_ONLY_PLAN_REVIEW_TYPES",
    "assurance_agent.artifacts.models.plan_checks.PlanCheckDocument",
    "assurance_agent.artifacts.models.plan_checks.LayerApplicability",
    "assurance_agent.artifacts.models.plan_checks.CheckEvidence",
    "assurance_agent.artifacts.models.plan_checks.PLAN_CHECK_IDS",
    "assurance_agent.artifacts.models.assurance.KNOWN_PLAN_CHECK_IDS",
    "assurance_agent.artifacts.models.data_knowledge.DataKnowledge",
    "assurance_agent.artifacts.models.data_knowledge.AuthLeaf",
    "assurance_agent.artifacts.models.data_knowledge.AccountLeaf",
    "assurance_agent.artifacts.models.data_knowledge.EntityLeaf",
    "assurance_agent.artifacts.models.data_knowledge.CleanupLeaf",
    "assurance_agent.artifacts.models.data_knowledge.CapabilityLeaf",
)

_GATE_SEMANTIC_CONSUMERS: tuple[str, ...] = (
    "check_gate_in_view",
    "plan_assurance_state",
    "plan_check_replay",
    "runtime_versions",
)

_SEMANTIC_VERSIONS: dict[str, str] = dict.fromkeys(_REPLAY_SEMANTIC_DEPENDENCIES, "1")


@dataclass(frozen=True, slots=True)
class SemanticSymbol:
    qualified_name: str
    kind: SymbolKind
    semantic_version: str
    implementation_digest: str


@dataclass(frozen=True, slots=True)
class GateSemanticsManifest:
    schema_version: str
    semantics_id: str
    symbols: tuple[SemanticSymbol, ...]
    consumers: tuple[str, ...]
    runtime_versions: dict[str, str]
    digest: str
    object_digest: str
    canonical_bytes: bytes


def normalized_ast_digest(source: str) -> str:
    tree = ast.parse(textwrap.dedent(source))
    dumped = ast.dump(tree, include_attributes=False)
    return hashlib.sha256(dumped.encode("utf-8")).hexdigest()


def normalized_value_digest(value: object) -> str:
    canonical = json.dumps(
        _normalize_value(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def discover_replay_semantic_dependencies() -> frozenset[str]:
    return frozenset(_REPLAY_SEMANTIC_DEPENDENCIES)


def resolve_runtime_versions() -> dict[str, str]:
    return {
        "python": f"{sys.version_info.major}.{sys.version_info.minor}",
        "pydantic": metadata.version("pydantic"),
        "pydantic_core": metadata.version("pydantic-core"),
        "pyyaml": metadata.version("PyYAML"),
    }


_AGENT_PREFIX = "assurance_agent."
_OPERATIONS_CATALOG_PREFIX = "workflow.driver.operations_catalog"
_MOVED_KERNEL_PREFIXES: tuple[str, ...] = (
    "workflow.graph",
    "workflow.core",
    "workflow.orchestration",
    "workflow.driver",
    "workflow.skill_memory",
    "artifacts",
    "verification",
    "evidence",
    "knowledge",
    "resources",
    "product",
    "exceptions",
    "config",
    "identifiers",
    "change_location",
)


def _rewrite_locked_kernel_pin(qualified_name: str) -> str:
    """Map frozen ``assurance_agent.<moved>`` digest pins onto ``assurance_kernel``."""
    if not qualified_name.startswith(_AGENT_PREFIX):
        return qualified_name
    rest = qualified_name[len(_AGENT_PREFIX) :]
    if rest == _OPERATIONS_CATALOG_PREFIX or rest.startswith(f"{_OPERATIONS_CATALOG_PREFIX}."):
        return qualified_name
    for prefix in _MOVED_KERNEL_PREFIXES:
        if rest == prefix or rest.startswith(f"{prefix}."):
            return f"assurance_kernel.{rest}"
    return qualified_name


def _import_qualified_object(qualified_name: str) -> object:
    parts = qualified_name.split(".")
    last_error: Exception | None = None
    for i in range(len(parts), 0, -1):
        module_name = ".".join(parts[:i])
        try:
            module = importlib.import_module(module_name)
        except ImportError as exc:
            last_error = exc
            continue
        obj: object = module
        try:
            for attr in parts[i:]:
                obj = getattr(obj, attr)
        except AttributeError as exc:
            last_error = exc
            continue
        return obj
    raise ImportError(f"cannot resolve semantic dependency: {qualified_name}") from last_error


def resolve_qualified_object(qualified_name: str) -> object:
    """Resolve ``package.module.Attr`` or ``package.module.Class.method``.

    Locked kernel pins keep their historical ``assurance_agent.*`` strings for
    resume digests, but the import is rewritten onto ``assurance_kernel``.
    Leftover product pins are resolved through ``ProductHooks.resolve_semantic_pin``.
    """
    import_name = _rewrite_locked_kernel_pin(qualified_name)
    if import_name.startswith(_AGENT_PREFIX):
        return current_product_hooks().resolve_semantic_pin(qualified_name)
    return _import_qualified_object(import_name)


def symbol_implementation_digest(
    qualified_name: str,
    *,
    source_override: str | None = None,
    constant_override: object | None = None,
) -> str:
    obj = resolve_qualified_object(qualified_name)
    if constant_override is not None:
        return normalized_value_digest(constant_override)
    if source_override is not None:
        return normalized_ast_digest(source_override)
    return implementation_digest_for(obj)


def implementation_digest_for(obj: object) -> str:
    if inspect.isfunction(obj) or inspect.isclass(obj) or inspect.ismethod(obj):
        return normalized_ast_digest(inspect.getsource(obj))
    return normalized_value_digest(obj)


def build_gate_semantics_manifest(
    *,
    source_overrides: Mapping[str, str] | None = None,
    constant_overrides: Mapping[str, object] | None = None,
    source_digest_overrides: Mapping[str, str] | None = None,
) -> GateSemanticsManifest:
    overrides = dict(source_overrides or {})
    constants = dict(constant_overrides or {})
    digest_overrides = dict(source_digest_overrides or {})
    symbols: list[SemanticSymbol] = []
    for qualified_name in _REPLAY_SEMANTIC_DEPENDENCIES:
        obj = resolve_qualified_object(qualified_name)
        kind = _symbol_kind(obj)
        if qualified_name in digest_overrides:
            impl_digest = digest_overrides[qualified_name]
        elif qualified_name in constants:
            impl_digest = normalized_value_digest(constants[qualified_name])
        elif qualified_name in overrides:
            impl_digest = normalized_ast_digest(overrides[qualified_name])
        else:
            impl_digest = implementation_digest_for(obj)
        symbols.append(
            SemanticSymbol(
                qualified_name=qualified_name,
                kind=kind,
                semantic_version=_SEMANTIC_VERSIONS[qualified_name],
                implementation_digest=impl_digest,
            )
        )
    runtime_versions = resolve_runtime_versions()
    consumers = tuple(sorted(_GATE_SEMANTIC_CONSUMERS))
    digest = _aggregate_semantic_digest(tuple(symbols), runtime_versions)
    canonical_bytes = _canonical_descriptor_bytes(
        semantics_id=SEMANTICS_ID,
        schema_version=MANIFEST_SCHEMA_VERSION,
        runtime_versions=runtime_versions,
        dependencies=[
            {
                "implementation_digest": symbol.implementation_digest,
                "kind": symbol.kind,
                "qualified_name": symbol.qualified_name,
                "semantic_version": symbol.semantic_version,
            }
            for symbol in symbols
        ],
        consumers=consumers,
        semantic_digest=digest,
    )
    return GateSemanticsManifest(
        schema_version=MANIFEST_SCHEMA_VERSION,
        semantics_id=SEMANTICS_ID,
        symbols=tuple(symbols),
        consumers=consumers,
        runtime_versions=runtime_versions,
        digest=digest,
        object_digest=hashlib.sha256(canonical_bytes).hexdigest(),
        canonical_bytes=canonical_bytes,
    )


@lru_cache(maxsize=1)
def gate_semantics_digest() -> str:
    return build_gate_semantics_manifest().digest


@lru_cache(maxsize=1)
def gate_semantics_bytes() -> bytes:
    return build_gate_semantics_manifest().canonical_bytes


@lru_cache(maxsize=1)
def gate_semantics_object_digest() -> str:
    return build_gate_semantics_manifest().object_digest


def canonical_descriptor_bytes(
    *,
    semantics_id: str,
    schema_version: str,
    runtime_versions: Mapping[str, str],
    dependencies: Sequence[Mapping[str, object]],
    consumers: tuple[str, ...],
    semantic_digest: str,
) -> bytes:
    return _canonical_descriptor_bytes(
        semantics_id=semantics_id,
        schema_version=schema_version,
        runtime_versions=runtime_versions,
        dependencies=dependencies,
        consumers=consumers,
        semantic_digest=semantic_digest,
    )


def aggregate_semantic_digest(
    dependencies: Sequence[Mapping[str, object]],
    runtime_versions: Mapping[str, str],
    *,
    schema_version: str,
) -> str:
    payload = {
        "runtime_versions": {key: runtime_versions[key] for key in sorted(runtime_versions)},
        "schema_version": schema_version,
        "symbols": [
            {
                "implementation_digest": item["implementation_digest"],
                "qualified_name": item["qualified_name"],
                "semantic_version": item["semantic_version"],
            }
            for item in dependencies
        ],
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256((canonical + "\n").encode("utf-8")).hexdigest()


def _aggregate_semantic_digest(
    symbols: tuple[SemanticSymbol, ...],
    runtime_versions: Mapping[str, str],
) -> str:
    return aggregate_semantic_digest(
        [
            {
                "implementation_digest": symbol.implementation_digest,
                "qualified_name": symbol.qualified_name,
                "semantic_version": symbol.semantic_version,
            }
            for symbol in symbols
        ],
        runtime_versions,
        schema_version=MANIFEST_SCHEMA_VERSION,
    )


def _canonical_descriptor_bytes(
    *,
    semantics_id: str,
    schema_version: str,
    runtime_versions: Mapping[str, str],
    dependencies: Sequence[Mapping[str, object]],
    consumers: tuple[str, ...],
    semantic_digest: str,
) -> bytes:
    payload = {
        "consumers": list(consumers),
        "dependencies": [dict(item) for item in dependencies],
        "runtime_versions": {key: runtime_versions[key] for key in sorted(runtime_versions)},
        "schema_version": schema_version,
        "semantic_digest": semantic_digest,
        "semantics_id": semantics_id,
    }
    return (json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode(
        "utf-8"
    )


def _symbol_kind(obj: object) -> SymbolKind:
    if inspect.isfunction(obj) or inspect.ismethod(obj):
        return "function"
    if inspect.isclass(obj):
        return "class"
    return "constant"


def _normalize_value(value: object) -> object:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, MappingProxyType):
        return _normalize_value(dict(value))
    if isinstance(value, Mapping):
        return {
            str(key): _normalize_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, frozenset):
        return [_normalize_value(item) for item in sorted(value, key=repr)]
    if isinstance(value, (set, tuple, list)):
        return [_normalize_value(item) for item in sorted(value, key=repr)]
    raise TypeError(f"unsupported constant type for digest: {type(value)!r}")


__all__ = [
    "SEMANTICS_ID",
    "MANIFEST_SCHEMA_VERSION",
    "SemanticSymbol",
    "GateSemanticsManifest",
    "aggregate_semantic_digest",
    "canonical_descriptor_bytes",
    "discover_replay_semantic_dependencies",
    "gate_semantics_bytes",
    "gate_semantics_digest",
    "gate_semantics_object_digest",
    "build_gate_semantics_manifest",
    "implementation_digest_for",
    "normalized_ast_digest",
    "normalized_value_digest",
    "resolve_qualified_object",
    "resolve_runtime_versions",
    "symbol_implementation_digest",
]
