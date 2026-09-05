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
from tests.phase4.agent_harness import FakeAgentAdapter
from tests.product.test_change_local_output_routing import dual_roots, execute_task
from tests.acg_plan_fixture import install_plan

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
from assurance_intake.contracts.agent import ArtifactListResultV1
from assurance_intake.contracts.attempts import AGENT_JOB_CONTRACTS
from assurance_intake.resource_loader import resource_text

_SHA = "a" * 64
_PLAN_DIGEST = "31e8e6ccff373c935bf09f5f83763f327bd54bc3c96a11b9db3bca1b7b22fa00"
_PLAN_REF: dict[str, JSONValue] = {
    "path": f"qa/changes/CH-DEMO-001/plan/{_PLAN_DIGEST}/resolved-assurance-plan.json",
    "digest": "a50f41ee57345754b5d4f2f3609baef0d8fe1ba9c0e50abaf09902d37e4150e2",
}
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
    "selected_test_families": ["api"],
    "plan_digest": _PLAN_DIGEST,
    "plan_ref": _PLAN_REF,
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
    if type(handler).__name__.startswith("Case"):
        exploration = workspace / "qa/changes/CH-DEMO-001/explore/exploration.json"
        minimum_required_coverage = None
        mismatched_exploration: bytes | None = None
        if exploration.is_file():
            existing_bytes = exploration.read_bytes()
            existing = json.loads(existing_bytes)
            if isinstance(existing, Mapping):
                candidate = existing.get("minimum_required_coverage")
                if isinstance(candidate, Mapping):
                    minimum_required_coverage = candidate
                if existing.get("change_id") != "CH-DEMO-001":
                    mismatched_exploration = existing_bytes
        plan, plan_ref = install_plan(
            workspace,
            "CH-DEMO-001",
            capability_leafs=VALID_LEAFS,
            minimum_required_coverage=minimum_required_coverage,
        )
        if isinstance(payload, dict):
            payload = {
                **payload,
                "plan_digest": plan.plan_digest,
                "plan_ref": cast(JSONValue, plan_ref),
            }
        if mismatched_exploration is not None:
            exploration.write_bytes(mismatched_exploration)
    if type(handler).__name__.startswith("Explore") and isinstance(payload, dict):
        payload = {**payload, "candidate_test_families": ["api"]}
    return await execute_task(handler, payload, workspace, binding_data=binding, write_root=write_root)


async def run_finalize(
    handler: TaskHandler,
    result: AgentRunResult,
    workspace: Path,
    write_root: Path | None = None,
) -> TaskOutcome:
    business: dict[str, JSONValue] = {
        "agent_result": result.model_dump(mode="json"),
        "capability_leafs": list(VALID_LEAFS),
        "artifact_paths": [],
    }
    if type(handler).__name__.startswith("Case"):
        install_plan(
            workspace,
            "CH-DEMO-001",
            capability_leafs=VALID_LEAFS,
        )
        business.update({"plan_digest": _PLAN_DIGEST, "plan_ref": _PLAN_REF})
    executed = await execute_task(
        handler,
        business,
        workspace,
        write_root=write_root,
    )
    return executed.outcome


