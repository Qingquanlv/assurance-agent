from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from agent_runtime_contracts import AgentRunRequest, AgentRunResult
from agent_runtime_contracts.schema import canonical_digest
from graph_engine.canonical import JSONValue
from graph_engine.plugin_api import TaskHandler, TaskOutcome
from graph_engine.runtime.task_workspace import TaskWorkspaceStore
from tests.phase4.agent_harness import FakeAgentAdapter
from tests.phase5.test_change_local_output_routing import dual_roots, execute_task

from assurance_intake.operations import (
    CaseDesignFinalizeHandler,
    CaseDesignPrepareHandler,
    CaseReviewFinalizeHandler,
    CaseReviewPrepareHandler,
    ExploreFinalizeHandler,
    ExplorePrepareHandler,
    IntakeFinalizeHandler,
    IntakePrepareHandler,
)
from assurance_intake.resource_loader import resource_text

_SHA = "a" * 64
_FIXTURES = Path(__file__).resolve().parent / "fixtures"
VALID_LEAFS = ("auth.session.create", "entities.item.create")
BINDING: dict[str, JSONValue] = {
    "agent_profile": "aa-doc-author",
    "execution": {
        "provider_model": "test-model",
        "worker_profile": "worker",
        "permission_profile_digest": _SHA,
        "limits": {"max_seconds": 5},
    },
    "request_policy_digest": _SHA,
    "request_config_digest": _SHA,
}
CASE_INPUT: dict[str, JSONValue] = {
    "change_id": "CH-DEMO-001",
    "capability_leafs": list(VALID_LEAFS),
    "artifact_paths": ["qa/changes/CH-DEMO-001/explore/advisory.json"],
    "selected_test_families": ["api", "e2e", "fuzz", "performance"],
    "case_delta_paths": ["qa/changes/CH-DEMO-001/cases/menus/case.yaml"],
}
CASE_REVIEW_INPUT: dict[str, JSONValue] = {
    key: value for key, value in CASE_INPUT.items() if key != "selected_test_families"
}
INTAKE_INPUT: dict[str, JSONValue] = {
    "change_id": "RET-dept-management",
    "requirement": "Cover department CRUD and the department tree page.",
    "capability_leafs": [],
    "artifact_paths": ["qa/changes"],
}


async def run_prepare(
    handler: TaskHandler,
    payload: JSONValue,
    binding: JSONValue,
    workspace: Path,
    write_root: Path | None = None,
) -> Any:
    return await execute_task(handler, payload, workspace, binding_data=binding, write_root=write_root)


async def run_finalize(
    handler: TaskHandler,
    result: AgentRunResult,
    workspace: Path,
    write_root: Path | None = None,
) -> TaskOutcome:
    executed = await execute_task(
        handler,
        {
            "agent_result": result.model_dump(mode="json"),
            "capability_leafs": list(VALID_LEAFS),
            "artifact_paths": [],
        },
        workspace,
        write_root=write_root,
    )
    return executed.outcome


def fake_agent_result(structured_result: JSONValue) -> AgentRunResult:
    return AgentRunResult(
        structured_result=structured_result,
        result_digest=canonical_digest(structured_result),
        evidence_digest=FakeAgentAdapter.EVIDENCE_DIGEST,
        adapter_id="test.fake",
        adapter_version="1.0.0",
    )


def test_intake_skill_requires_direct_change_write() -> None:
    skill = resource_text("skills/aa-intake/SKILL.md")
    normalized = " ".join(skill.split())
    persona = resource_text("personas/intake-host.md")
    assert "qa/changes/<change-id>" in skill
    assert "must not ask" in skill.lower() or "do not ask" in skill.lower()
    assert "must not require" in skill.lower() or "do not require" in skill.lower()
    assert "initialize" in skill.lower()
    assert "requirement.md" in skill
    assert "Call the native `write` tool exactly twice" in skill
    assert "read both files back" in skill
    assert "A final JSON response without those successful tool calls is invalid" in normalized
    assert "interactive" not in persona.lower()
    assert "do not ask" in persona.lower()


def test_explore_skill_returns_the_locked_result_contract() -> None:
    skill = resource_text("skills/aa-explore/SKILL.md")
    assert "return structured JSON only" in skill
    assert 'Set it to the exact string\n    `"explore/context.json"`' in skill
    assert "Do not expand it to" in skill
    assert '{"output_files":["qa/changes/<change-id>/explore/exploration.json"]}' in skill


def test_explore_resources_require_complete_output_even_when_evidence_is_degraded() -> None:
    resources = {
        "skill": resource_text("skills/aa-explore/SKILL.md"),
        "persona": resource_text("personas/explorer.md"),
        "prompt": resource_text("prompts/explore.md"),
    }

    for content in resources.values():
        assert "advisory.json" not in content
        assert "exploration.json" in content
        assert "degraded" in content.lower()
        assert "no-source" in content.lower()
        assert "must still" in content.lower()

    skill = resources["skill"]
    assert "do not return structured success" in skill.lower()
    assert 'never return `{"output_files":[]}`' in skill.lower()
    assert "must not synthesize such a state as successful" in skill.lower()
    assert "return only the non-empty structured" in resources["persona"].lower()


