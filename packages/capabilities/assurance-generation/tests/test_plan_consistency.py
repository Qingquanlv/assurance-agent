from __future__ import annotations

import json
from pathlib import Path

import pytest

from assurance_generation.contracts.codegen import CodegenMapping
from assurance_generation.operations.plan_consistency import check_plan_consistency
from assurance_generation.operations.planning import planning_handler
from assurance_intake.domain.planning_facts import build_planning_facts
from tests.product.test_change_local_output_routing import execute_task
from planning_fixtures import family_plan_files, fake_agent_result, valid_plan_result  # pyright: ignore[reportMissingImports]


def _mapping() -> CodegenMapping:
    return CodegenMapping.model_validate(
        {
            "schema_version": "1",
            "layer": "api",
            "entries": [
                {
                    "case_id": "TC_API_001",
                    "symbol": "test_tc_api_001__happy_path",
                    "target_file": "qa/tests/api/test_users.py",
                }
            ],
        }
    )


def test_check_collects_cross_artifact_contradictions_in_one_pass(tmp_path: Path) -> None:
    helper = tmp_path / "qa/tests/testdata/domain/item.py"
    helper.parent.mkdir(parents=True)
    helper.write_text("def make_item(): pass\n")
    facts = build_planning_facts(
        tmp_path,
        change_id="CH-1",
        capability_leafs=("entities.item.create",),
        families=("api",),
        target_files=("qa/tests/testdata/domain/item.py",),
    )
    images = {
        "plans/codegen.md": b"## Test Function Mapping\n| Case ID | Test Function | Target File |\n|---|---|---|\n| TC_API_001 | test_tc_api_001__wrong | qa/tests/api/test_users.py |\n",
        "plans/data.md": b"## Factory Mapping\n| Shared Module | Function | Ownership |\n|---|---|---|\n| qa/tests/testdata/domain/item.py | make_item | create-if-missing |\n\n## Capability Mapping\n| Capability |\n|---|\n| capabilities.adapters.api.invented |\n",
    }
    errors = check_plan_consistency(images, mapping=_mapping(), facts=facts)
    assert len(errors) == 3
    assert any("closed codegen mapping" in error for error in errors)
    assert any("observed definition" in error for error in errors)
    assert any("unknown capability leaf" in error for error in errors)


def test_unknown_fixture_and_planned_new_helper_do_not_become_absence_errors(tmp_path: Path) -> None:
    facts = build_planning_facts(tmp_path, change_id="CH-1", capability_leafs=(), families=("api",))
    image = b"## Factory Mapping\n| Shared Module | Function | Ownership |\n|---|---|---|\n| tests/new.py | new_helper | create-if-missing |\n\n## Fixture Mapping\n| Fixture | Source Factory |\n|---|---|\n| dynamic_fixture | external plugin |\n"
    assert check_plan_consistency({"plan.md": image}, mapping=_mapping(), facts=facts) == ()


def test_fenced_mapping_examples_do_not_override_actual_mapping(tmp_path: Path) -> None:
    facts = build_planning_facts(tmp_path, change_id="CH-1", capability_leafs=(), families=("api",))
    image = b"## Example\n```markdown\n| Case ID | Test Function | Target File |\n|---|---|---|\n| EXAMPLE | example | example.py |\n```\n"
    assert check_plan_consistency({"plan.md": image}, mapping=_mapping(), facts=facts) == ()


def test_summary_capability_column_is_checked_regardless_of_heading_case(tmp_path: Path) -> None:
    facts = build_planning_facts(tmp_path, change_id="CH-1", capability_leafs=(), families=("api",))
    image = b"## Summary\n| Required capabilities |\n|---|\n| capabilities.adapters.api.invented |\n"
    errors = check_plan_consistency({"summary.md": image}, mapping=_mapping(), facts=facts)
    assert len(errors) == 1
    assert "unknown capability leaf" in errors[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("content", [b"", b" \n\t", b"\xff"])
async def test_finalize_rejects_empty_or_non_utf8_plan_documents(tmp_path: Path, content: bytes) -> None:
    files = family_plan_files("api")
    stage = tmp_path / ".stage"
    for relative in files:
        path = stage / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_mapping().model_dump_json().encode() if relative.endswith(".json") else content)
    outcome = await execute_task(
        planning_handler("api", "finalize"),
        {**fake_agent_result(valid_plan_result("api")), "artifact_paths": list(files)},
        tmp_path,
        write_root=stage,
    )
    assert outcome.status == "failed"
    assert outcome.failure is not None
    assert outcome.failure.kind == "invalid_output"
    assert "plan Markdown must be" in outcome.failure.message


@pytest.mark.asyncio
async def test_finalize_rejects_mapping_drift_across_staged_and_unchanged_repair_files(
    tmp_path: Path,
) -> None:
    files = family_plan_files("api")
    for relative in files:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# Plan\n")
    mapping_path = tmp_path / "qa/results/plans/api-codegen-mapping.json"
    mapping_path.write_text(_mapping().model_dump_json())
    # Only one file is edited by this repair; the stale claim in an unchanged
    # file must still be checked against the complete package.
    unchanged = tmp_path / "qa/results/plans/api-codegen-plan.md"
    unchanged.write_text(
        "## Test Function Mapping\n| Case ID | Test Function | Target File |\n|---|---|---|\n| TC_API_001 | test_tc_api_001__wrong | tests/api/test_users.py |\n"
    )
    stage = tmp_path / ".stage"
    edited = stage / "qa/results/plans/api-test-data-plan.md"
    edited.parent.mkdir(parents=True)
    edited.write_text("# Repaired data plan\n")
    payload = fake_agent_result(valid_plan_result("api"))
    outcome = await execute_task(
        planning_handler("api", "finalize"),
        {**payload, "local_round": 1, "artifact_paths": list(files)},
        tmp_path,
        write_root=stage,
    )
    assert outcome.status == "failed"
    assert outcome.failure is not None
    assert "Test Function Mapping contradicts" in outcome.failure.message
    assert json.loads(mapping_path.read_bytes())["entries"][0]["symbol"] == "test_tc_api_001__happy_path"
