from __future__ import annotations

from pathlib import Path

import pytest

from assurance_generation.operations.review import review_finalize_handler, review_prepare_handler
from assurance_generation.operations.planning import planning_handler
from graph_engine.frozen_json import thaw_json
from tests.product.test_change_local_output_routing import execute_task
from planning_fixtures import (  # pyright: ignore[reportMissingImports]
    BINDING,
    fake_agent_result,
    family_plan_files,
    valid_plan_result,
)
from review_audit_fixtures import (  # pyright: ignore[reportMissingImports]
    HELPER_PATH,
    PLAN_PATH,
    audited_review,
    write,
    write_review,
)


async def finish(root: Path, review: dict, business: dict, *, local_round: int = 0):
    return await execute_task(
        review_finalize_handler("api"),
        {
            **fake_agent_result(review, capability_leafs=tuple(business["capability_leafs"])),
            "local_round": local_round,
        },
        root,
    )


@pytest.mark.parametrize("round_index", [0, 1, 3])
@pytest.mark.asyncio
async def test_review_coverage_is_required_in_first_and_followup_rounds(
    tmp_path: Path, round_index: int
) -> None:
    review, business, _ = await audited_review(tmp_path)
    review["review_audit"]["cases"] = []
    write_review(tmp_path, review)
    result = await finish(tmp_path, review, business, local_round=round_index)
    assert result.status == "failed"
    assert result.failure and "every selected case exactly once" in result.failure.message


@pytest.mark.parametrize("area", ["request", "auth", "setup", "assertion", "cleanup", "helpers"])
@pytest.mark.asyncio
async def test_a_review_cannot_omit_a_check_area(tmp_path: Path, area: str) -> None:
    review, business, _ = await audited_review(tmp_path)
    del review["review_audit"]["cases"][0]["checks"][area]
    result = await finish(tmp_path, review, business)
    assert result.status == "failed"
    assert result.failure and "Field required" in result.failure.message


@pytest.mark.parametrize(
    "mutation,expected",
    [
        ("duplicate_case", "every selected case exactly once"),
        ("missing_helper", "every prepared helper exactly once"),
        ("wrong_kind", "declared_kind contradicts source facts"),
        ("wrong_signature", "observed_signature contradicts source facts"),
        ("wrong_async", "observed_async contradicts source facts"),
        ("fake_evidence", "locked inputs or observed source"),
        ("stale_digest", "planning_facts_digest"),
        ("missing_input", "every locked input and digest"),
        ("unlinked_finding", "case finding checks must link"),
        ("unknown_finding", "unknown or duplicate finding IDs"),
        ("unresolved_helper", "unresolved helper must link"),
    ],
)
@pytest.mark.asyncio
async def test_audit_rejects_incomplete_or_invented_evidence(
    tmp_path: Path, mutation: str, expected: str
) -> None:
    review, business, _ = await audited_review(tmp_path, helper=True)
    audit = review["review_audit"]
    case = audit["cases"][0]
    helper = audit["helpers"][0]
    if mutation == "duplicate_case":
        audit["cases"].append(dict(case))
    elif mutation == "missing_helper":
        audit["helpers"] = []
    elif mutation == "wrong_kind":
        helper["declared_kind"] = "async_factory"
    elif mutation == "wrong_signature":
        helper["observed_signature"] = "rows(dept_id)"
    elif mutation == "wrong_async":
        helper["observed_async"] = True
    elif mutation == "fake_evidence":
        case["evidence_paths"] = ["../../private-source.py"]
    elif mutation == "stale_digest":
        audit["planning_facts_digest"] = "0" * 64
    elif mutation == "missing_input":
        audit["input_refs"].pop()
    elif mutation == "unlinked_finding":
        case["checks"]["setup"] = "finding"
    elif mutation == "unknown_finding":
        case["finding_ids"] = ["NOT-REPORTED"]
    elif mutation == "unresolved_helper":
        helper["implementation"] = "unresolved"
    write_review(tmp_path, review)
    result = await finish(tmp_path, review, business)
    assert result.status == "failed"
    assert result.failure and expected in result.failure.message


@pytest.mark.asyncio
async def test_missing_generated_target_is_not_satisfied_by_an_import(tmp_path: Path) -> None:
    review, business, _ = await audited_review(tmp_path, helper=True, generated_target=False)
    result = await finish(tmp_path, review, business)
    assert result.failure and "target is absent" in result.failure.message
    review["review_audit"]["helpers"][0]["plan_location"]["section"] = "Import Strategy"
    result = await finish(tmp_path, review, business)
    assert result.failure and "not an import" in result.failure.message


@pytest.mark.asyncio
async def test_stub_is_planned_work_not_an_existing_implementation(tmp_path: Path) -> None:
    review, business, _ = await audited_review(
        tmp_path, helper=True, source="def rows():\n    raise NotImplementedError\n"
    )
    accepted = await finish(tmp_path, review, business)
    assert accepted.status == "succeeded", accepted.failure
    review["review_audit"]["helpers"][0]["implementation"] = "existing"
    result = await finish(tmp_path, review, business)
    assert result.failure and "stub is executable" in result.failure.message


