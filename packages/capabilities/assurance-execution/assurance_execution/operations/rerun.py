"""Rewrite a rerun's generation from the applied repair healing wrote."""

from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError

from agent_runtime_contracts.ops import InputError
from graph_engine.artifacts import ArtifactReadError, open_artifact

from assurance_execution.contracts.agent import PreparedExecutionV1, RerunPrepareInputV1
from assurance_execution.contracts.workflow import AppliedRepairHandoffV1
from assurance_generation.contracts.workflow import GenerationCycleResultV1
from assurance_healing.contracts.application import AppliedTestRepairV1


def prepare_rerun(payload: RerunPrepareInputV1, workspace: Path) -> PreparedExecutionV1:
    """Load the generation cycle and, on rerun, splice the latest applied repair.

    The generation ref and the repair ref stay on the attempt input. After this
    returns, ``generation_result`` is the document the runner already used.
    """
    generation = _generation(workspace, payload)
    if payload.execution_kind != "run" or not _repair_present(payload):
        return _prepared(payload, generation)
    if payload.applied_repair_ref is None or payload.apply_receipt is None:
        raise InputError("rerun requires an applied test repair")
    if generation is None:
        raise InputError("rerun requires generation")
    try:
        handoff = open_artifact(workspace, payload.applied_repair_ref, model=AppliedRepairHandoffV1)
        repair_payload = handoff.model_dump(mode="json")
        repair_payload.pop("schema_version", None)
        repair = AppliedTestRepairV1.model_validate(
            {
                **repair_payload,
                "status": "applied",
                "receipt": payload.apply_receipt.model_dump(mode="json"),
            }
        )
    except (ArtifactReadError, OSError, ValidationError, ValueError) as error:
        raise InputError("rerun requires an applied test repair") from error
    source_refs = {ref.path: ref for ref in generation.source_refs}
    source_refs.update({ref.path: ref for ref in repair.changed_test_refs})
    if repair.mapping_ref is None:
        raise InputError("rerun requires an applied test repair")
    repaired = generation.model_copy(
        update={
            "mapping_ref": repair.mapping_ref,
            "source_refs": tuple(source_refs[path] for path in sorted(source_refs)),
        }
    )
    return _prepared(payload, repaired)


def _generation(workspace: Path, payload: RerunPrepareInputV1) -> GenerationCycleResultV1 | None:
    if payload.generation_ref is None:
        return None
    try:
        return open_artifact(workspace, payload.generation_ref, model=GenerationCycleResultV1)
    except (ArtifactReadError, OSError, ValidationError, ValueError) as error:
        raise InputError("rerun requires generation") from error


def _prepared(
    payload: RerunPrepareInputV1,
    generation: GenerationCycleResultV1 | None,
) -> PreparedExecutionV1:
    data = payload.model_dump(mode="json")
    data.pop("applied_repair_ref", None)
    data.pop("apply_receipt", None)
    data["generation_result"] = None if generation is None else generation.model_dump(mode="json")
    return PreparedExecutionV1.model_validate(data)


def _repair_present(payload: RerunPrepareInputV1) -> bool:
    return payload.apply_receipt is not None or payload.applied_repair_ref is not None


__all__ = ["prepare_rerun"]
