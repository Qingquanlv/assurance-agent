"""Intake seeds the requirement and run spec, then authenticates the change marker."""

from __future__ import annotations

import yaml

from agent_runtime_contracts.ops import ArtifactListResultV1, FinalizeContext, OutputError, PrepareContext

from assurance_intake.contracts.explore import REQUIREMENT_PATH, RUN_SPEC_SNAPSHOT_PATH
from assurance_intake.ops.intake.models import IntakeInputV1, IntakeQaV1

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


def after(ctx: FinalizeContext, business: IntakeInputV1, result: ArtifactListResultV1) -> dict[str, object]:
    marker = ctx.file(MARKER_PATH)
    assert isinstance(marker, IntakeQaV1)
    if marker.change_id != business.change_id:
        raise OutputError("qa/.qa.yaml change_id does not match locked change_id")
    return {}