@pytest.mark.asyncio
async def test_helper_kind_does_not_falsely_force_sync_invocation(tmp_path: Path) -> None:
    review, business, request = await audited_review(
        tmp_path, helper=True, source="async def rows():\n    return []\n"
    )
    helper = review["review_audit"]["helpers"][0]
    assert helper["declared_kind"] == "helper" and helper["observed_async"] is True
    result = await finish(tmp_path, review, business)
    assert result.status == "succeeded", result.failure
    schema = thaw_json(request.result_contract.schema_document)
    assert "review_audit" in schema["required"]
    assert schema["properties"]["review_audit"] == {"$ref": "#/$defs/PlanReviewAudit"}


@pytest.mark.parametrize(
    "reference,expected_count",
    [
        ("from tests.testdata.domain.item import rows", 1),
        ("qa/tests/testdata/domain/item.py", 1),
        ("tests.testdata.domain.item_extra.rows", 0),
        ("No helper is consumed by this plan.", 0),
    ],
)
@pytest.mark.asyncio
async def test_prepare_covers_module_only_references_without_inventing_unrelated_helpers(
    tmp_path: Path, reference: str, expected_count: int
) -> None:
    _, business, _ = await audited_review(tmp_path, helper=True)
    write(tmp_path, PLAN_PATH, "# Codegen\n\n## Import Strategy\n" + reference + "\n")
    result = await execute_task(
        review_prepare_handler("api"),
        {**business, "reviewed_plan": valid_plan_result("api")},
        tmp_path,
        binding_data=BINDING,
    )
    assert result.status == "succeeded", result.failure
    from agent_runtime_contracts import AgentRunRequest

    request = AgentRunRequest.model_validate(result.output)
    parts = [thaw_json(part.json_content) for part in request.instructions]
    requirements = next(
        part["review_requirements"]
        for part in parts
        if isinstance(part, dict) and "review_requirements" in part
    )
    assert len(requirements["helpers"]) == expected_count
    if expected_count:
        assert requirements["helpers"][0]["target_file"] == HELPER_PATH


@pytest.mark.parametrize("path", [PLAN_PATH, HELPER_PATH])
@pytest.mark.asyncio
async def test_changed_plan_or_source_invalidates_the_audit(tmp_path: Path, path: str) -> None:
    review, business, _ = await audited_review(tmp_path, helper=True, source="def rows():\n    return []\n")
    write(tmp_path, path, (tmp_path / path).read_text() + "\n# changed\n")
    result = await finish(tmp_path, review, business)
    assert result.status == "failed"
    assert result.failure and (
        "input_refs" in result.failure.message or "planning_facts_digest" in result.failure.message
    )


@pytest.mark.asyncio
async def test_artifact_must_carry_the_same_audit_as_the_final_json(tmp_path: Path) -> None:
    review, business, _ = await audited_review(tmp_path)
    write_review(tmp_path, {**review, "review_audit": None})
    result = await finish(tmp_path, review, business)
    assert result.failure and "artifact must match" in result.failure.message


@pytest.mark.asyncio
async def test_a_complete_needs_fix_review_can_report_a_missing_helper(tmp_path: Path) -> None:
    review, business, _ = await audited_review(tmp_path, helper=True, generated_target=False)
    review.update(
        decision="needs_fix",
        auto_fix_allowed=True,
        codegen_readiness="not_ready",
        auto_fix_plan=["F1"],
        findings=[
            {
                "id": "F1",
                "severity": "high",
                "category": "helper",
                "message": "Declare the helper target and reconcile its related imports.",
                "locator": {"artifact": PLAN_PATH, "key": "Target Files"},
            }
        ],
    )
    helper = review["review_audit"]["helpers"][0]
    helper.update(implementation="unresolved", invocation="unknown", plan_location=None, finding_ids=["F1"])
    case = review["review_audit"]["cases"][0]
    case["checks"]["helpers"] = "finding"
    case["finding_ids"] = ["F1"]
    write_review(tmp_path, review)
    result = await finish(tmp_path, review, business)
    assert result.status == "succeeded", result.failure

    # The same finding can reconcile targets, imports and summaries within the
    # existing plan package without authorizing tests, cases or SUT writes.
    repaired = await execute_task(
        planning_handler("api", "prepare"),
        {**business, "local_round": 1, "reviewed_plan": valid_plan_result("api")},
        tmp_path,
        binding_data=BINDING,
    )
    assert repaired.status == "succeeded", repaired.failure
    from agent_runtime_contracts import AgentRunRequest

    request = AgentRunRequest.model_validate(repaired.output)
    parts = [thaw_json(part.json_content) for part in request.instructions]
    scope = next(
        part["plan_repair_scope"] for part in parts if isinstance(part, dict) and "plan_repair_scope" in part
    )
    assert set(scope["allowed_artifacts"]) == set(family_plan_files("api"))
    assert scope["finding_ids"] == ["F1"]
    assert scope["related_consistency_edits"] is True
    assert scope["preserve_case_scope_and_oracles"] is True
    assert scope["mapping_changes_require_explicit_finding"] is True
    assert set(request.workspace.allowed_outputs) == set(family_plan_files("api"))
