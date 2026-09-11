from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import cast

import pytest
import yaml
from pydantic import ValidationError

from agent_runtime_contracts import AgentRunRequest
from graph_engine.canonical import canonical_json_bytes
from tests.product.test_change_local_output_routing import dual_roots, execute_task

from assurance_generation.contracts.codegen import durable_test_path, family_allows_target
from assurance_generation.operations.codegen import (
    CodegenFinalizeHandler,
    codegen_finalize_handler,
    codegen_prepare_handler,
    validate_codegen_input,
)
from assurance_generation.operations.planning import Family, InputError
from codegen_fixtures import (  # pyright: ignore[reportMissingImports]
    FAMILIES,
    codegen_input,
    codegen_result,
    durable_oracle_path,
    family_case_id,
    fake_agent_result,
    mapping_document,
)
from planning_fixtures import BINDING as PLAN_BINDING  # pyright: ignore[reportMissingImports]
from planning_fixtures import PLAN_DIGEST, PLAN_REF  # pyright: ignore[reportMissingImports]
from planning_fixtures import VALID_LEAFS, reviewed_cases  # pyright: ignore[reportMissingImports]

CHANGE_ID = "CH-DEMO-001"


def test_durable_test_path_requires_qa_tests_prefix() -> None:
    assert durable_test_path("qa/tests/api/test_dept.py") == "qa/tests/api/test_dept.py"
    import pytest

    with pytest.raises(ValueError):
        durable_test_path("tests/api/test_dept.py")
    with pytest.raises(ValueError):
        durable_test_path("qa/changes/CH-1/generated/api/files/tests/api/test_dept.py")


def test_family_allows_only_qa_tests_roots() -> None:
    assert family_allows_target("api", "qa/tests/api/test_dept.py")
    assert not family_allows_target("api", "tests/api/test_dept.py")


