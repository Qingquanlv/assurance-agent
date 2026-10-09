"""Rerun prepare loads generation and splices the applied-repair handoff."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_runtime_contracts.ops import InputError
from graph_engine.artifacts import stage_json_artifact
from graph_engine.attempts.models.resolutions import ReceiptRef

from assurance_execution.contracts.agent import PreparedExecutionV1, RerunPrepareInputV1
from assurance_execution.contracts.workflow import APPLIED_REPAIR_PATH, AppliedRepairHandoffV1
from assurance_execution.operations.rerun import prepare_rerun
from assurance_generation.contracts.workflow import GenerationCycleResultV1
from assurance_intake.contracts import EvidenceArtifactRefV1

from test_execution_graph_factory import generation_result  # pyright: ignore[reportMissingImports]


def _ref(path: str, digest: str) -> EvidenceArtifactRefV1:
    return EvidenceArtifactRefV1(path=path, digest=digest)


def _input(tmp_path: Path, **extra: object) -> RerunPrepareInputV1:
    generation = GenerationCycleResultV1.model_validate(generation_result())
    fields = RerunPrepareInputV1.model_fields
    payload = {
        key: generation_result()[key]
        for key in ("change_id", "plan_digest", "plan_ref", "coverage_epoch")
        if key in fields
    }
    payload.update(
        selected_test_families=["api"],
        capability_leafs=["entities.item.create"],
        execution_kind="run",
        coverage_epoch=generation.coverage_epoch,
    )
    payload.update(extra)
    return RerunPrepareInputV1.model_validate(payload)


def test_an_adapted_rerun_without_a_repair_loads_generation(tmp_path: Path) -> None:
    generation = GenerationCycleResultV1.model_validate(generation_result())
    staged = stage_json_artifact(tmp_path, "qa/results/codegen/generation-cycle.json", generation)
    payload = _input(
        tmp_path,
        generation_ref={"path": staged.path, "digest": staged.digest},
    )
    prepared = prepare_rerun(payload, tmp_path)
    assert isinstance(prepared, PreparedExecutionV1)
    assert prepared.generation_result == generation
    assert "apply_receipt" not in payload.model_dump(mode="json")


def test_an_applied_repair_rewrites_generation_from_the_handoff(tmp_path: Path) -> None:
    generation = GenerationCycleResultV1.model_validate(generation_result())
    staged = stage_json_artifact(tmp_path, "qa/results/codegen/generation-cycle.json", generation)
    new = _ref("tests/test_api.py", "b" * 64)
    mapping = _ref("qa/generation/mapping.json", "c" * 64)
    handoff = AppliedRepairHandoffV1(
        change_id=generation.change_id,
        plan_digest=generation.plan_digest,
        plan_ref=generation.plan_ref,
        coverage_epoch=generation.coverage_epoch,
        repair_round=1,
        changed_test_refs=(new,),
        mapping_ref=mapping,
    )
    href = stage_json_artifact(tmp_path, APPLIED_REPAIR_PATH, handoff)
    receipt = ReceiptRef(receipt_id="repair", receipt_digest="d" * 64)
    payload = _input(
        tmp_path,
        generation_ref={"path": staged.path, "digest": staged.digest},
        applied_repair_ref={"path": href.path, "digest": href.digest},
        apply_receipt=receipt.model_dump(mode="json"),
    )
    prepared = prepare_rerun(payload, tmp_path)
    assert prepared.generation_result is not None
    assert prepared.generation_result.mapping_ref == mapping
    assert prepared.generation_result.source_refs == tuple(
        sorted((*generation.source_refs, new), key=lambda item: item.path)
    )


def test_a_repair_receipt_without_the_handoff_is_an_input_failure(tmp_path: Path) -> None:
    payload = _input(
        tmp_path,
        apply_receipt=ReceiptRef(receipt_id="repair", receipt_digest="d" * 64).model_dump(mode="json"),
    )
    with pytest.raises(InputError, match="applied test repair"):
        prepare_rerun(payload, tmp_path)
