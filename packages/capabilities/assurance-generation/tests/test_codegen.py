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
)
from codegen_fixtures import (  # pyright: ignore[reportMissingImports]
    FAMILIES,
    codegen_input,
    codegen_result,
    durable_oracle_path,
    family_case_id,
    fake_agent_result,
    locked_oracle_paths,
    mapping_document,
)
from planning_fixtures import BINDING as PLAN_BINDING  # pyright: ignore[reportMissingImports]
from planning_fixtures import PLAN_DIGEST, PLAN_REF  # pyright: ignore[reportMissingImports]
from planning_fixtures import VALID_LEAFS, reviewed_case, reviewed_cases  # pyright: ignore[reportMissingImports]

CHANGE_ID = "CH-DEMO-001"


def test_durable_test_path_requires_qa_tests_prefix() -> None:
    assert durable_test_path("qa/tests/api/test_dept.py") == "qa/tests/api/test_dept.py"
    import pytest

    with pytest.raises(ValueError):
        durable_test_path("tests/api/test_dept.py")
    with pytest.raises(ValueError):
        durable_test_path("/".join(("generated", "api", "files", "qa", "tests", "api", "test_dept.py")))


def test_family_allows_only_qa_tests_roots() -> None:
    assert family_allows_target("api", "qa/tests/api/test_dept.py")
    assert not family_allows_target("api", "tests/api/test_dept.py")


def test_staged_generated_path_is_removed() -> None:
    import assurance_generation.contracts.codegen as codegen

    assert not hasattr(codegen, "staged_generated_path")


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


def _write_reviewed_cases(tmp_path: Path, family: str, *, extra_case: bool = False) -> None:
    document = reviewed_cases(family)
    if extra_case:
        second = reviewed_case(family)
        second["case_id"] = f"TC_{family.upper()}_002"
        document["added"].append(second)
    path = tmp_path / "qa/cases/items/case.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def _write_locked_generated(write_root: Path, family: str, module: str = "items") -> tuple[str, str]:
    test_path, data_path = locked_oracle_paths(family, module)
    _write_generated(write_root, family, test_path)
    _write_generated(write_root, family, data_path, b"helper\n")
    return test_path, data_path


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
    path = workspace / f"qa/results/plans/{family}-codegen-mapping.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.asyncio
