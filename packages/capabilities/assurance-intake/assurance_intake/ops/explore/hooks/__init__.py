"""Explore receives a frozen context, then finalize seals the official exploration."""

from __future__ import annotations

from typing import cast

from agent_runtime_contracts.ops import (
    ArtifactListResultV1,
    FinalizeContext,
    PrepareContext,
)
from graph_engine.canonical import canonical_json_bytes

from assurance_intake.contracts.explore import (
    EXPLORATION_PATH,
    REQUIREMENT_PATH,
    RUN_SPEC_SNAPSHOT_PATH,
    ExploreAdvisoryV1,
)
from assurance_intake.contracts.impact import ChangeImpactInventoryV1, INVENTORY_PATH
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1
from assurance_intake.domain.artifacts import leafs
from assurance_intake.ops.explore.hooks.context import build_explore_context
from assurance_intake.ops.explore.hooks.outputs import seal_official_exploration, validate_explore_outputs
from assurance_intake.ops.explore.models import (
    CONTEXT_PATH,
    EXPLORATION_DRAFT_PATH,
    CHANGE_EVIDENCE_PATH,
    ChangeEvidenceV1,
    ExploreContextV1,
    ExploreInputV1,
)


def before(ctx: PrepareContext, business: ExploreInputV1) -> ExploreInputV1:
    requirement = ctx.get_file(REQUIREMENT_PATH)
    snapshot = ctx.get_file(RUN_SPEC_SNAPSHOT_PATH)
    evidence = ctx.get_file(CHANGE_EVIDENCE_PATH)
    assert requirement is None or isinstance(requirement, bytes)
    assert snapshot is None or isinstance(snapshot, bytes)
    assert evidence is None or isinstance(evidence, ChangeEvidenceV1)
    document = build_explore_context(
        ctx.project_root,
        change_id=business.change_id,
        capability_leafs=business.capability_leafs,
        requirement_data=requirement,
        snapshot_data=snapshot,
        evidence=evidence,
        requirement_ref=(
            EvidenceArtifactRefV1.model_validate(ctx.project_ref(REQUIREMENT_PATH).model_dump(mode="json"))
            if requirement is not None
            else None
        ),
        snapshot_ref=(
            EvidenceArtifactRefV1.model_validate(
                ctx.project_ref(RUN_SPEC_SNAPSHOT_PATH).model_dump(mode="json")
            )
            if snapshot is not None
            else None
        ),
    )
    ctx.write(CONTEXT_PATH, canonical_json_bytes(document.model_dump(mode="json")) + b"\n")
    return business


def after(ctx: FinalizeContext, business: ExploreInputV1, result: ArtifactListResultV1) -> dict[str, object]:
    advisory = ctx.file(EXPLORATION_DRAFT_PATH)
    inventory = ctx.file(INVENTORY_PATH)
    context = ctx.file(CONTEXT_PATH)
    assert isinstance(advisory, ExploreAdvisoryV1)
    assert isinstance(inventory, ChangeImpactInventoryV1)
    assert isinstance(context, ExploreContextV1)
    validate_explore_outputs(
        advisory,
        inventory,
        context,
        change_id=business.change_id,
        capability_leafs=leafs(business.capability_leafs),
    )
    official = seal_official_exploration(
        ctx.write_root,
        advisory,
        requirement_bytes=cast(bytes | None, ctx.get_project_file(REQUIREMENT_PATH)),
        snapshot_bytes=cast(bytes | None, ctx.get_project_file(RUN_SPEC_SNAPSHOT_PATH)),
        candidate_families=frozenset(),
        policy_required=frozenset(),
    )
    ctx.stage(EXPLORATION_PATH, official)
    return {}