def test_case_design_skill_returns_the_locked_file_receipt_contract() -> None:
    skill = resource_text("skills/aa-case-design/SKILL.md")
    assert "final assistant response" in " ".join(skill.split())
    assert '"output_files"' in skill
    assert "written files are the sole source of truth" in skill
    assert "Do not duplicate the case delta" in skill
    assert "case_delta_paths" in skill
    assert "phases.explore.status == done" not in skill
    assert "Emit a knowledge proposal" not in skill
    assert "Never inspect `.qa.yaml` `phases.explore`" in skill
    assert (
        '{"output_files":["qa/changes/<change-id>/.qa.yaml",'
        '"qa/changes/<change-id>/cases/<trusted-module>/case.yaml",'
        '"qa/changes/<change-id>/proposal.md",'
        '"qa/changes/<change-id>/trace/minimum-coverage-matrix.json"]}'
    ) in skill
    assert "every written `cases/**/case.yaml`" not in skill
    assert '"qa/changes/<change-id>/trace/minimum-coverage-matrix.json"' in skill
    assert "The MRC matrix path is mandatory" in skill
    assert "deterministic finalize step" in skill
    assert "json.dumps(yaml.safe_load" not in skill


def test_case_design_skill_spells_out_the_typed_trace_value_shape() -> None:
    skill = resource_text("skills/aa-case-design/SKILL.md")

    assert "Every `trace` value is an object with the single field `covered: true`" in skill
    assert "<capability-leaf>: true" in skill
    assert "Treat `capability_leafs` as a closed enum" in skill
    assert "does not select an adapter namespace" in skill
    assert "capabilities.adapters.api.dept.create" not in skill
    assert "Do not\nput MRC IDs" in skill


def test_case_reviewer_treats_graph_invocation_as_phase_predecessor_proof() -> None:
    skill = resource_text("skills/aa-case-reviewer/SKILL.md")
    assert "authenticated predecessor proof" in skill
    assert "do not search `.qa.yaml`" in skill.lower()


def test_case_reviewer_required_fields_match_typed_case_contract() -> None:
    skill = resource_text("skills/aa-case-reviewer/SKILL.md")

    assert "CaseYamlAuthoring is the sole required-field source" in skill
    assert "\ntags:" not in skill
    assert "framework: pytest|pytest-playwright|schemathesis|locust|null" not in skill
    assert "trace: {}" not in skill


def test_case_reviewer_returns_complete_structured_result() -> None:
    skill = resource_text("skills/aa-case-reviewer/SKILL.md")

    assert "Return the complete parsed JSON object" in skill
    assert "exactly the same object written to `case-review.json`" in skill
    assert "Do not return only the routing fields" in skill
    assert "Case review completed.\n\nDecision:" not in skill


def test_case_reviewer_routes_source_proven_case_defects_to_agent_fix_loop() -> None:
    skill = resource_text("skills/aa-case-reviewer/SKILL.md")

    assert "Severity alone does not require human review" in skill
    assert "A high-severity finding may still be mechanically fixable" in skill
    assert "must use `needs_fix`" in skill
    assert "Risk level is high or critical" not in skill


def test_case_skills_keep_advisory_mrc_complete_without_inventing_human_blockers() -> None:
    designer = " ".join(resource_text("skills/aa-case-design/SKILL.md").split())
    reviewer = " ".join(resource_text("skills/aa-case-reviewer/SKILL.md").split())

    assert "every advisory MRC item still gets exactly one matrix row" in designer
    assert "advisory expansion lacks a frozen oracle" in designer
    assert "not as an implicit business oracle" in reviewer
    assert "Do not create a blocking `needs_review` item" in reviewer
    assert "Never require a proposed MRC key in case `trace`" in reviewer


@pytest.mark.asyncio
async def test_intake_prepare_rejects_missing_requirement(tmp_path: Path) -> None:
    payload = {key: value for key, value in INTAKE_INPUT.items() if key != "requirement"}
    prepared = await run_prepare(IntakePrepareHandler(), payload, BINDING, tmp_path)
    assert prepared.status == "failed"
    assert prepared.failure is not None
    assert prepared.failure.kind == "invalid_input"
    assert prepared.failure.retryable is False


@pytest.mark.asyncio
async def test_intake_prepare_embeds_locked_requirement_and_write_rules(tmp_path: Path) -> None:
    prepared = await run_prepare(IntakePrepareHandler(), INTAKE_INPUT, BINDING, tmp_path)
    request = AgentRunRequest.model_validate(prepared.output)
    assert request.workspace.agent_profile == "assurance-v1-doc-author"
    assert request.workspace.allowed_outputs == (
        "qa/changes/RET-dept-management/.qa.yaml",
        "qa/changes/RET-dept-management/requirement.md",
    )
    skill, persona, business = request.instructions
    assert "Capability-owned intake" in (skill.text_content or "")
    assert "Do not ask" in (skill.text_content or "")
    assert "qa/changes/<change-id>" in (skill.text_content or "")
    assert "Do not ask" in (persona.text_content or "")
    payload = cast(Mapping[str, object], business.json_content)
    assert payload["change_id"] == "RET-dept-management"
    assert payload["requirement"] == "Cover department CRUD and the department tree page."


