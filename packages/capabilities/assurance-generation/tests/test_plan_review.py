from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest

from agent_runtime_contracts import AgentRunRequest
from graph_engine.frozen_json import thaw_json
from tests.product.test_change_local_output_routing import execute_task

from assurance_generation.operations.review import review_finalize_handler, review_prepare_handler
from assurance_generation.resource_loader import resource_text
from review_audit_fixtures import (  # pyright: ignore[reportMissingImports]
    PLAN_PATH,
    review_prepare_input,
    write_codegen_artifacts,
    write_review,
)
from planning_fixtures import (  # pyright: ignore[reportMissingImports]
    BINDING,
    FAMILIES,
    fake_agent_result,
    plan_input,
    review_result,
    reviewed_cases,
    valid_plan_review,
)


def test_review_audit_modules_are_gone() -> None:
    import importlib

    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("assurance_generation.operations.review_audit")
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("assurance_generation.contracts.review_audit")


def test_plan_review_schema_has_no_review_audit() -> None:
    from assurance_generation.resource_loader import resource_bytes

    for relative in (
        "result-contracts/plan-review.v1.schema.json",
        "schemas/plan-review.v1.schema.json",
    ):
        schema = json.loads(resource_bytes(relative))
        assert "review_audit" not in schema["properties"]
        assert "review_audit" not in schema["required"]
        defs = schema.get("$defs", {})
        for name in (
            "PlanReviewAudit",
            "CaseReviewChecks",
            "CaseReviewCoverage",
            "HelperReviewEvidence",
            "HelperPlanLocation",
        ):
            assert name not in defs


def _write_review_workspace(tmp_path: Path, family: str = "api") -> None:
    proposal_path = tmp_path / "qa/proposal.md"
    proposal_path.parent.mkdir(parents=True, exist_ok=True)
    proposal_path.write_text("# Proposal\n", encoding="utf-8")
    write_codegen_artifacts(tmp_path, family)
    case_path = tmp_path / "qa/cases/items/case.yaml"
    case_path.parent.mkdir(parents=True, exist_ok=True)
    case_path.write_text(json.dumps(reviewed_cases(family)), encoding="utf-8")


@pytest.mark.asyncio
async def test_api_review_finalizes_without_review_audit(tmp_path: Path) -> None:
    outcome = await execute_task(
        review_finalize_handler("api"),
        fake_agent_result(valid_plan_review()),
        tmp_path,
    )
    assert outcome.status == "succeeded", outcome.failure
    output = cast(dict[str, object], outcome.output)
    assert output["route"] == "codegen"
    assert "review_audit" not in output


@pytest.mark.asyncio
async def test_api_review_finalize_strips_leftover_review_audit(tmp_path: Path) -> None:
    leftover = {
        **valid_plan_review(),
        "review_audit": {
            "input_refs": [],
            "planning_facts_digest": "0" * 64,
            "cases": [],
            "helpers": [],
        },
    }
    outcome = await execute_task(
        review_finalize_handler("api"),
        fake_agent_result(leftover),
        tmp_path,
    )
    assert outcome.status == "succeeded", outcome.failure
    output = cast(dict[str, object], outcome.output)
    assert "review_audit" not in output


@pytest.mark.parametrize(
    "skill_id",
    (
        "aa-api-codegen-reviewer",
        "aa-e2e-codegen-reviewer",
        "aa-fuzz-codegen-reviewer",
        "aa-performance-codegen-reviewer",
    ),
)
def test_plan_reviewer_skills_do_not_instruct_removed_decisions(skill_id: str) -> None:
    skill = resource_text(f"skills/{skill_id}/SKILL.md")
    assert '"approved"' not in skill
    assert "changes_requested" not in skill


@pytest.mark.parametrize(
    "skill_id",
    (
        "aa-api-codegen-reviewer",
        "aa-e2e-codegen-reviewer",
        "aa-fuzz-codegen-reviewer",
        "aa-performance-codegen-reviewer",
    ),
)
def test_codegen_reviewer_skills_do_not_require_review_audit(skill_id: str) -> None:
    skill = resource_text(f"skills/{skill_id}/SKILL.md")
    assert "review_audit" not in skill
    assert "review_requirements" not in skill
    assert "plan_location" not in skill
    assert "edit plan files" not in skill


