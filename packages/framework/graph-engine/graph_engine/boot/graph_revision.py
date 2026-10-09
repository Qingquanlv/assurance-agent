from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
import re
from types import MappingProxyType
from typing import TYPE_CHECKING

from graph_engine.canonical import JSONValue, canonical_digest

if TYPE_CHECKING:
    from langgraph.graph.state import CompiledStateGraph
    from pydantic import BaseModel

    from graph_engine.attempts.models.contracts import ResolvedAttemptContract


_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _sha256(value: str, kind: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{kind} digest must be a lowercase SHA-256 hex value")
    return value


def _frozen_sorted_sha256_mapping(values: Mapping[str, str], kind: str) -> Mapping[str, str]:
    if any(not key for key in values):
        raise ValueError(f"{kind} keys must be nonempty")
    normalized = {key: _sha256(digest, kind) for key, digest in values.items()}
    return MappingProxyType(dict(sorted(normalized.items())))


def _frozen_nonempty_versions(values: Mapping[str, str]) -> Mapping[str, str]:
    if any(not key for key in values):
        raise ValueError("state schema version keys must be nonempty")
    if any(not version for version in values.values()):
        raise ValueError("state schema version must be nonempty")
    return MappingProxyType(dict(sorted(values.items())))


def _unique_sorted_symbols(symbols: tuple[str, ...]) -> tuple[str, ...]:
    if any(not symbol for symbol in symbols):
        raise ValueError("factory symbols must be nonempty")
    if len(symbols) != len(set(symbols)):
        raise ValueError("factory symbols must be unique")
    return tuple(sorted(symbols))


@dataclass(frozen=True, slots=True)
class FeatureFactoryRef:
    owner_id: str
    symbol: str

    def __post_init__(self) -> None:
        if not self.owner_id:
            raise ValueError("feature factory owner id must be nonempty")
        if not self.symbol:
            raise ValueError("feature factory symbol must be nonempty")


@dataclass(frozen=True, slots=True)
class EntrypointGraphContract:
    name: str
    input_model: str
    output_model: str
    state_model: str
    input_schema_digest: str
    output_schema_digest: str
    state_schema_digest: str
    state_schema_version: str
    recursion_limit: int

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("entrypoint name must be nonempty")
        for field_name in ("input_model", "output_model", "state_model"):
            if not getattr(self, field_name):
                raise ValueError(f"{field_name.replace('_', ' ')} must be nonempty")
        _sha256(self.input_schema_digest, "entrypoint input schema")
        _sha256(self.output_schema_digest, "entrypoint output schema")
        _sha256(self.state_schema_digest, "entrypoint state schema")
        if not self.state_schema_version:
            raise ValueError("state schema version must be nonempty")
        if self.recursion_limit < 1:
            raise ValueError("recursion limit must be positive")

    def canonical_projection(self) -> dict[str, JSONValue]:
        return {
            "name": self.name,
            "input_model": self.input_model,
            "output_model": self.output_model,
            "state_model": self.state_model,
            "input_schema_digest": self.input_schema_digest,
            "output_schema_digest": self.output_schema_digest,
            "state_schema_digest": self.state_schema_digest,
            "state_schema_version": self.state_schema_version,
            "recursion_limit": self.recursion_limit,
        }


@dataclass(frozen=True, slots=True)
class GraphRevision:
    revision_id: str
    product_lock_digest: str
    wheel_source_digests: Mapping[str, str]
    factory_symbols: tuple[str, ...]
    state_schema_versions: Mapping[str, str]
    langgraph_version: str
    checkpoint_contract_version: str

    def __post_init__(self) -> None:
        _sha256(self.revision_id, "graph revision")
        _sha256(self.product_lock_digest, "product lock")
        object.__setattr__(
            self,
            "wheel_source_digests",
            _frozen_sorted_sha256_mapping(self.wheel_source_digests, "wheel source"),
        )
        object.__setattr__(self, "factory_symbols", _unique_sorted_symbols(self.factory_symbols))
        object.__setattr__(
            self,
            "state_schema_versions",
            _frozen_nonempty_versions(self.state_schema_versions),
        )
        if not self.langgraph_version:
            raise ValueError("langgraph version must be nonempty")
        if not self.checkpoint_contract_version:
            raise ValueError("checkpoint contract version must be nonempty")

    @classmethod
    def build(
        cls,
        *,
        product_lock_digest: str,
        wheel_source_digests: Mapping[str, str],
        factory_symbols: tuple[str, ...],
        state_schema_versions: Mapping[str, str],
        langgraph_version: str,
        checkpoint_contract_version: str,
    ) -> GraphRevision:
        draft = cls(
            revision_id="0" * 64,
            product_lock_digest=product_lock_digest,
            wheel_source_digests=wheel_source_digests,
            factory_symbols=factory_symbols,
            state_schema_versions=state_schema_versions,
            langgraph_version=langgraph_version,
            checkpoint_contract_version=checkpoint_contract_version,
        )
        return replace(draft, revision_id=draft.canonical_revision_id())

    def canonical_projection(self) -> dict[str, JSONValue]:
        return {
            "product_lock_digest": self.product_lock_digest,
            "wheel_source_digests": dict(self.wheel_source_digests),
            "factory_symbols": list(self.factory_symbols),
            "state_schema_versions": dict(self.state_schema_versions),
            "langgraph_version": self.langgraph_version,
            "checkpoint_contract_version": self.checkpoint_contract_version,
        }

    def canonical_revision_id(self) -> str:
        return canonical_digest(self.canonical_projection())

    def model_dump(self, *, mode: str = "python") -> dict[str, object]:
        symbols: object = list(self.factory_symbols) if mode == "json" else self.factory_symbols
        return {
            "revision_id": self.revision_id,
            "product_lock_digest": self.product_lock_digest,
            "wheel_source_digests": dict(self.wheel_source_digests),
            "factory_symbols": symbols,
            "state_schema_versions": dict(self.state_schema_versions),
            "langgraph_version": self.langgraph_version,
            "checkpoint_contract_version": self.checkpoint_contract_version,
        }


@dataclass(frozen=True, slots=True)
class GraphBuildManifest:
    revision: GraphRevision
    entrypoint_contract_digests: Mapping[str, str]
    attempt_contract_digests: Mapping[str, str]

    def __post_init__(self) -> None:
        if not isinstance(self.revision, GraphRevision):
            raise TypeError("manifest revision must be a GraphRevision")
        object.__setattr__(
            self,
            "entrypoint_contract_digests",
            _frozen_sorted_sha256_mapping(self.entrypoint_contract_digests, "entrypoint contract"),
        )
        object.__setattr__(
            self,
            "attempt_contract_digests",
            _frozen_sorted_sha256_mapping(self.attempt_contract_digests, "attempt contract"),
        )

    def model_dump(self, *, mode: str = "python") -> dict[str, object]:
        return {
            "revision": self.revision.model_dump(mode=mode),
            "entrypoint_contract_digests": dict(self.entrypoint_contract_digests),
            "attempt_contract_digests": dict(self.attempt_contract_digests),
        }


@dataclass(frozen=True, slots=True)
class BootArtifact:
    manifest: GraphBuildManifest
    entrypoints: Mapping[str, CompiledStateGraph]
    attempt_contracts: Mapping[str, ResolvedAttemptContract[BaseModel, BaseModel]]
    checkpointer_backend_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.manifest, GraphBuildManifest):
            raise TypeError("boot artifact manifest must be a GraphBuildManifest")
        if not self.checkpointer_backend_id:
            raise ValueError("checkpointer backend id must be nonempty")
        object.__setattr__(self, "entrypoints", MappingProxyType(dict(self.entrypoints)))
        object.__setattr__(self, "attempt_contracts", MappingProxyType(dict(self.attempt_contracts)))


__all__ = [
    "BootArtifact",
    "EntrypointGraphContract",
    "FeatureFactoryRef",
    "GraphBuildManifest",
    "GraphRevision",
]