@pytest.mark.asyncio
async def test_explore_prepare_materializes_deterministic_graph_context(tmp_path: Path) -> None:
    project, write_root = dual_roots(tmp_path)
    requirement = project / "qa/changes/CH-DEMO-001/requirement.md"
    requirement.parent.mkdir(parents=True, exist_ok=True)
    requirement.write_text("# Requirement\n\nCover item creation.\n", encoding="utf-8")
    payload = {
        "change_id": "CH-DEMO-001",
        "capability_leafs": list(VALID_LEAFS),
        "artifact_paths": ["qa/changes"],
    }

    first = await run_prepare(ExplorePrepareHandler(), payload, BINDING, project, write_root)
    context_path = write_root / "qa/changes/CH-DEMO-001/explore/context.json"
    first_bytes = context_path.read_bytes()
    second = await run_prepare(ExplorePrepareHandler(), payload, BINDING, project, write_root)

    assert first.status == second.status == "succeeded"
    assert context_path.read_bytes() == first_bytes
    context_document = json.loads(first_bytes)
    assert context_document["change_id"] == "CH-DEMO-001"
    assert context_document["requirement_summary"].startswith("# Requirement")
    assert "no_diff: no authenticated diff projection was supplied" in context_document["degraded_reasons"]


@pytest.mark.asyncio
async def test_case_design_prepare_is_canonical_and_provider_neutral(tmp_path: Path) -> None:
    first = await run_prepare(CaseDesignPrepareHandler(), CASE_INPUT, BINDING, tmp_path)
    second = await run_prepare(CaseDesignPrepareHandler(), CASE_INPUT, BINDING, tmp_path)
    request = AgentRunRequest.model_validate(first.output)
    assert request.canonical_bytes() == AgentRunRequest.model_validate(second.output).canonical_bytes()
    assert request.workspace.allowed_outputs == (
        "qa/changes/CH-DEMO-001/.qa.yaml",
        "qa/changes/CH-DEMO-001/cases/menus/case.yaml",
        "qa/changes/CH-DEMO-001/proposal.md",
        "qa/changes/CH-DEMO-001/trace/minimum-coverage-matrix.json",
    )
    assert not any("**" in path for path in request.workspace.allowed_outputs)
    assert request.workspace.allowed_outputs.count("qa/changes/CH-DEMO-001/cases/menus/case.yaml") == 1


@pytest.mark.asyncio
async def test_case_design_prepare_consumes_typed_current_change_exploration(tmp_path: Path) -> None:
    relative = "qa/changes/CH-DEMO-001/explore/exploration.json"
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    advisory = _valid_explore_advisory()
    advisory["minimum_required_coverage"] = {"api": ["create_item"]}
    path.write_text(json.dumps(advisory), encoding="utf-8")

    prepared = await run_prepare(CaseDesignPrepareHandler(), CASE_INPUT, BINDING, tmp_path)

    assert prepared.status == "succeeded"
    request = AgentRunRequest.model_validate(prepared.output)
    business = cast(Mapping[str, object], request.instructions[2].json_content)
    exploration = cast(Mapping[str, object], business["exploration"])
    assert exploration["change_id"] == "CH-DEMO-001"
    assert dict(cast(Mapping[str, object], exploration["minimum_required_coverage"])) == {
        "api": ("create_item",)
    }


@pytest.mark.asyncio
async def test_case_design_prepare_allows_missing_exploration_for_standalone_case(
    tmp_path: Path,
) -> None:
    prepared = await run_prepare(CaseDesignPrepareHandler(), CASE_INPUT, BINDING, tmp_path)

    assert prepared.status == "succeeded"
    request = AgentRunRequest.model_validate(prepared.output)
    business = cast(Mapping[str, object], request.instructions[2].json_content)
    assert business["exploration"] is None


@pytest.mark.asyncio
async def test_case_design_prepare_rejects_mismatched_exploration_identity(tmp_path: Path) -> None:
    relative = "qa/changes/CH-DEMO-001/explore/exploration.json"
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    advisory = _valid_explore_advisory()
    advisory["change_id"] = "CH-SIBLING"
    path.write_text(json.dumps(advisory), encoding="utf-8")

    prepared = await run_prepare(CaseDesignPrepareHandler(), CASE_INPUT, BINDING, tmp_path)

    assert prepared.status == "failed"
    assert prepared.failure is not None
    assert prepared.failure.kind == "invalid_input"
    assert "change_id" in prepared.failure.message


@pytest.mark.asyncio
async def test_case_review_prepare_locks_exact_current_change_inputs(tmp_path: Path) -> None:
    change_root = tmp_path / "qa/changes/CH-DEMO-001"
    (change_root / "trace").mkdir(parents=True)
    (change_root / "cases/menus").mkdir(parents=True)
    (change_root / ".qa.yaml").write_text("change_id: CH-DEMO-001\n", encoding="utf-8")
    (change_root / "proposal.md").write_text("# Proposal\n", encoding="utf-8")
    (change_root / "trace/minimum-coverage-matrix.json").write_text("[]\n", encoding="utf-8")
    (change_root / "cases/menus/case.yaml").write_text(
        "schema_version: '1'\nadded: []\nmodified: []\nremoved: []\n",
        encoding="utf-8",
    )

    prepared = await run_prepare(CaseReviewPrepareHandler(), CASE_REVIEW_INPUT, BINDING, tmp_path)

    assert prepared.status == "succeeded"
    request = AgentRunRequest.model_validate(prepared.output)
    business = cast(Mapping[str, object], request.instructions[2].json_content)
    assert business["case_delta_paths"] == ("qa/changes/CH-DEMO-001/cases/menus/case.yaml",)
    assert business["review_input_paths"] == (
        "qa/changes/CH-DEMO-001/.qa.yaml",
        "qa/changes/CH-DEMO-001/cases/menus/case.yaml",
        "qa/changes/CH-DEMO-001/proposal.md",
        "qa/changes/CH-DEMO-001/trace/minimum-coverage-matrix.json",
    )


