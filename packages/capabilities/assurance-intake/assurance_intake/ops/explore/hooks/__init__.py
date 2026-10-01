"""Explore receives a frozen context, then finalize seals the official exploration."""

from __future__ import annotations

from agent_runtime_contracts.ops import (
    ArtifactListResultV1,
    FinalizeContext,
    InputError,
    OutputError,
    PrepareContext,
)
from graph_engine.canonical import canonical_json_bytes

from assurance_intake.contracts.agent import FinalizedArtifactsV1
from assurance_intake.contracts.explore import EXPLORATION_PATH, ExploreAdvisoryV1
from assurance_intake.domain.artifacts import authenticate_receipt, file_digest, leafs
from assurance_intake.ops.explore.hooks.context import build_explore_context
from assurance_intake.ops.explore.hooks.outputs import seal_official_exploration, validate_explore_outputs
from assurance_intake.ops.explore.models import (
    CONTEXT_PATH,
    EXPLORE_AGENT_OUTPUT_PATHS,
    EXPLORE_OFFICIAL_OUTPUT_PATHS,
    ExploreInputV1,
)


def before(ctx: PrepareContext, business: ExploreInputV1) -> ExploreInputV1:
    document = build_explore_context(
        ctx.project_root,
        change_id=business.change_id,
        capability_leafs=business.capability_leafs,
    )
    ctx.write(CONTEXT_PATH, canonical_json_bytes(document.model_dump(mode="json")) + b"\n")
    return business


def after(
    ctx: FinalizeContext, business: ExploreInputV1, result: ArtifactListResultV1
) -> FinalizedArtifactsV1:
    if not business.artifact_paths:
        raise InputError("artifact_paths must lock the expected output files")
    if set(result.output_files) != set(EXPLORE_AGENT_OUTPUT_PATHS):
        raise OutputError(
            "explore receipt must declare exactly exploration-draft.json and impact-inventory.json"
        )
    artifacts = authenticate_receipt(ctx.write_root, result, business.artifact_paths)
    validate_explore_outputs(
        ctx.write_root,
        result.output_files,
        change_id=business.change_id,
        capability_leafs=leafs(business.capability_leafs),
    )
    advisory = ExploreAdvisoryV1.model_validate_json(
        ctx.write_root.joinpath(*EXPLORE_AGENT_OUTPUT_PATHS[0].split("/")).read_bytes()
    )
    _, official_bytes = seal_official_exploration(
        ctx.write_root,
        advisory,
        candidate_families=frozenset(),
        policy_required=frozenset(),
    )
    artifacts = [item for item in artifacts if item["path"] != EXPLORE_AGENT_OUTPUT_PATHS[0]]
    artifacts.append({"path": EXPLORATION_PATH, "digest": file_digest(official_bytes)})
    if not set(EXPLORE_OFFICIAL_OUTPUT_PATHS) <= {item["path"] for item in artifacts}:
        raise OutputError("finalize must return official exploration and inventory refs")
    return FinalizedArtifactsV1.model_validate(
        {"artifacts": sorted(artifacts, key=lambda item: item["path"])}
    )
