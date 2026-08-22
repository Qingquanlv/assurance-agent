from __future__ import annotations

import hashlib
from pathlib import Path
from typing import cast

import pytest

from agent_runtime_contracts import AgentRunRequest
from graph_engine.canonical import canonical_json_bytes
from tests.phase4.conformance import execute_task

from assurance_generation.operations.codegen import (
    CodegenFinalizeHandler,
    codegen_finalize_handler,
    codegen_fix_finalize_handler,
    codegen_fix_prepare_handler,
    codegen_prepare_handler,
)
from codegen_fixtures import (  # pyright: ignore[reportMissingImports]
    FAMILIES,
    codegen_fix_input,
    codegen_input,
    codegen_result,
    family_case_id,
    family_test_file,
    fake_agent_result,
)
from planning_fixtures import BINDING as PLAN_BINDING  # pyright: ignore[reportMissingImports]


def _write_generated(tmp_path: Path, relative: str, content: bytes = b"test\n") -> str:
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


@pytest.mark.asyncio
async def test_codegen_finalize_rejects_claimed_but_missing_file(tmp_path: Path) -> None:
    outcome = await execute_task(
        CodegenFinalizeHandler("api"),
        fake_agent_result(codegen_result(files=["tests/api/test_users.py"])),
        tmp_path,
    )
    assert outcome.failure is not None
    assert outcome.failure.kind == "invalid_output"


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.asyncio
async def test_codegen_prepare_is_deterministic_for_every_family(family: str, tmp_path: Path) -> None:
    handler = codegen_prepare_handler(family)
    first = await execute_task(handler, codegen_input(family), tmp_path, binding_data=PLAN_BINDING)
    second = await execute_task(handler, codegen_input(family), tmp_path, binding_data=PLAN_BINDING)
    assert canonical_json_bytes(first.output) == canonical_json_bytes(second.output)


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.asyncio
async def test_codegen_prepare_uses_reviewed_plan_and_baseline(family: str, tmp_path: Path) -> None:
    prepared = await execute_task(
        codegen_prepare_handler(family),
        codegen_input(family),
        tmp_path,
        binding_data=PLAN_BINDING,
    )
    assert prepared.status == "succeeded"
    request = AgentRunRequest.model_validate(prepared.output)
    assert len(request.instructions) == 5
    skill, persona, plan, cases, context = request.instructions
    assert f"{family} codegen" in (skill.text_content or "").lower()
    assert "test-author persona" in (persona.text_content or "").lower()
    assert plan.media_type == "application/json"
    assert cases.media_type == "application/json"
    assert context.media_type == "application/json"
    plan_payload = cast(dict[str, object], plan.json_content)
    context_payload = cast(dict[str, object], context.json_content)
    assert plan_payload["family"] == family
    assert context_payload["baseline_tree_id"] == "0" * 64
    encoded = request.canonical_bytes().decode("utf-8").lower()
    assert "opencode" not in encoded
    assert "cursor" not in encoded
    assert "assurance_agent" not in encoded


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.asyncio
async def test_codegen_finalize_authenticates_workspace_bytes(family: str, tmp_path: Path) -> None:
    relative = family_test_file(family)
    digest = _write_generated(tmp_path, relative)
    executed = await execute_task(
        codegen_finalize_handler(family),
        fake_agent_result(codegen_result(files=[relative], family=family)),
        tmp_path,
    )
    assert executed.status == "succeeded"
    output = cast(dict[str, object], executed.output)
    assert output["layer"] == family
    files = cast(list[dict[str, object]], output["files"])
    assert files[0]["repo_path"] == relative
    assert files[0]["content_sha256"] == digest
    assert files[0]["case_ids"] == [family_case_id(family)]


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.asyncio
async def test_codegen_finalize_rejects_wrong_family(family: str, tmp_path: Path) -> None:
    other = "e2e" if family == "api" else "api"
    relative = family_test_file(other)
    _write_generated(tmp_path, relative)
    payload = codegen_result(files=[relative], family=other)
    executed = await execute_task(
        codegen_finalize_handler(family),
        fake_agent_result(payload),
        tmp_path,
    )
    assert executed.status == "failed"
    assert executed.failure is not None
    assert executed.failure.kind == "invalid_output"
    assert executed.failure.retryable is True


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.asyncio
async def test_codegen_finalize_rejects_unknown_leaf(family: str, tmp_path: Path) -> None:
    relative = family_test_file(family)
    _write_generated(tmp_path, relative)
    payload = codegen_result(files=[relative], family=family, required_capabilities=["auth.fake"])
    executed = await execute_task(
        codegen_finalize_handler(family),
        fake_agent_result(payload),
        tmp_path,
    )
    assert executed.status == "failed"
    assert executed.failure is not None
    assert executed.failure.kind == "invalid_output"


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.asyncio
async def test_codegen_finalize_rejects_undeclared_file(family: str, tmp_path: Path) -> None:
    relative = family_test_file(family)
    extra = "tests/unmapped_test.py"
    _write_generated(tmp_path, relative)
    _write_generated(tmp_path, extra)
    payload = codegen_result(files=[relative, extra], family=family)
    executed = await execute_task(
        codegen_finalize_handler(family),
        fake_agent_result(payload, artifact_paths=[relative]),
        tmp_path,
    )
    assert executed.status == "failed"
    assert executed.failure is not None
    assert executed.failure.kind == "invalid_output"