def fake_agent_result(structured_result: JSONValue) -> AgentRunResult:
    return AgentRunResult(
        result_payload=structured_result,
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
    assert '`schema_version` must be exactly `"1"`' in skill
    assert "schemas/explore-advisory.schema.json" not in skill
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
        assert "Do not use glob to check either Explore path" in content

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
    assert "Never inspect `.qa.yaml` for Explore state" in skill
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


def test_case_design_review_reentry_preserves_unnamed_trace_keys() -> None:
    skill = " ".join(resource_text("skills/aa-case-design/SKILL.md").split())

    assert (
        "If no auto-fix finding names a case trace field, preserve every existing trace key and value byte-for-byte"
        in skill
    )
    assert "Do not add a family-matching adapter key while rewriting the case file" in skill
    assert (
        "compare the complete post-edit trace-key set with capability_leafs by exact string membership"
        in skill
    )
    assert "use `apply_patch` only" in skill
    assert "Never replace an existing `case.yaml` as a whole" in skill


def test_case_repair_skill_is_locator_bounded() -> None:
    skill = " ".join(resource_text("skills/aa-case-repair/SKILL.md").split())

    assert "review_repair" in skill
    assert "Only edit the exact artifact, case_id, and allowed_paths" in skill
    assert "Do not add, remove, reorder, or rewrite any case" in skill
    assert "Do not modify any non-target output file" in skill
    assert "complete `## ` heading" in skill
    assert "exact `mrc_id` values" in skill
    assert "`.qa.yaml` is never an automatic repair target" in skill
    assert "apply_patch" in skill


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
    assert '"edits":["one exact edit instruction"]' in skill
    assert "split it into one finding per artifact" in skill
    assert "`auto_fix_plan[].artifact` MUST equal that finding's `locator.artifact`" in skill
    assert 'use `"key":"steps,assertions"`' in skill.lower()
    assert '`"key":"steps/assertions"` is invalid' in skill


def test_case_reviewer_routes_source_proven_case_defects_to_agent_fix_loop() -> None:
    skill = resource_text("skills/aa-case-reviewer/SKILL.md")

    assert "Severity alone does not require human review" in skill
    assert "A high-severity finding may still be mechanically fixable" in skill
    assert "must use `needs_fix`" in skill
    assert "Risk level is high or critical" not in skill


def test_case_reviewer_uses_locked_requirement_and_reports_findings_exhaustively() -> None:
    resources = {
        "skill": resource_text("skills/aa-case-reviewer/SKILL.md"),
        "persona": resource_text("personas/reviewer.md"),
        "prompt": resource_text("prompts/case-review.md"),
    }

    for content in resources.values():
        assert "requirement.md" in content
        assert "explicit numerical thresholds and load values" in content
        assert "complete all review criteria before writing the verdict" in content

    designer = resource_text("skills/aa-case-design/SKILL.md")
    assert "Apply every listed auto-fix finding in one pass" in designer
    assert "re-run the complete self-review against the resulting files" in designer


def test_case_skills_do_not_treat_ignore_aware_search_as_source_absence() -> None:
    designer = resource_text("skills/aa-case-design/SKILL.md")
    reviewer = resource_text("skills/aa-case-reviewer/SKILL.md")

    for skill in (designer, reviewer):
        assert "A glob result of `No files found` is not evidence that product source is absent" in skill
        assert "ignored product source" in skill
        assert "path-scoped grep" in skill
    assert "read every exact product-source path from that review before changing" in designer
    assert "read those exact paths independently before attempting discovery" in reviewer


def test_case_skills_resolve_e2e_entry_from_ignored_menu_source_before_human_review() -> None:
    designer = " ".join(resource_text("skills/aa-case-design/SKILL.md").split())
    reviewer = " ".join(resource_text("skills/aa-case-reviewer/SKILL.md").split())

    for skill in (designer, reviewer):
        assert (
            "path-scoped search for the exact component path and page or menu label under `app/` and `web/src/`"
            in skill
        )
        assert "even when those roots are ignored" in skill
    assert "do not escalate the E2E entry mechanism to human review" in reviewer
    assert "route it as bounded `needs_fix`" in reviewer


def test_case_skills_keep_advisory_mrc_complete_without_inventing_human_blockers() -> None:
    designer = " ".join(resource_text("skills/aa-case-design/SKILL.md").split())
    reviewer = " ".join(resource_text("skills/aa-case-reviewer/SKILL.md").split())

    assert "every advisory MRC item still gets exactly one matrix row" in designer
    assert "advisory expansion lacks a frozen oracle" in designer
    assert "not as an implicit business oracle" in reviewer
    assert "Do not create a blocking `needs_review` item" in reviewer
    assert "Never require a proposed MRC key in case `trace`" in reviewer


def test_case_prompts_prevent_incremental_closed_key_review_churn() -> None:
    designer = " ".join(resource_text("prompts/case-design.md").split())
    reviewer = " ".join(resource_text("prompts/case-review.md").split())
    persona = " ".join(resource_text("personas/reviewer.md").split())

    assert "product source is verification evidence, not a frozen business oracle" in designer.lower()
    assert "classify every MRC row in one complete pass" in designer
    assert "audit every MRC row in one complete pass" in reviewer
    assert "product source is verification evidence, not a frozen business oracle" in reviewer.lower()
    assert "report all currently observable closed-key defects together" in persona.lower()
    assert "an absent stable target is not a finding" in persona.lower()


def test_case_reviewer_closed_key_repairs_stay_inside_case_design_write_set() -> None:
    designer = " ".join(resource_text("skills/aa-case-design/SKILL.md").split())
    reviewer = " ".join(resource_text("skills/aa-case-reviewer/SKILL.md").split())

    assert "Do not create a data-knowledge proposal file; it is not an authorized output" in designer
    assert "Do not instruct `aa-case-design` to create a data-knowledge proposal" in reviewer
    assert "mark that exact matrix row `skipped_by_scope`" in reviewer
    assert "Every automatic repair artifact must be an authorized case-design output" in reviewer
    assert "unknown keys require a knowledge proposal" not in reviewer


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
async def test_case_design_prepare_includes_deterministic_validation_feedback(tmp_path: Path) -> None:
    prepared = await run_prepare(
        CaseDesignPrepareHandler(),
        {
            **CASE_INPUT,
            "validation_attempt": 1,
            "validation_error": (
                "capability key is not a declared typed leaf: "
                "entities.dept.constraints.unauthorized_user_management_api_access"
            ),
        },
        BINDING,
        tmp_path,
    )

    assert prepared.status == "succeeded"
    request = AgentRunRequest.model_validate(prepared.output)
    business = cast(Mapping[str, object], request.instructions[2].json_content)
    assert business["validation_attempt"] == 1
    assert business["validation_error"] == (
        "capability key is not a declared typed leaf: "
        "entities.dept.constraints.unauthorized_user_management_api_access"
    )


@pytest.mark.asyncio
async def test_case_design_prepare_builds_a_deterministic_review_repair_contract(
    tmp_path: Path,
) -> None:
    authored = yaml.safe_load((_FIXTURES / "case-authoring-valid.yaml").read_text(encoding="utf-8"))
    outputs = _write_case_design_outputs(tmp_path, authored)
    _write_fixable_case_review(tmp_path, allowed_key="title")

    prepared = await run_prepare(CaseDesignPrepareHandler(), CASE_INPUT, BINDING, tmp_path)

    assert prepared.status == "succeeded"
    request = AgentRunRequest.model_validate(prepared.output)
    assert "Locator-bounded case repair" in (request.instructions[0].text_content or "")
    business = cast(Mapping[str, object], request.instructions[2].json_content)
    repair = cast(Mapping[str, object], business["review_repair"])
    actions = cast(tuple[object, ...], repair["actions"])
    action = cast(Mapping[str, object], actions[0])
    assert action["finding_id"] == "CR-001"
    assert action["case_id"] == "TC_MENU_001"
    assert action["allowed_paths"] == ("title",)
    assert set(cast(Mapping[str, str], repair["baseline_file_digests"])) == set(outputs)


@pytest.mark.asyncio
async def test_case_design_prepare_rejects_action_repair_alias(tmp_path: Path) -> None:
    authored = yaml.safe_load((_FIXTURES / "case-authoring-valid.yaml").read_text(encoding="utf-8"))
    _write_case_design_outputs(tmp_path, authored)
    _write_fixable_case_review(tmp_path, allowed_key="title")
    review_path = tmp_path / "qa/changes/CH-DEMO-001/review/case-review.json"
    review = json.loads(review_path.read_text(encoding="utf-8"))
    review["auto_fix_plan"][0].pop("case_id")
    review["auto_fix_plan"][0]["action"] = review["auto_fix_plan"][0].pop("edits")[0]
    review_path.write_text(json.dumps(review), encoding="utf-8")

    prepared = await run_prepare(CaseDesignPrepareHandler(), CASE_INPUT, BINDING, tmp_path)

    assert prepared.status == "failed"
    assert prepared.failure is not None
    assert "edits" in (prepared.failure.message or "")


@pytest.mark.asyncio
async def test_case_design_prepare_accepts_exact_document_section_repair_locator(
    tmp_path: Path,
) -> None:
    authored = yaml.safe_load((_FIXTURES / "case-authoring-valid.yaml").read_text(encoding="utf-8"))
    _write_case_design_outputs(tmp_path, authored)
    _write_fixable_case_review(tmp_path, allowed_key="title")
    review_path = tmp_path / "qa/changes/CH-DEMO-001/review/case-review.json"
    review = json.loads(review_path.read_text(encoding="utf-8"))
    artifact = "qa/changes/CH-DEMO-001/proposal.md"
    review["findings"][0]["locator"] = {
        "artifact": artifact,
        "case_id": None,
        "key": "## Test Conditions",
    }
    review["auto_fix_plan"] = [
        {
            "finding_id": "CR-001",
            "artifact": artifact,
            "edits": ["Revise only the Test Conditions section."],
        }
    ]
    review_path.write_text(json.dumps(review), encoding="utf-8")

    prepared = await run_prepare(CaseDesignPrepareHandler(), CASE_INPUT, BINDING, tmp_path)

    assert prepared.status == "succeeded"
    request = AgentRunRequest.model_validate(prepared.output)
    business = cast(Mapping[str, object], request.instructions[2].json_content)
    repair = cast(Mapping[str, object], business["review_repair"])
    action = cast(Mapping[str, object], cast(tuple[object, ...], repair["actions"])[0])
    assert action["allowed_paths"] == ("## Test Conditions",)


@pytest.mark.asyncio
async def test_case_design_prepare_rejects_bare_proposal_section_locator(tmp_path: Path) -> None:
    authored = yaml.safe_load((_FIXTURES / "case-authoring-valid.yaml").read_text(encoding="utf-8"))
    _write_case_design_outputs(tmp_path, authored)
    artifact = "qa/changes/CH-DEMO-001/proposal.md"
    _write_fixable_case_review(
        tmp_path,
        allowed_key="Test Conditions",
        artifact=artifact,
        case_id=None,
    )

    prepared = await run_prepare(CaseDesignPrepareHandler(), CASE_INPUT, BINDING, tmp_path)

    assert prepared.status == "failed"
    assert prepared.failure is not None
    assert "full level-two Markdown heading" in prepared.failure.message


@pytest.mark.asyncio
async def test_case_design_prepare_rejects_qa_yaml_automatic_repair(tmp_path: Path) -> None:
    authored = yaml.safe_load((_FIXTURES / "case-authoring-valid.yaml").read_text(encoding="utf-8"))
    _write_case_design_outputs(tmp_path, authored)
    _write_fixable_case_review(
        tmp_path,
        allowed_key="approval.mode",
        artifact="qa/changes/CH-DEMO-001/.qa.yaml",
        case_id=None,
    )

    prepared = await run_prepare(CaseDesignPrepareHandler(), CASE_INPUT, BINDING, tmp_path)

    assert prepared.status == "failed"
    assert prepared.failure is not None
    assert ".qa.yaml cannot be repaired automatically" in prepared.failure.message


@pytest.mark.asyncio
async def test_case_design_prepare_rejects_mrc_field_selector_locator(tmp_path: Path) -> None:
    authored = yaml.safe_load((_FIXTURES / "case-authoring-valid.yaml").read_text(encoding="utf-8"))
    _write_case_design_outputs(tmp_path, authored)
    _write_fixable_case_review(
        tmp_path,
        allowed_key="MRC-API-001.status",
        artifact="qa/changes/CH-DEMO-001/trace/minimum-coverage-matrix.json",
        case_id=None,
    )

    prepared = await run_prepare(CaseDesignPrepareHandler(), CASE_INPUT, BINDING, tmp_path)

    assert prepared.status == "failed"
    assert prepared.failure is not None
    assert "exact mrc_id values" in prepared.failure.message


@pytest.mark.asyncio
async def test_case_design_prepare_preserves_mrc_locator_baseline_order(tmp_path: Path) -> None:
    authored = yaml.safe_load((_FIXTURES / "case-authoring-valid.yaml").read_text(encoding="utf-8"))
    _write_case_design_outputs(tmp_path, authored)
    artifact = "qa/changes/CH-DEMO-001/trace/minimum-coverage-matrix.json"
    matrix_path = tmp_path / artifact
    rows = json.loads(matrix_path.read_text(encoding="utf-8"))
    second = deepcopy(rows[0])
    rows[0].update({"mrc_id": "MRC-Z", "key": "z_item"})
    second.update({"mrc_id": "MRC-A", "key": "a_item"})
    rows.append(second)
    matrix_path.write_text(json.dumps(rows), encoding="utf-8")
    _write_fixable_case_review(
        tmp_path,
        allowed_key="MRC-Z,MRC-A",
        artifact=artifact,
        case_id=None,
    )

    prepared = await run_prepare(CaseDesignPrepareHandler(), CASE_INPUT, BINDING, tmp_path)

    assert prepared.status == "succeeded"
    request = AgentRunRequest.model_validate(prepared.output)
    business = cast(Mapping[str, object], request.instructions[2].json_content)
    repair = cast(Mapping[str, object], business["review_repair"])
    action = cast(Mapping[str, object], cast(tuple[object, ...], repair["actions"])[0])
    assert action["allowed_paths"] == ("MRC-Z", "MRC-A")


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
async def test_case_design_prepare_reads_plan_bound_exploration_for_standalone_case(
    tmp_path: Path,
) -> None:
    prepared = await run_prepare(CaseDesignPrepareHandler(), CASE_INPUT, BINDING, tmp_path)

    assert prepared.status == "succeeded"
    request = AgentRunRequest.model_validate(prepared.output)
    business = cast(Mapping[str, object], request.instructions[2].json_content)
    assert isinstance(business["exploration"], Mapping)


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
    (change_root / "requirement.md").write_text(
        "# Requirement\n\nP95 must be at most 500 ms.\n", encoding="utf-8"
    )
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
        "qa/changes/CH-DEMO-001/requirement.md",
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
    assert payload["selected_test_families"] == ("api",)
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
    validation_attempt: int | None = None,
    review_repair: object | None = None,
    coverage_epoch: int = 0,
    review_round: int = 0,
    preparation_refs: list[dict[str, str]] | None = None,
    case_refs: list[dict[str, str]] | None = None,
) -> Any:
    result = fake_agent_result(structured_result)
    plan = None
    plan_ref = None
    if type(handler).__name__.startswith("Case"):
        selected = tuple(cast(Any, selected_test_families or ["api"]))
        plan, plan_ref = install_plan(
            workspace,
            change_id or "CH-DEMO-001",
            capability_leafs=VALID_LEAFS,
            candidates=selected,
            proposed=selected,
        )
    bound_preparation_refs = list(preparation_refs or [])
    if plan_ref is not None and plan_ref not in bound_preparation_refs:
        bound_preparation_refs.append(plan_ref)
        bound_preparation_refs.sort(key=lambda item: (item["path"], item["digest"]))
    finalize_input: dict[str, object] = {
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
        **({"validation_attempt": validation_attempt} if validation_attempt is not None else {}),
        **({"review_repair": review_repair} if review_repair is not None else {}),
        "coverage_epoch": coverage_epoch,
        "review_round": review_round,
        "preparation_refs": bound_preparation_refs,
        "case_refs": case_refs or [],
        **(
            {"plan_digest": plan.plan_digest, "plan_ref": plan_ref}
            if plan is not None and plan_ref is not None
            else {}
        ),
    }
    executed = await execute_task(
        handler,
        cast(JSONValue, finalize_input),
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


def _write_fixable_case_review(
    workspace: Path,
    *,
    allowed_key: str,
    artifact: str = "qa/changes/CH-DEMO-001/cases/menus/case.yaml",
    case_id: str | None = "TC_MENU_001",
) -> None:
    document = cast(dict[str, Any], _case_review_document(missing=[]))
    document.update(
        {
            "decision": "needs_fix",
            "findings": [
                {
                    "id": "CR-001",
                    "severity": "medium",
                    "category": "case_quality",
                    "message": "Apply one exact case-field repair.",
                    "locator": {
                        "artifact": artifact,
                        "case_id": case_id,
                        "key": allowed_key,
                    },
                    "auto_fix_allowed": True,
                    "human_review_required": False,
                }
            ],
            "auto_fix_plan": [
                {
                    "finding_id": "CR-001",
                    "artifact": artifact,
                    "case_id": case_id,
                    "edits": [f"Update only {allowed_key}."],
                }
            ],
            "next_action": "run_case_design",
            "auto_fix_allowed": True,
        }
    )
    path = workspace / "qa/changes/CH-DEMO-001/review/case-review.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")