@pytest.mark.asyncio
async def test_case_review_prepare_rejects_missing_locked_input(tmp_path: Path) -> None:
    prepared = await run_prepare(CaseReviewPrepareHandler(), CASE_REVIEW_INPUT, BINDING, tmp_path)

    assert prepared.status == "failed"
    assert prepared.failure is not None
    assert prepared.failure.kind == "invalid_input"
    assert "missing case-review input" in prepared.failure.message


@pytest.mark.asyncio
async def test_case_review_prepare_rejects_intermediate_directory_symlink(tmp_path: Path) -> None:
    change_root = tmp_path / "qa/changes/CH-DEMO-001"
    sibling_cases = tmp_path / "qa/changes/CH-SIBLING/cases/menus"
    (change_root / "trace").mkdir(parents=True)
    (change_root / "cases").mkdir()
    sibling_cases.mkdir(parents=True)
    (change_root / ".qa.yaml").write_text("change_id: CH-DEMO-001\n", encoding="utf-8")
    (change_root / "proposal.md").write_text("# Proposal\n", encoding="utf-8")
    (change_root / "trace/minimum-coverage-matrix.json").write_text("[]\n", encoding="utf-8")
    (sibling_cases / "case.yaml").write_text(
        "schema_version: '1'\nadded: []\nmodified: []\nremoved: []\n",
        encoding="utf-8",
    )
    (change_root / "cases/menus").symlink_to(sibling_cases, target_is_directory=True)

    prepared = await run_prepare(CaseReviewPrepareHandler(), CASE_REVIEW_INPUT, BINDING, tmp_path)

    assert prepared.status == "failed"
    assert prepared.failure is not None
    assert prepared.failure.kind == "invalid_input"
    assert "must not contain a symlink" in prepared.failure.message


@pytest.mark.asyncio
async def test_case_review_finalize_accepts_mrc_key_that_is_not_a_capability_leaf(
    tmp_path: Path,
) -> None:
    _write_review_matrix(tmp_path, missing=["entities.fake"])
    result = fake_agent_result(_case_review_document(missing=["entities.fake"]))
    outcome = await run_finalize(CaseReviewFinalizeHandler(), result, tmp_path)
    assert outcome.status == "succeeded"


@pytest.mark.asyncio
async def test_prepare_instruction_order_is_skill_persona_business(tmp_path: Path) -> None:
    prepared = await run_prepare(CaseDesignPrepareHandler(), CASE_INPUT, BINDING, tmp_path)
    request = AgentRunRequest.model_validate(prepared.output)
    assert len(request.instructions) == 3
    skill, persona, business = request.instructions
    assert skill.media_type == "text/plain"
    assert persona.media_type == "text/plain"
    assert business.media_type == "application/json"
    assert "Capability-owned case-design skill" in (skill.text_content or "")
    assert "Document-author persona" in (persona.text_content or "")
    payload = cast(Mapping[str, object], business.json_content)
    leafs = payload["capability_leafs"]
    assert payload["change_id"] == "CH-DEMO-001"
    assert payload["selected_test_families"] == ("api", "e2e", "fuzz", "performance")
    assert isinstance(leafs, list | tuple)
    assert tuple(leafs) == VALID_LEAFS
    encoded = request.canonical_bytes().decode("utf-8").lower()
    assert "opencode" not in encoded
    assert "cursor" not in encoded
    assert request.execution.provider_model == "test-model"
    assert request.result_contract.schema_document is not None
    assert request.result_contract.schema_id == "assurance.intake.result.case-design.v1"


@pytest.mark.asyncio
async def test_prepare_rejects_routing_marker_as_invalid_input(tmp_path: Path) -> None:
    binding = {
        **BINDING,
        "execution": {
            **BINDING["execution"],  # type: ignore[arg-type]
            "provider_model": "primary,fallback",
        },
    }
    prepared = await run_prepare(CaseDesignPrepareHandler(), CASE_INPUT, binding, tmp_path)
    assert prepared.status == "failed"
    assert prepared.failure is not None
    assert prepared.failure.kind == "invalid_input"
    assert prepared.failure.retryable is False


@pytest.mark.asyncio
async def test_finalize_rejects_malformed_input(tmp_path: Path) -> None:
    executed = await execute_task(CaseReviewFinalizeHandler(), {"agent_result": {}}, tmp_path)
    assert executed.status == "failed"
    assert executed.failure is not None
    assert executed.failure.kind == "invalid_input"
    assert executed.failure.retryable is False


