from __future__ import annotations

from pathlib import Path

import pytest
from agent_runtime_contracts.schema import validate_structured_result

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


def validate_prepared_result(review: dict, request) -> None:
    validate_structured_result(
        review,
        schema=request.result_contract.schema_document,
        schema_digest=request.result_contract.schema_digest,
    )


@pytest.mark.asyncio
async def test_prepared_evidence_paths_close_both_case_and_helper_schema(tmp_path: Path) -> None:
    # A file can exist and be readable without belonging to the locked evidence inventory.
    write(tmp_path, "app/core/exceptions.py", "class DomainError(Exception):\n    pass\n")
    review, business, request = await audited_review(
        tmp_path, helper=True, source="def rows():\n    return []\n"
    )
    requirements = next(
        part["review_requirements"]
        for item in request.instructions
        if isinstance(part := thaw_json(item.json_content), dict) and "review_requirements" in part
    )
    allowed = requirements.get("allowed_evidence_paths", [])
    assert HELPER_PATH in allowed
    assert ".aa/data-knowledge.yaml" in allowed
    assert "qa/cases/items/case.yaml" in allowed
    assert "app/core/exceptions.py" not in allowed
    assert allowed == sorted(set(allowed))
    validate_prepared_result(review, request)
    for group in ("cases", "helpers"):
        row = review["review_audit"][group][0]
        row["evidence_paths"].append("app/core/exceptions.py")
        validate_prepared_result(review, request)
        write_review(tmp_path, review)
        accepted = await finish(tmp_path, review, business)
        assert accepted.status == "succeeded", accepted.failure
        assert "app/core/exceptions.py" not in accepted.output["review_audit"][group][0]["evidence_paths"]
        row["evidence_paths"].pop()


@pytest.mark.asyncio
async def test_planned_unknown_invocation_is_rejected_before_finalize(tmp_path: Path) -> None:
    review, business, request = await audited_review(tmp_path, helper=True)
    validate_prepared_result(review, request)
    helper = review["review_audit"]["helpers"][0]
    helper["invocation"] = "unknown"
    validate_prepared_result(review, request)
    write_review(tmp_path, review)
    accepted = await finish(tmp_path, review, business)
    assert accepted.status == "succeeded", accepted.failure
    assert accepted.output["review_audit"]["helpers"][0]["invocation"] in {"sync", "async"}

    # Unknown is still a valid observation when paired with a real unresolved finding.
    helper.update(implementation="unresolved", plan_location=None, finding_ids=["F1"])
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
                "message": "Specify the helper invocation in the plan.",
                "locator": {"artifact": PLAN_PATH, "key": "Target Files"},
            }
        ],
    )
    validate_prepared_result(review, request)
    write_review(tmp_path, review)
    accepted = await finish(tmp_path, review, business)
    assert accepted.status == "succeeded", accepted.failure


@pytest.mark.asyncio
async def test_valid_final_result_cannot_hide_missing_artifact_readiness(tmp_path: Path) -> None:
    review, business, request = await audited_review(tmp_path)
    validate_prepared_result(review, request)
    artifact = dict(review)
    del artifact["codegen_readiness"]
    write_review(tmp_path, artifact)
    accepted = await finish(tmp_path, review, business)
    assert accepted.status == "succeeded", accepted.failure
    assert accepted.output["codegen_readiness"] in {"ready", "ready_with_warnings"}


@pytest.mark.parametrize("round_index", [0, 1, 3])
@pytest.mark.asyncio
async def test_review_coverage_is_required_in_first_and_followup_rounds(
    tmp_path: Path, round_index: int
) -> None:
    review, business, _ = await audited_review(tmp_path)
    review["review_audit"]["cases"] = []
    write_review(tmp_path, review)
    result = await finish(tmp_path, review, business, local_round=round_index)
    assert result.status == "succeeded", result.failure
    assert [row["case_id"] for row in result.output["review_audit"]["cases"]]


@pytest.mark.parametrize("area", ["request", "auth", "setup", "assertion", "cleanup", "helpers"])
@pytest.mark.asyncio
async def test_a_review_cannot_omit_a_check_area(tmp_path: Path, area: str) -> None:
    review, business, _ = await audited_review(tmp_path)
    del review["review_audit"]["cases"][0]["checks"][area]
    result = await finish(tmp_path, review, business)
    assert result.status == "failed"
    assert result.failure and "Field required" in result.failure.message


@pytest.mark.parametrize(
    "mutation",
    [
        "duplicate_case",
        "missing_helper",
        "wrong_kind",
        "wrong_signature",
        "wrong_async",
        "fake_evidence",
        "stale_digest",
        "missing_input",
        "unlinked_finding",
        "unknown_finding",
        "unresolved_helper",
    ],
)
@pytest.mark.asyncio
async def test_audit_repairs_incomplete_or_invented_evidence(tmp_path: Path, mutation: str) -> None:
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
    assert result.status == "succeeded", result.failure
    repaired = result.output["review_audit"]
    assert [row["case_id"] for row in repaired["cases"]] == [case["case_id"]]
    assert [row["capability"] for row in repaired["helpers"]]
    if mutation == "fake_evidence":
        assert "../../private-source.py" not in repaired["cases"][0]["evidence_paths"]
    if mutation == "unresolved_helper":
        assert (
            repaired["helpers"][0]["implementation"] != "unresolved" or repaired["helpers"][0]["finding_ids"]
        )
    if mutation == "wrong_kind":
        assert repaired["helpers"][0]["declared_kind"] == "helper"


@pytest.mark.asyncio
async def test_missing_generated_target_is_not_satisfied_by_an_import(tmp_path: Path) -> None:
    review, business, _ = await audited_review(tmp_path, helper=True, generated_target=False)
    result = await finish(tmp_path, review, business)
    assert result.status == "succeeded", result.failure
    review["review_audit"]["helpers"][0]["plan_location"]["section"] = "Import Strategy"
    write_review(tmp_path, review)
    result = await finish(tmp_path, review, business)
    assert result.status == "succeeded", result.failure
    assert result.output["review_audit"]["helpers"][0]["plan_location"]["section"] != "Import Strategy"


@pytest.mark.asyncio
async def test_stub_is_planned_work_not_an_existing_implementation(tmp_path: Path) -> None:
    review, business, _ = await audited_review(
        tmp_path, helper=True, source="def rows():\n    raise NotImplementedError\n"
    )
    accepted = await finish(tmp_path, review, business)
    assert accepted.status == "succeeded", accepted.failure
    review["review_audit"]["helpers"][0]["implementation"] = "existing"
    write_review(tmp_path, review)
    result = await finish(tmp_path, review, business)
    assert result.status == "succeeded", result.failure
    assert result.output["review_audit"]["helpers"][0]["implementation"] == "planned"


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
    assert result.status == "succeeded", result.failure
    assert result.output["review_audit"]["planning_facts_digest"]


@pytest.mark.asyncio
async def test_artifact_must_carry_the_same_audit_as_the_final_json(tmp_path: Path) -> None:
    review, business, _ = await audited_review(tmp_path)
    write_review(tmp_path, {**review, "review_audit": None})
    result = await finish(tmp_path, review, business)
    assert result.status == "succeeded", result.failure
    assert result.output["review_audit"] is not None


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