async def _prepared_review_repair(workspace: Path) -> Mapping[str, object]:
    prepared = await run_prepare(CaseDesignPrepareHandler(), CASE_INPUT, BINDING, workspace)
    assert prepared.status == "succeeded"
    request = AgentRunRequest.model_validate(prepared.output)
    dumped = request.model_dump(mode="json")
    instructions = cast(list[dict[str, object]], dumped["instructions"])
    business = cast(dict[str, object], instructions[2]["json_content"])
    return cast(Mapping[str, object], business["review_repair"])


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
        "test_strategy": {
            "scope": None,
            "data_focus": [],
            "depth": "smoke",
            "layer_recommendation": [
                {
                    "layer": layer,
                    "recommended": False,
                    "rationale": "No authenticated source evidence was available.",
                    "evidence_ids": [],
                }
                for layer in ("API", "E2E", "Fuzz", "Performance")
            ],
            "approach": None,
        },
    }


@pytest.mark.asyncio
async def test_explore_finalize_returns_artifact_digests(tmp_path: Path) -> None:
    project, write_root = dual_roots(tmp_path)
    relative = "qa/changes/CH-DEMO-001/explore/exploration.json"
    path = write_root / relative
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
    contract = AGENT_JOB_CONTRACTS["explore"]
    assert contract.agent_result_model is ArtifactListResultV1
    contract.output_model.model_validate(executed.output)


