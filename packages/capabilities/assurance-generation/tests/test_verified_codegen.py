from __future__ import annotations

import json
from pathlib import Path
from collections.abc import Callable
from typing import Any, cast

import pytest

from tests.product.test_change_local_output_routing import dual_roots, execute_task
from tests.verified_generation_fixture import accepted_verified_execution_input

from assurance_generation.contracts.execution_plan import CasePlanContextV1, CaseExecutionPlanSetV1
from assurance_generation.operations.codegen import CodegenFinalizeHandler
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1
from codegen_fixtures import fake_agent_result  # pyright: ignore[reportMissingImports]
from planning_fixtures import VALID_LEAFS  # pyright: ignore[reportMissingImports]


def _verified_inputs(project: Path, write_root: Path) -> tuple[CasePlanContextV1, EvidenceArtifactRefV1, str]:
    root = accepted_verified_execution_input(project)
    generation = root.generation_result
    assert generation is not None and generation.case_execution_plan_ref is not None
    machine_ref = generation.case_execution_plan_ref
    machine = CaseExecutionPlanSetV1.model_validate_json((project / machine_ref.path).read_bytes())
    formal = machine.cases[0]
    context = CasePlanContextV1(
        change_id=formal.change_id,
        coverage_epoch=formal.coverage_epoch,
        plan_digest=formal.plan_digest,
        plan_ref=formal.plan_ref,
        reviewed_case=formal.reviewed_case,
        verification_policy_digest=formal.verification_policy_digest,
        technical_config_digest=formal.technical_config_digest,
        sut_digest=formal.sut_digest,
    )
    target = "tests/api/test_user_create.py"
    relative = f"qa/changes/{root.change_id}/generated/api/files/{target}"
    destination = write_root / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes((project / relative).read_bytes())
    return context, machine_ref, formal.spec_digest


def _mapping(
    context: CasePlanContextV1, machine_ref: EvidenceArtifactRefV1, spec_digest: str
) -> dict[str, Any]:
    return {
        "schema_version": "1",
        "layer": "api",
        "entries": [
            {
                "case_id": "TC_USER_CREATE_001",
                "symbol": "test_tc_user_create_001__create",
                "target_file": "tests/api/test_user_create.py",
            }
        ],
        "validation_profile": "api_db.v1",
        "coverage_epoch": context.coverage_epoch,
        "plan_digest": context.plan_digest,
        "plan_ref": context.plan_ref.model_dump(mode="json"),
        "reviewed_case": context.reviewed_case.model_dump(mode="json"),
        "case_execution_plan_ref": machine_ref.model_dump(mode="json"),
        "case_execution_plan_digest": machine_ref.digest,
        "case_spec_digests": {"TC_USER_CREATE_001": spec_digest},
    }


async def _finalize(
    tmp_path: Path,
    *,
    mutate_mapping: Callable[[dict[str, Any]], None] | None = None,
    source: str | None = None,
):
    project, write_root = dual_roots(tmp_path, "CH-USER-001")
    context, machine_ref, spec_digest = _verified_inputs(project, write_root)
    mapping = _mapping(context, machine_ref, spec_digest)
    if mutate_mapping is not None:
        mutate_mapping(mapping)
    target = "tests/api/test_user_create.py"
    if source is not None:
        (write_root / f"qa/changes/CH-USER-001/generated/api/files/{target}").write_text(source)
    document = {
        "schema_version": "1",
        "change_id": "CH-USER-001",
        "layer": "api",
        "files": [
            {
                "repo_path": target,
                "disposition": "generated",
                "role": "test_entry",
                "case_ids": ["TC_USER_CREATE_001"],
            }
        ],
        "mapping": mapping,
        "required_capabilities": ["entities.item.create"],
    }
    manifest = write_root / "qa/changes/CH-USER-001/codegen/api-generated-files.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps(document), encoding="utf-8")
    finalize = fake_agent_result(document, capability_leafs=VALID_LEAFS)
    finalize.update(
        {
            "change_id": context.change_id,
            "plan_digest": context.plan_digest,
            "plan_ref": context.plan_ref.model_dump(mode="json"),
            "coverage_epoch": context.coverage_epoch,
            "reviewed_case": context.reviewed_case.model_dump(mode="json"),
            "validation_profile": "api_db.v1",
            "case_execution_plan_ref": machine_ref.model_dump(mode="json"),
            "case_execution_plan_digest": machine_ref.digest,
        }
    )
    return await execute_task(
        CodegenFinalizeHandler("api"),
        finalize,
        project,
        write_root=write_root,
    )


@pytest.mark.asyncio
async def test_verified_codegen_accepts_only_the_frozen_bridge_entry(tmp_path: Path) -> None:
    outcome = await _finalize(tmp_path)

    assert outcome.status == "succeeded", outcome.failure
    output = cast(dict[str, Any], outcome.output)
    assert output["mapping"]["validation_profile"] == "api_db.v1"
    assert output["mapping"]["case_execution_plan_digest"] != output["mapping"]["plan_digest"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        (lambda value: value["entries"][0].update(case_id="TC_USER_CREATE_MISSING"), "mapped Case"),
        (lambda value: value["reviewed_case"].update(coverage_epoch=1), "ReviewedCase"),
        (lambda value: value.update(plan_digest="0" * 64), "plan binding"),
        (lambda value: value.update(case_execution_plan_digest="1" * 64), "machine plan"),
        (
            lambda value: value["case_spec_digests"].update(TC_USER_CREATE_001="2" * 64),
            "spec digest",
        ),
    ],
)
async def test_verified_codegen_rejects_stale_or_incomplete_bindings(
    tmp_path: Path, mutation: Callable[[dict[str, Any]], None], reason: str
) -> None:
    outcome = await _finalize(tmp_path, mutate_mapping=mutation)

    assert outcome.status == "failed"
    assert outcome.failure is not None
    assert reason.lower() in outcome.failure.message.lower()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source",
    [
        "def test_tc_user_create_001__create():\n    assert True\n",
        (
            "from assurance_execution.bridge import execute_case\n\n"
            "def test_tc_user_create_001__create():\n"
            "    pass\n"
        ),
        (
            "import httpx\n"
            "def test_tc_user_create_001__create():\n"
            '    assert httpx.post("http://127.0.0.1:8000/api/v1/user/create").status_code == 200\n'
        ),
        (
            "from assurance_execution.bridge import execute_case\n"
            "DB_PATH = '/tmp/db.sqlite3'\n"
            "def test_tc_user_create_001__create():\n"
            '    execute_case("TC_USER_CREATE_001")\n'
        ),
        (
            "from assurance_execution.bridge import execute_case\n"
            "def test_tc_user_create_001__create():\n"
            '    execute_case("TC_USER_CREATE_001")\n'
            "def test_tc_user_create_001__create():\n"
            '    execute_case("TC_USER_CREATE_001")\n'
        ),
        (
            "from assurance_execution.bridge import execute_case\n"
            "def test_tc_user_create_001__create() -> execute_case('TC_USER_CREATE_001'):\n"
            '    execute_case("TC_USER_CREATE_001")\n'
        ),
    ],
)
async def test_verified_codegen_rejects_candidate_oracles_and_all_pass_assertions(
    tmp_path: Path, source: str
) -> None:
    outcome = await _finalize(tmp_path, source=source)

    assert outcome.status == "failed"
    assert outcome.failure is not None
    assert "bridge" in outcome.failure.message.lower()
