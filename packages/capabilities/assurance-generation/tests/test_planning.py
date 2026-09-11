from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest
import yaml

from agent_runtime_contracts import AgentRunRequest
from graph_engine.canonical import canonical_json_bytes
from tests.product.test_change_local_output_routing import (
    dual_roots,
    execute_task,
    task_context,
    task_request,
)

from assurance_generation.operations.planning import plan_outputs, plan_review_outputs, planning_handler
from assurance_generation.resource_loader import resource_text
from planning_fixtures import (  # pyright: ignore[reportMissingImports]
    BINDING,
    FAMILIES,
    PLAN_DIGEST,
    PLAN_REF,
    VALID_LEAFS,
    family_plan_files,
    fake_agent_result,
    plan_input,
    review_result,
    reviewed_cases,
    valid_plan_result,
)


def test_e2e_plan_skill_reentry_reads_family_prefixed_review() -> None:
    skill = resource_text("skills/aa-e2e-plan/SKILL.md")
    assert "review/e2e-plan-review.json" in skill
    assert "review/plan-review.json" not in skill


def test_plan_outputs_use_results_plans_and_review() -> None:
    assert plan_outputs("CH-DEMO-001", "api") == (
        "qa/results/plans/api-codegen-mapping.json",
        "qa/results/plans/api-codegen-plan.md",
        "qa/results/plans/api-plan.md",
        "qa/results/plans/api-test-data-plan.md",
        "qa/results/plans/m3-review-summary.md",
    )
    assert plan_review_outputs("CH-DEMO-001", "api") == (
        "qa/results/review/api-plan-review-summary.md",
        "qa/results/review/api-plan-review.json",
    )
    for family in FAMILIES:
        assert all(path.startswith("qa/results/plans/") for path in plan_outputs("CH-DEMO-001", family))
        assert all(
            path.startswith("qa/results/review/") for path in plan_review_outputs("CH-DEMO-001", family)
        )
        assert not any(
            "/".join(("qa", "changes")) + "/" in path
            for path in (*plan_outputs("CH-DEMO-001", family), *plan_review_outputs("CH-DEMO-001", family))
        )


def _write_reviewed_cases(tmp_path: Path, family: str) -> None:
    path = tmp_path / "qa/cases/items/case.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(reviewed_cases(family), sort_keys=False), encoding="utf-8")