@pytest.mark.asyncio
async def test_explore_finalize_rejects_placeholder_advisory(tmp_path: Path) -> None:
    project, write_root = dual_roots(tmp_path)
    relative = "qa/changes/CH-DEMO-001/explore/exploration.json"
    path = write_root / relative
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
async def test_explore_finalize_rejects_a_declared_missing_advisory_as_invalid_output(
    tmp_path: Path,
) -> None:
    project, write_root = dual_roots(tmp_path)
    relative = "qa/changes/CH-DEMO-001/explore/exploration.json"

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
    assert executed.failure.retryable is True
    assert executed.failure.message == f"declared output file is missing: {relative}"


@pytest.mark.asyncio
async def test_intake_finalize_accepts_files_under_locked_prefix(tmp_path: Path) -> None:
    project, write_root = dual_roots(tmp_path)
    relative = "qa/changes/RET-dept-management/requirement.md"
    payload = b"# RET-dept-management\n\nCover department CRUD.\n"
    path = write_root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)

    executed = await _finalize_files(
        IntakeFinalizeHandler(),
        {"output_files": [relative]},
        project,
        ["qa/archive", "qa/cases", "qa/changes"],
        write_root=write_root,
    )
    assert executed.status == "succeeded"
    assert executed.output == {
        "artifacts": [{"path": relative, "digest": hashlib.sha256(payload).hexdigest()}]
    }


@pytest.mark.asyncio
async def test_intake_finalize_rejects_file_outside_locked_prefix(tmp_path: Path) -> None:
    project, write_root = dual_roots(tmp_path)
    relative = "qa/notes/outside.md"
    path = write_root / relative
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
    path = write_root / relative
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
    AGENT_JOB_CONTRACTS["intake"].output_model.model_validate(executed.output)


@pytest.mark.asyncio
async def test_intake_finalize_rejects_stale_canonical_file_when_candidate_is_missing(
    tmp_path: Path,
) -> None:
    project, write_root = dual_roots(tmp_path)
    relative = "qa/changes/CH-DEMO-001/requirement.md"
    stale = project / relative
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text("stale canonical content\n", encoding="utf-8")

    executed = await _finalize_files(
        IntakeFinalizeHandler(),
        {"output_files": [relative]},
        project,
        [relative],
        write_root=write_root,
    )

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
    outputs = _write_case_design_outputs(write_root, authored)
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
    assert executed.output["validation_status"] == "pass"
    assert [artifact["path"] for artifact in executed.output["artifacts"]] == sorted(outputs)
    assert "added" not in executed.output
    AGENT_JOB_CONTRACTS["case-design"].output_model.model_validate(executed.output)


@pytest.mark.asyncio
async def test_case_design_finalize_accepts_only_the_review_locator_change(tmp_path: Path) -> None:
    authored = yaml.safe_load((_FIXTURES / "case-authoring-valid.yaml").read_text(encoding="utf-8"))
    project, write_root = dual_roots(tmp_path)
    outputs = _write_case_design_outputs(project, authored)
    _write_fixable_case_review(project, allowed_key="title")
    repair = await _prepared_review_repair(project)
    authored["added"][0]["title"] = "create menu with validated response"
    _write_case_delta(write_root, authored)

    executed = await _finalize_files(
        CaseDesignFinalizeHandler(),
        cast(JSONValue, {"output_files": outputs}),
        project,
        ["qa/changes"],
        change_id="CH-DEMO-001",
        selected_test_families=["api"],
        write_root=write_root,
        review_repair=repair,
    )

    assert executed.status == "succeeded"
    assert executed.output["validation_status"] == "pass"


@pytest.mark.asyncio
async def test_case_design_finalize_reads_unchanged_review_outputs_from_baseline(
    tmp_path: Path,
) -> None:
    authored = yaml.safe_load((_FIXTURES / "case-authoring-valid.yaml").read_text(encoding="utf-8"))
    project, write_root = dual_roots(tmp_path)
    outputs = _write_case_design_outputs(project, authored)
    _write_fixable_case_review(project, allowed_key="title")
    repair = await _prepared_review_repair(project)
    authored["added"][0]["title"] = "create menu with validated response"
    _write_case_delta(write_root, authored)

    executed = await _finalize_files(
        CaseDesignFinalizeHandler(),
        cast(JSONValue, {"output_files": outputs}),
        project,
        ["qa/changes"],
        change_id="CH-DEMO-001",
        selected_test_families=["api"],
        write_root=write_root,
        review_repair=repair,
    )

    assert executed.status == "succeeded"
    assert executed.output["validation_status"] == "pass"
    assert [artifact["path"] for artifact in executed.output["artifacts"]] == sorted(outputs)
    assert (write_root / "qa/changes/CH-DEMO-001/cases/menus/case.yaml").is_file()
    assert not (write_root / "qa/changes/CH-DEMO-001/.qa.yaml").exists()
    assert not (write_root / "qa/changes/CH-DEMO-001/proposal.md").exists()
    assert not (write_root / "qa/changes/CH-DEMO-001/trace/minimum-coverage-matrix.json").exists()


