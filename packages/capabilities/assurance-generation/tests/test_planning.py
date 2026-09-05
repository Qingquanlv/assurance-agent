from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest
import yaml

from agent_runtime_contracts import AgentRunRequest
from graph_engine.canonical import canonical_json_bytes
from tests.product.test_change_local_output_routing import dual_roots, execute_task

from assurance_generation.operations.planning import planning_handler
from assurance_generation.resource_loader import resource_text
from planning_fixtures import (  # pyright: ignore[reportMissingImports]
    BINDING,
    FAMILIES,
    VALID_LEAFS,
    family_plan_files,
    fake_agent_result,
    plan_input,
    reviewed_cases,
    valid_plan_result,
)


def test_e2e_plan_skill_reentry_reads_family_prefixed_review() -> None:
    skill = resource_text("skills/aa-e2e-plan/SKILL.md")
    assert "review/e2e-plan-review.json" in skill
    assert "review/plan-review.json" not in skill


def _write_reviewed_cases(tmp_path: Path, family: str) -> None:
    path = tmp_path / "qa/changes/CH-DEMO-001/cases/items/case.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(reviewed_cases(family), sort_keys=False), encoding="utf-8")


def _write_plan_package(project: Path, family: str) -> tuple[str, ...]:
    files = family_plan_files(family)
    for relative in files:
        path = project / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("ok\n", encoding="utf-8")
    mapping = project / f"qa/changes/CH-DEMO-001/plans/{family}-codegen-mapping.json"
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
                            "api": "tests/api/test_users.py",
                            "e2e": "tests/e2e/test_users.py",
                            "fuzz": "tests/fuzz/test_users.py",
                            "performance": "tests/perf/test_users.py",
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
            "capability_leafs": list(VALID_LEAFS),
            "artifact_paths": ["qa/changes"],
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
async def test_plan_finalize_accepts_typed_family_plan(family: str, tmp_path: Path) -> None:
    project, write_root = dual_roots(tmp_path)
    files = _write_plan_package(write_root, family)
    executed = await execute_task(
        planning_handler(family, "finalize"),
        fake_agent_result(valid_plan_result(family), artifact_paths=list(files)),
        project,
        write_root=write_root,
    )
    assert executed.status == "succeeded"
    output = cast(dict[str, object], executed.output)
    assert output["family"] == family
    assert output["case_ids"] == [f"TC_{family.upper()}_001"]


@pytest.mark.asyncio
async def test_plan_finalize_rejects_malformed_closed_codegen_mapping(tmp_path: Path) -> None:
    project, write_root = dual_roots(tmp_path)
    files = family_plan_files("e2e")
    for relative in files:
        path = write_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("ok\n", encoding="utf-8")
    mapping = write_root / "qa/changes/CH-DEMO-001/plans/e2e-codegen-mapping.json"
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


@pytest.mark.asyncio
async def test_plan_finalize_accepts_files_below_declared_artifact_root(tmp_path: Path) -> None:
    project, write_root = dual_roots(tmp_path)
    _write_plan_package(write_root, "api")
    executed = await execute_task(
        planning_handler("api", "finalize"),
        fake_agent_result(valid_plan_result("api"), artifact_paths=["qa/changes"]),
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
    canonical = project / "qa/changes/CH-DEMO-001/plans/api-plan.md"
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