@pytest.mark.parametrize("family", ("api", "e2e"))
@pytest.mark.asyncio
async def test_codegen_fix_prepare_includes_allowed_paths(family: str, tmp_path: Path) -> None:
    prepared = await execute_task(
        codegen_fix_prepare_handler(family),
        codegen_fix_input(family),
        tmp_path,
        binding_data=PLAN_BINDING,
    )
    assert prepared.status == "succeeded"
    request = AgentRunRequest.model_validate(prepared.output)
    skill, _persona, _plan, _cases, context = request.instructions
    assert f"{family} codegen fix" in (skill.text_content or "").lower()
    context_payload = cast(dict[str, object], context.json_content)
    assert list(cast(list[str], context_payload["allowed_paths"])) == [family_test_file(family)]
    proposal = cast(dict[str, object], context_payload["approved_proposal"])
    assert proposal["status"] == "approved"


@pytest.mark.parametrize("family", ("api", "e2e"))
@pytest.mark.asyncio
async def test_codegen_fix_finalize_rejects_file_outside_allowed_set(family: str, tmp_path: Path) -> None:
    allowed = family_test_file(family)
    extra = "tests/testdata/domain/users.py"
    _write_generated(tmp_path, allowed)
    _write_generated(tmp_path, extra)
    payload = codegen_result(files=[allowed, extra], family=family)
    executed = await execute_task(
        codegen_fix_finalize_handler(family),
        fake_agent_result(
            payload,
            allowed_paths=[allowed],
            baseline_tree_id="0" * 64,
        ),
        tmp_path,
    )
    assert executed.status == "failed"
    assert executed.failure is not None
    assert executed.failure.kind == "invalid_output"


@pytest.mark.parametrize("family", ("fuzz", "performance"))
def test_codegen_fix_has_no_handler_for_fuzz_or_performance(family: str) -> None:
    with pytest.raises(ValueError, match="codegen-fix has no handler"):
        codegen_fix_prepare_handler(family)
    with pytest.raises(ValueError, match="codegen-fix has no handler"):
        codegen_fix_finalize_handler(family)


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.asyncio
async def test_codegen_prepare_rejects_routing_marker(family: str, tmp_path: Path) -> None:
    binding = {
        **PLAN_BINDING,
        "execution": {
            **cast(dict[str, object], PLAN_BINDING["execution"]),
            "provider_model": "primary,fallback",
        },
    }
    prepared = await execute_task(
        codegen_prepare_handler(family),
        codegen_input(family),
        tmp_path,
        binding_data=binding,
    )
    assert prepared.status == "failed"
    assert prepared.failure is not None
    assert prepared.failure.kind == "invalid_input"
    assert prepared.failure.retryable is False


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.asyncio
async def test_codegen_finalize_rejects_malformed_input(family: str, tmp_path: Path) -> None:
    executed = await execute_task(codegen_finalize_handler(family), {"agent_result": {}}, tmp_path)
    assert executed.status == "failed"
    assert executed.failure is not None
    assert executed.failure.kind == "invalid_input"
    assert executed.failure.retryable is False