@pytest.mark.asyncio
async def test_case_design_finalize_accepts_only_the_named_proposal_section(tmp_path: Path) -> None:
    authored = yaml.safe_load((_FIXTURES / "case-authoring-valid.yaml").read_text(encoding="utf-8"))
    project, write_root = dual_roots(tmp_path)
    outputs = _write_case_design_outputs(project, authored)
    proposal = project / "qa/changes/CH-DEMO-001/proposal.md"
    proposal.write_text(
        "# Proposal\n\n## Data Needs\n- old need\n\n## Other\n- unchanged\n",
        encoding="utf-8",
    )
    artifact = "qa/changes/CH-DEMO-001/proposal.md"
    _write_fixable_case_review(
        project,
        allowed_key="## Data Needs",
        artifact=artifact,
        case_id=None,
    )
    repair = await _prepared_review_repair(project)
    staged = write_root / artifact
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_text(
        "# Proposal\n\n## Data Needs\n- repaired need\n\n## Other\n- unchanged\n",
        encoding="utf-8",
    )

    executed = await _finalize_files(
        CaseDesignFinalizeHandler(),
        cast(JSONValue, {"output_files": outputs}),
        project,
        ["qa/changes"],
        change_id="CH-DEMO-001",
        selected_test_families=["api"],
        write_root=write_root,
        review_repair=repair,
    )

    assert executed.status == "succeeded"
    assert executed.output["validation_status"] == "pass"


@pytest.mark.asyncio
async def test_case_design_finalize_rejects_proposal_change_outside_named_section(
    tmp_path: Path,
) -> None:
    authored = yaml.safe_load((_FIXTURES / "case-authoring-valid.yaml").read_text(encoding="utf-8"))
    project, write_root = dual_roots(tmp_path)
    outputs = _write_case_design_outputs(project, authored)
    proposal = project / "qa/changes/CH-DEMO-001/proposal.md"
    proposal.write_text(
        "# Proposal\n\n## Data Needs\n- old need\n\n## Other\n- unchanged\n",
        encoding="utf-8",
    )
    artifact = "qa/changes/CH-DEMO-001/proposal.md"
    _write_fixable_case_review(
        project,
        allowed_key="## Data Needs",
        artifact=artifact,
        case_id=None,
    )
    repair = await _prepared_review_repair(project)
    staged = write_root / artifact
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_text(
        "# Proposal\n\n## Data Needs\n- repaired need\n\n## Other\n- unauthorized\n",
        encoding="utf-8",
    )

    executed = await _finalize_files(
        CaseDesignFinalizeHandler(),
        cast(JSONValue, {"output_files": outputs}),
        project,
        ["qa/changes"],
        change_id="CH-DEMO-001",
        selected_test_families=["api"],
        write_root=write_root,
        validation_attempt=1,
        review_repair=repair,
    )

    assert executed.status == "failed"
    assert executed.failure is not None
    assert "outside named section" in executed.failure.message


@pytest.mark.asyncio
async def test_case_design_finalize_treats_h1_as_end_of_named_proposal_section(
    tmp_path: Path,
) -> None:
    authored = yaml.safe_load((_FIXTURES / "case-authoring-valid.yaml").read_text(encoding="utf-8"))
    project, write_root = dual_roots(tmp_path)
    outputs = _write_case_design_outputs(project, authored)
    proposal = project / "qa/changes/CH-DEMO-001/proposal.md"
    proposal.write_text(
        "# Proposal\n\n## Data Needs\n- old need\n\n# Appendix\n- unchanged\n",
        encoding="utf-8",
    )
    artifact = "qa/changes/CH-DEMO-001/proposal.md"
    _write_fixable_case_review(
        project,
        allowed_key="## Data Needs",
        artifact=artifact,
        case_id=None,
    )
    repair = await _prepared_review_repair(project)
    staged = write_root / artifact
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_text(
        "# Proposal\n\n## Data Needs\n- repaired need\n\n# Appendix\n- unauthorized\n",
        encoding="utf-8",
    )

    executed = await _finalize_files(
        CaseDesignFinalizeHandler(),
        cast(JSONValue, {"output_files": outputs}),
        project,
        ["qa/changes"],
        change_id="CH-DEMO-001",
        selected_test_families=["api"],
        write_root=write_root,
        validation_attempt=1,
        review_repair=repair,
    )

    assert executed.status == "failed"
    assert executed.failure is not None
    assert "outside named section" in executed.failure.message


@pytest.mark.asyncio
async def test_case_design_finalize_ignores_heading_inside_proposal_fence(tmp_path: Path) -> None:
    authored = yaml.safe_load((_FIXTURES / "case-authoring-valid.yaml").read_text(encoding="utf-8"))
    project, write_root = dual_roots(tmp_path)
    outputs = _write_case_design_outputs(project, authored)
    proposal = project / "qa/changes/CH-DEMO-001/proposal.md"
    proposal.write_text(
        "# Proposal\n\n```md\n## Data Needs\nexample\n```\n\n## Data Needs\n- old need\n",
        encoding="utf-8",
    )
    artifact = "qa/changes/CH-DEMO-001/proposal.md"
    _write_fixable_case_review(
        project,
        allowed_key="## Data Needs",
        artifact=artifact,
        case_id=None,
    )
    repair = await _prepared_review_repair(project)
    staged = write_root / artifact
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_text(
        "# Proposal\n\n```md\n## Data Needs\nexample\n```\n\n## Data Needs\n- repaired need\n",
        encoding="utf-8",
    )

    executed = await _finalize_files(
        CaseDesignFinalizeHandler(),
        cast(JSONValue, {"output_files": outputs}),
        project,
        ["qa/changes"],
        change_id="CH-DEMO-001",
        selected_test_families=["api"],
        write_root=write_root,
        review_repair=repair,
    )

    assert executed.status == "succeeded"


