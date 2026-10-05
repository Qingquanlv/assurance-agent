"""Build the case-rework document for the round ``coverage.next`` just opened."""

from __future__ import annotations

from agent_runtime_contracts.ops import InputError
from graph_engine.artifacts import ArtifactReadError, open_artifact, stage_json_artifact
from graph_engine.plugin_api import TaskContext

from assurance_intake.contracts.coverage_rework import (
    REWORK_CONTEXT_PATH,
    CoverageReworkHandoffV1,
    CoverageReworkInputV1,
    CoverageReworkOutputV1,
)
from assurance_intake.contracts.workflow import CaseReworkContextV1, EvidenceArtifactRefV1, ReviewedCaseV1


def run(ctx: TaskContext, business: CoverageReworkInputV1) -> CoverageReworkOutputV1:
    try:
        reviewed_case = open_artifact(ctx.project_root, business.reviewed_case_ref, model=ReviewedCaseV1)
        handoff = open_artifact(ctx.project_root, business.handoff_ref, model=CoverageReworkHandoffV1)
    except ArtifactReadError as error:
        raise InputError(str(error)) from error
    if reviewed_case.change_id != business.change_id:
        raise ValueError("coverage rework change_id must match the reviewed case")
    # The coverage loop increments before this task, so the new round is one past the inspect epoch.
    if business.coverage_epoch != handoff.source_epoch + 1:
        raise ValueError("coverage advance source epoch does not match current state")
    gaps = tuple(
        ref
        for ref in handoff.assessment_refs
        if ref.path.endswith("/gaps.json") or ref.path.endswith("/coverage-gaps.json")
    )
    if len(gaps) != 1:
        raise ValueError("coverage advance requires exactly one authenticated gaps ref")
    document = CaseReworkContextV1(
        previous_case=reviewed_case,
        inspect_receipt=business.inspect_receipt,
        assessment_refs=handoff.assessment_refs,
        gaps_ref=gaps[0],
        target_case_paths=tuple(ref.path for ref in reviewed_case.case_refs),
    )
    staged = stage_json_artifact(ctx.write_root, REWORK_CONTEXT_PATH, document)
    return CoverageReworkOutputV1(rework_ref=EvidenceArtifactRefV1(path=staged.path, digest=staged.digest))