def _case_review_document(*, missing: list[str]) -> JSONValue:
    skipped = len(missing)
    return cast(
        JSONValue,
        {
            "schema_version": "1.0",
            "review_type": "case",
            "change_id": "CH-DEMO-001",
            "decision": "pass",
            "findings": [],
            "auto_fix_plan": [],
            "next_action": "continue",
            "auto_fix_allowed": False,
            "human_review_required": False,
            "risk_level": "low",
            "minimum_coverage": {
                "total_required": 2,
                "covered": 2 - skipped,
                "skipped_by_scope": skipped,
                "missing": missing,
            },
            "source_verification": {
                "independent": True,
                "reviewed_source_files": ["src/app.py"],
                "verified_claims": [
                    {"claim": "create item persists a menu record", "evidence_files": ["src/app.py"]}
                ],
            },
        },
    )


def _write_review_matrix(workspace: Path, *, missing: list[str]) -> None:
    keys = ["create_item", "update_item"] if not missing else ["create_item", missing[0]]
    rows = []
    for index, key in enumerate(keys, start=1):
        skipped = key in missing
        rows.append(
            {
                "mrc_id": f"MRC-API-{index:03d}",
                "key": key,
                "required": True,
                "covered_by_cases": [] if skipped else ["TC_MENU_001"],
                "status": "skipped_by_scope" if skipped else "covered",
                "skip_reason": "outside locked scope" if skipped else None,
                "category": "api",
                "layer": "api",
            }
        )
    path = workspace / "qa/changes/CH-DEMO-001/trace/minimum-coverage-matrix.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows), encoding="utf-8")


async def _finalize_files(
    handler: TaskHandler,
    structured_result: JSONValue,
    workspace: Path,
    artifact_paths: list[str],
    *,
    change_id: str | None = None,
    selected_test_families: list[str] | None = None,
    write_root: Path | None = None,
    case_delta_paths: list[str] | None = None,
) -> Any:
    result = fake_agent_result(structured_result)
    executed = await execute_task(
        handler,
        cast(
            JSONValue,
            {
                "agent_result": result.model_dump(mode="json"),
                "capability_leafs": list(VALID_LEAFS),
                "artifact_paths": artifact_paths,
                "selected_test_families": selected_test_families or [],
                "case_delta_paths": (
                    case_delta_paths
                    if case_delta_paths is not None
                    else (
                        ["qa/changes/CH-DEMO-001/cases/menus/case.yaml"]
                        if isinstance(handler, CaseDesignFinalizeHandler)
                        else []
                    )
                ),
                **({"change_id": change_id} if change_id is not None else {}),
            },
        ),
        workspace,
        write_root=write_root,
    )
    return executed


def _write_case_delta(workspace: Path, document: object) -> str:
    relative = "qa/changes/CH-DEMO-001/cases/menus/case.yaml"
    path = workspace / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return relative


def _write_case_design_outputs(workspace: Path, document: object) -> list[str]:
    change_root = workspace / "qa/changes/CH-DEMO-001"
    change_root.mkdir(parents=True, exist_ok=True)
    (change_root / ".qa.yaml").write_text("approval:\n  mode: autonomous\n", encoding="utf-8")
    (change_root / "proposal.md").write_text("# Proposal\n", encoding="utf-8")
    relative = _write_case_delta(workspace, document)
    matrix_relative = "qa/changes/CH-DEMO-001/trace/minimum-coverage-matrix.json"
    matrix_path = workspace / matrix_relative
    matrix_path.parent.mkdir(parents=True)
    matrix_path.write_text(
        json.dumps(
            [
                {
                    "mrc_id": "MRC-API-001",
                    "key": "create_item",
                    "required": True,
                    "covered_by_cases": ["TC_MENU_001"],
                    "status": "covered",
                    "skip_reason": None,
                    "category": "api",
                    "layer": "api",
                }
            ]
        ),
        encoding="utf-8",
    )
    return [
        "qa/changes/CH-DEMO-001/.qa.yaml",
        "qa/changes/CH-DEMO-001/proposal.md",
        matrix_relative,
        relative,
    ]


def _with_performance_case(document: object) -> dict[str, Any]:
    result = deepcopy(cast(dict[str, Any], document))
    api_case = cast(dict[str, Any], result["added"][0])
    performance_case = deepcopy(api_case)
    performance_case.update(
        {
            "case_id": "TC_MENU_PERF_001",
            "title": "list menus within the latency budget",
            "type": "Performance",
            "related_cases": ["TC_MENU_001"],
            "automation": {
                "required": True,
                "framework": "locust",
                "status": "planned",
                "performance": {
                    "scenario": {
                        "capability": "entities.item.create",
                        "endpoint": "GET /items",
                        "load": {
                            "concurrency": 10,
                            "spawn_rate_per_second": 2,
                            "duration_seconds": 60,
                        },
                        "thresholds": {"p95_ms": 500, "error_rate_max": 0.01},
                    }
                },
            },
        }
    )
    result["added"].append(performance_case)
    return result