def test_e2e_reviewer_skill_outputs_use_family_prefixed_names() -> None:
    skill = resource_text("skills/aa-e2e-codegen-reviewer/SKILL.md")
    assert "qa/results/review/e2e-codegen-review.json" in skill
    assert "qa/results/review/e2e-codegen-review-summary.md" in skill
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
    review = valid_plan_review() if family == "api" else {**valid_plan_review(), "review_type": f"{family}-codegen"}
    executed = await execute_task(
        review_finalize_handler(family),
        fake_agent_result(review),
        tmp_path,
    )
    assert executed.status == "succeeded"
    output = cast(dict[str, object], executed.output)
    assert output["review_type"] == f"{family}-codegen"
    assert output["route"] == "codegen"
    assert output["required_capabilities"] == ["entities.item.create"]
    assert "rounds_used" not in output
    assert "rounds_budget" not in output


@pytest.mark.parametrize(
    ("fix_ids", "expected"),
    (
        (["F1"], "finding_ids must include every finding exactly once"),
        (["F1", "F1", "F2"], "finding_ids must be unique"),
    ),
)
@pytest.mark.asyncio
async def test_plan_review_requires_complete_unique_repair_set(
    fix_ids: list[str], expected: str, tmp_path: Path
) -> None:
    review = {
        **valid_plan_review(),
        "route": "auto_fix",
        "findings": [
            {
                "id": name,
                "severity": "medium",
                "category": "consistency",
                "message": "Repair this affected section",
                "locator": {"artifact": artifact, "key": "Factory Mapping"},
            }
            for name, artifact in (
                ("F1", "qa/results/codegen/api-codegen-summary.md"),
                ("F2", "qa/tests/api/test_users.py"),
            )
        ],
        "finding_ids": fix_ids,
    }
    outcome = await execute_task(review_finalize_handler("api"), fake_agent_result(review), tmp_path)
    assert outcome.status == "failed"
    assert outcome.failure is not None
    assert outcome.failure.kind == "invalid_output"
    assert outcome.failure.retryable is True
    assert expected in outcome.failure.message


@pytest.mark.asyncio
async def test_plan_review_finalize_persists_epoch_scoped_history(tmp_path: Path) -> None:
    family = "api"
    review = valid_plan_review()
    _write_review_workspace(tmp_path)
    write_review(tmp_path, review)
    latest = tmp_path / "qa/results/review/api-codegen-review.json"
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
    history_path = stage / "qa/results/codegen/api/reviews/epochs/2/rounds/1.json"
    history = json.loads(history_path.read_bytes())
    assert history["loop_kind"] == "plan_review"
    assert history["family"] == "api"
    assert history["coverage_epoch"] == 2
    assert history["round_index"] == 1
    output = cast(dict[str, object], executed.output)
    history_ref = cast(dict[str, object], output["history_ref"])
    assert history_ref["path"] == history_path.relative_to(stage).as_posix()


def _runner_finding() -> dict[str, object]:
    return {
        "id": "API-PLAN-001",
        "severity": "high",
        "category": "test-runner",
        "message": "Markers: none required contradicts asyncio_mode = strict",
        "locator": {"artifact": PLAN_PATH, "key": "7. Run Guidance"},
    }


def _semantic_finding(finding_id: str = "API-PLAN-002") -> dict[str, object]:
    return {
        "id": finding_id,
        "severity": "medium",
        "category": "coverage",
        "message": "TC_DEPT_006 must construct the empty-string name",
        "locator": {
            "artifact": PLAN_PATH,
            "case_id": "TC_DEPT_006",
            "key": "4. Request Strategy",
        },
    }


