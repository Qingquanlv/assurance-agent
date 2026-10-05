"""Repair fields computed from the generation and execution files."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from graph_engine.artifacts import ArtifactReadError, open_artifact

from assurance_execution.contracts.workflow import ExecutionCycleDocumentV1, ExecutionCycleResultV1
from assurance_generation.contracts.workflow import GenerationCycleResultV1
from assurance_healing.contracts.repair_input import RepairBoundInputV1
from assurance_intake.contracts import PolicyResourceV1


def opened_repair_input(root: Path, business: RepairBoundInputV1, error: type[Exception]) -> dict[str, Any]:
    """Open the two producer files and keep every field the caller already set."""

    try:
        generation = open_artifact(root, business.generation_ref, model=GenerationCycleResultV1)
        document = open_artifact(root, business.execution_ref, model=ExecutionCycleDocumentV1)
    except ArtifactReadError as read_error:
        if read_error.reason == "digest":
            raise error(f"evidence digest changed: {read_error.path}") from read_error
        raise error(str(read_error)) from read_error
    execution = {
        **document.model_dump(mode="json"),
        "receipt": business.execution_receipt.model_dump(mode="json"),
    }
    payload = business.model_dump(mode="json")
    payload.pop("generation_ref", None)
    payload.pop("execution_ref", None)
    payload.pop("execution_receipt", None)
    payload["generation_result"] = generation.model_dump(mode="json")
    payload["execution_result"] = execution
    return derive_repair_input(payload)


def derive_repair_input(value: Mapping[str, Any]) -> dict[str, Any]:
    """Add the adapter's computed fields only when the caller did not pass them."""
    filled = dict(value)
    if _missing(
        value,
        "allowed_paths",
        "allowed_test_paths",
        "allowed_roots",
        "source_refs",
        "mapping_paths",
        "mapping_ref",
        "reviewed_case",
        "plan_digest",
        "plan_ref",
    ):
        generation = _as(GenerationCycleResultV1, value.get("generation_result"), "generation_result")
        paths = tuple(sorted(ref.path for ref in generation.source_refs))
        _fill(filled, value, "allowed_paths", paths)
        _fill(filled, value, "allowed_test_paths", paths)
        _fill(filled, value, "allowed_roots", tuple(sorted({path.split("/", 1)[0] for path in paths})))
        _fill(filled, value, "source_refs", generation.source_refs)
        _fill(filled, value, "mapping_paths", (generation.mapping_ref.path,))
        _fill(filled, value, "mapping_ref", generation.mapping_ref)
        _fill(filled, value, "reviewed_case", generation.reviewed_case)
        _fill(filled, value, "plan_digest", generation.plan_digest)
        _fill(filled, value, "plan_ref", generation.plan_ref)
    if _missing(
        value,
        "baseline_digest",
        "execution_evidence_digest",
        "execution_ref",
        "candidate_digest",
        "coverage_epoch",
    ):
        execution = _as(ExecutionCycleResultV1, value.get("execution_result"), "execution_result")
        _fill(filled, value, "baseline_digest", execution.evidence_ref.digest)
        _fill(filled, value, "execution_evidence_digest", execution.evidence_ref.digest)
        _fill(filled, value, "execution_ref", execution.evidence_ref)
        _fill(filled, value, "candidate_digest", execution.receipt.receipt_digest)
        _fill(filled, value, "coverage_epoch", execution.coverage_epoch)
    if "policy_digest" not in value:
        policy = _as(PolicyResourceV1, _policy(value.get("product_policy")), "product_policy")
        filled["product_policy"] = policy
        _fill(filled, value, "policy_digest", policy.sha256)
    _fill(filled, value, "owner_id", "assurance.healing")
    return filled


def _policy(value: object) -> object:
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return dump(mode="json")
    return value


def _as(model: type[BaseModel], value: object, name: str) -> Any:
    if isinstance(value, model):
        return value
    try:
        return model.model_validate(value)
    except Exception as error:
        raise ValueError(f"{name} is not a {model.__name__}") from error


def _missing(value: Mapping[str, Any], *keys: str) -> bool:
    return any(key not in value for key in keys)


def _fill(filled: dict[str, Any], original: Mapping[str, Any], key: str, item: object) -> None:
    if key not in original and item is not None:
        filled[key] = item


__all__ = ["derive_repair_input", "opened_repair_input"]
