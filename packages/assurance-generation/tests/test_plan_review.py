from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from agent_runtime_contracts import AgentRunRequest
from tests.phase5.test_change_local_output_routing import execute_task

from assurance_generation.operations.review import review_finalize_handler, review_prepare_handler
from planning_fixtures import (  # pyright: ignore[reportMissingImports]
    BINDING,
    FAMILIES,
    fake_agent_result,
    plan_input,
    review_result,
)


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
    prepared = await execute_task(
        review_prepare_handler(family),
        plan_input(family),
        tmp_path,
        binding_data=BINDING,
    )
    assert prepared.status == "succeeded"
    request = AgentRunRequest.model_validate(prepared.output)
    assert len(request.instructions) == 4
    skill, persona, reviewed, constraints = request.instructions
    assert f"{family} plan review" in (skill.text_content or "").lower()
    assert "reviewer persona" in (persona.text_content or "").lower()
    assert reviewed.media_type == "application/json"
    assert constraints.media_type == "application/json"
    schema = request.result_contract.schema_document
    assert schema is not None
    thawed_schema = cast(dict[str, object], schema)
    properties = cast(dict[str, object], thawed_schema["properties"])
    required_capabilities = cast(dict[str, object], properties["required_capabilities"])
    items = cast(dict[str, object], required_capabilities["items"])
    assert items["enum"] == ("auth.session.create", "entities.item.create")


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.asyncio
async def test_plan_review_finalize_rejects_malformed_input(family: str, tmp_path: Path) -> None:
    executed = await execute_task(review_finalize_handler(family), {"agent_result": {}}, tmp_path)
    assert executed.status == "failed"
    assert executed.failure is not None
    assert executed.failure.kind == "invalid_input"
    assert executed.failure.retryable is False
