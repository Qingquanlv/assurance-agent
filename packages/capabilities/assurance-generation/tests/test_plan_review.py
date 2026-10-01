from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest

from agent_runtime_contracts import AgentRunRequest
from graph_engine.frozen_json import thaw_json
from tests.product.test_change_local_output_routing import execute_task

from graph_engine.canonical import canonical_json_bytes

from assurance_generation.contracts.reviews import public_review_outcome
from assurance_generation.operations.plan_review_policy import write_finding_scope
from assurance_generation.operations.planning import (
    closed_family,
    evidence_ref,
    expected_plan_review_history,
    plan_review_input_paths,
)
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
    PLAN_DIGEST,
    PLAN_REF,
    fake_agent_result,
    plan_input,
    review_result,
    reviewed_cases,
    valid_plan_review,
)


def _review_skill(skill_id: str) -> str:
    family = skill_id.removeprefix("aa-").removesuffix("-reviewer")
    return f"ops/{family.replace('-', '_')}_review/SKILL.md"


def _codegen_with_method() -> dict[str, object]:
    return {
        "schema_version": "1",
        "change_id": "CH-DEMO-001",
        "layer": "api",
        "files": [],
        "mapping": {
            "schema_version": "1",
            "layer": "api",
            "entries": [
                {
                    "case_id": "TC_API_001",
                    "symbol": "test_lockout",
                    "target_file": "qa/tests/api/items/test_items.py",
                }
            ],
        },
        "required_capabilities": ["entities.item.create"],
        "method_plans": [
            {
                "mrc_id": "MRC-LOCK",
                "requirement_id": "REQ-LOCK",
                "profile_id": "api.state-sequence.v1",
                "case_ids": ["TC_API_001"],
                "prerequisites": [],
                "steps": [{"step_id": "S1", "purpose": "observe", "action": "login"}],
                "observations": [
                    {
                        "observation_id": "OBS-LOCK",
                        "observation_key": "locked_status",
                        "step_id": "S1",
                        "test_nodeid": "qa/tests/api/items/test_items.py::test_lockout",
                        "assertion_id": "A1",
                    }
                ],
            }
        ],
    }


def test_review_audit_modules_are_gone() -> None:
    import importlib

    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("assurance_generation.operations.review_audit")
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("assurance_generation.contracts.review_audit")