def _write_plan_package(project: Path, family: str) -> tuple[str, ...]:
    files = family_plan_files(family)
    for relative in files:
        path = project / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("ok\n", encoding="utf-8")
    mapping = project / f"qa/results/plans/{family}-codegen-mapping.json"
    mapping.write_text(
        json.dumps(
            {
                "schema_version": "1",
                "layer": family,
                "entries": [
                    {
                        "case_id": f"TC_{family.upper()}_001",
                        "symbol": f"test_tc_{family}_001__happy_path",
                        "target_file": {
                            "api": "qa/tests/api/test_users.py",
                            "e2e": "qa/tests/e2e/test_users.py",
                            "fuzz": "qa/tests/fuzz/test_users.py",
                            "performance": "qa/tests/perf/test_users.py",
                        }[family],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return files


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.asyncio
async def test_plan_prepare_is_deterministic_for_every_family(family: str, tmp_path: Path) -> None:
    handler = planning_handler(family, "prepare")
    first = await execute_task(handler, plan_input(family), tmp_path, binding_data=BINDING)
    second = await execute_task(handler, plan_input(family), tmp_path, binding_data=BINDING)
    assert canonical_json_bytes(first.output) == canonical_json_bytes(second.output)


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.asyncio
async def test_plan_prepare_instruction_order_is_skill_persona_cases_constraints(
    family: str, tmp_path: Path
) -> None:
    prepared = await execute_task(
        planning_handler(family, "prepare"),
        plan_input(family),
        tmp_path,
        binding_data=BINDING,
    )
    assert prepared.status == "succeeded"
    request = AgentRunRequest.model_validate(prepared.output)
    assert len(request.instructions) == 4
    skill, persona, reviewed, constraints = request.instructions
    assert skill.media_type == "text/plain"
    assert persona.media_type == "text/plain"
    assert reviewed.media_type == "application/json"
    assert constraints.media_type == "application/json"
    facts = cast(dict[str, object], constraints.json_content)["planning_facts"]
    assert cast(dict[str, object], facts)["capability_leafs"] == VALID_LEAFS
    assert f"{family} plan" in (skill.text_content or "").lower()
    assert "test-author persona" in (persona.text_content or "").lower()
    cases = cast(dict[str, object], reviewed.json_content)
    added = cases["added"]
    assert isinstance(added, list | tuple) and added
    first_case = cast(dict[str, object], added[0])
    assert (
        first_case["type"]
        == {"api": "API", "e2e": "E2E", "fuzz": "Fuzz", "performance": "Performance"}[family]
    )
    encoded = request.canonical_bytes().decode("utf-8").lower()
    assert "opencode" not in encoded
    assert "cursor" not in encoded
    assert "assurance_agent" not in encoded
    assert request.execution.provider_model == "test-model"

    raw_schema = request.result_contract.schema_document
    assert raw_schema is not None
    schema = cast(dict[str, object], raw_schema)
    properties = cast(dict[str, object], schema["properties"])
    required = cast(dict[str, object], properties["required_capabilities"])
    required_items = cast(dict[str, object], required["items"])
    assert required_items["enum"] == VALID_LEAFS
    definitions = cast(dict[str, object], schema["$defs"])
    coverage = cast(dict[str, object], definitions["PlanCoverageRow"])
    coverage_properties = cast(dict[str, object], coverage["properties"])
    coverage_required = cast(dict[str, object], coverage_properties["required_capabilities"])
    coverage_items = cast(dict[str, object], coverage_required["items"])
    assert coverage_items["enum"] == VALID_LEAFS
    performance = cast(dict[str, object], definitions["PerformanceScenarioV1"])
    performance_properties = cast(dict[str, object], performance["properties"])
    performance_capability = cast(dict[str, object], performance_properties["capability"])
    assert performance_capability["enum"] == VALID_LEAFS


@pytest.mark.asyncio
async def test_plan_retry_injects_authenticated_current_review(tmp_path: Path) -> None:
    _write_reviewed_cases(tmp_path, "api")
    relative = "qa/results/review/api-plan-review.json"
    review_path = tmp_path / relative
    review_path.parent.mkdir(parents=True, exist_ok=True)
    review = review_result("api")
    review.update(
        {
            "decision": "needs_fix",
            "findings": [
                {
                    "id": "API-PLAN-001",
                    "severity": "blocking",
                    "category": "runtime_contract",
                    "message": "Use admin_token and construct the token header locally.",
                    "locator": {
                        "artifact": "qa/results/plans/api-plan.md",
                        "case_id": "TC_API_001",
                        "key": "Auth Strategy",
                    },
                }
            ],
            "auto_fix_plan": ["API-PLAN-001"],
            "next_action": "repair the locked finding",
            "auto_fix_allowed": True,
            "codegen_readiness": "not_ready",
        }
    )
    review_path.write_text(json.dumps(review), encoding="utf-8")

    prepared = await execute_task(
        planning_handler("api", "prepare"),
        {**plan_input("api"), "local_round": 1, "reviewed_plan": valid_plan_result("api")},
        tmp_path,
        binding_data=BINDING,
    )

    assert prepared.status == "succeeded"
    request = AgentRunRequest.model_validate(prepared.output)
    assert len(request.instructions) == 6
    repair = cast(dict[str, object], request.instructions[5].json_content)
    assert repair["review_path"] == relative
    assert isinstance(repair["review_digest"], str) and len(repair["review_digest"]) == 64
    locked = cast(dict[str, object], repair["plan_repair_review"])
    assert locked["change_id"] == "CH-DEMO-001"
    assert locked["decision"] == "needs_fix"
    assert locked["auto_fix_plan"] == ("API-PLAN-001",)


@pytest.mark.asyncio
async def test_plan_retry_fails_closed_without_current_review(tmp_path: Path) -> None:
    _write_reviewed_cases(tmp_path, "api")

    prepared = await execute_task(
        planning_handler("api", "prepare"),
        {**plan_input("api"), "local_round": 1, "reviewed_plan": valid_plan_result("api")},
        tmp_path,
        binding_data=BINDING,
    )

    assert prepared.status == "failed"
    assert prepared.failure is not None
    assert prepared.failure.kind == "invalid_input"
    assert "plan repair review is not a regular single-link file" in prepared.failure.message


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.parametrize("defect", ("missing", "malformed", "change", "family", "case_scope"))
@pytest.mark.asyncio
async def test_plan_retry_rejects_invalid_previous_plan_before_dispatch(
    family: str, defect: str, tmp_path: Path
) -> None:
    review_path = tmp_path / f"qa/results/review/{family}-plan-review.json"
    review_path.parent.mkdir(parents=True)
    review_path.write_text(json.dumps(review_result(family)), encoding="utf-8")
    previous = valid_plan_result(family)
    if defect == "change":
        previous["change_id"] = "CH-OTHER"
    elif defect == "family":
        previous = valid_plan_result("api" if family != "api" else "e2e")
    elif defect == "case_scope":
        previous["case_ids"] = ["TC_OTHER_001"]
        previous["coverage"][0]["case_id"] = "TC_OTHER_001"
    elif defect == "malformed":
        previous.clear()
        previous["status"] = "reviewed"
    prepared = await execute_task(
        planning_handler(family, "prepare"),
        {**plan_input(family), "local_round": 1, "reviewed_plan": None if defect == "missing" else previous},
        tmp_path,
        binding_data=BINDING,
    )
    assert prepared.status == "failed"
    assert prepared.failure is not None
    assert prepared.failure.kind == "invalid_input"
    assert not prepared.failure.retryable
    assert "previous plan" in prepared.failure.message


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.asyncio
async def test_plan_prepare_hydrates_family_input_from_reviewed_workspace_cases(
    family: str, tmp_path: Path
) -> None:
    _write_reviewed_cases(tmp_path, family)
    prepared = await execute_task(
        planning_handler(family, "prepare"),
        {
            "change_id": "CH-DEMO-001",
            "plan_digest": PLAN_DIGEST,
            "plan_ref": PLAN_REF,
            "capability_leafs": list(VALID_LEAFS),
            "artifact_paths": ["qa/cases", "qa/fixtures", "qa/results", "qa/tests"],
        },
        tmp_path,
        binding_data=BINDING,
    )
    assert prepared.status == "succeeded"
    request = AgentRunRequest.model_validate(prepared.output)
    cases = cast(dict[str, object], request.instructions[2].json_content)
    constraints = cast(dict[str, object], request.instructions[3].json_content)
    assert [case["case_id"] for case in cast(list[dict[str, object]], cases["added"])] == [
        f"TC_{family.upper()}_001"
    ]
    assert constraints["operations"] == ("COND-1",)
    assert constraints["risks"] == ("high",)


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.asyncio
async def test_plan_finalize_classifies_unreadable_output(
    family: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, stage = dual_roots(tmp_path)
    files = _write_plan_package(stage, family)
    unreadable = stage / f"qa/results/plans/{family}-plan.md"
    read_bytes = Path.read_bytes

    def read(path: Path) -> bytes:
        if path == unreadable:
            raise PermissionError("output is unreadable")
        return read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", read)
    outcome = await planning_handler(family, "finalize").execute(
        task_request(fake_agent_result(valid_plan_result(family), artifact_paths=list(files))),
        task_context(project, stage),
    )
    assert outcome.status == "failed"
    assert outcome.failure is not None
    assert outcome.failure.kind == "invalid_output"
    assert "declared output file is unreadable" in outcome.failure.message


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.asyncio
async def test_plan_finalize_accepts_typed_family_plan(family: str, tmp_path: Path) -> None:
    project, write_root = dual_roots(tmp_path)
    files = _write_plan_package(write_root, family)
    finalize_input = fake_agent_result(valid_plan_result(family), artifact_paths=list(files))
    finalize_input["local_round"] = 1
    executed = await execute_task(
        planning_handler(family, "finalize"),
        finalize_input,
        project,
        write_root=write_root,
    )
    assert executed.status == "succeeded"
    output = cast(dict[str, object], executed.output)
    assert output["family"] == family
    assert output["case_ids"] == [f"TC_{family.upper()}_001"]
    assert "rounds_used" not in output
    assert "rounds_budget" not in output


@pytest.mark.asyncio
async def test_plan_retry_reads_unchanged_outputs_from_committed_baseline(tmp_path: Path) -> None:
    project, write_root = dual_roots(tmp_path)
    files = _write_plan_package(project, "api")
    changed = write_root / "qa/results/plans/m3-review-summary.md"
    changed.parent.mkdir(parents=True, exist_ok=True)
    changed.write_text("review feedback applied\n", encoding="utf-8")
    finalize_input = fake_agent_result(valid_plan_result("api"), artifact_paths=list(files))
    finalize_input["local_round"] = 1

    executed = await execute_task(
        planning_handler("api", "finalize"),
        finalize_input,
        project,
        write_root=write_root,
    )

    assert executed.status == "succeeded"
    assert changed.is_file()
    assert not (write_root / "qa/results/plans/api-codegen-mapping.json").exists()


@pytest.mark.asyncio
async def test_initial_plan_does_not_fall_back_to_committed_outputs(tmp_path: Path) -> None:
    project, write_root = dual_roots(tmp_path)
    files = _write_plan_package(project, "api")

    executed = await execute_task(
        planning_handler("api", "finalize"),
        fake_agent_result(valid_plan_result("api"), artifact_paths=list(files)),
        project,
        write_root=write_root,
    )

    assert executed.status == "failed"
    assert executed.failure is not None
    assert executed.failure.kind == "invalid_output"
    assert "declared output file is missing" in executed.failure.message


@pytest.mark.asyncio
async def test_plan_finalize_rejects_malformed_closed_codegen_mapping(tmp_path: Path) -> None:
    project, write_root = dual_roots(tmp_path)
    files = family_plan_files("e2e")
    for relative in files:
        path = write_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("ok\n", encoding="utf-8")
    mapping = write_root / "qa/results/plans/e2e-codegen-mapping.json"
    mapping.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "family": "e2e",
                "change_id": "CH-DEMO-001",
                "mappings": [
                    {
                        "case_id": "TC_E2E_001",
                        "test_function": "test_tc_e2e_001__happy_path",
                        "target_file": "tests/e2e/test_users.py",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    executed = await execute_task(
        planning_handler("e2e", "finalize"),
        fake_agent_result(valid_plan_result("e2e"), artifact_paths=list(files)),
        project,
        write_root=write_root,
    )

    assert executed.status == "failed"
    assert executed.failure is not None
    assert executed.failure.kind == "invalid_output"
    assert "closed codegen mapping is invalid" in executed.failure.message


@pytest.mark.asyncio
async def test_performance_plan_finalize_rejects_unknown_scenario_capability(
    tmp_path: Path,
) -> None:
    payload = valid_plan_result("performance")
    scenarios = cast(list[dict[str, object]], payload["performance_scenarios"])
    scenarios[0]["capability"] = "capabilities.adapters.performance.virtual"

    executed = await execute_task(
        planning_handler("performance", "finalize"),
        fake_agent_result(payload),
        tmp_path,
    )

    assert executed.status == "failed"
    assert executed.failure is not None
    assert executed.failure.kind == "invalid_output"
    assert "unknown capability leaf" in executed.failure.message


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.asyncio
async def test_plan_finalize_rejects_incomplete_output_package(family: str, tmp_path: Path) -> None:
    files = _write_plan_package(tmp_path, family)
    payload = valid_plan_result(family)
    payload["output_files"] = [path for path in files if not path.endswith(f"/{family}-plan.md")]
    outcome = await execute_task(
        planning_handler(family, "finalize"),
        fake_agent_result(payload, artifact_paths=list(files)),
        tmp_path,
    )
    assert outcome.status == "failed"
    assert outcome.failure is not None
    assert "complete family plan package" in outcome.failure.message


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.asyncio
async def test_plan_finalize_cannot_skip_file_authentication_without_artifact_roots(
    family: str, tmp_path: Path
) -> None:
    _, stage = dual_roots(tmp_path)
    _write_plan_package(stage, family)
    (stage / f"qa/results/plans/{family}-plan.md").unlink()
    outcome = await execute_task(
        planning_handler(family, "finalize"),
        fake_agent_result(valid_plan_result(family)),
        tmp_path,
        write_root=stage,
    )
    assert outcome.status == "failed"
    assert outcome.failure is not None
    assert "undeclared output file" in outcome.failure.message


@pytest.mark.parametrize(
    "field,value", [("p95_ms", -1), ("p95_ms", 0), ("error_rate_max", -0.1), ("error_rate_max", 1.1)]
)
@pytest.mark.asyncio
async def test_performance_plan_rejects_impossible_thresholds(
    field: str, value: float, tmp_path: Path
) -> None:
    files = _write_plan_package(tmp_path, "performance")
    payload = valid_plan_result("performance")
    payload["performance_scenarios"][0][field] = value
    outcome = await execute_task(
        planning_handler("performance", "finalize"),
        fake_agent_result(payload, artifact_paths=list(files)),
        tmp_path,
    )
    assert outcome.status == "failed"
    assert outcome.failure is not None
    assert field in outcome.failure.message


@pytest.mark.asyncio
async def test_plan_finalize_accepts_files_below_declared_artifact_root(tmp_path: Path) -> None:
    project, write_root = dual_roots(tmp_path)
    _write_plan_package(write_root, "api")
    executed = await execute_task(
        planning_handler("api", "finalize"),
        fake_agent_result(valid_plan_result("api"), artifact_paths=["qa/results"]),
        project,
        write_root=write_root,
    )
    assert executed.status == "succeeded"


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.asyncio
async def test_plan_finalize_rejects_wrong_family(family: str, tmp_path: Path) -> None:
    other = "e2e" if family == "api" else "api"
    payload = valid_plan_result(family)
    payload["family"] = other
    executed = await execute_task(
        planning_handler(family, "finalize"),
        fake_agent_result(payload),
        tmp_path,
    )
    assert executed.status == "failed"
    assert executed.failure is not None
    assert executed.failure.kind == "invalid_output"
    assert executed.failure.retryable is True


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.asyncio
async def test_plan_prepare_rejects_routing_marker_as_invalid_input(family: str, tmp_path: Path) -> None:
    binding = {
        **BINDING,
        "execution": {
            **cast(dict[str, object], BINDING["execution"]),
            "provider_model": "primary,fallback",
        },
    }
    prepared = await execute_task(
        planning_handler(family, "prepare"),
        plan_input(family),
        tmp_path,
        binding_data=binding,
    )
    assert prepared.status == "failed"
    assert prepared.failure is not None
    assert prepared.failure.kind == "invalid_input"
    assert prepared.failure.retryable is False


@pytest.mark.asyncio
async def test_failed_plan_validation_leaves_canonical_outputs_unchanged(tmp_path: Path) -> None:
    project, write_root = dual_roots(tmp_path)
    canonical = project / "qa/results/plans/api-plan.md"
    canonical.parent.mkdir(parents=True)
    original = b"# Canonical API plan\n"
    canonical.write_bytes(original)
    payload = valid_plan_result("api")
    payload["family"] = "e2e"
    executed = await execute_task(
        planning_handler("api", "finalize"),
        fake_agent_result(payload),
        project,
        write_root=write_root,
    )
    assert executed.status == "failed"
    assert executed.failure is not None
    assert executed.failure.kind == "invalid_output"
    assert canonical.read_bytes() == original
