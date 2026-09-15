from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path

import pytest

from graph_engine.frozen_json import thaw_json

from assurance_generation.contracts.review_audit import PlanReviewAudit
from assurance_generation.contracts.reviews import PlanReviewAuthoring
from assurance_generation.operations.review_audit import (
    api_review_requirements,
    repair_api_review_audit,
    validate_api_review_audit,
)
from assurance_generation.operations.review import review_finalize_handler
from planning_fixtures import fake_agent_result  # pyright: ignore[reportMissingImports]
from review_audit_fixtures import (  # pyright: ignore[reportMissingImports]
    PLAN_PATH,
    audited_review,
    write_review,
)
from tests.product.test_change_local_output_routing import execute_task


async def finish(root: Path, review: dict, business: dict, *, local_round: int = 0):
    return await execute_task(
        review_finalize_handler("api"),
        {
            **fake_agent_result(review, capability_leafs=tuple(business["capability_leafs"])),
            "local_round": local_round,
        },
        root,
    )


def _leafs(business: dict) -> frozenset[str]:
    return frozenset(business["capability_leafs"])


def _authoring(review: dict, business: dict) -> PlanReviewAuthoring:
    return PlanReviewAuthoring.model_validate(review, context={"capability_leafs": _leafs(business)})


@pytest.mark.asyncio
async def test_repair_strips_unobserved_evidence_and_keeps_pass(tmp_path: Path) -> None:
    review, business, request = await audited_review(tmp_path, helper=True)
    review["review_audit"]["cases"][0]["evidence_paths"].append("qa/tests/testdata/domain/item.py")
    review["review_audit"]["cases"][0]["evidence_paths"].append("app/core/exceptions.py")
    write_review(tmp_path, review)
    accepted = await finish(tmp_path, review, business)
    assert accepted.status == "succeeded", accepted.failure
    assert isinstance(accepted.output, Mapping)
    audit = PlanReviewAudit.model_validate(accepted.output["review_audit"])
    allowed = set(
        next(
            part["review_requirements"]["allowed_evidence_paths"]
            for item in request.instructions
            if isinstance(part := thaw_json(item.json_content), dict) and "review_requirements" in part
        )
    )
    assert "app/core/exceptions.py" not in audit.cases[0].evidence_paths
    assert set(audit.cases[0].evidence_paths) <= allowed


@pytest.mark.asyncio
async def test_repair_does_not_fill_missing_helper_and_case_rows(tmp_path: Path) -> None:
    review, business, _ = await audited_review(tmp_path, helper=True)
    review["review_audit"]["helpers"] = []
    review["review_audit"]["cases"] = []
    write_review(tmp_path, review)
    accepted = await finish(tmp_path, review, business)
    assert accepted.status == "failed"
    assert accepted.failure and "cover every selected case" in accepted.failure.message


@pytest.mark.asyncio
async def test_repair_does_not_invent_a_plan_location_or_invocation(tmp_path: Path) -> None:
    review, business, _ = await audited_review(tmp_path, helper=True)
    helper = review["review_audit"]["helpers"][0]
    helper["invocation"] = "unknown"
    helper["plan_location"] = {"artifact": PLAN_PATH, "section": "Import Strategy"}
    write_review(tmp_path, review)
    accepted = await finish(tmp_path, review, business)
    assert accepted.status == "failed"
    assert accepted.failure and "generation target" in accepted.failure.message


@pytest.mark.asyncio
async def test_repair_rejects_stub_claimed_as_existing(tmp_path: Path) -> None:
    review, business, _ = await audited_review(
        tmp_path, helper=True, source="def rows():\n    raise NotImplementedError\n"
    )
    review["review_audit"]["helpers"][0]["implementation"] = "existing"
    write_review(tmp_path, review)
    accepted = await finish(tmp_path, review, business)
    assert accepted.status == "failed"
    assert accepted.failure and "stub" in accepted.failure.message


def test_repair_function_drops_foreign_paths_without_raising() -> None:
    document = PlanReviewAuthoring.model_validate(
        {
            "schema_version": "1.0",
            "review_type": "api-plan",
            "change_id": "CH-DEMO-001",
            "decision": "pass",
            "findings": [],
            "auto_fix_plan": [],
            "next_action": "proceed",
            "auto_fix_allowed": False,
            "human_review_required": False,
            "codegen_readiness": "ready",
            "risk_level": "medium",
            "required_capabilities": ["entities.item.create"],
            "review_audit": {
                "input_refs": [
                    {
                        "path": "qa/results/plans/api-plan.md",
                        "digest": hashlib.sha256(b"# Plan\n").hexdigest(),
                    }
                ],
                "planning_facts_digest": "c" * 64,
                "cases": [
                    {
                        "case_id": "TC_API_001",
                        "checks": {
                            "request": "pass",
                            "auth": "pass",
                            "setup": "pass",
                            "assertion": "pass",
                            "cleanup": "pass",
                            "helpers": "pass",
                        },
                        "evidence_paths": ["qa/results/plans/api-plan.md", "app/core/exceptions.py"],
                        "finding_ids": [],
                        "rationale": "ok",
                    }
                ],
                "helpers": [],
            },
        },
        context={"capability_leafs": frozenset({"entities.item.create"})},
    )
    plan = b"# Plan\n"
    images = {"qa/results/plans/api-plan.md": plan}
    facts = {
        "digest": "c" * 64,
        "inputs": [{"path": "qa/proposal.md", "digest": "d" * 64}],
        "files": [],
        "declared_symbols": [],
    }
    requirements = api_review_requirements(case_ids=("TC_API_001",), facts=facts, images=images)
    repaired, warnings = repair_api_review_audit(
        document, requirements=requirements, facts=facts, images=images
    )
    validate_api_review_audit(repaired, requirements=requirements, facts=facts, images=images)
    assert repaired.review_audit is not None
    assert "app/core/exceptions.py" not in repaired.review_audit.cases[0].evidence_paths
    assert any("evidence_paths" in item for item in warnings)