def test_plan_review_schema_has_no_review_audit() -> None:
    from assurance_generation.resource_loader import resource_bytes

    for relative in (
        "ops/api_codegen_review/result.schema.json",
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


def _stage_root(tmp_path: Path) -> Path:
    return tmp_path / "qa" / ".staging" / "attempt-1"


def _write_plan_review_runtime_seal(
    write_root: Path,
    project_root: Path,
    *,
    family: str,
    change_id: str,
    coverage_epoch: int,
    local_round: int,
    review: dict[str, object],
) -> None:
    typed_family = closed_family(family)
    input_paths = plan_review_input_paths(project_root, change_id=change_id, family=typed_family)
    input_refs = tuple(evidence_ref(project_root, path) for path in input_paths)
    review_ref = evidence_ref(write_root, f"qa/results/review/{family}-codegen-review.json")
    history = expected_plan_review_history(
        change_id=change_id,
        coverage_epoch=coverage_epoch,
        family=typed_family,
        round_index=local_round,
        outcome=public_review_outcome(str(review["route"])),
        input_refs=input_refs,
        source_refs=(*input_refs, review_ref),
    )
    relative = f"qa/results/codegen/{family}/reviews/epochs/{coverage_epoch}/rounds/{local_round}.json"
    path = write_root.joinpath(*relative.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json_bytes(history.model_dump(mode="json")) + b"\n")
    write_finding_scope(
        write_root,
        family=family,
        coverage_epoch=coverage_epoch,
        change_id=change_id,
        route=str(review["route"]),
        finding_ids=tuple(str(item) for item in cast(list[object], review["finding_ids"])),
    )


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
async def test_review_requires_typed_semantic_review_for_each_method_plan(tmp_path: Path) -> None:
    payload = fake_agent_result(valid_plan_review())
    payload["codegen_output"] = _codegen_with_method()

    missing = await execute_task(review_finalize_handler("api"), payload, tmp_path)
    assert missing.status == "failed"
    assert missing.failure is not None
    assert "semantic_reviews" in missing.failure.message

    review = {
        **valid_plan_review(),
        "semantic_reviews": [
            {
                "frozen_plan_digest": PLAN_DIGEST,
                "mrc_id": "MRC-LOCK",
                "requirement_id": "REQ-LOCK",
                "plan_ref": PLAN_REF,
                "status": "pass",
                "reason": "the frozen requirement supports the expected status",
                "source_refs": [],
                "expectation_reviews": [
                    {
                        "observation_key": "locked_status",
                        "status": "pass",
                        "reason": "the expected status is explicit",
                        "basis_refs": [],
                    }
                ],
            }
        ],
    }
    accepted_payload = fake_agent_result(review)
    accepted_payload["codegen_output"] = _codegen_with_method()
    accepted = await execute_task(review_finalize_handler("api"), accepted_payload, tmp_path)
    assert accepted.status == "succeeded", accepted.failure


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
    skill = resource_text(_review_skill(skill_id))
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
    skill = resource_text(_review_skill(skill_id))
    assert "review_audit" not in skill
    assert "review_requirements" not in skill
    assert "plan_location" not in skill
    assert "edit plan files" not in skill


def test_e2e_reviewer_skill_outputs_use_family_prefixed_names() -> None:
    skill = resource_text("ops/e2e_codegen_review/SKILL.md")
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
    review = (
        valid_plan_review()
        if family == "api"
        else {**valid_plan_review(), "review_type": f"{family}-codegen"}
    )
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
    _write_plan_review_runtime_seal(
        stage,
        tmp_path,
        family=family,
        change_id="CH-DEMO-001",
        coverage_epoch=2,
        local_round=1,
        review=review,
    )
    before_history = (stage / "qa/results/codegen/api/reviews/epochs/2/rounds/1.json").read_bytes()
    before_scope = (stage / "qa/results/codegen/api/reviews/epochs/2/finding-scope.json").read_bytes()

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
    assert history_path.read_bytes() == before_history
    assert (stage / "qa/results/codegen/api/reviews/epochs/2/finding-scope.json").read_bytes() == before_scope


@pytest.mark.asyncio
@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.parametrize("disk_content", ["{}", "not json", "different_review", "different_route"])
async def test_plan_review_rejects_disk_result_mismatch(
    tmp_path: Path, family: str, disk_content: str
) -> None:
    review = review_result(family)
    _write_review_workspace(tmp_path, family)
    stage = tmp_path / ".stage"
    path = stage / f"qa/results/review/{family}-codegen-review.json"
    path.parent.mkdir(parents=True)
    if disk_content == "different_review":
        disk_content = json.dumps({**review, "change_id": "CH-DIFFERENT"})
    elif disk_content == "different_route":
        disk_content = json.dumps({**review, "route": "human"})
    path.write_text(disk_content, encoding="utf-8")
    _write_plan_review_runtime_seal(
        stage,
        tmp_path,
        family=family,
        change_id="CH-DEMO-001",
        coverage_epoch=2,
        local_round=1,
        review=review,
    )

    outcome = await execute_task(
        review_finalize_handler(family),
        {**fake_agent_result(review), "change_id": "CH-DEMO-001", "coverage_epoch": 2, "local_round": 1},
        tmp_path,
        write_root=stage,
    )

    assert outcome.status == "failed"
    assert outcome.failure is not None
    assert outcome.failure.kind == "invalid_output"
    assert "review artifact" in outcome.failure.message
    assert path.read_text(encoding="utf-8") == disk_content


@pytest.mark.asyncio
@pytest.mark.parametrize("family", FAMILIES)
async def test_plan_review_accepts_matching_artifact_without_rewriting(tmp_path: Path, family: str) -> None:
    review = review_result(family)
    _write_review_workspace(tmp_path, family)
    stage = tmp_path / ".stage"
    path = stage / f"qa/results/review/{family}-codegen-review.json"
    path.parent.mkdir(parents=True)
    original = json.dumps(review, indent=4).encode("utf-8") + b"\n"
    path.write_bytes(original)
    _write_plan_review_runtime_seal(
        stage,
        tmp_path,
        family=family,
        change_id="CH-DEMO-001",
        coverage_epoch=2,
        local_round=1,
        review=review,
    )

    outcome = await execute_task(
        review_finalize_handler(family),
        {**fake_agent_result(review), "change_id": "CH-DEMO-001", "coverage_epoch": 2, "local_round": 1},
        tmp_path,
        write_root=stage,
    )

    assert outcome.status == "succeeded", outcome.failure
    assert cast(dict[str, object], outcome.output)["route"] == "codegen"
    assert path.read_bytes() == original


@pytest.mark.asyncio
@pytest.mark.parametrize("family", FAMILIES)
async def test_plan_review_finalize_generates_host_seals(tmp_path: Path, family: str) -> None:
    from assurance_generation.contracts.attempts import AGENT_JOB_CONTRACTS
    from tests.capabilities.finalize_phase import checked_finalize

    review = review_result(family)
    _write_review_workspace(tmp_path, family)
    stage = _stage_root(tmp_path)
    raw = stage / f"qa/results/review/{family}-codegen-review.json"
    raw.parent.mkdir(parents=True)
    original = json.dumps(review).encode()
    raw.write_bytes(original)
    executed = await checked_finalize(
        AGENT_JOB_CONTRACTS[f"{family}.codegen-review"],
        stage,
        lambda: execute_task(
            review_finalize_handler(family),
            {**fake_agent_result(review), "change_id": "CH-DEMO-001", "coverage_epoch": 0},
            tmp_path,
        ),
    )
    assert executed.status == "succeeded", executed.failure
    history = stage / f"qa/results/codegen/{family}/reviews/epochs/0/rounds/0.json"
    scope = stage / f"qa/results/codegen/{family}/reviews/epochs/0/finding-scope.json"
    assert json.loads(history.read_bytes())["outcome"] == "pass"
    assert json.loads(scope.read_bytes())["route"] == "codegen"
    assert raw.read_bytes() == original


@pytest.mark.asyncio
async def test_plan_review_finalize_regenerates_history_from_raw_review(tmp_path: Path) -> None:
    review = valid_plan_review()
    _write_review_workspace(tmp_path)
    write_review(tmp_path, review)
    write_finding_scope(
        _stage_root(tmp_path),
        family="api",
        coverage_epoch=0,
        change_id="CH-DEMO-001",
        route=str(review["route"]),
        finding_ids=tuple(str(item) for item in review["finding_ids"]),
    )
    executed = await execute_task(
        review_finalize_handler("api"),
        {**fake_agent_result(review), "change_id": "CH-DEMO-001", "coverage_epoch": 0},
        tmp_path,
    )
    assert executed.status == "succeeded", executed.failure
    history = _stage_root(tmp_path) / "qa/results/codegen/api/reviews/epochs/0/rounds/0.json"
    assert json.loads(history.read_bytes())["outcome"] == "pass"


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
    _write_plan_review_runtime_seal(
        _stage_root(tmp_path),
        tmp_path,
        family="api",
        change_id="CH-DEMO-001",
        coverage_epoch=0,
        local_round=0,
        review=review,
    )
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
    _write_plan_review_runtime_seal(
        _stage_root(tmp_path),
        tmp_path,
        family="api",
        change_id="CH-DEMO-001",
        coverage_epoch=1,
        local_round=0,
        review=first,
    )
    first_pass = await execute_task(
        review_finalize_handler("api"),
        {**fake_agent_result(first), "change_id": "CH-DEMO-001", "coverage_epoch": 1},
        tmp_path,
    )
    assert first_pass.status == "succeeded", first_pass.failure
    assert cast(dict[str, object], first_pass.output)["route"] == "auto_fix"

    second = _as_auto_fix(review, _semantic_finding(), _semantic_finding("API-PLAN-004"))
    write_review(tmp_path, second)
    _write_plan_review_runtime_seal(
        _stage_root(tmp_path),
        tmp_path,
        family="api",
        change_id="CH-DEMO-001",
        coverage_epoch=1,
        local_round=0,
        review=second,
    )
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
    _write_plan_review_runtime_seal(
        _stage_root(tmp_path),
        tmp_path,
        family="api",
        change_id="CH-DEMO-001",
        coverage_epoch=4,
        local_round=0,
        review=review,
    )
    passed = await execute_task(
        review_finalize_handler("api"),
        {**fake_agent_result(review), "change_id": "CH-DEMO-001", "coverage_epoch": 4},
        tmp_path,
    )
    assert passed.status == "succeeded", passed.failure
    assert cast(dict[str, object], passed.output)["route"] == "codegen"

    later = _as_auto_fix(review, _semantic_finding())
    write_review(tmp_path, later)
    _write_plan_review_runtime_seal(
        _stage_root(tmp_path),
        tmp_path,
        family="api",
        change_id="CH-DEMO-001",
        coverage_epoch=4,
        local_round=0,
        review=later,
    )
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
async def test_plan_review_prepare_uses_reviewer_skill(family: str, tmp_path: Path) -> None:
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
    assert len(request.instructions) == 5
    skill, reviewed, constraints, locked_inputs, extra = request.instructions
    assert f"{family} codegen review" in (skill.text_content or "").lower()
    assert "Evidence-proven, bounded defects" in (skill.text_content or "")
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
    assert all(not (isinstance(part, dict) and "review_requirements" in part) for part in parts)
