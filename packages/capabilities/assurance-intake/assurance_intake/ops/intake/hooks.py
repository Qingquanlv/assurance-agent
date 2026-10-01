"""Intake seeds the requirement and run spec, then authenticates the change marker."""

from __future__ import annotations

import yaml
from pydantic import ValidationError

from agent_runtime_contracts.ops import FinalizeContext, OutputError, PrepareContext

from assurance_intake.contracts.cases import IntakeQaV1
from assurance_intake.contracts.explore import REQUIREMENT_PATH, RUN_SPEC_SNAPSHOT_PATH
from assurance_intake.domain.artifacts import (
    ArtifactListResultV1,
    FinalizedArtifactsV1,
    authenticate_files,
    authenticate_receipt,
    file_digest,
    read_regular_bytes,
)
from assurance_intake.ops.intake.models import IntakeInputV1

MARKER_PATH = "qa/.qa.yaml"


def materialize_requirement(text: str) -> bytes:
    return (text.removesuffix("\n") + "\n").encode("utf-8")


def before(ctx: PrepareContext, business: IntakeInputV1) -> IntakeInputV1:
    ctx.write(REQUIREMENT_PATH, materialize_requirement(business.requirement))
    snapshot = yaml.safe_dump(
        {"candidate_test_families": list(business.candidate_test_families)},
        sort_keys=True,
    ).encode("utf-8")
    ctx.write(RUN_SPEC_SNAPSHOT_PATH, snapshot)
    return business


def after(
    ctx: FinalizeContext, business: IntakeInputV1, result: ArtifactListResultV1
) -> FinalizedArtifactsV1:
    artifacts = authenticate_receipt(ctx.write_root, result, business.artifact_paths)
    requirement = authenticate_files(
        ctx.write_root,
        (REQUIREMENT_PATH,),
        business.artifact_paths + (REQUIREMENT_PATH,),
    )
    marker_ref = next((item for item in artifacts if item["path"] == MARKER_PATH), None)
    if marker_ref is None:
        raise OutputError("intake receipt must include qa/.qa.yaml")
    marker_bytes = read_regular_bytes(ctx.write_root, MARKER_PATH, kind="intake marker")
    if file_digest(marker_bytes) != marker_ref["digest"]:
        raise OutputError("qa/.qa.yaml changed during finalization")
    try:
        marker = IntakeQaV1.model_validate(yaml.safe_load(marker_bytes))
    except (yaml.YAMLError, UnicodeError, ValidationError) as error:
        raise OutputError(f"invalid qa/.qa.yaml: {error}") from error
    if marker.change_id != business.change_id:
        raise OutputError("qa/.qa.yaml change_id does not match locked change_id")
    merged = {item["path"]: item for item in (*artifacts, *requirement)}
    return FinalizedArtifactsV1.model_validate({"artifacts": [merged[path] for path in sorted(merged)]})