@pytest.mark.asyncio
async def test_case_design_finalize_accepts_only_named_mrc_rows(tmp_path: Path) -> None:
    authored = yaml.safe_load((_FIXTURES / "case-authoring-valid.yaml").read_text(encoding="utf-8"))
    project, write_root = dual_roots(tmp_path)
    outputs = _write_case_design_outputs(project, authored)
    artifact = "qa/changes/CH-DEMO-001/trace/minimum-coverage-matrix.json"
    matrix_path = project / artifact
    rows = json.loads(matrix_path.read_text(encoding="utf-8"))
    rows[0].update({"covered_by_cases": [], "status": "skipped_by_scope", "skip_reason": "not mapped"})
    matrix_path.write_text(json.dumps(rows), encoding="utf-8")
    _write_fixable_case_review(
        project,
        allowed_key="MRC-API-001",
        artifact=artifact,
        case_id=None,
    )
    repair = await _prepared_review_repair(project)
    rows[0].update({"covered_by_cases": ["TC_MENU_001"], "status": "covered", "skip_reason": None})
    staged = write_root / artifact
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_text(json.dumps(rows), encoding="utf-8")

    executed = await _finalize_files(
        CaseDesignFinalizeHandler(),
        cast(JSONValue, {"output_files": outputs}),
        project,
        ["qa/changes"],
        change_id="CH-DEMO-001",
        selected_test_families=["api"],
        write_root=write_root,
        review_repair=repair,
    )

    assert executed.status == "succeeded"
    assert executed.output["validation_status"] == "pass"


@pytest.mark.asyncio
async def test_case_design_finalize_rejects_mrc_change_outside_named_rows(tmp_path: Path) -> None:
    authored = yaml.safe_load((_FIXTURES / "case-authoring-valid.yaml").read_text(encoding="utf-8"))
    project, write_root = dual_roots(tmp_path)
    outputs = _write_case_design_outputs(project, authored)
    artifact = "qa/changes/CH-DEMO-001/trace/minimum-coverage-matrix.json"
    matrix_path = project / artifact
    rows = json.loads(matrix_path.read_text(encoding="utf-8"))
    rows[0].update({"covered_by_cases": [], "status": "skipped_by_scope", "skip_reason": "not mapped"})
    second = deepcopy(rows[0])
    second.update({"mrc_id": "MRC-API-002", "key": "update_item"})
    rows.append(second)
    matrix_path.write_text(json.dumps(rows), encoding="utf-8")
    _write_fixable_case_review(
        project,
        allowed_key="MRC-API-001",
        artifact=artifact,
        case_id=None,
    )
    repair = await _prepared_review_repair(project)
    rows[0].update({"covered_by_cases": ["TC_MENU_001"], "status": "covered", "skip_reason": None})
    rows[1]["key"] = "unauthorized_scope_change"
    staged = write_root / artifact
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_text(json.dumps(rows), encoding="utf-8")

    executed = await _finalize_files(
        CaseDesignFinalizeHandler(),
        cast(JSONValue, {"output_files": outputs}),
        project,
        ["qa/changes"],
        change_id="CH-DEMO-001",
        selected_test_families=["api"],
        write_root=write_root,
        validation_attempt=1,
        review_repair=repair,
    )

    assert executed.status == "failed"
    assert executed.failure is not None
    assert "outside named MRC rows" in executed.failure.message


@pytest.mark.asyncio
async def test_case_design_finalize_rejects_review_receipt_outside_frozen_outputs(
    tmp_path: Path,
) -> None:
    authored = yaml.safe_load((_FIXTURES / "case-authoring-valid.yaml").read_text(encoding="utf-8"))
    project, write_root = dual_roots(tmp_path)
    outputs = _write_case_design_outputs(project, authored)
    _write_fixable_case_review(project, allowed_key="title")
    repair = await _prepared_review_repair(project)
    authored["added"][0]["title"] = "create menu with validated response"
    _write_case_delta(write_root, authored)
    extra = "qa/changes/CH-DEMO-001/repair-notes.txt"
    extra_path = write_root / extra
    extra_path.parent.mkdir(parents=True, exist_ok=True)
    extra_path.write_text("unauthorized expansion\n", encoding="utf-8")

    executed = await _finalize_files(
        CaseDesignFinalizeHandler(),
        cast(JSONValue, {"output_files": sorted([*outputs, extra])}),
        project,
        ["qa/changes"],
        change_id="CH-DEMO-001",
        selected_test_families=["api"],
        write_root=write_root,
        validation_attempt=1,
        review_repair=repair,
    )

    assert executed.status == "failed"
    assert executed.failure is not None
    assert "frozen case-design outputs" in executed.failure.message


@pytest.mark.asyncio
async def test_case_design_finalize_rejects_non_regular_staged_review_output(
    tmp_path: Path,
) -> None:
    authored = yaml.safe_load((_FIXTURES / "case-authoring-valid.yaml").read_text(encoding="utf-8"))
    project, write_root = dual_roots(tmp_path)
    outputs = _write_case_design_outputs(project, authored)
    _write_fixable_case_review(project, allowed_key="title")
    repair = await _prepared_review_repair(project)
    authored["added"][0]["title"] = "create menu with validated response"
    _write_case_delta(write_root, authored)
    (write_root / "qa/changes/CH-DEMO-001/proposal.md").mkdir(parents=True)

    executed = await _finalize_files(
        CaseDesignFinalizeHandler(),
        cast(JSONValue, {"output_files": outputs}),
        project,
        ["qa/changes"],
        change_id="CH-DEMO-001",
        selected_test_families=["api"],
        write_root=write_root,
        validation_attempt=1,
        review_repair=repair,
    )

    assert executed.status == "failed"
    assert executed.failure is not None
    assert "staged a non-target output" in executed.failure.message


@pytest.mark.asyncio
async def test_case_design_finalize_accepts_exact_dotted_trace_leaf_repair(tmp_path: Path) -> None:
    authored = yaml.safe_load((_FIXTURES / "case-authoring-valid.yaml").read_text(encoding="utf-8"))
    authored["added"][0]["trace"]["auth.session.create"] = {"covered": True}
    project, write_root = dual_roots(tmp_path)
    outputs = _write_case_design_outputs(project, authored)
    _write_fixable_case_review(project, allowed_key="trace.entities.item.create")
    repair = await _prepared_review_repair(project)
    authored["added"][0]["trace"].pop("entities.item.create")
    _write_case_delta(write_root, authored)

    executed = await _finalize_files(
        CaseDesignFinalizeHandler(),
        cast(JSONValue, {"output_files": outputs}),
        project,
        ["qa/changes"],
        change_id="CH-DEMO-001",
        selected_test_families=["api"],
        write_root=write_root,
        review_repair=repair,
    )

    assert executed.status == "succeeded"
    assert executed.output["validation_status"] == "pass"


