from __future__ import annotations

import hashlib
from pathlib import Path
from typing import cast

import pytest
import yaml

from agent_runtime_contracts import AgentRunRequest
from graph_engine.canonical import JSONValue
from graph_engine.frozen_json import thaw_json
from assurance_generation.contracts.agent import CodegenInputV1
from assurance_generation.operations.codegen import codegen_prepare_handler
from assurance_generation.operations.review import review_prepare_handler
from tests.product.test_change_local_output_routing import execute_task
from codegen_fixtures import codegen_result, durable_oracle_path, locked_oracle_paths  # pyright: ignore[reportMissingImports]
from planning_fixtures import BINDING, FAMILIES, plan_input, reviewed_cases  # pyright: ignore[reportMissingImports]
from review_audit_fixtures import review_prepare_input, write_codegen_artifacts  # pyright: ignore[reportMissingImports]


@pytest.mark.parametrize("family", FAMILIES)
def test_codegen_retry_handoff_preserves_previous_codegen_output(family: str) -> None:
    business = plan_input(family)
    first = codegen_result(files=[durable_oracle_path(family=family)], family=family)
    revised = dict(first)
    revised = {**first, "required_capabilities": ["auth.session.create", "entities.item.create"]}
    selected = CodegenInputV1.model_validate({**business, "local_round": 1, "codegen_output": revised})
    assert selected.local_round == 1
    assert selected.codegen_output == revised


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.asyncio
async def test_codegen_retry_prepare_receives_previous_codegen_output(family: str, tmp_path: Path) -> None:
    business = plan_input(family)
    test_path, data_path = locked_oracle_paths(family)
    previous = codegen_result(files=[test_path, data_path], family=family)
    for entry in previous["files"]:
        target = tmp_path / entry["repo_path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"baseline\n")
        entry["content_sha256"] = f"sha256:{hashlib.sha256(target.read_bytes()).hexdigest()}"
    previous["files"].sort(key=lambda entry: entry["repo_path"])
    case_path = tmp_path / "qa/cases/items/case.yaml"
    case_path.parent.mkdir(parents=True, exist_ok=True)
    case_path.write_text(yaml.safe_dump(reviewed_cases(family), sort_keys=False), encoding="utf-8")
    selected = CodegenInputV1.model_validate({**business, "local_round": 1, "codegen_output": previous})
    assert selected.local_round > 0
    assert selected.codegen_output == previous

    selected = selected.model_copy(update={"reviewed_cases": business["reviewed_cases"]})
    outcome = await execute_task(
        codegen_prepare_handler(family),
        cast(dict[str, JSONValue], selected.model_dump(mode="json")),
        tmp_path,
        binding_data=BINDING,
    )
    assert outcome.status == "succeeded", outcome.failure
    request = AgentRunRequest.model_validate(outcome.output)
    context = thaw_json(request.instructions[-1].json_content)
    assert isinstance(context, dict)
    assert context["codegen_scope"]["family"] == family
    assert context["baseline_files"] == sorted((test_path, data_path))


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.asyncio
async def test_codegen_review_prepare_receives_codegen_output(family: str, tmp_path: Path) -> None:
    business = plan_input(family)
    test_path, data_path = locked_oracle_paths(family)
    output = codegen_result(files=[test_path, data_path], family=family)
    write_codegen_artifacts(tmp_path, family)
    (tmp_path / "qa/proposal.md").parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / "qa/proposal.md").write_text("# Proposal\n", encoding="utf-8")
    case_path = tmp_path / "qa/cases/items/case.yaml"
    case_path.parent.mkdir(parents=True, exist_ok=True)
    case_path.write_text(yaml.safe_dump(reviewed_cases(family), sort_keys=False), encoding="utf-8")
    payload = review_prepare_input(family, {**business, "codegen_output": output})
    outcome = await execute_task(
        review_prepare_handler(family),
        payload,
        tmp_path,
        binding_data=BINDING,
    )
    assert outcome.status == "succeeded", outcome.failure
    request = AgentRunRequest.model_validate(outcome.output)
    extras = [thaw_json(part.json_content) for part in request.instructions if part.json_content is not None]
    assert any(isinstance(part, dict) and part.get("codegen_output") == output for part in extras)
