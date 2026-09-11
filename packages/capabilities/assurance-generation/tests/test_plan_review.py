from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest

from agent_runtime_contracts import AgentRunRequest
from tests.product.test_change_local_output_routing import execute_task

from assurance_generation.operations.review import review_finalize_handler, review_prepare_handler
from assurance_generation.resource_loader import resource_text
from planning_fixtures import (  # pyright: ignore[reportMissingImports]
    BINDING,
    FAMILIES,
    family_plan_files,
    fake_agent_result,
    plan_input,
    review_result,
    valid_plan_result,
)


@pytest.mark.parametrize(
    "skill_id",
    (
        "aa-api-plan-reviewer",
        "aa-e2e-plan-reviewer",
        "aa-fuzz-plan-reviewer",
        "aa-performance-plan-reviewer",
    ),
)
def test_plan_reviewer_skills_do_not_instruct_removed_decisions(skill_id: str) -> None:
    skill = resource_text(f"skills/{skill_id}/SKILL.md")
    assert '"approved"' not in skill
    assert "changes_requested" not in skill


def test_e2e_reviewer_skill_outputs_use_family_prefixed_names() -> None:
    skill = resource_text("skills/aa-e2e-plan-reviewer/SKILL.md")
    assert "qa/results/review/e2e-plan-review.json" in skill
    assert "qa/results/review/e2e-plan-review-summary.md" in skill
    assert "qa/results/review/plan-review.json" not in skill
    assert "qa/results/review/plan-review-summary.md" not in skill


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.asyncio
async def test_plan_review_finalize_rejects_unknown_leaf(family: str, tmp_path: Path) -> None:
    result = fake_agent_result(review_result(family, leaf="auth.fake"))
    outcome = await execute_task(review_finalize_handler(family), result, tmp_path)
    assert outcome.status == "failed"
    assert outcome.failure is not None and outcome.failure.kind == "invalid_output"


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.asyncio
async def test_plan_review_finalize_rejects_prefix_leaf(family: str, tmp_path: Path) -> None:
    result = fake_agent_result(review_result(family, leaf="entities.item"))
    outcome = await execute_task(review_finalize_handler(family), result, tmp_path)
    assert outcome.status == "failed"
    assert outcome.failure is not None
    assert outcome.failure.kind == "invalid_output"
    assert outcome.failure.retryable is True


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.asyncio
async def test_plan_review_finalize_accepts_typed_review(family: str, tmp_path: Path) -> None:
    executed = await execute_task(
        review_finalize_handler(family),
        fake_agent_result(review_result(family)),
        tmp_path,
    )
    assert executed.status == "succeeded"
    output = cast(dict[str, object], executed.output)
    assert output["review_type"] == f"{family}-plan"
    assert output["decision"] == "pass"
    assert output["required_capabilities"] == ["entities.item.create"]
    assert "rounds_used" not in output
    assert "rounds_budget" not in output


@pytest.mark.parametrize("fix_ids", (["F1"], ["F1", "F1", "F2"]))
@pytest.mark.asyncio
async def test_plan_review_requires_complete_unique_repair_set(fix_ids: list[str], tmp_path: Path) -> None:
    review = review_result("api")
    review.update(
        {
            "decision": "needs_fix",
            "auto_fix_allowed": True,
            "codegen_readiness": "not_ready",
            "findings": [
                {
                    "id": name,
                    "severity": "medium",
                    "category": "consistency",
                    "message": "Repair this affected section",
                    "locator": {"artifact": f"qa/results/plans/{file}", "key": "Factory Mapping"},
                }
                for name, file in (("F1", "api-codegen-plan.md"), ("F2", "api-test-data-plan.md"))
            ],
            "auto_fix_plan": fix_ids,
        }
    )
    outcome = await execute_task(review_finalize_handler("api"), fake_agent_result(review), tmp_path)
    assert outcome.status == "failed"
    assert outcome.failure is not None
    assert "every finding exactly once" in outcome.failure.message


