"""Blob 摄入（v7.1 §8 / S2）。"""

from __future__ import annotations

import json
from collections.abc import Mapping

import yaml
from pydantic import BaseModel

from assurance_agent.artifacts.registry import match_artifact
from assurance_agent.workflow.graph.frozen_output import FrozenOutput, enforce_size_limits
from assurance_agent.workflow.graph.ingest_catalog import (
    IngestArtifactCatalog,
    IngestArtifactDef,
    catalog_for_output_path,
    resolve_model,
    validate_catalog_runtime,
)
from assurance_agent.workflow.graph.workspace import TreeStore, WorkspaceError


def _lookup_catalog_entry(
    output_path: str,
    *,
    catalog: IngestArtifactCatalog | None,
) -> tuple[str, IngestArtifactDef] | None:
    if catalog is None:
        return catalog_for_output_path(output_path)
    for symbol, spec in catalog.artifacts.items():
        if spec.path == output_path and spec.kind == "file_ingest":
            return symbol, spec
    return None


def ingest_from_write_set(
    store: TreeStore,
    *,
    write_set_id: str,
    output_paths: tuple[str, ...],
    catalog: IngestArtifactCatalog | None = None,
    model_map: Mapping[str, type[BaseModel]] | None = None,
) -> dict[str, FrozenOutput]:
    """从 content-addressed write-set blob 摄入 catalog 声明的 concrete file outputs。"""
    resolved_catalog = catalog
    if resolved_catalog is None:
        if model_map is not None:
            raise ValueError("model_map requires an explicit ingest catalog")
        # Legacy/current-path callers may omit both; pinned execution must supply them.
        validate_catalog_runtime()
        resolved_catalog = None
    write_set = store.load_write_set(write_set_id)
    frozen: dict[str, FrozenOutput] = {}
    for output_path in output_paths:
        root, _, rest = output_path.partition(":")
        if root != "change" or not rest or rest.endswith("/"):
            continue
        hit = _lookup_catalog_entry(output_path, catalog=resolved_catalog)
        if hit is None:
            # Fall back to artifact registry for must_compat json outputs.
            spec = match_artifact(rest)
            if spec is None or spec.compat != "must_compat":
                continue
            symbol = rest.replace("/", "_").replace(".", "_")
            sha = write_set.outputs_sha256.get(output_path)
            if sha is None:
                continue
            blob = store.read_object(sha)
            if rest.endswith((".yaml", ".yml")):
                data = yaml.safe_load(blob.decode("utf-8"))
            else:
                data = json.loads(blob.decode("utf-8"))
            spec.model.model_validate(data)
            value = json.loads(json.dumps(data, default=str))
            frozen[symbol] = FrozenOutput(
                value=value,
                source_path=output_path,
                source_sha256=sha,
                model_id=spec.artifact_type,
                model_schema_digest="",
                catalog_symbol=symbol,
            )
            continue
        symbol, art = hit
        sha = write_set.outputs_sha256.get(output_path)
        if sha is None:
            continue
        try:
            blob = store.read_object(sha)
        except WorkspaceError:
            continue
        if art.codec == "yaml":
            data = yaml.safe_load(blob.decode("utf-8"))
        else:
            data = json.loads(blob.decode("utf-8"))
        model_id = art.model or "review@1"
        if model_map is not None:
            model = model_map.get(model_id)
            if model is None:
                raise ValueError(f"pinned ingest model map missing {model_id!r}")
        else:
            model = resolve_model(model_id)
        validated = model.model_validate(data)
        value = validated.model_dump(mode="json", exclude_unset=True)
        frozen[symbol] = FrozenOutput(
            value=value,
            source_path=output_path,
            source_sha256=sha,
            model_id=art.model or "",
            model_schema_digest=art.model_schema_digest,
            catalog_symbol=symbol,
        )
    enforce_size_limits(frozen)
    return frozen