async def test_codegen_mapping_order_has_no_semantic_effect(family: str, tmp_path: Path) -> None:
    project, write_root = dual_roots(tmp_path)
    _write_reviewed_cases(project, family, extra_case=True)
    test_path, data_path = _write_locked_generated(write_root, family)
    authored = codegen_result(files=[test_path, data_path], family=family)
    second_id = f"TC_{family.upper()}_002"
    authored["mapping"]["entries"].append(
        {"case_id": second_id, "symbol": "test_second_case", "target_file": test_path}
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
    _write_reviewed_cases(tmp_path, "api")
    test_path, data_path = locked_oracle_paths("api")
    outcome = await execute_task(
        CodegenFinalizeHandler("api"),
        fake_agent_result(codegen_result(files=[test_path, data_path])),
        tmp_path,
    )
    assert outcome.failure is not None
    assert outcome.failure.kind == "invalid_output"


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.asyncio
async def test_codegen_prepare_is_deterministic_for_every_family(family: str, tmp_path: Path) -> None:
    _write_reviewed_cases(tmp_path, family)
    _write_plan_mapping(tmp_path, family, [durable_oracle_path(family=family)])
    handler = codegen_prepare_handler(family)
    first = await execute_task(handler, codegen_input(family), tmp_path, binding_data=PLAN_BINDING)
    second = await execute_task(handler, codegen_input(family), tmp_path, binding_data=PLAN_BINDING)
    assert first.status == second.status == "succeeded"
    assert canonical_json_bytes(first.output) == canonical_json_bytes(second.output)


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.asyncio
async def test_codegen_prepare_accepts_product_artifact_lock(family: str, tmp_path: Path) -> None:
    _write_reviewed_cases(tmp_path, family)
    _write_plan_mapping(tmp_path, family, [durable_oracle_path(family=family)])
    payload = codegen_input(family)
    payload["artifact_paths"] = [
        "qa/.qa.yaml",
        "qa/cases",
        "qa/fixtures",
        "qa/proposal.md",
        "qa/requirement.md",
        "qa/results",
        "qa/tests",
    ]

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
    _write_reviewed_cases(tmp_path, family)
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
async def test_codegen_prepare_authorizes_locked_outputs_only(family: str, tmp_path: Path) -> None:
    _write_reviewed_cases(tmp_path, family)
    prepared = await execute_task(
        codegen_prepare_handler(family),
        codegen_input(family),
        tmp_path,
        binding_data=PLAN_BINDING,
    )
    assert prepared.status == "succeeded"
    request = AgentRunRequest.model_validate(prepared.output)
    directory = "perf" if family == "performance" else family
    locked_test = f"qa/tests/{directory}/items/test_items.py"
    locked_data = f"qa/tests/testdata/{directory}/items.py"
    allowed = request.workspace.allowed_outputs
    assert allowed == tuple(
        sorted(
            (
                f"qa/results/codegen/{family}-codegen-summary.md",
                f"qa/results/codegen/{family}-generated-files.json",
                locked_test,
                locked_data,
            )
        )
    )
    assert "qa/tests/api" not in allowed
    assert "qa/tests/testdata" not in allowed
    assert f"qa/tests/{directory}" not in allowed


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.asyncio
async def test_codegen_prepare_authorizes_exact_staged_mapping_targets(family: str, tmp_path: Path) -> None:
    directory = "perf" if family == "performance" else family
    mapped = f"qa/tests/{directory}/test_users.py"
    extra = f"qa/tests/{directory}/test_unmapped.py"
    _write_reviewed_cases(tmp_path, family)
    _write_plan_mapping(tmp_path, family, [mapped])

    prepared = await execute_task(
        codegen_prepare_handler(family),
        codegen_input(family),
        tmp_path,
        binding_data=PLAN_BINDING,
    )

    assert prepared.status == "succeeded"
    request = AgentRunRequest.model_validate(prepared.output)
    locked_test = f"qa/tests/{directory}/items/test_items.py"
    assert locked_test in request.workspace.allowed_outputs
    context_payload = cast(dict[str, object], request.instructions[4].json_content)
    assert context_payload["allowed_outputs"] == request.workspace.allowed_outputs
    assert extra not in request.workspace.allowed_outputs
    assert mapped not in request.workspace.allowed_outputs
    assert all("**" not in path for path in request.workspace.allowed_outputs)
    assert all(not path.startswith("tests/") for path in request.workspace.allowed_outputs)
    assert f"qa/results/codegen/{family}-generated-files.json" in request.workspace.allowed_outputs


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.asyncio
async def test_codegen_prepare_succeeds_without_mapping(family: str, tmp_path: Path) -> None:
    _write_reviewed_cases(tmp_path, family)
    prepared = await execute_task(
        codegen_prepare_handler(family),
        codegen_input(family),
        tmp_path,
        binding_data=PLAN_BINDING,
    )

    assert prepared.status == "succeeded"


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.asyncio
async def test_codegen_prepare_rejects_missing_reviewed_cases(family: str, tmp_path: Path) -> None:
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
    assert "reviewed case" in prepared.failure.message


@pytest.mark.asyncio
async def test_codegen_finalize_accepts_host_locked_paths(tmp_path: Path) -> None:
    project, write_root = dual_roots(tmp_path)
    _write_reviewed_cases(project, "api")
    test_path, data_path = _write_locked_generated(write_root, "api")
    authored = codegen_result(files=[test_path, data_path], family="api")
    _write_manifest(write_root, authored)
    outcome = await execute_task(
        codegen_finalize_handler("api"),
        fake_agent_result(authored),
        project,
        write_root=write_root,
    )
    assert outcome.status == "succeeded", outcome.failure


@pytest.mark.asyncio
async def test_codegen_finalize_rejects_write_root_as_file(tmp_path: Path) -> None:
    project, write_root = dual_roots(tmp_path)
    _write_reviewed_cases(project, "api")
    bad = "qa/tests/api"
    _write_generated(write_root, "api", bad)
    authored = codegen_result(files=[bad], family="api")
    authored["mapping"]["entries"][0]["target_file"] = bad
    _write_manifest(write_root, authored)
    outcome = await execute_task(
        codegen_finalize_handler("api"),
        fake_agent_result(authored),
        project,
        write_root=write_root,
    )
    assert outcome.status == "failed"
    assert outcome.failure is not None
    assert outcome.failure.kind == "invalid_output"
    assert outcome.failure.retryable is True


@pytest.mark.asyncio
async def test_codegen_finalize_rejects_missing_testdata(tmp_path: Path) -> None:
    project, write_root = dual_roots(tmp_path)
    _write_reviewed_cases(project, "api")
    test_path, _ = locked_oracle_paths("api")
    _write_generated(write_root, "api", test_path)
    authored = codegen_result(files=[test_path], family="api")
    _write_manifest(write_root, authored)
    outcome = await execute_task(
        codegen_finalize_handler("api"),
        fake_agent_result(authored),
        project,
        write_root=write_root,
    )
    assert outcome.status == "failed"
    assert outcome.failure is not None
    assert outcome.failure.kind == "invalid_output"
    assert outcome.failure.retryable is True


@pytest.mark.asyncio
async def test_codegen_finalize_rejects_target_file_mismatch(tmp_path: Path) -> None:
    project, write_root = dual_roots(tmp_path)
    _write_reviewed_cases(project, "api")
    test_path, data_path = _write_locked_generated(write_root, "api")
    authored = codegen_result(files=[test_path, data_path], family="api")
    authored["mapping"]["entries"][0]["target_file"] = "qa/tests/api/other/test_other.py"
    _write_manifest(write_root, authored)
    outcome = await execute_task(
        codegen_finalize_handler("api"),
        fake_agent_result(authored),
        project,
        write_root=write_root,
    )
    assert outcome.status == "failed"
    assert outcome.failure is not None
    assert outcome.failure.kind == "invalid_output"
    assert outcome.failure.retryable is True


@pytest.mark.asyncio
async def test_codegen_finalize_rejects_invalid_case_path(tmp_path: Path) -> None:
    project, write_root = dual_roots(tmp_path)
    bad = project / "qa/cases/dept.yaml"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("schema_version: '1.0'\nadded: []\nmodified: []\nremoved: []\n", encoding="utf-8")
    outcome = await execute_task(
        codegen_finalize_handler("api"),
        fake_agent_result(codegen_result(files=["qa/tests/api/dept/test_dept.py"])),
        project,
        write_root=write_root,
    )
    assert outcome.status == "failed"
    assert outcome.failure is not None
    assert outcome.failure.kind == "invalid_input"
    assert outcome.failure.retryable is False


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.asyncio
async def test_codegen_finalize_rejects_symbol_drift_from_reviewed_mapping(
    family: str, tmp_path: Path
) -> None:
    project, write_root = dual_roots(tmp_path)
    _write_reviewed_cases(project, family)
    test_path, data_path = _write_locked_generated(write_root, family)
    authored = codegen_result(files=[test_path, data_path], family=family)
    authored["mapping"]["entries"][0]["symbol"] = "different_unreviewed_symbol"
    _write_manifest(write_root, authored)
    payload = fake_agent_result(authored)
    payload["reviewed_mapping"] = mapping_document(family)
    outcome = await execute_task(codegen_finalize_handler(family), payload, project, write_root=write_root)
    assert outcome.status == "failed"
    assert outcome.failure is not None
    assert outcome.failure.kind == "invalid_output"
    assert "mapping must exactly match the reviewed plan" in outcome.failure.message


@pytest.mark.parametrize("field", ("required_capabilities",))
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
    _write_reviewed_cases(project, "api")
    _write_locked_generated(write_root, "api")
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
    second = "qa/tests/api/test_orders.py"
    project, write_root = dual_roots(tmp_path)
    _write_reviewed_cases(project, "api")
    test_path, data_path = _write_locked_generated(write_root, "api")
    _write_generated(write_root, "api", second)
    payload = codegen_result(files=[test_path, data_path])
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
    assert "mapping target_file must equal the locked test file for TC_API_002" in executed.failure.message


@pytest.mark.asyncio
async def test_codegen_finalize_keeps_support_as_extra_hashed_entry(tmp_path: Path) -> None:
    project, write_root = dual_roots(tmp_path)
    _write_reviewed_cases(project, "api")
    test_path, data_path = locked_oracle_paths("api")
    mapped_digest = _write_generated(write_root, "api", test_path)
    support_digest = _write_generated(write_root, "api", data_path, b"helper\n")
    payload = codegen_result(files=[test_path, data_path])
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
    assert files[test_path]["content_sha256"] == mapped_digest
    assert files[test_path]["role"] == "test_entry"
    assert files[test_path]["case_ids"] == [family_case_id("api")]
    assert files[data_path]["content_sha256"] == support_digest
    assert files[data_path]["role"] == "support"
    assert files[data_path]["case_ids"] == []
    mapping = cast(dict[str, object], output["mapping"])
    targets = [item["target_file"] for item in cast(list[dict[str, object]], mapping["entries"])]
    assert targets == [test_path]


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.asyncio
async def test_codegen_finalize_authenticates_workspace_bytes(family: str, tmp_path: Path) -> None:
    project, write_root = dual_roots(tmp_path)
    _write_reviewed_cases(project, family)
    test_path, data_path = locked_oracle_paths(family)
    digest = _write_generated(write_root, family, test_path)
    _write_generated(write_root, family, data_path, b"helper\n")
    payload = codegen_result(files=[test_path, data_path], family=family)
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
    files = {item["repo_path"]: item for item in cast(list[dict[str, object]], output["files"])}
    assert files[test_path]["content_sha256"] == digest
    assert files[test_path]["case_ids"] == [family_case_id(family)]


@pytest.mark.asyncio
async def test_codegen_finalize_rejects_missing_staged_manifest(tmp_path: Path) -> None:
    project, write_root = dual_roots(tmp_path)
    _write_reviewed_cases(project, "api")
    test_path, data_path = _write_locked_generated(write_root, "api")

    executed = await execute_task(
        codegen_finalize_handler("api"),
        fake_agent_result(codegen_result(files=[test_path, data_path])),
        project,
        write_root=write_root,
    )

    assert executed.status == "failed"
    assert executed.failure is not None
    assert executed.failure.kind == "invalid_output"
    assert "generated-files manifest is missing" in executed.failure.message


@pytest.mark.asyncio
async def test_codegen_finalize_rejects_manifest_that_differs_from_result(tmp_path: Path) -> None:
    project, write_root = dual_roots(tmp_path)
    _write_reviewed_cases(project, "api")
    test_path, data_path = _write_locked_generated(write_root, "api")
    payload = codegen_result(files=[test_path, data_path])
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
    project, write_root = dual_roots(tmp_path)
    _write_reviewed_cases(project, "api")
    test_path, data_path = _write_locked_generated(write_root, "api")
    payload = codegen_result(files=[test_path, data_path])
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
    assert "required_capabilities must exactly match the host codegen scope" in executed.failure.message


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.asyncio
async def test_codegen_finalize_canonicalizes_capability_order(family: str, tmp_path: Path) -> None:
    project, write_root = dual_roots(tmp_path)
    _write_reviewed_cases(project, family)
    test_path, data_path = _write_locked_generated(write_root, family)
    payload = codegen_result(
        files=[test_path, data_path],
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
    directory = "perf" if family == "performance" else family
    extra = f"qa/tests/{directory}/conftest.py"
    project, write_root = dual_roots(tmp_path)
    _write_reviewed_cases(project, family)
    test_path, data_path = _write_locked_generated(write_root, family)
    _write_generated(write_root, family, extra)
    payload = codegen_result(files=[test_path, data_path, extra], family=family)
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
async def test_codegen_prepare_rejects_routing_marker(family: str, tmp_path: Path) -> None:
    binding = {
        **PLAN_BINDING,
        "execution": {
            **cast(dict[str, object], PLAN_BINDING["execution"]),
            "provider_model": "primary,fallback",
        },
    }
    _write_reviewed_cases(tmp_path, family)
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
    project, write_root = dual_roots(tmp_path)
    _write_reviewed_cases(project, family)
    test_path, data_path = locked_oracle_paths(family)
    for relative in (test_path, data_path):
        sut_leaf = f"tests/{relative.removeprefix('qa/tests/')}"
        direct = write_root.joinpath(*sut_leaf.split("/"))
        direct.parent.mkdir(parents=True, exist_ok=True)
        direct.write_bytes(b"test\n")
    executed = await execute_task(
        codegen_finalize_handler(family),
        fake_agent_result(codegen_result(files=[test_path, data_path], family=family)),
        project,
        write_root=write_root,
    )
    assert executed.status == "failed"
    assert executed.failure is not None
    assert executed.failure.kind == "invalid_output"
    assert not (project / test_path).exists()
    assert not (project / data_path).exists()


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


def test_e2e_codegen_skill_reads_host_codegen_scope() -> None:
    from assurance_generation.resource_loader import resource_text

    skill = resource_text("skills/aa-e2e-codegen/SKILL.md")
    assert "host-built E2E codegen scope" in skill
    assert "locked_outputs" in skill
    assert "qa/cases/**/case.yaml" in skill
    assert "review/plan-review.json" not in skill
    assert "qa/results/review/e2e-plan-review.json" not in skill