def _valid_explore_advisory() -> dict[str, Any]:
    return {
        "schema_version": "1",
        "change_id": "CH-DEMO-001",
        "context_ref": "explore/context.json",
        "generated_at": "2026-08-23T00:00:00Z",
        "executive_summary": "Department API source was inspected.",
        "watchlist": [],
        "evidence_inventory": {"available": [], "missing": [], "not_inspected": []},
        "source_code_evidence": [],
        "case_design_guidance": {
            "priority_hints": [],
            "suggested_scenarios": [],
            "regression_focus": [],
        },
        "minimum_required_coverage": {"api": []},
        "open_questions_for_case_design": [],
        "test_strategy": {"layer_recommendation": [{"layer": "API", "recommended": True}]},
    }


@pytest.mark.asyncio
async def test_explore_finalize_returns_artifact_digests(tmp_path: Path) -> None:
    project, write_root = dual_roots(tmp_path)
    relative = "qa/changes/CH-DEMO-001/explore/exploration.json"
    path = project / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(_valid_explore_advisory()).encode()
    path.write_bytes(payload)
    result = fake_agent_result({"output_files": [relative]})
    executed = await execute_task(
        ExploreFinalizeHandler(),
        {
            "agent_result": result.model_dump(mode="json"),
            "change_id": "CH-DEMO-001",
            "capability_leafs": list(VALID_LEAFS),
            "artifact_paths": ["qa/changes"],
        },
        project,
        write_root=write_root,
    )
    assert executed.status == "succeeded"
    assert executed.output == {
        "artifacts": [
            {"path": relative, "digest": hashlib.sha256(payload).hexdigest()},
        ]
    }


@pytest.mark.asyncio
async def test_explore_finalize_rejects_placeholder_advisory(tmp_path: Path) -> None:
    project, write_root = dual_roots(tmp_path)
    relative = "qa/changes/CH-DEMO-001/explore/exploration.json"
    path = project / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": "1",
                "change_id": "CH-DEMO-001",
                "watchlist": [],
                "open_questions_for_case_design": [],
            }
        ),
        encoding="utf-8",
    )

    executed = await _finalize_files(
        ExploreFinalizeHandler(),
        {"output_files": [relative]},
        project,
        [relative],
        change_id="CH-DEMO-001",
        write_root=write_root,
    )

    assert executed.status == "failed"
    assert executed.failure is not None
    assert executed.failure.kind == "invalid_output"
    assert "exploration.json" in executed.failure.message


@pytest.mark.asyncio
async def test_intake_finalize_accepts_files_under_locked_prefix(tmp_path: Path) -> None:
    project = tmp_path
    change_root = project / "qa/changes/RET-dept-management"
    relative = "qa/changes/RET-dept-management/requirement.md"
    payload = b"# RET-dept-management\n\nCover department CRUD.\n"
    store = TaskWorkspaceStore(
        project,
        change_root / ".staging",
        change_root / ".runtime/receipts",
    )
    try:
        execute = store.begin(task_id="intake-execute", attempt=1, output_paths=(relative,))
        path = execute.write_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        store.promote(execute.identity, store.seal(execute.identity))

        finalize = store.begin(task_id="intake-finalize", attempt=1, output_paths=())
        assert list(finalize.write_root.iterdir()) == []
        executed = await _finalize_files(
            IntakeFinalizeHandler(),
            {"output_files": [relative]},
            project,
            ["qa/archive", "qa/cases", "qa/changes"],
            write_root=finalize.write_root,
        )
    finally:
        store.close()
    assert executed.status == "succeeded"
    assert executed.output == {
        "artifacts": [{"path": relative, "digest": hashlib.sha256(payload).hexdigest()}]
    }


@pytest.mark.asyncio
async def test_intake_finalize_rejects_file_outside_locked_prefix(tmp_path: Path) -> None:
    project, write_root = dual_roots(tmp_path)
    relative = "qa/notes/outside.md"
    path = project / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"nope\n")
    executed = await _finalize_files(
        IntakeFinalizeHandler(),
        {"output_files": [relative]},
        project,
        ["qa/changes"],
        write_root=write_root,
    )
    assert executed.status == "failed"
    assert executed.failure is not None
    assert executed.failure.kind == "invalid_output"
    assert "undeclared" in executed.failure.message


@pytest.mark.asyncio
async def test_intake_finalize_returns_artifact_digests(tmp_path: Path) -> None:
    project, write_root = dual_roots(tmp_path)
    relative = "qa/changes/CH-DEMO-001/explore/advisory.json"
    path = project / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = b'{"ok":true}'
    path.write_bytes(payload)
    executed = await _finalize_files(
        IntakeFinalizeHandler(),
        {"output_files": [relative]},
        project,
        [relative],
        write_root=write_root,
    )
    assert executed.status == "succeeded"
    assert executed.output == {
        "artifacts": [{"path": relative, "digest": hashlib.sha256(payload).hexdigest()}]
    }


@pytest.mark.asyncio
async def test_intake_finalize_rejects_file_present_only_in_its_write_root(tmp_path: Path) -> None:
    project = tmp_path
    change_root = project / "qa/changes/CH-DEMO-001"
    relative = "qa/changes/CH-DEMO-001/requirement.md"
    store = TaskWorkspaceStore(
        project,
        change_root / ".staging",
        change_root / ".runtime/receipts",
    )
    try:
        finalize = store.begin(task_id="intake-finalize", attempt=1, output_paths=())
        staged_only = finalize.write_root / relative
        staged_only.parent.mkdir(parents=True, exist_ok=True)
        staged_only.write_text("unpromoted\n", encoding="utf-8")

        executed = await _finalize_files(
            IntakeFinalizeHandler(),
            {"output_files": [relative]},
            project,
            [relative],
            write_root=finalize.write_root,
        )
    finally:
        store.close()

    assert executed.status == "failed"
    assert executed.failure is not None
    assert executed.failure.kind == "invalid_output"
    assert executed.failure.message == f"declared output file is missing: {relative}"


