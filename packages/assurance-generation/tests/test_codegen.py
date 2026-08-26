from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import cast

import pytest
import yaml

from agent_runtime_contracts import AgentRunRequest
from graph_engine.canonical import canonical_json_bytes
from tests.phase5.test_change_local_output_routing import dual_roots, execute_task

from assurance_generation.operations.codegen import (
    CodegenFinalizeHandler,
    codegen_finalize_handler,
    codegen_fix_finalize_handler,
    codegen_fix_prepare_handler,
    codegen_prepare_handler,
    validate_codegen_fix_input,
    validate_codegen_input,
)
from assurance_generation.operations.planning import InputError
from codegen_fixtures import (  # pyright: ignore[reportMissingImports]
    FAMILIES,
    codegen_fix_input,
    codegen_input,
    codegen_result,
    family_case_id,
    family_test_file,
    fake_agent_result,
    mapping_document,
    staged_generated_file,
)
from planning_fixtures import BINDING as PLAN_BINDING  # pyright: ignore[reportMissingImports]
from planning_fixtures import VALID_LEAFS, reviewed_cases  # pyright: ignore[reportMissingImports]

CHANGE_ID = "CH-DEMO-001"


def _staged_path(family: str, target: str, change_id: str = CHANGE_ID) -> str:
    return f"qa/changes/{change_id}/generated/{family}/files/{target}"


def _write_generated(root: Path, family: str, target: str, content: bytes = b"test\n") -> str:
    path = root.joinpath(*_staged_path(family, target).split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _write_reviewed_cases(tmp_path: Path, family: str) -> None:
    path = tmp_path / "qa/changes/CH-DEMO-001/cases/items/case.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(reviewed_cases(family), sort_keys=False), encoding="utf-8")


def _write_plan_mapping(workspace: Path, family: str, targets: list[str]) -> None:
    entries = [
        {
            "case_id": f"TC_{family.upper()}_{index:03d}",
            "symbol": f"test_tc_{family}_{index:03d}__happy_path",
            "target_file": target,
        }
        for index, target in enumerate(targets, start=1)
    ]
    document = mapping_document(family, target_file=targets[0])
    document["entries"] = entries
    path = workspace / f"qa/changes/CH-DEMO-001/plans/{family}-codegen-mapping.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")


def test_codegen_input_rejects_reviewed_plan_for_a_different_change(tmp_path: Path) -> None:
    payload = codegen_input("api")
    payload["reviewed_plan"]["change_id"] = "CH-OTHER-001"

    with pytest.raises(InputError, match="reviewed plan change_id"):
        validate_codegen_input(payload, "api", tmp_path)


