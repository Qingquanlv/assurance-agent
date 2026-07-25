"""IngestArtifactCatalog loader（v7.1 §3 / S1）。"""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from assurance_agent.artifacts.models.review import Review

_CATALOG_PATH = Path(__file__).resolve().parents[2] / "_resources/schemas/ingest-artifact-catalog.yaml"


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
    registry: dict[str, type[BaseModel]] = {"review@1": Review}
    if model_id not in registry:
        raise ValueError(f"unknown ingest model id '{model_id}'")
    return registry[model_id]


def model_schema_digest(model_id: str) -> str:
    schema = resolve_model(model_id).model_json_schema(mode="serialization")
    text = json.dumps(schema, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@lru_cache(maxsize=1)
def load_ingest_catalog() -> IngestArtifactCatalog:
    raw = yaml.safe_load(_CATALOG_PATH.read_text(encoding="utf-8"))
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