@pytest.mark.asyncio
async def test_case_design_finalize_accepts_typed_authoring(tmp_path: Path) -> None:
    authored = cast(
        JSONValue,
        yaml.safe_load((_FIXTURES / "case-authoring-valid.yaml").read_text(encoding="utf-8")),
    )
    project, write_root = dual_roots(tmp_path)
    outputs = _write_case_design_outputs(project, authored)
    executed = await _finalize_files(
        CaseDesignFinalizeHandler(),
        cast(JSONValue, {"output_files": outputs}),
        project,
        ["qa/changes"],
        change_id="CH-DEMO-001",
        selected_test_families=["api"],
        write_root=write_root,
    )
    assert executed.status == "succeeded"
    assert executed.output["added"][0]["trace"] == {"entities.item.create": {"covered": True}}


@pytest.mark.asyncio
async def test_case_design_finalize_rejects_unlocked_module_or_sibling_case(tmp_path: Path) -> None:
    authored = cast(
        JSONValue,
        yaml.safe_load((_FIXTURES / "case-authoring-valid.yaml").read_text(encoding="utf-8")),
    )
    project, write_root = dual_roots(tmp_path)
    outputs = _write_case_design_outputs(project, authored)

    for locked in (
        ["qa/changes/CH-DEMO-001/cases/roles/case.yaml"],
        ["qa/changes/CH-SIBLING/cases/menus/case.yaml"],
    ):
        executed = await _finalize_files(
            CaseDesignFinalizeHandler(),
            cast(JSONValue, {"output_files": outputs}),
            project,
            ["qa/changes"],
            change_id="CH-DEMO-001",
            selected_test_families=["api"],
            write_root=write_root,
            case_delta_paths=locked,
        )
        assert executed.status == "failed"
        assert executed.failure is not None
        assert executed.failure.kind in {"invalid_input", "invalid_output"}


@pytest.mark.asyncio
async def test_case_design_finalize_requires_minimum_coverage_matrix(tmp_path: Path) -> None:
    authored = cast(
        JSONValue,
        yaml.safe_load((_FIXTURES / "case-authoring-valid.yaml").read_text(encoding="utf-8")),
    )
    project, write_root = dual_roots(tmp_path)
    outputs = _write_case_design_outputs(project, authored)
    matrix = "qa/changes/CH-DEMO-001/trace/minimum-coverage-matrix.json"
    outputs.remove(matrix)

    executed = await _finalize_files(
        CaseDesignFinalizeHandler(),
        cast(JSONValue, {"output_files": outputs}),
        project,
        ["qa/changes"],
        change_id="CH-DEMO-001",
        selected_test_families=["api"],
        write_root=write_root,
    )

    assert executed.status == "failed"
    assert executed.failure is not None
    assert executed.failure.kind == "invalid_output"
    assert "minimum-coverage-matrix.json" in executed.failure.message


@pytest.mark.asyncio
async def test_case_design_finalize_rejects_missing_selected_family(tmp_path: Path) -> None:
    authored = cast(
        JSONValue,
        yaml.safe_load((_FIXTURES / "case-authoring-valid.yaml").read_text(encoding="utf-8")),
    )
    project, write_root = dual_roots(tmp_path)
    outputs = _write_case_design_outputs(project, authored)
    executed = await _finalize_files(
        CaseDesignFinalizeHandler(),
        cast(JSONValue, {"output_files": outputs}),
        project,
        ["qa/changes"],
        change_id="CH-DEMO-001",
        selected_test_families=["api", "fuzz"],
        write_root=write_root,
    )
    assert executed.status == "failed"
    assert executed.failure is not None
    assert executed.failure.kind == "invalid_output"
    assert "fuzz" in executed.failure.message


@pytest.mark.asyncio
async def test_case_design_finalize_rejects_legacy_full_delta_result(
    tmp_path: Path,
) -> None:
    authored = yaml.safe_load((_FIXTURES / "case-authoring-valid.yaml").read_text(encoding="utf-8"))
    project, write_root = dual_roots(tmp_path)
    _write_case_design_outputs(project, authored)

    executed = await _finalize_files(
        CaseDesignFinalizeHandler(),
        cast(JSONValue, authored),
        project,
        ["qa/changes"],
        change_id="CH-DEMO-001",
        selected_test_families=["api"],
        write_root=write_root,
    )

    assert executed.status == "failed"
    assert executed.failure is not None
    assert executed.failure.kind == "invalid_output"
    assert "output_files" in executed.failure.message