@pytest.mark.asyncio
async def test_case_design_finalize_rejects_review_repair_that_rewrites_non_target_output(
    tmp_path: Path,
) -> None:
    authored = yaml.safe_load((_FIXTURES / "case-authoring-valid.yaml").read_text(encoding="utf-8"))
    project, write_root = dual_roots(tmp_path)
    outputs = _write_case_design_outputs(project, authored)
    _write_fixable_case_review(project, allowed_key="title")
    repair = await _prepared_review_repair(project)
    authored["added"][0]["title"] = "create menu with validated response"
    _write_case_delta(write_root, authored)
    (write_root / "qa/changes/CH-DEMO-001/proposal.md").write_text("# Replanned proposal\n", encoding="utf-8")

    executed = await _finalize_files(
        CaseDesignFinalizeHandler(),
        cast(JSONValue, {"output_files": outputs}),
        project,
        ["qa/changes"],
        change_id="CH-DEMO-001",
        selected_test_families=["api"],
        write_root=write_root,
        validation_attempt=1,
        review_repair=repair,
    )

    assert executed.status == "failed"
    assert executed.failure is not None
    assert "staged a non-target output" in executed.failure.message


@pytest.mark.asyncio
async def test_case_design_finalize_rejects_review_repair_outside_allowed_case_fields(
    tmp_path: Path,
) -> None:
    authored = yaml.safe_load((_FIXTURES / "case-authoring-valid.yaml").read_text(encoding="utf-8"))
    project, write_root = dual_roots(tmp_path)
    outputs = _write_case_design_outputs(project, authored)
    _write_fixable_case_review(project, allowed_key="title")
    repair = await _prepared_review_repair(project)
    authored["added"][0]["title"] = "create menu with validated response"
    authored["added"][0]["objective"] = "unauthorized replanning"
    _write_case_delta(write_root, authored)

    executed = await _finalize_files(
        CaseDesignFinalizeHandler(),
        cast(JSONValue, {"output_files": outputs}),
        project,
        ["qa/changes"],
        change_id="CH-DEMO-001",
        selected_test_families=["api"],
        write_root=write_root,
        validation_attempt=1,
        review_repair=repair,
    )

    assert executed.status == "failed"
    assert executed.failure is not None
    assert "outside allowed_paths" in executed.failure.message


@pytest.mark.asyncio
async def test_case_design_finalize_rejects_review_repair_that_adds_case_top_level_data(
    tmp_path: Path,
) -> None:
    authored = yaml.safe_load((_FIXTURES / "case-authoring-valid.yaml").read_text(encoding="utf-8"))
    project, write_root = dual_roots(tmp_path)
    outputs = _write_case_design_outputs(project, authored)
    _write_fixable_case_review(project, allowed_key="title")
    repair = await _prepared_review_repair(project)
    authored["added"][0]["title"] = "create menu with validated response"
    authored["unauthorized"] = {"approval": "forged"}
    _write_case_delta(write_root, authored)

    executed = await _finalize_files(
        CaseDesignFinalizeHandler(),
        cast(JSONValue, {"output_files": outputs}),
        project,
        ["qa/changes"],
        change_id="CH-DEMO-001",
        selected_test_families=["api"],
        write_root=write_root,
        validation_attempt=1,
        review_repair=repair,
    )

    assert executed.status == "failed"
    assert executed.failure is not None
    assert "case document structure" in executed.failure.message


@pytest.mark.asyncio
async def test_case_design_finalize_returns_one_bounded_repair_for_first_invalid_output(
    tmp_path: Path,
) -> None:
    authored = yaml.safe_load((_FIXTURES / "case-authoring-valid.yaml").read_text(encoding="utf-8"))
    authored["added"][0]["trace"] = {
        "entities.dept.constraints.unauthorized_user_management_api_access": {"covered": True}
    }
    project, write_root = dual_roots(tmp_path)
    outputs = _write_case_design_outputs(write_root, authored)

    executed = await _finalize_files(
        CaseDesignFinalizeHandler(),
        cast(JSONValue, {"output_files": outputs}),
        project,
        ["qa/changes"],
        change_id="CH-DEMO-001",
        selected_test_families=["api"],
        write_root=write_root,
        validation_attempt=0,
    )

    assert executed.status == "succeeded"
    assert executed.output["validation_status"] == "needs_fix"
    assert executed.output["validation_attempt"] == 1
    assert "capability key is not a declared typed leaf" in executed.output["validation_error"]


@pytest.mark.asyncio
async def test_case_design_finalize_rejects_unlocked_module_or_sibling_case(tmp_path: Path) -> None:
    authored = cast(
        JSONValue,
        yaml.safe_load((_FIXTURES / "case-authoring-valid.yaml").read_text(encoding="utf-8")),
    )
    project, write_root = dual_roots(tmp_path)
    outputs = _write_case_design_outputs(write_root, authored)

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
    outputs = _write_case_design_outputs(write_root, authored)
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
        selected_test_families=["api"],
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
    outputs = _write_case_design_outputs(write_root, authored)

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
    output = executed.output
    assert isinstance(output, dict)
    assert output["decision"] == "pass"
    coverage = output["minimum_coverage"]
    assert isinstance(coverage, dict)
    assert coverage["missing"] == []


@pytest.mark.asyncio
async def test_case_review_finalize_publishes_reviewed_case_manifest(tmp_path: Path) -> None:
    project, write_root = dual_roots(tmp_path)
    _write_review_matrix(project, missing=[])
    change_root = project / "qa/changes/CH-DEMO-001"
    requirement = change_root / "requirement.md"
    requirement.write_text("# Requirement\n", encoding="utf-8")
    case_path = change_root / "cases/menus/case.yaml"
    case_path.parent.mkdir(parents=True, exist_ok=True)
    case_path.write_text("schema_version: '1.0'\nadded: []\nmodified: []\nremoved: []\n", encoding="utf-8")
    review_document = _case_review_document(missing=[])
    review_path = write_root / "qa/changes/CH-DEMO-001/review/case-review.json"
    review_path.parent.mkdir(parents=True, exist_ok=True)
    review_path.write_text(json.dumps(review_document), encoding="utf-8")
    (review_path.parent / "case-review-summary.md").write_text("# Case review\n", encoding="utf-8")
    preparation_refs = [
        {
            "path": requirement.relative_to(project).as_posix(),
            "digest": hashlib.sha256(requirement.read_bytes()).hexdigest(),
        }
    ]
    case_refs = [
        {
            "path": case_path.relative_to(project).as_posix(),
            "digest": hashlib.sha256(case_path.read_bytes()).hexdigest(),
        }
    ]

    executed = await _finalize_files(
        CaseReviewFinalizeHandler(),
        review_document,
        project,
        ["qa/changes"],
        change_id="CH-DEMO-001",
        preparation_refs=preparation_refs,
        case_refs=case_refs,
        write_root=write_root,
    )

    assert executed.status == "succeeded", executed.failure
    output = cast(dict[str, object], executed.output)
    reviewed = cast(dict[str, object], output["reviewed_case"])
    assert reviewed["case_refs"] == case_refs
    manifest = write_root / "qa/changes/CH-DEMO-001/cases/reviewed-case.json"
    assert json.loads(manifest.read_bytes()) == reviewed