def test_codegen_fix_input_rejects_reviewed_plan_for_a_different_change() -> None:
    payload = codegen_fix_input("api")
    payload["reviewed_plan"]["change_id"] = "CH-OTHER-001"

    with pytest.raises(InputError, match="reviewed plan change_id"):
        validate_codegen_fix_input(payload, "api")


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
async def test_codegen_prepare_accepts_product_artifact_lock(family: str, tmp_path: Path) -> None:
    payload = codegen_input(family)
    payload["artifact_paths"] = ["qa/archive", "qa/cases", "qa/changes", "tests"]

    prepared = await execute_task(
        codegen_prepare_handler(family),
        payload,
        tmp_path,
        binding_data=PLAN_BINDING,
    )

    assert prepared.status == "succeeded"


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
    assert request.workspace.scope_id == CHANGE_ID
    assert len(request.instructions) == 5
    skill, persona, plan, cases, context = request.instructions
    assert f"{family} codegen" in (skill.text_content or "").lower()
    assert f"qa/changes/<change-id>/generated/{family}/files/" in (skill.text_content or "")
    assert "test-author persona" in (persona.text_content or "").lower()
    assert plan.media_type == "application/json"
    assert cases.media_type == "application/json"
    assert context.media_type == "application/json"
    plan_payload = cast(dict[str, object], plan.json_content)
    context_payload = cast(dict[str, object], context.json_content)
    assert plan_payload["family"] == family
    assert context_payload["baseline_tree_id"] == "0" * 64
    assert context_payload["generated_files_root"] == (f"qa/changes/CH-DEMO-001/generated/{family}/files")
    encoded = request.canonical_bytes().decode("utf-8").lower()
    assert "opencode" not in encoded
    assert "cursor" not in encoded
    assert "assurance_agent" not in encoded

    schema = request.result_contract.schema_document
    assert schema is not None
    properties = cast(dict[str, object], schema["properties"])
    assert "needs_fix" not in properties
    files_schema = cast(dict[str, object], properties["files"])
    assert files_schema["minItems"] == 1
    definitions = cast(dict[str, object], schema["$defs"])
    file_definition = cast(dict[str, object], definitions["CodegenGeneratedFileAuthoring"])
    file_properties = cast(dict[str, object], file_definition["properties"])
    assert "content_sha256" not in file_properties


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.asyncio
async def test_codegen_prepare_authorizes_exact_staged_mapping_targets(family: str, tmp_path: Path) -> None:
    mapped = family_test_file(family)
    extra = f"tests/{family}/test_unmapped.py" if family != "performance" else "tests/perf/test_unmapped.py"
    _write_plan_mapping(tmp_path, family, [mapped])

    prepared = await execute_task(
        codegen_prepare_handler(family),
        codegen_input(family),
        tmp_path,
        binding_data=PLAN_BINDING,
    )

    assert prepared.status == "succeeded"
    request = AgentRunRequest.model_validate(prepared.output)
    staged = staged_generated_file(family, mapped)
    assert staged in request.workspace.allowed_outputs
    assert staged_generated_file(family, extra) not in request.workspace.allowed_outputs
    assert all("**" not in path for path in request.workspace.allowed_outputs)
    assert all(not path.startswith("tests/") for path in request.workspace.allowed_outputs)
    assert (
        f"qa/changes/CH-DEMO-001/codegen/{family}-generated-files.json" in request.workspace.allowed_outputs
    )


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.asyncio
async def test_codegen_prepare_omits_generated_writes_when_mapping_is_absent(
    family: str, tmp_path: Path
) -> None:
    prepared = await execute_task(
        codegen_prepare_handler(family),
        codegen_input(family),
        tmp_path,
        binding_data=PLAN_BINDING,
    )

    assert prepared.status == "succeeded"
    request = AgentRunRequest.model_validate(prepared.output)
    assert request.workspace.allowed_outputs == (
        f"qa/changes/CH-DEMO-001/codegen/{family}-codegen-summary.md",
        f"qa/changes/CH-DEMO-001/codegen/{family}-generated-files.json",
    )
    assert all(
        not path.startswith("qa/changes/CH-DEMO-001/generated/") for path in request.workspace.allowed_outputs
    )


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.asyncio
async def test_codegen_prepare_hydrates_missing_business_input_from_workspace(
    family: str, tmp_path: Path
) -> None:
    _write_reviewed_cases(tmp_path, family)
    prepared = await execute_task(
        codegen_prepare_handler(family),
        {
            "change_id": "CH-DEMO-001",
            "capability_leafs": list(VALID_LEAFS),
        },
        tmp_path,
        binding_data=PLAN_BINDING,
    )
    assert prepared.status == "succeeded"
    request = AgentRunRequest.model_validate(prepared.output)
    plan = cast(dict[str, object], request.instructions[2].json_content)
    context = cast(dict[str, object], request.instructions[4].json_content)
    assert plan["family"] == family
    assert plan["case_ids"] == (f"TC_{family.upper()}_001",)
    assert "baseline_tree_id" not in context


@pytest.mark.asyncio
async def test_codegen_finalize_rejects_empty_files_when_mapping_is_live(tmp_path: Path) -> None:
    project, write_root = dual_roots(tmp_path)
    _write_generated(write_root, "api", "tests/api/test_users.py")
    executed = await execute_task(
        codegen_finalize_handler("api"),
        fake_agent_result(codegen_result(files=[])),
        project,
        write_root=write_root,
    )
    assert executed.status == "failed"
    assert executed.failure is not None
    assert executed.failure.kind == "invalid_output"
    assert executed.failure.retryable is True


@pytest.mark.asyncio
async def test_codegen_finalize_rejects_partial_mapping_listing(tmp_path: Path) -> None:
    first = "tests/api/test_users.py"
    second = "tests/api/test_orders.py"
    project, write_root = dual_roots(tmp_path)
    _write_generated(write_root, "api", first)
    _write_generated(write_root, "api", second)
    payload = codegen_result(files=[first])
    payload["mapping"]["entries"] = [
        payload["mapping"]["entries"][0],
        {
            "case_id": "TC_API_002",
            "symbol": "test_tc_api_002__happy_path",
            "target_file": second,
        },
    ]
    executed = await execute_task(
        codegen_finalize_handler("api"),
        fake_agent_result(payload),
        project,
        write_root=write_root,
    )
    assert executed.status == "failed"
    assert executed.failure is not None
    assert executed.failure.kind == "invalid_output"
    assert executed.failure.retryable is True