@pytest.mark.asyncio
async def test_plan_review_finalize_persists_epoch_scoped_history(tmp_path: Path) -> None:
    family = "api"
    cases_root = tmp_path / "qa/cases/items"
    cases_root.mkdir(parents=True)
    (cases_root / "case.yaml").write_text(
        "schema_version: '1.0'\nadded: []\nmodified: []\nremoved: []\n",
        encoding="utf-8",
    )
    (tmp_path / "qa").mkdir(parents=True, exist_ok=True)
    (tmp_path / "qa/proposal.md").write_text("# Proposal\n", encoding="utf-8")
    for relative in family_plan_files(family):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("locked plan input\n", encoding="utf-8")
    review = review_result(family)
    latest = tmp_path / "qa/results/review/api-plan-review.json"
    latest.parent.mkdir(parents=True)
    latest.write_text(json.dumps(review), encoding="utf-8")
    envelope = fake_agent_result(review)
    stage = tmp_path / ".stage"
    staged_review = stage / latest.relative_to(tmp_path)
    staged_review.parent.mkdir(parents=True)
    staged_review.write_bytes(latest.read_bytes())

    executed = await execute_task(
        review_finalize_handler(family),
        {
            **envelope,
            "change_id": "CH-DEMO-001",
            "coverage_epoch": 2,
            "local_round": 1,
        },
        tmp_path,
        write_root=stage,
    )

    assert executed.status == "succeeded", executed.failure
    history_path = stage / "qa/results/plan/api/reviews/epochs/2/rounds/1.json"
    history = json.loads(history_path.read_bytes())
    assert history["loop_kind"] == "plan_review"
    assert history["family"] == "api"
    assert history["coverage_epoch"] == 2
    assert history["round_index"] == 1
    output = cast(dict[str, object], executed.output)
    history_ref = cast(dict[str, object], output["history_ref"])
    assert history_ref["path"] == history_path.relative_to(stage).as_posix()


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.asyncio
async def test_plan_review_finalize_rejects_wrong_family(family: str, tmp_path: Path) -> None:
    other = "e2e" if family == "api" else "api"
    payload = review_result(family)
    payload["review_type"] = f"{other}-plan"
    executed = await execute_task(
        review_finalize_handler(family),
        fake_agent_result(payload),
        tmp_path,
    )
    assert executed.status == "failed"
    assert executed.failure is not None
    assert executed.failure.kind == "invalid_output"


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.asyncio
async def test_plan_review_prepare_uses_reviewer_persona(family: str, tmp_path: Path) -> None:
    proposal_path = tmp_path / "qa/proposal.md"
    proposal_path.parent.mkdir(parents=True, exist_ok=True)
    proposal_path.write_text("# Proposal\n", encoding="utf-8")
    for relative in family_plan_files(family):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("locked plan input\n", encoding="utf-8")
    case_path = tmp_path / "qa/cases/items/case.yaml"
    case_path.parent.mkdir(parents=True, exist_ok=True)
    case_path.write_text("schema_version: '1.0'\nadded: []\nmodified: []\nremoved: []\n", encoding="utf-8")

    prepared = await execute_task(
        review_prepare_handler(family),
        {**plan_input(family), "reviewed_plan": valid_plan_result(family)},
        tmp_path,
        binding_data=BINDING,
    )
    assert prepared.status == "succeeded"
    request = AgentRunRequest.model_validate(prepared.output)
    assert len(request.instructions) == 6
    skill, persona, reviewed, constraints, locked_inputs, _typed_plan = request.instructions
    assert f"{family} plan review" in (skill.text_content or "").lower()
    assert "reviewer persona" in (persona.text_content or "").lower()
    assert reviewed.media_type == "application/json"
    assert constraints.media_type == "application/json"
    facts = cast(dict[str, object], constraints.json_content)["planning_facts"]
    assert cast(dict[str, object], facts)["capability_leafs"] == (
        "auth.session.create",
        "entities.item.create",
    )
    assert locked_inputs.json_content == {
        "review_input_paths": tuple(
            sorted(
                (
                    *family_plan_files(family),
                    "qa/cases/items/case.yaml",
                    "qa/proposal.md",
                )
            )
        )
    }
    schema = request.result_contract.schema_document
    assert schema is not None
    thawed_schema = cast(dict[str, object], schema)
    properties = cast(dict[str, object], thawed_schema["properties"])
    required_capabilities = cast(dict[str, object], properties["required_capabilities"])
    items = cast(dict[str, object], required_capabilities["items"])
    assert items["enum"] == ("auth.session.create", "entities.item.create")


@pytest.mark.asyncio
async def test_plan_review_prepare_fails_closed_when_locked_plan_input_is_missing(tmp_path: Path) -> None:
    proposal_path = tmp_path / "qa/proposal.md"
    proposal_path.parent.mkdir(parents=True, exist_ok=True)
    proposal_path.write_text("# Proposal\n", encoding="utf-8")
    case_path = tmp_path / "qa/cases/items/case.yaml"
    case_path.parent.mkdir(parents=True, exist_ok=True)
    case_path.write_text("schema_version: '1.0'\nadded: []\nmodified: []\nremoved: []\n", encoding="utf-8")

    prepared = await execute_task(
        review_prepare_handler("api"),
        {**plan_input("api"), "reviewed_plan": valid_plan_result("api")},
        tmp_path,
        binding_data=BINDING,
    )

    assert prepared.status == "failed"
    assert prepared.failure is not None
    assert prepared.failure.kind == "invalid_input"
    assert "plan input is not a regular single-link file" in prepared.failure.message
    assert "qa/results/plans/api-" in prepared.failure.message


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.asyncio
async def test_plan_review_prepare_requires_the_published_typed_plan(family: str, tmp_path: Path) -> None:
    outcome = await execute_task(
        review_prepare_handler(family), plan_input(family), tmp_path, binding_data=BINDING
    )
    assert outcome.status == "failed"
    assert outcome.failure is not None
    assert outcome.failure.kind == "invalid_input"
    assert "reviewed_plan is required" in outcome.failure.message


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.asyncio
async def test_plan_review_finalize_rejects_malformed_input(family: str, tmp_path: Path) -> None:
    executed = await execute_task(review_finalize_handler(family), {"agent_result": {}}, tmp_path)
    assert executed.status == "failed"
    assert executed.failure is not None
    assert executed.failure.kind == "invalid_input"
    assert executed.failure.retryable is False
