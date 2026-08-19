"""IngestArtifactCatalog loader（v7.1 §3 / S1）。"""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from assurance_agent import resources
from assurance_agent.artifacts.models.review import Review


def _catalog_digest(payload: dict[str, object]) -> str:
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class IngestModeDef(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    mode: Literal["full"] = "full"


class IngestArtifactDef(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    kind: Literal["file_ingest", "path_only"]
    model: str | None = None
    model_schema_digest: str = ""
    path: str
    codec: Literal["json", "yaml"] = "json"
    cardinality: Literal["one"] = "one"
    compat: Literal["must_compat", "versioned", "free"] = "must_compat"
    ingest: IngestModeDef = Field(default_factory=IngestModeDef)


class IngestArtifactCatalog(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: int
    artifacts: dict[str, IngestArtifactDef]

    @property
    def digest(self) -> str:
        payload = self.model_dump(mode="json")
        return _catalog_digest(payload)


def resolve_model(model_id: str) -> type[BaseModel]:
    from assurance_agent.verification.generated_files import (
        generated_files_contract_for_model_id,
        get_generated_files_model,
    )

    registry: dict[str, type[BaseModel]] = {"review@1": Review}
    if model_id in registry:
        return registry[model_id]
    try:
        contract = generated_files_contract_for_model_id(model_id)
    except ValueError as exc:
        raise ValueError(f"unknown ingest model id '{model_id}'") from exc
    return get_generated_files_model(contract.layer)


def model_schema_digest(model_id: str) -> str:
    schema = resolve_model(model_id).model_json_schema(mode="serialization")
    text = json.dumps(schema, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@lru_cache(maxsize=1)
def load_ingest_catalog() -> IngestArtifactCatalog:
    raw = yaml.safe_load(resources.read_text("schemas", "ingest-artifact-catalog.yaml"))
    if not isinstance(raw, dict):
        raise ValueError("ingest catalog root must be a mapping")
    try:
        return IngestArtifactCatalog.model_validate(raw)
    except ValidationError as exc:
        raise ValueError(f"invalid ingest catalog: {exc}") from exc


def catalog_for_output_path(output_path: str) -> tuple[str, IngestArtifactDef] | None:
    catalog = load_ingest_catalog()
    for symbol, spec in catalog.artifacts.items():
        if spec.path == output_path and spec.kind == "file_ingest":
            return symbol, spec
    return None


def validate_catalog_runtime() -> IngestArtifactCatalog:
    catalog = load_ingest_catalog()
    patched: dict[str, IngestArtifactDef] = {}
    for symbol, spec in catalog.artifacts.items():
        if spec.kind == "file_ingest" and spec.model:
            computed = model_schema_digest(spec.model)
            # Pinned digests are authoritative once set: a mismatch means the
            # model's serialization schema drifted from what the catalog froze,
            # so fail closed instead of silently ingesting a stale contract.
            if spec.model_schema_digest and spec.model_schema_digest != computed:
                raise ValueError(
                    f"ingest catalog model_schema_digest drift for '{symbol}' "
                    f"(model {spec.model!r}): pinned {spec.model_schema_digest!r} "
                    f"!= computed {computed!r}"
                )
            patched[symbol] = spec.model_copy(update={"model_schema_digest": computed})
        else:
            patched[symbol] = spec
    return catalog.model_copy(update={"artifacts": patched})


def _canonical_ingest_catalog_bytes(catalog: IngestArtifactCatalog) -> bytes:
    text = json.dumps(
        catalog.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return (text + "\n").encode("utf-8")


def parse_ingest_catalog_snapshot(data: bytes) -> IngestArtifactCatalog:
    """Parse schema version 1 and canonical JSON bytes without resolving models."""
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"ingest catalog snapshot is malformed: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("ingest catalog snapshot must be a JSON object")
    version = payload.get("schema_version")
    if version != 1:
        raise ValueError(f"ingest catalog snapshot schema_version must be 1, got {version!r}")
    try:
        catalog = IngestArtifactCatalog.model_validate(payload)
    except ValidationError as exc:
        raise ValueError(f"ingest catalog snapshot is invalid: {exc}") from exc
    if catalog.schema_version != 1:
        raise ValueError(f"ingest catalog snapshot schema_version must be 1, got {catalog.schema_version!r}")
    if _canonical_ingest_catalog_bytes(catalog) != data:
        raise ValueError("ingest catalog snapshot bytes are not canonical")
    return catalog