@pytest.mark.asyncio
async def test_codegen_finalize_keeps_support_as_extra_hashed_entry(tmp_path: Path) -> None:
    mapped = family_test_file("api")
    support = "tests/api/conftest.py"
    project, write_root = dual_roots(tmp_path)
    mapped_digest = _write_generated(write_root, "api", mapped)
    support_digest = _write_generated(write_root, "api", support, b"fixture\n")
    payload = codegen_result(files=[mapped])
    payload["files"].append(
        {
            "repo_path": support,
            "disposition": "generated",
            "role": "support",
            "case_ids": [],
        }
    )
    executed = await execute_task(
        codegen_finalize_handler("api"),
        fake_agent_result(payload),
        project,
        write_root=write_root,
    )
    assert executed.status == "succeeded"
    output = cast(dict[str, object], executed.output)
    files = {item["repo_path"]: item for item in cast(list[dict[str, object]], output["files"])}
    assert files[mapped]["content_sha256"] == mapped_digest
    assert files[mapped]["role"] == "test_entry"
    assert files[mapped]["case_ids"] == [family_case_id("api")]
    assert files[support]["content_sha256"] == support_digest
    assert files[support]["role"] == "support"
    assert files[support]["case_ids"] == []
    mapping = cast(dict[str, object], output["mapping"])
    targets = [item["target_file"] for item in cast(list[dict[str, object]], mapping["entries"])]
    assert targets == [mapped]


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.asyncio
async def test_codegen_finalize_authenticates_workspace_bytes(family: str, tmp_path: Path) -> None:
    relative = family_test_file(family)
    project, write_root = dual_roots(tmp_path)
    digest = _write_generated(write_root, family, relative)
    executed = await execute_task(
        codegen_finalize_handler(family),
        fake_agent_result(codegen_result(files=[relative], family=family)),
        project,
        write_root=write_root,
    )
    assert executed.status == "succeeded"
    output = cast(dict[str, object], executed.output)
    assert output["needs_fix"] is False
    assert output["layer"] == family
    files = cast(list[dict[str, object]], output["files"])
    assert files[0]["repo_path"] == relative
    assert files[0]["content_sha256"] == digest
    assert files[0]["case_ids"] == [family_case_id(family)]


@pytest.mark.asyncio
async def test_codegen_finalize_rejects_manifest_that_differs_from_result(tmp_path: Path) -> None:
    relative = family_test_file("api")
    project, write_root = dual_roots(tmp_path)
    _write_generated(write_root, "api", relative)
    payload = codegen_result(files=[relative])
    manifest = dict(payload)
    manifest["required_capabilities"] = ["auth.session.create"]
    manifest_path = write_root / "qa/changes/CH-DEMO-001/codegen/api-generated-files.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    executed = await execute_task(
        codegen_finalize_handler("api"),
        fake_agent_result(payload),
        project,
        write_root=write_root,
    )

    assert executed.status == "failed"
    assert executed.failure is not None
    assert executed.failure.kind == "invalid_output"
    assert "does not match" in executed.failure.message


