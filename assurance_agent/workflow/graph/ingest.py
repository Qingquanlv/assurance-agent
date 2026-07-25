"""Blob 摄入（v7.1 §8 / S2）。"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from assurance_agent.artifacts.registry import match_artifact
from assurance_agent.workflow.graph.frozen_output import FrozenOutput, enforce_size_limits
from assurance_agent.workflow.graph.ingest_catalog import (
    catalog_for_output_path,
    resolve_model,
    validate_catalog_runtime,
)
from assurance_agent.workflow.graph.workspace import TreeStore, WorkspaceError


def ingest_from_write_set(
    store: TreeStore,
    *,
    write_set_id: str,
    output_paths: tuple[str, ...],
) -> dict[str, FrozenOutput]:
    """从 content-addressed write-set blob 摄入 catalog 声明的 concrete file outputs。"""
    catalog = validate_catalog_runtime()
    write_set = store.load_write_set(write_set_id)
    frozen: dict[str, FrozenOutput] = {}
    for output_path in output_paths:
        root, _, rest = output_path.partition(":")
        if root != "change" or not rest or rest.endswith("/"):
            continue
        hit = catalog_for_output_path(output_path)
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
            # Mirror the codec detection used by the catalog branch below and by
            # finalize._validate_registry_outputs: YAML must_compat artifacts
            # (e.g. .qa.yaml / case.yaml) must not be parsed as JSON.
            if rest.endswith((".yaml", ".yml")):
                data = yaml.safe_load(blob.decode("utf-8"))
            else:
                data = json.loads(blob.decode("utf-8"))
            # Validate against the must_compat contract (fail closed) but freeze the
            # RAW parsed document as the value. These registry-fallback symbols are
            # the ones finalize._candidate_artifact_overrides feeds back into the
            # attached-gate dual run, which compares an in-memory candidate override
            # against the gate's raw on-disk read. A model_dump() projection is lossy
            # — it drops fields absent from the model (e.g. .qa.yaml's `approval`,
            # which case-design-gate reads) and renames aliased fields (`schema` →
            # `schema_`) — so it would flip the gate verdict and trip the dual-run
            # guard. The raw doc matches the disk parse; json round-trip keeps the
            # frozen value wire-serializable (FrozenOutput.canonical_value_bytes).
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
        model = resolve_model(art.model or "review@1")
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
