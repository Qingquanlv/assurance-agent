from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from agent_runtime_contracts import AgentRunRequest
from graph_engine.canonical import canonical_json_bytes
from tests.phase4.conformance import execute_task

from assurance_generation.operations.planning import planning_handler
from planning_fixtures import (  # pyright: ignore[reportMissingImports]
    BINDING,
    FAMILIES,
    family_plan_files,
    fake_agent_result,
    plan_input,
    valid_plan_result,
)


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


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.asyncio
async def test_plan_finalize_accepts_typed_family_plan(family: str, tmp_path: Path) -> None:
    files = family_plan_files(family)
    for relative in files:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("ok\n", encoding="utf-8")
    executed = await execute_task(
        planning_handler(family, "finalize"),
        fake_agent_result(valid_plan_result(family), artifact_paths=list(files)),
        tmp_path,
    )
    assert executed.status == "succeeded"
    output = cast(dict[str, object], executed.output)
    assert output["family"] == family
    assert output["case_ids"] == [f"TC_{family.upper()}_001"]


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