@pytest.mark.asyncio
async def test_codegen_finalize_accepts_files_below_declared_artifact_roots(tmp_path: Path) -> None:
    relative = family_test_file("api")
    project, write_root = dual_roots(tmp_path)
    _write_generated(write_root, "api", relative)
    executed = await execute_task(
        codegen_finalize_handler("api"),
        fake_agent_result(
            codegen_result(files=[relative]),
            artifact_paths=["qa/changes", "tests"],
        ),
        project,
        write_root=write_root,
    )
    assert executed.status == "succeeded"


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.asyncio
async def test_codegen_finalize_rejects_wrong_family(family: str, tmp_path: Path) -> None:
    other = "e2e" if family == "api" else "api"
    relative = family_test_file(other)
    project, write_root = dual_roots(tmp_path)
    _write_generated(write_root, other, relative)
    payload = codegen_result(files=[relative], family=other)
    executed = await execute_task(
        codegen_finalize_handler(family),
        fake_agent_result(payload),
        project,
        write_root=write_root,
    )
    assert executed.status == "failed"
    assert executed.failure is not None
    assert executed.failure.kind == "invalid_output"
    assert executed.failure.retryable is True


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.asyncio
async def test_codegen_finalize_rejects_unknown_leaf(family: str, tmp_path: Path) -> None:
    relative = family_test_file(family)
    project, write_root = dual_roots(tmp_path)
    _write_generated(write_root, family, relative)
    payload = codegen_result(files=[relative], family=family, required_capabilities=["auth.fake"])
    executed = await execute_task(
        codegen_finalize_handler(family),
        fake_agent_result(payload),
        project,
        write_root=write_root,
    )
    assert executed.status == "failed"
    assert executed.failure is not None
    assert executed.failure.kind == "invalid_output"


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.asyncio
async def test_codegen_finalize_rejects_undeclared_file(family: str, tmp_path: Path) -> None:
    relative = family_test_file(family)
    extra = "tests/unmapped_test.py"
    project, write_root = dual_roots(tmp_path)
    _write_generated(write_root, family, relative)
    _write_generated(write_root, family, extra)
    payload = codegen_result(files=[relative, extra], family=family)
    executed = await execute_task(
        codegen_finalize_handler(family),
        fake_agent_result(payload, artifact_paths=[relative]),
        project,
        write_root=write_root,
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
    assert request.workspace.scope_id == CHANGE_ID
    skill, _persona, _plan, _cases, context = request.instructions
    assert f"{family} codegen fix" in (skill.text_content or "").lower()
    context_payload = cast(dict[str, object], context.json_content)
    assert list(cast(list[str], context_payload["allowed_paths"])) == [family_test_file(family)]
    proposal = cast(dict[str, object], context_payload["approved_proposal"])
    assert proposal["status"] == "approved"
    staged = staged_generated_file(family)
    assert staged in request.workspace.allowed_outputs
    assert all("**" not in path for path in request.workspace.allowed_outputs)
    assert all(not path.startswith("tests/") for path in request.workspace.allowed_outputs)


@pytest.mark.parametrize("family", ("api", "e2e"))
@pytest.mark.asyncio
async def test_codegen_fix_finalize_rejects_file_outside_allowed_set(family: str, tmp_path: Path) -> None:
    allowed = family_test_file(family)
    extra = "tests/testdata/domain/users.py"
    project, write_root = dual_roots(tmp_path)
    _write_generated(write_root, family, allowed)
    _write_generated(write_root, family, extra)
    payload = codegen_result(files=[allowed, extra], family=family)
    executed = await execute_task(
        codegen_fix_finalize_handler(family),
        fake_agent_result(
            payload,
            allowed_paths=[allowed],
            baseline_tree_id="0" * 64,
        ),
        project,
        write_root=write_root,
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


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.asyncio
async def test_codegen_finalize_rejects_bytes_written_only_under_tests(family: str, tmp_path: Path) -> None:
    relative = family_test_file(family)
    project, write_root = dual_roots(tmp_path)
    direct = write_root.joinpath(*relative.split("/"))
    direct.parent.mkdir(parents=True, exist_ok=True)
    direct.write_bytes(b"test\n")
    executed = await execute_task(
        codegen_finalize_handler(family),
        fake_agent_result(codegen_result(files=[relative], family=family)),
        project,
        write_root=write_root,
    )
    assert executed.status == "failed"
    assert executed.failure is not None
    assert executed.failure.kind == "invalid_output"
    assert not (project / relative).exists()


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.asyncio
async def test_codegen_finalize_rejects_target_outside_family_policy(family: str, tmp_path: Path) -> None:
    other = "e2e" if family == "api" else "api"
    foreign = family_test_file(other)
    project, write_root = dual_roots(tmp_path)
    _write_generated(write_root, family, foreign)
    payload = codegen_result(files=[foreign], family=family)
    payload["mapping"]["entries"][0]["target_file"] = foreign
    executed = await execute_task(
        codegen_finalize_handler(family),
        fake_agent_result(payload),
        project,
        write_root=write_root,
    )
    assert executed.status == "failed"
    assert executed.failure is not None
    assert executed.failure.kind == "invalid_output"


@pytest.mark.asyncio
async def test_e2e_codegen_skill_reads_family_prefixed_review() -> None:
    from assurance_generation.resource_loader import resource_text

    skill = resource_text("skills/aa-e2e-codegen/SKILL.md")
    assert "qa/changes/<change-id>/review/e2e-plan-review.json" in skill
    assert "review/plan-review.json" not in skill