def _as_auto_fix(review: dict[str, object], *findings: dict[str, object]) -> dict[str, object]:
    updated = dict(review)
    updated.update(
        {
            "route": "auto_fix",
            "findings": list(findings),
            "finding_ids": [str(item["id"]) for item in findings],
            "next_action": "run api planner",
        }
    )
    return updated


@pytest.mark.asyncio
async def test_plan_review_finalize_strips_runner_contract_findings(tmp_path: Path) -> None:
    review = valid_plan_review()
    _write_review_workspace(tmp_path)
    write_review(tmp_path, review)
    review = _as_auto_fix(review, _runner_finding())
    write_review(tmp_path, review)
    executed = await execute_task(
        review_finalize_handler("api"),
        {**fake_agent_result(review), "change_id": "CH-DEMO-001", "coverage_epoch": 0},
        tmp_path,
    )
    assert executed.status == "succeeded", executed.failure
    output = cast(dict[str, object], executed.output)
    assert output["route"] == "auto_fix"
    assert [item["id"] for item in cast(list[dict[str, object]], output["findings"])] == ["API-PLAN-001"]


@pytest.mark.asyncio
async def test_plan_review_finalize_retry_drops_new_finding_ids(tmp_path: Path) -> None:
    review = valid_plan_review()
    _write_review_workspace(tmp_path)
    write_review(tmp_path, review)
    first = _as_auto_fix(review, _semantic_finding())
    write_review(tmp_path, first)
    first_pass = await execute_task(
        review_finalize_handler("api"),
        {**fake_agent_result(first), "change_id": "CH-DEMO-001", "coverage_epoch": 1},
        tmp_path,
    )
    assert first_pass.status == "succeeded", first_pass.failure
    assert cast(dict[str, object], first_pass.output)["route"] == "auto_fix"

    second = _as_auto_fix(review, _semantic_finding(), _semantic_finding("API-PLAN-004"))
    write_review(tmp_path, second)
    retried = await execute_task(
        review_finalize_handler("api"),
        {**fake_agent_result(second), "change_id": "CH-DEMO-001", "coverage_epoch": 1},
        tmp_path,
    )
    assert retried.status == "succeeded", retried.failure
    output = cast(dict[str, object], retried.output)
    assert output["route"] == "auto_fix"
    assert [item["id"] for item in cast(list[dict[str, object]], output["findings"])] == [
        "API-PLAN-002",
        "API-PLAN-004",
    ]