def _write_generated(root: Path, family: str, target: str, content: bytes = b"test\n") -> str:
    relative = target if target.startswith("qa/tests/") else durable_oracle_path(family=family)
    path = root.joinpath(*relative.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _write_manifest(root: Path, payload: Mapping[str, object]) -> None:
    family = cast(str, payload["layer"])
    path = root / f"qa/results/codegen/{family}-generated-files.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_reviewed_cases(tmp_path: Path, family: str) -> None:
    path = tmp_path / "qa/cases/items/case.yaml"
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


@pytest.mark.parametrize("family", FAMILIES)
def test_codegen_input_rejects_case_scope_drift(family: Family, tmp_path: Path) -> None:
    payload = codegen_input(family)
    payload["reviewed_plan"]["case_ids"] = ["TC_UNREVIEWED_001"]
    payload["reviewed_plan"]["coverage"][0]["case_id"] = "TC_UNREVIEWED_001"
    with pytest.raises(InputError, match="reviewed plan case_ids"):
        validate_codegen_input(payload, family, tmp_path)


@pytest.mark.parametrize(
    "field,value", [("endpoint", "GET /unreviewed"), ("p95_ms", 2000), ("error_rate_max", 0.9)]
)
def test_performance_codegen_rejects_scenario_drift_from_cases(
    field: str, value: object, tmp_path: Path
) -> None:
    payload = codegen_input("performance")
    payload["reviewed_plan"]["performance_scenarios"][0][field] = value
    with pytest.raises(InputError, match="performance scenarios must match reviewed cases"):
        validate_codegen_input(payload, "performance", tmp_path)


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.asyncio
async def test_codegen_mapping_order_has_no_semantic_effect(family: str, tmp_path: Path) -> None:
    target = durable_oracle_path(family=family)
    project, write_root = dual_roots(tmp_path)
    _write_generated(write_root, family, target)
    authored = codegen_result(files=[target], family=family)
    second_id = f"TC_{family.upper()}_002"
    authored["mapping"]["entries"].append(
        {"case_id": second_id, "symbol": "test_second_case", "target_file": target}
    )
    authored["files"][0]["case_ids"].append(second_id)
    _write_manifest(write_root, authored)
    payload = fake_agent_result(authored)
    payload["reviewed_mapping"] = {
        **authored["mapping"],
        "entries": list(reversed(authored["mapping"]["entries"])),
    }
    outcome = await execute_task(codegen_finalize_handler(family), payload, project, write_root=write_root)
    assert outcome.status == "succeeded", outcome.failure


@pytest.mark.asyncio
async def test_codegen_finalize_rejects_claimed_but_missing_file(tmp_path: Path) -> None:
    outcome = await execute_task(
        CodegenFinalizeHandler("api"),
        fake_agent_result(codegen_result(files=[durable_oracle_path()])),
        tmp_path,
    )
    assert outcome.failure is not None
    assert outcome.failure.kind == "invalid_output"


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.asyncio
async def test_codegen_prepare_is_deterministic_for_every_family(family: str, tmp_path: Path) -> None:
    _write_plan_mapping(tmp_path, family, [durable_oracle_path(family=family)])
    handler = codegen_prepare_handler(family)
    first = await execute_task(handler, codegen_input(family), tmp_path, binding_data=PLAN_BINDING)
    second = await execute_task(handler, codegen_input(family), tmp_path, binding_data=PLAN_BINDING)
    assert first.status == second.status == "succeeded"
    assert canonical_json_bytes(first.output) == canonical_json_bytes(second.output)


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.asyncio
async def test_codegen_prepare_accepts_product_artifact_lock(family: str, tmp_path: Path) -> None:
    _write_plan_mapping(tmp_path, family, [durable_oracle_path(family=family)])
    payload = codegen_input(family)
    payload["artifact_paths"] = ["qa/archive", "qa/cases", "qa/changes", "qa/tests"]

    prepared = await execute_task(
        codegen_prepare_handler(family),
        payload,
        tmp_path,
        binding_data=PLAN_BINDING,
    )

    assert prepared.status == "succeeded"


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.asyncio
async def test_codegen_prepare_uses_reviewed_plan_and_constraints(family: str, tmp_path: Path) -> None:
    _write_plan_mapping(tmp_path, family, [durable_oracle_path(family=family)])
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
    assert "qa/tests/" in (skill.text_content or "")
    assert "test-author persona" in (persona.text_content or "").lower()
    assert plan.media_type == "application/json"
    assert cases.media_type == "application/json"
    assert context.media_type == "application/json"
    plan_payload = cast(dict[str, object], plan.json_content)
    context_payload = cast(dict[str, object], context.json_content)
    assert plan_payload["family"] == family
    assert context_payload["generated_files_root"] == "qa/tests"
    encoded = request.canonical_bytes().decode("utf-8").lower()
    assert "opencode" not in encoded
    assert "cursor" not in encoded
    assert "assurance_agent" not in encoded

    schema = request.result_contract.schema_document
    assert isinstance(schema, Mapping)
    properties = schema["properties"]
    assert isinstance(properties, Mapping)
    assert "needs_fix" not in properties
    files_schema = properties["files"]
    assert isinstance(files_schema, Mapping)
    assert files_schema["minItems"] == 1
    definitions = schema["$defs"]
    assert isinstance(definitions, Mapping)
    file_definition = definitions["CodegenGeneratedFileAuthoring"]
    assert isinstance(file_definition, Mapping)
    file_properties = file_definition["properties"]
    assert isinstance(file_properties, Mapping)
    assert "content_sha256" not in file_properties


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.asyncio
async def test_codegen_prepare_authorizes_exact_staged_mapping_targets(family: str, tmp_path: Path) -> None:
    mapped = durable_oracle_path(family=family)
    extra = (
        f"qa/tests/{family}/test_unmapped.py" if family != "performance" else "qa/tests/perf/test_unmapped.py"
    )
    _write_plan_mapping(tmp_path, family, [mapped])

    prepared = await execute_task(
        codegen_prepare_handler(family),
        codegen_input(family),
        tmp_path,
        binding_data=PLAN_BINDING,
    )

    assert prepared.status == "succeeded"
    request = AgentRunRequest.model_validate(prepared.output)
    assert mapped in request.workspace.allowed_outputs
    context_payload = cast(dict[str, object], request.instructions[4].json_content)
    assert context_payload["allowed_outputs"] == request.workspace.allowed_outputs
    assert extra not in request.workspace.allowed_outputs
    assert all("**" not in path for path in request.workspace.allowed_outputs)
    assert all(not path.startswith("tests/") for path in request.workspace.allowed_outputs)
    assert f"qa/results/codegen/{family}-generated-files.json" in request.workspace.allowed_outputs


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.asyncio
async def test_codegen_prepare_rejects_missing_mapping(family: str, tmp_path: Path) -> None:
    prepared = await execute_task(
        codegen_prepare_handler(family),
        codegen_input(family),
        tmp_path,
        binding_data=PLAN_BINDING,
    )

    assert prepared.status == "failed"
    assert prepared.failure is not None
    assert "closed codegen mapping is missing" in prepared.failure.message


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.asyncio
async def test_codegen_prepare_rejects_missing_reviewed_plan(family: str, tmp_path: Path) -> None:
    _write_reviewed_cases(tmp_path, family)
    prepared = await execute_task(
        codegen_prepare_handler(family),
        {
            "change_id": "CH-DEMO-001",
            "plan_digest": PLAN_DIGEST,
            "plan_ref": PLAN_REF,
            "capability_leafs": list(VALID_LEAFS),
        },
        tmp_path,
        binding_data=PLAN_BINDING,
    )
    assert prepared.status == "failed"
    assert prepared.failure is not None
    assert "reviewed_plan" in prepared.failure.message


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.asyncio
async def test_codegen_finalize_rejects_symbol_drift_from_reviewed_mapping(
    family: str, tmp_path: Path
) -> None:
    target = durable_oracle_path(family=family)
    project, write_root = dual_roots(tmp_path)
    _write_generated(write_root, family, target)
    authored = codegen_result(files=[target], family=family)
    authored["mapping"]["entries"][0]["symbol"] = "different_unreviewed_symbol"
    _write_manifest(write_root, authored)
    payload = fake_agent_result(authored)
    payload["reviewed_mapping"] = mapping_document(family)
    outcome = await execute_task(codegen_finalize_handler(family), payload, project, write_root=write_root)
    assert outcome.status == "failed"
    assert outcome.failure is not None
    assert outcome.failure.kind == "invalid_output"
    assert "mapping must exactly match the reviewed plan" in outcome.failure.message


@pytest.mark.parametrize("field", ("reviewed_mapping", "required_capabilities"))
@pytest.mark.asyncio
async def test_codegen_missing_trusted_input_does_not_retry_the_agent(field: str, tmp_path: Path) -> None:
    payload = fake_agent_result(codegen_result(files=[durable_oracle_path()]))
    del payload[field]
    outcome = await execute_task(codegen_finalize_handler("api"), payload, tmp_path)
    assert outcome.failure is not None
    assert outcome.failure.kind == "invalid_input"
    assert outcome.failure.retryable is False


@pytest.mark.asyncio
async def test_codegen_finalize_rejects_empty_files_when_mapping_is_live(tmp_path: Path) -> None:
    project, write_root = dual_roots(tmp_path)
    _write_generated(write_root, "api", durable_oracle_path())
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
    first = durable_oracle_path()
    second = "qa/tests/api/test_orders.py"
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
    finalize_input = fake_agent_result(payload)
    finalize_input["reviewed_mapping"] = payload["mapping"]
    executed = await execute_task(
        codegen_finalize_handler("api"),
        finalize_input,
        project,
        write_root=write_root,
    )
    assert executed.status == "failed"
    assert executed.failure is not None
    assert executed.failure.kind == "invalid_output"
    assert executed.failure.retryable is True
    assert f"codegen mapping is missing: {second}" in executed.failure.message


@pytest.mark.asyncio
async def test_codegen_finalize_keeps_support_as_extra_hashed_entry(tmp_path: Path) -> None:
    mapped = durable_oracle_path()
    support = "qa/tests/api/conftest.py"
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
    _write_manifest(write_root, payload)
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
    relative = durable_oracle_path(family=family)
    project, write_root = dual_roots(tmp_path)
    digest = _write_generated(write_root, family, relative)
    payload = codegen_result(files=[relative], family=family)
    _write_manifest(write_root, payload)
    executed = await execute_task(
        codegen_finalize_handler(family),
        fake_agent_result(payload),
        project,
        write_root=write_root,
    )
    assert executed.status == "succeeded"
    output = cast(dict[str, object], executed.output)
    assert output["schema_version"] == "1"
    assert {"verdict", "repair", "needs_fix"}.isdisjoint(output)
    assert output["layer"] == family
    files = cast(list[dict[str, object]], output["files"])
    assert files[0]["repo_path"] == relative
    assert files[0]["content_sha256"] == digest
    assert files[0]["case_ids"] == [family_case_id(family)]


@pytest.mark.asyncio
async def test_codegen_finalize_rejects_missing_staged_manifest(tmp_path: Path) -> None:
    relative = durable_oracle_path()
    project, write_root = dual_roots(tmp_path)
    _write_generated(write_root, "api", relative)

    executed = await execute_task(
        codegen_finalize_handler("api"),
        fake_agent_result(codegen_result(files=[relative])),
        project,
        write_root=write_root,
    )

    assert executed.status == "failed"
    assert executed.failure is not None
    assert executed.failure.kind == "invalid_output"
    assert "generated-files manifest is missing" in executed.failure.message


@pytest.mark.asyncio
async def test_codegen_finalize_rejects_manifest_that_differs_from_result(tmp_path: Path) -> None:
    relative = durable_oracle_path()
    project, write_root = dual_roots(tmp_path)
    _write_generated(write_root, "api", relative)
    payload = codegen_result(files=[relative])
    manifest = dict(payload)
    manifest["required_capabilities"] = ["auth.session.create"]
    manifest_path = write_root / "qa/results/codegen/api-generated-files.json"
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
    relative = durable_oracle_path()
    project, write_root = dual_roots(tmp_path)
    _write_generated(write_root, "api", relative)
    payload = codegen_result(files=[relative])
    _write_manifest(write_root, payload)
    executed = await execute_task(
        codegen_finalize_handler("api"),
        fake_agent_result(
            payload,
            artifact_paths=["qa/tests"],
        ),
        project,
        write_root=write_root,
    )
    assert executed.status == "succeeded"


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.asyncio
async def test_codegen_finalize_rejects_wrong_family(family: str, tmp_path: Path) -> None:
    other = "e2e" if family == "api" else "api"
    relative = durable_oracle_path(family=other)
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
    relative = durable_oracle_path(family=family)
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
async def test_codegen_finalize_rejects_capabilities_dropped_from_reviewed_plan(
    family: str,
    tmp_path: Path,
) -> None:
    relative = durable_oracle_path(family=family)
    project, write_root = dual_roots(tmp_path)
    _write_generated(write_root, family, relative)
    payload = codegen_result(
        files=[relative],
        family=family,
        required_capabilities=["entities.item.create"],
    )
    _write_manifest(write_root, payload)
    finalize_input = fake_agent_result(payload)
    finalize_input["required_capabilities"] = [
        "auth.session.create",
        "entities.item.create",
    ]

    executed = await execute_task(
        codegen_finalize_handler(family),
        finalize_input,
        project,
        write_root=write_root,
    )

    assert executed.status == "failed"
    assert executed.failure is not None
    assert executed.failure.kind == "invalid_output"
    assert "required_capabilities must exactly match the reviewed plan" in executed.failure.message


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.asyncio
async def test_codegen_finalize_canonicalizes_capability_order(family: str, tmp_path: Path) -> None:
    relative = durable_oracle_path(family=family)
    project, write_root = dual_roots(tmp_path)
    _write_generated(write_root, family, relative)
    payload = codegen_result(
        files=[relative],
        family=family,
        required_capabilities=["entities.item.create", "auth.session.create"],
    )
    _write_manifest(write_root, payload)

    executed = await execute_task(
        codegen_finalize_handler(family),
        fake_agent_result(payload),
        project,
        write_root=write_root,
    )

    assert executed.status == "succeeded", executed.failure
    assert isinstance(executed.output, dict)
    assert executed.output["required_capabilities"] == [
        "auth.session.create",
        "entities.item.create",
    ]


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.asyncio
async def test_codegen_finalize_rejects_legacy_wrapped_input(
    family: str,
    tmp_path: Path,
) -> None:
    relative = durable_oracle_path(family=family)
    project, write_root = dual_roots(tmp_path)
    _write_generated(write_root, family, relative)
    payload = codegen_result(
        files=[relative],
        family=family,
        required_capabilities=["entities.item.create"],
    )
    _write_manifest(write_root, payload)
    flat = fake_agent_result(payload)
    agent_result = flat.pop("agent_result")

    executed = await execute_task(
        codegen_finalize_handler(family),
        {"validated_input": flat, "agent_result": agent_result},
        project,
        write_root=write_root,
    )

    assert executed.status == "failed"
    assert executed.failure is not None
    assert executed.failure.kind == "invalid_input"
    assert executed.failure.retryable is False


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.asyncio
async def test_codegen_finalize_rejects_undeclared_file(family: str, tmp_path: Path) -> None:
    relative = durable_oracle_path(family=family)
    extra = "qa/tests/unmapped_test.py"
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
    relative = durable_oracle_path(family=family)
    project, write_root = dual_roots(tmp_path)
    sut_leaf = f"tests/{relative.removeprefix('qa/tests/')}"
    direct = write_root.joinpath(*sut_leaf.split("/"))
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
    foreign = durable_oracle_path(family=other)
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


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.parametrize(
    "control_field,control_value", [("needs_fix", False), ("verdict", "accepted"), ("repair", None)]
)
def test_codegen_rejects_repair_control_fields(
    family: str, control_field: str, control_value: object
) -> None:
    from assurance_generation.contracts.codegen import CodegenResultV1

    payload = {
        "schema_version": "1",
        control_field: control_value,
        "change_id": CHANGE_ID,
        "layer": family,
        "files": [],
        "mapping": mapping_document(family),
    }
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        CodegenResultV1.model_validate(payload, context={"capability_leafs": frozenset(VALID_LEAFS)})


@pytest.mark.asyncio
async def test_e2e_codegen_skill_reads_family_prefixed_review() -> None:
    from assurance_generation.resource_loader import resource_text

    skill = resource_text("skills/aa-e2e-codegen/SKILL.md")
    assert "qa/results/review/e2e-plan-review.json" in skill
    assert "review/plan-review.json" not in skill