@pytest.mark.asyncio
async def test_case_review_finalize_preserves_each_epoch_history_and_updates_latest(
    tmp_path: Path,
) -> None:
    project, write_root = dual_roots(tmp_path)
    _write_review_matrix(project, missing=[])
    change_root = project / "qa/changes/CH-DEMO-001"
    requirement = change_root / "requirement.md"
    requirement.write_text("# Requirement\n", encoding="utf-8")
    case_path = change_root / "cases/menus/case.yaml"
    case_path.parent.mkdir(parents=True, exist_ok=True)
    case_path.write_text(
        "schema_version: '1.0'\nadded: []\nmodified: []\nremoved: []\n",
        encoding="utf-8",
    )
    review_document = _case_review_document(missing=[])
    review_path = write_root / "qa/changes/CH-DEMO-001/review/case-review.json"
    review_path.parent.mkdir(parents=True, exist_ok=True)
    review_path.write_text(json.dumps(review_document), encoding="utf-8")
    (review_path.parent / "case-review-summary.md").write_text("# Case review\n", encoding="utf-8")
    preparation_refs = [
        {
            "path": requirement.relative_to(project).as_posix(),
            "digest": hashlib.sha256(requirement.read_bytes()).hexdigest(),
        }
    ]
    case_refs = [
        {
            "path": case_path.relative_to(project).as_posix(),
            "digest": hashlib.sha256(case_path.read_bytes()).hexdigest(),
        }
    ]

    first = await _finalize_files(
        CaseReviewFinalizeHandler(),
        review_document,
        project,
        ["qa/changes"],
        change_id="CH-DEMO-001",
        coverage_epoch=0,
        review_round=0,
        preparation_refs=preparation_refs,
        case_refs=case_refs,
        write_root=write_root,
    )
    assert first.status == "succeeded", first.failure
    first_history_path = write_root / "qa/changes/CH-DEMO-001/cases/reviews/epochs/0/rounds/0.json"
    first_history_bytes = first_history_path.read_bytes()
    resumed = await _finalize_files(
        CaseReviewFinalizeHandler(),
        review_document,
        project,
        ["qa/changes"],
        change_id="CH-DEMO-001",
        coverage_epoch=0,
        review_round=0,
        preparation_refs=preparation_refs,
        case_refs=case_refs,
        write_root=write_root,
    )
    second = await _finalize_files(
        CaseReviewFinalizeHandler(),
        review_document,
        project,
        ["qa/changes"],
        change_id="CH-DEMO-001",
        coverage_epoch=1,
        review_round=0,
        preparation_refs=preparation_refs,
        case_refs=case_refs,
        write_root=write_root,
    )

    assert first.status == resumed.status == second.status == "succeeded"
    assert first_history_path.read_bytes() == first_history_bytes
    first_history = write_root / "qa/changes/CH-DEMO-001/cases/reviews/epochs/0/rounds/0.json"
    second_history = write_root / "qa/changes/CH-DEMO-001/cases/reviews/epochs/1/rounds/0.json"
    assert json.loads(first_history.read_bytes())["coverage_epoch"] == 0
    assert json.loads(second_history.read_bytes())["coverage_epoch"] == 1
    manifest = json.loads((write_root / "qa/changes/CH-DEMO-001/cases/reviewed-case.json").read_bytes())
    assert manifest["coverage_epoch"] == 1
    assert cast(dict[str, object], second.output)["history_ref"] == {
        "path": "qa/changes/CH-DEMO-001/cases/reviews/epochs/1/rounds/0.json",
        "digest": hashlib.sha256(second_history.read_bytes()).hexdigest(),
    }


@pytest.mark.asyncio
async def test_case_review_finalize_rejects_auto_fix_outside_case_design_write_set(
    tmp_path: Path,
) -> None:
    _write_review_matrix(tmp_path, missing=[])
    document = cast(dict[str, Any], _case_review_document(missing=[]))
    document.update(
        {
            "decision": "needs_fix",
            "findings": [
                {
                    "id": "CR-001",
                    "severity": "medium",
                    "category": "minimum_coverage",
                    "message": "closed key needs a bounded repair",
                    "locator": {"artifact": "qa/changes/CH-DEMO-001/trace/minimum-coverage-matrix.json"},
                    "auto_fix_allowed": True,
                    "human_review_required": False,
                }
            ],
            "auto_fix_plan": [
                {
                    "finding_id": "CR-001",
                    "artifact": "plans/data-knowledge.proposal.dept.yaml",
                    "instructions": "create a proposal",
                }
            ],
            "next_action": "run_case_design",
            "auto_fix_allowed": True,
        }
    )

    executed = await _finalize_files(
        CaseReviewFinalizeHandler(),
        cast(JSONValue, document),
        tmp_path,
        [],
        change_id="CH-DEMO-001",
        case_delta_paths=["qa/changes/CH-DEMO-001/cases/menus/case.yaml"],
    )

    assert executed.status == "failed"
    assert executed.failure is not None
    assert executed.failure.kind == "invalid_output"
    assert "automatic repair artifact is outside the locked case-design write set" in (
        executed.failure.message
    )


@pytest.mark.asyncio
async def test_case_review_finalize_rejects_auto_fix_without_an_exact_field_locator(
    tmp_path: Path,
) -> None:
    authored = yaml.safe_load((_FIXTURES / "case-authoring-valid.yaml").read_text(encoding="utf-8"))
    _write_case_design_outputs(tmp_path, authored)
    _write_review_matrix(tmp_path, missing=[])
    _write_fixable_case_review(tmp_path, allowed_key="title")
    document = json.loads(
        (tmp_path / "qa/changes/CH-DEMO-001/review/case-review.json").read_text(encoding="utf-8")
    )
    document["findings"][0]["locator"]["key"] = None

    executed = await _finalize_files(
        CaseReviewFinalizeHandler(),
        cast(JSONValue, document),
        tmp_path,
        [],
        change_id="CH-DEMO-001",
        case_delta_paths=["qa/changes/CH-DEMO-001/cases/menus/case.yaml"],
    )

    assert executed.status == "failed"
    assert executed.failure is not None
    assert "exact locator key" in executed.failure.message


@pytest.mark.asyncio
async def test_case_review_finalize_replaces_projection_drift_from_authenticated_matrix(
    tmp_path: Path,
) -> None:
    _write_review_matrix(tmp_path, missing=["skipped_item"])
    result = fake_agent_result(_case_review_document(missing=["entities.item"]))
    outcome = await run_finalize(CaseReviewFinalizeHandler(), result, tmp_path)
    assert outcome.status == "succeeded"
    output = outcome.output
    assert isinstance(output, dict)
    assert output["minimum_coverage"] == {
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