@pytest.mark.asyncio
async def test_plan_review_finalize_keeps_a_prior_pass(tmp_path: Path) -> None:
    review = valid_plan_review()
    _write_review_workspace(tmp_path)
    write_review(tmp_path, review)
    passed = await execute_task(
        review_finalize_handler("api"),
        {**fake_agent_result(review), "change_id": "CH-DEMO-001", "coverage_epoch": 4},
        tmp_path,
    )
    assert passed.status == "succeeded", passed.failure
    assert cast(dict[str, object], passed.output)["route"] == "codegen"

    later = _as_auto_fix(review, _semantic_finding())
    write_review(tmp_path, later)
    retried = await execute_task(
        review_finalize_handler("api"),
        {**fake_agent_result(later), "change_id": "CH-DEMO-001", "coverage_epoch": 4},
        tmp_path,
    )
    assert retried.status == "succeeded", retried.failure
    retried_output = cast(dict[str, object], retried.output)
    assert retried_output["route"] == "auto_fix"
    findings = cast(list[dict[str, object]], retried_output["findings"])
    assert [item["id"] for item in findings] == ["API-PLAN-002"]


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.asyncio
async def test_plan_review_finalize_rejects_wrong_family(family: str, tmp_path: Path) -> None:
    other = "e2e" if family == "api" else "api"
    payload = review_result(family)
    payload["review_type"] = f"{other}-codegen"
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
    write_codegen_artifacts(tmp_path, family)
    case_path = tmp_path / "qa/cases/items/case.yaml"
    case_path.parent.mkdir(parents=True, exist_ok=True)
    case_path.write_text(json.dumps(reviewed_cases(family)), encoding="utf-8")

    prepared = await execute_task(
        review_prepare_handler(family),
        review_prepare_input(family, plan_input(family)),
        tmp_path,
        binding_data=BINDING,
    )
    assert prepared.status == "succeeded", prepared.failure
    request = AgentRunRequest.model_validate(prepared.output)
    assert len(request.instructions) == 6
    skill, persona, reviewed, constraints, locked_inputs, extra = request.instructions
    assert f"{family} codegen review" in (skill.text_content or "").lower()
    assert "reviewer persona" in (persona.text_content or "").lower()
    assert reviewed.media_type == "application/json"
    assert constraints.media_type == "application/json"
    facts = cast(dict[str, object], constraints.json_content)["planning_facts"]
    assert cast(dict[str, object], facts)["capability_leafs"] == (
        "auth.session.create",
        "entities.item.create",
    )
    assert cast(dict, locked_inputs.json_content)["review_input_paths"] == tuple(
        sorted(
            (
                f"qa/results/codegen/{family}-codegen-summary.md",
                f"qa/results/codegen/{family}-generated-files.json",
                "qa/cases/items/case.yaml",
                "qa/proposal.md",
            )
        )
    )
    extra_payload = cast(dict[str, object], extra.json_content)
    assert "codegen_output" in extra_payload
    assert "codegen_scope" in extra_payload
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
    case_path.write_text(json.dumps(reviewed_cases("api")), encoding="utf-8")

    prepared = await execute_task(
        review_prepare_handler("api"),
        review_prepare_input("api"),
        tmp_path,
        binding_data=BINDING,
    )

    assert prepared.status == "failed"
    assert prepared.failure is not None
    assert prepared.failure.kind == "invalid_input"
    assert "plan input is not a regular single-link file" in prepared.failure.message
    assert "qa/results/codegen/api-" in prepared.failure.message


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.asyncio
async def test_plan_review_prepare_requires_codegen_output(family: str, tmp_path: Path) -> None:
    case_path = tmp_path / "qa/cases/items/case.yaml"
    case_path.parent.mkdir(parents=True, exist_ok=True)
    case_path.write_text(json.dumps(reviewed_cases(family)), encoding="utf-8")
    outcome = await execute_task(
        review_prepare_handler(family), plan_input(family), tmp_path, binding_data=BINDING
    )
    assert outcome.status == "failed"
    assert outcome.failure is not None
    assert outcome.failure.kind == "invalid_input"
    assert "codegen_output is required" in outcome.failure.message


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.asyncio
async def test_plan_review_finalize_rejects_malformed_input(family: str, tmp_path: Path) -> None:
    executed = await execute_task(review_finalize_handler(family), {"agent_result": {}}, tmp_path)
    assert executed.status == "failed"
    assert executed.failure is not None
    assert executed.failure.kind == "invalid_input"
    assert executed.failure.retryable is True


@pytest.mark.asyncio
async def test_api_review_prepare_does_not_require_review_audit(tmp_path: Path) -> None:
    proposal_path = tmp_path / "qa/proposal.md"
    proposal_path.parent.mkdir(parents=True, exist_ok=True)
    proposal_path.write_text("# Proposal\n", encoding="utf-8")
    write_codegen_artifacts(tmp_path, "api")
    case_path = tmp_path / "qa/cases/items/case.yaml"
    case_path.parent.mkdir(parents=True, exist_ok=True)
    case_path.write_text(json.dumps(reviewed_cases("api")), encoding="utf-8")

    prepared = await execute_task(
        review_prepare_handler("api"),
        review_prepare_input("api", plan_input("api")),
        tmp_path,
        binding_data=BINDING,
    )
    assert prepared.status == "succeeded", prepared.failure
    request = AgentRunRequest.model_validate(prepared.output)
    schema = cast(dict[str, object], request.result_contract.schema_document)
    assert "review_audit" not in cast(list[object], schema["required"])
    parts = [thaw_json(part.json_content) for part in request.instructions if part.json_content is not None]
    assert all(
        not (isinstance(part, dict) and "review_requirements" in part) for part in parts
    )
