from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest

from agent_runtime_contracts import AgentRunRequest
from graph_engine.canonical import JSONValue
from graph_engine.frozen_json import thaw_json
from assurance_generation.graphs.nodes import publish_plan, select_codegen, select_plan, select_plan_review
from assurance_generation.operations.codegen import codegen_prepare_handler
from assurance_generation.operations.planning import planning_handler
from assurance_generation.operations.review import review_prepare_handler
from tests.product.test_change_local_output_routing import execute_task
from planning_fixtures import (  # pyright: ignore[reportMissingImports]
    BINDING,
    FAMILIES,
    plan_input,
    family_plan_files,
    review_result,
    valid_plan_result,
)
from codegen_fixtures import mapping_document  # pyright: ignore[reportMissingImports]


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.asyncio
async def test_plan_handoff_preserves_reviewed_decisions_through_codegen(family: str, tmp_path: Path) -> None:
    business = plan_input(family)
    state = {**business, "family": family, "allowed_artifact_paths": business["artifact_paths"]}
    first = valid_plan_result(family)
    revised = valid_plan_result(family)
    revised["coverage"][0]["operation"] = "reviewed-operation"
    state.update(publish_plan(state, first, None))
    state.update(publish_plan(state, revised, None))

    review = select_plan_review(state)
    selected = select_codegen(state)
    assert review.reviewed_plan == revised
    assert selected.reviewed_plan == revised

    for relative in (
        *family_plan_files(family),
        "qa/changes/CH-DEMO-001/proposal.md",
        "qa/changes/CH-DEMO-001/cases/items/case.yaml",
    ):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("review input\n", encoding="utf-8")
    review = review.model_copy(update={"reviewed_cases": business["reviewed_cases"]})
    review_outcome = await execute_task(
        review_prepare_handler(family),
        review.model_dump(mode="json"),
        tmp_path,
        binding_data=BINDING,
    )
    assert review_outcome.status == "succeeded", review_outcome.failure
    review_request = AgentRunRequest.model_validate(review_outcome.output)
    assert {"reviewed_plan": revised} in [
        thaw_json(part.json_content) for part in review_request.instructions if part.json_content is not None
    ]

    mapping_path = tmp_path / f"qa/changes/CH-DEMO-001/plans/{family}-codegen-mapping.json"
    mapping_path.parent.mkdir(parents=True, exist_ok=True)
    mapping_path.write_text(json.dumps(mapping_document(family)), encoding="utf-8")
    selected = selected.model_copy(update={"reviewed_cases": business["reviewed_cases"]})
    outcome = await execute_task(
        codegen_prepare_handler(family),
        cast(dict[str, JSONValue], selected.model_dump(mode="json")),
        tmp_path,
        binding_data=BINDING,
    )
    assert outcome.status == "succeeded", outcome.failure
    request = AgentRunRequest.model_validate(outcome.output)
    assert thaw_json(request.instructions[2].json_content) == {
        **revised,
        "fuzz_strategy": revised.get("fuzz_strategy"),
        "performance_scenarios": revised.get("performance_scenarios", []),
        "case_execution_plan_ref": None,
        "case_execution_plan_digest": None,
    }
    context = thaw_json(request.instructions[4].json_content)
    assert isinstance(context, dict)
    assert context["reviewed_mapping"] == {
        **mapping_document(family),
        "schema_case_ids": None,
        "validation_profile": None,
        "coverage_epoch": None,
        "plan_digest": None,
        "plan_ref": None,
        "reviewed_case": None,
        "case_execution_plan_ref": None,
        "case_execution_plan_digest": None,
        "case_spec_digests": None,
    }


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.asyncio
async def test_plan_retry_handoff_preserves_previous_plan_but_not_across_epochs(
    family: str, tmp_path: Path
) -> None:
    business = plan_input(family)
    state = {**business, "family": family, "allowed_artifact_paths": business["artifact_paths"]}
    previous = valid_plan_result(family)
    previous["coverage"][0]["operation"] = "reviewed-operation"
    state.update(publish_plan(state, previous, None))
    state["rounds_used"] = 1
    selected = select_plan(state)
    assert selected.reviewed_plan == previous

    review = review_result(family)
    review.update(
        decision="needs_human_review",
        human_review_required=True,
        codegen_readiness="not_ready",
        next_action="human requested rework",
    )
    review_path = tmp_path / f"qa/changes/CH-DEMO-001/review/{family}-plan-review.json"
    review_path.parent.mkdir(parents=True)
    review_path.write_text(json.dumps(review), encoding="utf-8")
    selected = selected.model_copy(update={"reviewed_cases": business["reviewed_cases"]})
    outcome = await execute_task(
        planning_handler(family, "prepare"),
        selected.model_dump(mode="json"),
        tmp_path,
        binding_data=BINDING,
    )
    assert outcome.status == "succeeded", outcome.failure
    request = AgentRunRequest.model_validate(outcome.output)
    instructions = [thaw_json(part.json_content) for part in request.instructions]
    assert {"reviewed_plan": previous} in instructions
    assert any(isinstance(part, dict) and "plan_repair_review" in part for part in instructions)

    # A fresh coverage epoch must plan from its new Case version, not a previous lane's plan.
    state.update(coverage_epoch=1, rounds_used=0)
    assert select_plan(state).reviewed_plan is None


@pytest.mark.asyncio
async def test_verified_first_plan_publishes_only_machine_identity_to_review_and_codegen(
    tmp_path: Path,
) -> None:
    from assurance_generation.operations.planning import PlanFinalizeHandler
    from test_execution_plan import _materialize_context, _write_api_candidate, _finalize_input  # pyright: ignore[reportMissingImports]

    context = _materialize_context(tmp_path)
    paths, result = _write_api_candidate(tmp_path)
    payload = _finalize_input(result, project=tmp_path, context=context, artifact_paths=paths)
    payload.pop("assertion_sources")
    outcome = await execute_task(PlanFinalizeHandler("api"), payload, tmp_path, write_root=tmp_path)
    assert outcome.status == "succeeded", outcome.failure
    state = {
        key: payload[key]
        for key in (
            "change_id",
            "plan_ref",
            "plan_digest",
            "reviewed_case",
            "coverage_epoch",
            "validation_profile",
            "capability_leafs",
        )
    }
    state.update({"family": "api", "allowed_artifact_paths": list(paths)})
    published = publish_plan(state, outcome.output, None)
    assert "case_plan_context" not in published
    state.update(published)
    review, codegen = select_plan_review(state), select_codegen(state)
    assert review.case_execution_plan_ref == codegen.case_execution_plan_ref
    assert review.case_execution_plan_digest == codegen.case_execution_plan_digest
    assert review.plan_digest == context.plan_digest
    assert codegen.plan_digest == context.plan_digest