@pytest.mark.asyncio
async def test_case_design_finalize_requires_every_locked_case_yaml(
    tmp_path: Path,
) -> None:
    authored = yaml.safe_load((_FIXTURES / "case-authoring-valid.yaml").read_text(encoding="utf-8"))
    project, write_root = dual_roots(tmp_path)
    outputs = _write_case_design_outputs(project, authored)
    catalog = [
        "qa/changes/CH-DEMO-001/.qa.yaml",
        "qa/changes/CH-DEMO-001/proposal.md",
        "qa/changes/CH-DEMO-001/trace/minimum-coverage-matrix.json",
    ]
    assert set(catalog) == set(outputs) - {outputs[-1]}

    executed = await _finalize_files(
        CaseDesignFinalizeHandler(),
        cast(JSONValue, {"output_files": catalog}),
        project,
        ["qa/changes"],
        change_id="CH-DEMO-001",
        selected_test_families=[],
        write_root=write_root,
    )

    assert executed.status == "failed"
    assert executed.failure is not None
    assert executed.failure.kind == "invalid_output"
    assert "case_delta_paths" in executed.failure.message


@pytest.mark.asyncio
async def test_case_design_finalize_rejects_invalid_written_case_yaml(tmp_path: Path) -> None:
    base = yaml.safe_load((_FIXTURES / "case-authoring-valid.yaml").read_text(encoding="utf-8"))
    structured = _with_performance_case(base)
    authored = deepcopy(structured)
    authored["added"][1]["automation"]["performance"]["scenario"]["endpoint"] = "menu tree listing"
    project, write_root = dual_roots(tmp_path)
    outputs = _write_case_design_outputs(project, authored)

    executed = await _finalize_files(
        CaseDesignFinalizeHandler(),
        cast(JSONValue, {"output_files": outputs}),
        project,
        ["qa/changes"],
        change_id="CH-DEMO-001",
        selected_test_families=["api", "performance"],
        write_root=write_root,
    )

    assert executed.status == "failed"
    assert executed.failure is not None
    assert executed.failure.kind == "invalid_output"
    assert "endpoint" in executed.failure.message


@pytest.mark.asyncio
async def test_case_review_finalize_accepts_typed_review(tmp_path: Path) -> None:
    _write_review_matrix(tmp_path, missing=[])
    executed = await _finalize_files(
        CaseReviewFinalizeHandler(),
        _case_review_document(missing=[]),
        tmp_path,
        [],
    )
    assert executed.status == "succeeded"
    assert executed.output["decision"] == "pass"
    assert executed.output["minimum_coverage"]["missing"] == []


@pytest.mark.asyncio
async def test_case_review_finalize_replaces_projection_drift_from_authenticated_matrix(
    tmp_path: Path,
) -> None:
    _write_review_matrix(tmp_path, missing=["skipped_item"])
    result = fake_agent_result(_case_review_document(missing=["entities.item"]))
    outcome = await run_finalize(CaseReviewFinalizeHandler(), result, tmp_path)
    assert outcome.status == "succeeded"
    assert outcome.output["minimum_coverage"] == {
        "total_required": 2,
        "covered": 1,
        "skipped_by_scope": 1,
        "missing": ["skipped_item"],
    }


@pytest.mark.asyncio
async def test_explore_finalize_rejects_empty_artifact_paths(tmp_path: Path) -> None:
    project, write_root = dual_roots(tmp_path)
    relative = "qa/changes/CH-DEMO-001/explore/exploration.json"
    path = project / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_valid_explore_advisory()), encoding="utf-8")
    executed = await _finalize_files(
        ExploreFinalizeHandler(),
        {"output_files": [relative]},
        project,
        [],
        change_id="CH-DEMO-001",
        write_root=write_root,
    )
    assert executed.status == "failed"
    assert executed.failure is not None
    assert executed.failure.kind == "invalid_input"
    assert executed.failure.retryable is False


@pytest.mark.asyncio
async def test_explore_finalize_rejects_path_escape(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (tmp_path / "secret.json").write_bytes(b'{"secret":true}')
    executed = await _finalize_files(
        ExploreFinalizeHandler(),
        {"output_files": ["../secret.json"]},
        workspace,
        ["qa/changes/CH-DEMO-001/explore/exploration.json"],
        change_id="CH-DEMO-001",
    )
    assert executed.status == "failed"
    assert executed.failure is not None
    assert executed.failure.kind == "invalid_output"
    assert executed.failure.retryable is True
    assert executed.output is None


@pytest.mark.asyncio
async def test_failed_case_design_validation_leaves_canonical_outputs_unchanged(tmp_path: Path) -> None:
    project, write_root = dual_roots(tmp_path)
    authored = yaml.safe_load((_FIXTURES / "case-authoring-valid.yaml").read_text(encoding="utf-8"))
    canonical = project / "qa/changes/CH-DEMO-001/proposal.md"
    canonical.parent.mkdir(parents=True, exist_ok=True)
    original = b"# Canonical proposal\n"
    canonical.write_bytes(original)
    _write_case_design_outputs(write_root, authored)
    executed = await _finalize_files(
        CaseDesignFinalizeHandler(),
        cast(JSONValue, authored),
        project,
        ["qa/changes"],
        change_id="CH-DEMO-001",
        selected_test_families=["api"],
        write_root=write_root,
    )
    assert executed.status == "failed"
    assert executed.failure is not None
    assert executed.failure.kind == "invalid_output"
    assert canonical.read_bytes() == original
