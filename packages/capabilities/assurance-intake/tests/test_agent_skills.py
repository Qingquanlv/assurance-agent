from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from collections.abc import Mapping
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest
import yaml
from pydantic import BaseModel

from agent_runtime_contracts import AgentRunRequest, AgentRunResult
from agent_runtime_contracts.wire.schema import canonical_digest
from graph_engine.canonical import JSONValue, canonical_json_bytes
from graph_engine.plugin_api import TaskHandler, TaskOutcome
from tests.capabilities.agent_harness import FakeAgentAdapter
from tests.product.test_change_local_output_routing import dual_roots, execute_task
from tests.acg_plan_fixture import install_plan

from tests.op_handlers import op_handler

from assurance_intake import ops as intake_ops
from agent_runtime_contracts.ops import ArtifactListResultV1
from assurance_intake.feature import AGENT_JOB_CONTRACTS
from assurance_intake.contracts.review import CaseReviewResultV1
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1
from assurance_intake.ops.case_review.hooks import case_review_outputs
from assurance_intake.ops.case_review.hooks.seal import (
    collect_selected_cases,
    expected_case_selection,
    expected_review_history,
    expected_reviewed_case,
)
from assurance_intake.contracts.explore import (
    ExploreAdvisoryV1,
    ObligationDraftV1,
    PreparedExploreV1,
)
from assurance_intake.domain.obligations import normalize_obligation_drafts
from assurance_intake.domain import case_delta as case_delta_domain
from assurance_intake.ops.intake import hooks as intake_hooks

prepare = op_handler("assurance.intake.intake.prepare")
finalize = op_handler("assurance.intake.intake.finalize")
explore_prepare = op_handler("assurance.intake.explore.prepare")
explore_finalize = op_handler("assurance.intake.explore.finalize")
case_design_prepare = op_handler("assurance.intake.case-design.prepare")
case_design_finalize = op_handler("assurance.intake.case-design.finalize")
case_repair_prepare = op_handler("assurance.intake.case-repair.prepare")
case_repair_finalize = op_handler("assurance.intake.case-repair.finalize")
case_review_prepare = op_handler("assurance.intake.case-review.prepare")
case_review_finalize = op_handler("assurance.intake.case-review.finalize")


def _handler_id(handler: object) -> str:
    return str(getattr(handler, "handler_id", ""))


def _is_plan_bound(handler_id: str) -> bool:
    return any(op in handler_id for op in (".case-design.", ".case-repair.", ".case-review."))


def test_every_op_handler_routes_through_the_ops_entry_module() -> None:
    from assurance_intake.plugin import IntakePlugin

    handlers = IntakePlugin.spec.task_handlers
    assert set(handlers) == set(intake_ops.router.routes())
    for handler in handlers.values():
        assert isinstance(handler, ModuleType)
        assert handler.__name__ == "assurance_intake.ops"
        assert vars(handler)["execute"].__globals__ is vars(handler)


_SHA = "a" * 64
_PLAN_DIGEST = "31e8e6ccff373c935bf09f5f83763f327bd54bc3c96a11b9db3bca1b7b22fa00"
_PLAN_REF: dict[str, JSONValue] = {
    "path": f"qa/results/plan/{_PLAN_DIGEST}/resolved-assurance-plan.json",
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
    "artifact_paths": ["qa/results/explore/advisory.json"],
    "selected_test_families": ["api"],
    "plan_digest": _PLAN_DIGEST,
    "plan_ref": _PLAN_REF,
    "case_delta_paths": ["qa/cases/menus/case.yaml"],
}
CASE_REVIEW_INPUT: dict[str, JSONValue] = {
    key: value for key, value in CASE_INPUT.items() if key != "selected_test_families"
}
INTAKE_INPUT: dict[str, JSONValue] = {
    "change_id": "RET-dept-management",
    "requirement": "Cover department CRUD and the department tree page.",
    "capability_leafs": [],
    "artifact_paths": [
        "qa/.qa.yaml",
        "qa/cases",
        "qa/fixtures",
        "qa/proposal.md",
        "qa/requirement.md",
        "qa/results",
        "qa/tests",
    ],
}


async def run_prepare(
    handler: TaskHandler,
    payload: JSONValue,
    binding: JSONValue,
    workspace: Path,
    write_root: Path | None = None,
    impact_rows: tuple[Mapping[str, object], ...] = (),
) -> Any:
    handler_module = _handler_id(handler)
    if _is_plan_bound(handler_module):
        exploration = workspace / "qa/results/explore/exploration.json"
        minimum_required_coverage = None
        mismatched_exploration: bytes | None = None
        if exploration.is_file():
            existing_bytes = exploration.read_bytes()
            existing = json.loads(existing_bytes)
            if isinstance(existing, Mapping):
                candidate = existing.get("minimum_required_coverage")
                if isinstance(candidate, Mapping | list):
                    minimum_required_coverage = candidate
                if existing.get("change_id") != "CH-DEMO-001":
                    mismatched_exploration = existing_bytes
        plan, plan_ref = install_plan(
            workspace,
            "CH-DEMO-001",
            capability_leafs=VALID_LEAFS,
            minimum_required_coverage=minimum_required_coverage,
            impact_rows=impact_rows,
        )
        if isinstance(payload, dict):
            payload = {
                **payload,
                "plan_digest": plan.plan_digest,
                "plan_ref": cast(JSONValue, plan_ref),
            }
        if mismatched_exploration is not None:
            exploration.write_bytes(mismatched_exploration)
    if ".case-review." in handler_module and isinstance(payload, dict):
        payload = _with_case_refs(workspace, payload)
    if ".explore." in handler_module and isinstance(payload, dict):
        payload = {**payload, "candidate_test_families": ["api"]}
    return await execute_task(handler, payload, workspace, binding_data=binding, write_root=write_root)


def _with_case_refs(workspace: Path, payload: dict[str, JSONValue]) -> dict[str, JSONValue]:
    paths = payload.get("case_delta_paths")
    raw_refs = payload.get("case_refs")
    existing: list[JSONValue] = (
        [item for item in raw_refs if isinstance(item, Mapping)] if isinstance(raw_refs, list) else []
    )
    bound = {str(item.get("path")) for item in existing if isinstance(item, Mapping)}
    refs: list[JSONValue] = list(existing)
    if isinstance(paths, list):
        for relative in paths:
            if not isinstance(relative, str) or relative in bound:
                continue
            path = workspace.joinpath(*relative.split("/"))
            if path.is_file():
                refs.append({"path": relative, "digest": hashlib.sha256(path.read_bytes()).hexdigest()})
    return {**payload, "case_refs": refs}


async def run_finalize(
    handler: TaskHandler,
    result: AgentRunResult,
    workspace: Path,
    write_root: Path | None = None,
) -> TaskOutcome:
    business: dict[str, JSONValue] = {
        "change_id": "CH-DEMO-001",
        "capability_leafs": list(VALID_LEAFS),
        "artifact_paths": [],
    }
    if _is_plan_bound(_handler_id(handler)):
        install_plan(
            workspace,
            "CH-DEMO-001",
            capability_leafs=VALID_LEAFS,
        )
        business.update({"plan_digest": _PLAN_DIGEST, "plan_ref": _PLAN_REF})
    executed = await execute_task(
        handler,
        {"prepare": business, "agent_result": result.model_dump(mode="json")},
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
    skill = intake_ops.router.resource_text("ops/intake/SKILL.md")
    normalized = " ".join(skill.split())
    assert "`qa/` is allowed to be missing" in skill
    assert "must not ask" in skill.lower() or "do not ask" in skill.lower()
    assert "must not require" in skill.lower() or "do not require" in skill.lower()
    assert "initialize" in skill.lower()
    assert "requirement.md" in skill
    assert "Call the native `write` tool exactly once" in skill
    assert "read" in skill and "back" in skill
    assert "A final JSON response without those successful tool calls is invalid" in normalized
    assert "interactive" not in skill.lower()


def test_explore_skill_returns_the_locked_result_contract() -> None:
    skill = intake_ops.router.resource_text("ops/explore/SKILL.md")
    assert "return structured JSON only" in skill
    assert '`schema_version` must be exactly `"1"`' in skill
    assert "schemas/explore-advisory.schema.json" not in skill
    assert 'Set it to the exact string\n    `"explore/context.json"`' in skill
    assert "Do not expand it to" in skill
    assert (
        '{"output_files":["qa/results/explore/exploration-draft.json","qa/results/explore/impact-inventory.json"]}'
        in skill
    )


def test_explore_skill_requires_a_complete_impact_inventory() -> None:
    skill = intake_ops.router.resource_text("ops/explore/SKILL.md")
    assert "## Step 4b — Change impact inventory" in skill
    assert "qa/results/explore/impact-inventory.json" in skill
    assert (
        "every `impact.seeds[].seed_id` must appear in at least one row's `change_evidence_ids` or in `exclusions[]`"
        in skill
    )
    for disposition in ("`reuse`", "`modify`", "`add`", "`capability_gap`", "`pending_confirmation`"):
        assert disposition in skill
    assert "`CF-*`, `CS-*`, `HI-*`" in skill
    assert "The product finalizer rejects any id that does not resolve" in skill
    assert "case_module" in skill
    assert "The operator never supplies modules" in skill
    assert "One requirement commonly spans several modules" in skill


def test_case_design_skill_covers_actionable_impact_rows() -> None:
    skill = intake_ops.router.resource_text("ops/case_design/SKILL.md")
    assert "`impact_inventory`" in skill
    assert "`impact_rows`" in skill
    assert "every row with disposition `add` or `modify` must be covered by at least one case" in skill
    assert "`capability_gap` and `pending_confirmation` rows are listed in `proposal.md`" in skill


def test_explore_skill_requires_evidence_ids_on_every_layer_recommendation() -> None:
    skill = intake_ops.router.resource_text("ops/explore/SKILL.md")

    assert "Every layer recommendation entry MUST include `evidence_ids`" in skill
    assert "an empty list for an evidence-limited declined layer" in skill
    assert "`data_focus` is an array of plain strings" in skill
    assert "never objects with `field` or `evidence_ids` keys" in skill


def test_explore_skill_keeps_explicit_api_only_scope_out_of_e2e_obligations() -> None:
    skill = intake_ops.router.resource_text("ops/explore/SKILL.md")

    assert "An explicit API-only requirement, or a candidate set that does not include `e2e`" in skill
    assert "makes E2E journeys inapplicable" in skill
    assert 'do not emit `category: "e2e"` drafts' in skill
    assert "Do not keep a catalog journey as required after declining E2E" in skill
    assert "set `minimum_required_coverage.e2e` to `[]`" not in skill
    assert '"minimum_required_coverage": {' not in skill


def test_explore_skill_mrc_example_validates_as_obligation_drafts() -> None:
    skill = intake_ops.router.resource_text("ops/explore/SKILL.md")
    start = skill.index("*Example (menu-management, only when these exact catalog leaves")
    fence = skill.index("```json", start)
    end = skill.index("```", fence + 7)
    blob = skill[fence + 7 : end].strip().rstrip(",")
    document = json.loads("{" + blob + "}" if blob.startswith('"minimum_required_coverage"') else blob)
    drafts = document["minimum_required_coverage"]
    assert isinstance(drafts, list) and drafts
    for item in drafts:
        ObligationDraftV1.model_validate(item)
    assert {item["category"] for item in drafts} <= {"api", "e2e", "negative", "data_integrity"}


def test_case_design_skill_requires_cleanup_for_every_successful_persistent_create() -> None:
    skill = intake_ops.router.resource_text("ops/case_design/SKILL.md")

    assert "Every successful step that creates persistent test data" in skill
    assert "including valid boundary-value records" in skill
    assert "matching cleanup action in `postconditions`" in skill


def test_explore_skill_requires_complete_output_even_when_evidence_is_degraded() -> None:
    skill = intake_ops.router.resource_text("ops/explore/SKILL.md")

    assert "advisory.json" not in skill
    assert "exploration.json" in skill
    assert "degraded" in skill.lower()
    assert "no-source" in skill.lower()
    assert "must still" in skill.lower()
    assert "Do not use glob to check either Explore path" in skill
    assert "do not return structured success" in skill.lower()
    assert 'never return `{"output_files":[]}`' in skill.lower()
    assert "must not synthesize such a state as successful" in skill.lower()
    assert "every successful run returns exactly the non-empty receipt" in skill.lower()


def test_case_design_skill_returns_the_locked_file_receipt_contract() -> None:
    skill = intake_ops.router.resource_text("ops/case_design/SKILL.md")
    assert "final assistant response" in " ".join(skill.split())
    assert '"output_files"' in skill
    assert "written files are the sole source of truth" in skill
    assert "Do not duplicate the case delta" in skill
    assert "case_delta_paths" in skill
    assert "phases.explore.status == done" not in skill
    assert "Emit a knowledge proposal" not in skill
    assert "Never inspect `.qa.yaml` for Explore state" in skill
    assert (
        '{"output_files":["qa/.qa.yaml",'
        '"qa/cases/<trusted-module>/case.yaml",'
        '"qa/proposal.md",'
        '"qa/results/trace/minimum-coverage-matrix.json"]}'
    ) in skill
    assert "every written `cases/**/case.yaml`" not in skill
    assert '"qa/results/trace/minimum-coverage-matrix.json"' in skill
    assert "The MRC matrix path is mandatory" in skill
    assert "deterministic finalize step" in skill
    assert "json.dumps(yaml.safe_load" not in skill


def test_case_design_skill_spells_out_the_typed_trace_value_shape() -> None:
    skill = intake_ops.router.resource_text("ops/case_design/SKILL.md")

    assert "Every `trace` value is an object with the single field `covered: true`" in skill
    assert "<capability-leaf>: true" in skill
    assert "Treat `capability_leafs` as a closed enum" in skill
    assert "does not select an adapter namespace" in skill
    assert "capabilities.adapters.api.dept.create" not in skill
    assert "Do not\nput MRC IDs" in skill


def test_case_design_review_reentry_preserves_unnamed_trace_keys() -> None:
    skill = " ".join(intake_ops.router.resource_text("ops/case_design/SKILL.md").split())

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
    skill = " ".join(intake_ops.router.resource_text("ops/case_repair/SKILL.md").split())

    assert "review_repair" in skill
    assert "Only edit the exact artifact, case_id, and allowed_paths" in skill
    assert "Do not add, remove, reorder, or rewrite any case" in skill
    assert "Do not modify any non-target output file" in skill
    assert "complete `## ` heading" in skill
    assert "exact `mrc_id` values" in skill
    assert "`.qa.yaml` is never an automatic repair target" in skill
    assert "apply_patch" in skill
    assert "strict field allowlist, not a semantic-consistency hint" in skill
    assert "preserve `objective` and `summary` byte-for-byte" in skill
    assert "must remain a complete case document" in skill
    assert "Never replace it with only the target cases or allowed fields" in skill


def test_case_reviewer_treats_graph_invocation_as_phase_predecessor_proof() -> None:
    skill = intake_ops.router.resource_text("ops/case_review/SKILL.md")
    assert "authenticated predecessor proof" in skill
    assert "do not search `.qa.yaml`" in skill.lower()


def test_case_reviewer_required_fields_match_typed_case_contract() -> None:
    skill = intake_ops.router.resource_text("ops/case_review/SKILL.md")

    assert "CaseYamlAuthoring is the sole required-field source" in skill
    assert "\ntags:" not in skill
    assert "framework: pytest|pytest-playwright|schemathesis|locust|null" not in skill
    assert "trace: {}" not in skill


def test_case_reviewer_returns_complete_structured_result() -> None:
    skill = intake_ops.router.resource_text("ops/case_review/SKILL.md")

    assert "Return the complete parsed JSON object" in skill
    assert "exactly the same object written to `case-review.json`" in skill
    assert "Do not return only the routing fields" in skill
    assert "Case review completed.\n\nDecision:" not in skill
    assert '"edits":["one exact edit instruction"]' in skill
    assert "split it into one finding per artifact" in skill
    assert "`auto_fix_plan[].artifact` MUST equal that finding's `locator.artifact`" in skill
    assert 'use `"key":"steps,assertions"`' in skill.lower()
    assert '`"key":"steps/assertions"` is invalid' in skill


def test_case_reviewer_encodes_whole_case_changes_as_section_scope() -> None:
    skill = " ".join(intake_ops.router.resource_text("ops/case_review/SKILL.md").split())

    assert "add an entire missing current-change case" in skill
    assert "remove an entire current-change case" in skill
    assert "use the exact current delta section: `added` or `modified`" in skill
    assert "Never use `case_id` as `locator.key`" in skill
    assert "`case_id` identifies the case; it is not a mutable field scope" in skill


def test_case_reviewer_routes_source_proven_case_defects_to_agent_fix_loop() -> None:
    skill = intake_ops.router.resource_text("ops/case_review/SKILL.md")

    assert "Severity alone does not require human review" in skill
    assert "A high-severity finding may still be mechanically fixable" in skill
    assert "must use `needs_fix`" in skill
    assert "Risk level is high or critical" not in skill
    assert "it is a mechanical repair: use `severity: high`" in skill
    assert "forbidden content is never, by itself, a reason for `needs_human_review`" in skill
    assert "Flag as a blocker if found" not in skill


def test_case_reviewer_uses_locked_requirement_and_reports_findings_exhaustively() -> None:
    skill = intake_ops.router.resource_text("ops/case_review/SKILL.md")
    normalized = " ".join(skill.split()).lower()

    assert "requirement.md" in skill
    assert "explicit numerical thresholds and load values" in skill
    assert "complete all review criteria before writing the verdict" in skill
    assert "report all currently observable closed-key defects together" in normalized
    assert "an absent stable target is not a finding" in normalized

    designer = intake_ops.router.resource_text("ops/case_design/SKILL.md")
    assert "Apply every listed auto-fix finding in one pass" in designer
    assert "re-run the complete self-review against the resulting files" in designer

    reviewer = " ".join(skill.split())
    assert "cross-check every assertion" in reviewer
    assert "cross-check every trace key" in reviewer
    assert "On review re-entry, re-review every case in full" in reviewer
    assert "Do not defer a currently observable finding to a later review round" in reviewer


def test_case_skills_do_not_treat_ignore_aware_search_as_source_absence() -> None:
    designer = intake_ops.router.resource_text("ops/case_design/SKILL.md")
    reviewer = intake_ops.router.resource_text("ops/case_review/SKILL.md")

    for skill in (designer, reviewer):
        assert "A glob result of `No files found` is not evidence that product source is absent" in skill
        assert "ignored product source" in skill
        assert "path-scoped grep" in skill
    assert "read every exact product-source path from that review before changing" in designer
    assert "read those exact paths independently before attempting discovery" in reviewer


def test_case_skills_resolve_e2e_entry_from_ignored_menu_source_before_human_review() -> None:
    designer = " ".join(intake_ops.router.resource_text("ops/case_design/SKILL.md").split())
    reviewer = " ".join(intake_ops.router.resource_text("ops/case_review/SKILL.md").split())

    for skill in (designer, reviewer):
        assert (
            "path-scoped search for the exact component path and page or menu label under `app/` and `web/src/`"
            in skill
        )
        assert "even when those roots are ignored" in skill
    assert "do not escalate the E2E entry mechanism to human review" in reviewer
    assert "route it as bounded `needs_fix`" in reviewer


def test_case_skills_keep_advisory_mrc_complete_without_inventing_human_blockers() -> None:
    designer = " ".join(intake_ops.router.resource_text("ops/case_design/SKILL.md").split())
    reviewer = " ".join(intake_ops.router.resource_text("ops/case_review/SKILL.md").split())

    assert "every advisory MRC item still gets exactly one matrix row" in designer
    assert "advisory expansion lacks a frozen oracle" in designer
    assert "not as an implicit business oracle" in reviewer
    assert "Do not create a blocking `needs_review` item" in reviewer
    assert "Never require a proposed MRC key in case `trace`" in reviewer


def _sealed_rows(*drafts: dict[str, object]) -> list[dict[str, object]]:
    advisory = ExploreAdvisoryV1.model_validate(_valid_explore_advisory())
    advisory = advisory.model_copy(
        update={
            "change_id": "CH-DEMO-001",
            "minimum_required_coverage": tuple(ObligationDraftV1.model_validate(draft) for draft in drafts),
        }
    )
    sealed = PreparedExploreV1(
        schema_version="1",
        change_id=advisory.change_id,
        context_ref=advisory.context_ref,
        generated_at=advisory.generated_at,
        executive_summary=advisory.executive_summary,
        watchlist=tuple(advisory.watchlist),
        evidence_inventory=advisory.evidence_inventory,
        source_code_evidence=tuple(advisory.source_code_evidence),
        case_design_guidance=advisory.case_design_guidance,
        minimum_required_coverage=normalize_obligation_drafts(
            advisory.minimum_required_coverage,
            resolved_quotes={},
        ),
        open_questions_for_case_design=tuple(advisory.open_questions_for_case_design),
        test_strategy=advisory.test_strategy,
    )
    return [row.model_dump(mode="json") for row in sealed.minimum_required_coverage]


def _obligation_draft(
    *,
    draft_id: str,
    proposed_key: str | None,
    category: str,
) -> dict[str, object]:
    return {
        "draft_id": draft_id,
        "proposed_key": proposed_key,
        "category": category,
        "layer": "api",
        "statement": f"{draft_id} must hold",
        "applicability_conditions": [],
        "impact_row_ids": [],
        "proposed_profile_id": None,
        "prerequisites": [],
        "observation_goals": [],
        "basis_quotes": [],
        "open_questions": [],
    }


@pytest.mark.asyncio
async def test_case_repair_prepare_rejects_covered_repair_for_unbound_mrc(tmp_path: Path) -> None:
    authored = yaml.safe_load((_FIXTURES / "case-authoring-valid.yaml").read_text(encoding="utf-8"))
    _write_case_design_outputs(tmp_path, authored)
    exploration = tmp_path / "qa/results/explore/exploration.json"
    exploration.parent.mkdir(parents=True, exist_ok=True)
    exploration.write_text(
        json.dumps(
            {
                **_sealed_explore_document(),
                "change_id": "CH-DEMO-001",
                "minimum_required_coverage": _sealed_rows(
                    _obligation_draft(
                        draft_id="MRC-API-001",
                        proposed_key="create_user",
                        category="api",
                    ),
                    _obligation_draft(
                        draft_id="MRC-NEGATIVE-009",
                        proposed_key="entities.item.constraints.missing",
                        category="negative",
                    ),
                ),
            }
        ),
        encoding="utf-8",
    )
    _write_fixable_case_review(
        tmp_path,
        allowed_key="MRC-NEGATIVE-009",
        artifact="qa/results/trace/minimum-coverage-matrix.json",
        case_id=None,
    )
    review_path = tmp_path / "qa/results/review/case-review.json"
    review = json.loads(review_path.read_text(encoding="utf-8"))
    review["auto_fix_plan"][0]["edits"] = ["Keep key empty and set status to covered."]
    review_path.write_text(json.dumps(review), encoding="utf-8")

    prepared = await run_prepare(cast(TaskHandler, case_repair_prepare), CASE_INPUT, BINDING, tmp_path)

    assert prepared.status == "failed"
    assert prepared.failure is not None
    assert "frozen key is empty" in prepared.failure.message
    assert "MRC-NEGATIVE-009" in prepared.failure.message


@pytest.mark.asyncio
async def test_case_repair_prepare_allows_covered_repair_for_bound_api_key(tmp_path: Path) -> None:
    authored = yaml.safe_load((_FIXTURES / "case-authoring-valid.yaml").read_text(encoding="utf-8"))
    _write_case_design_outputs(tmp_path, authored)
    exploration = tmp_path / "qa/results/explore/exploration.json"
    exploration.parent.mkdir(parents=True, exist_ok=True)
    exploration.write_text(
        json.dumps(
            {
                **_sealed_explore_document(),
                "change_id": "CH-DEMO-001",
                "minimum_required_coverage": _sealed_rows(
                    _obligation_draft(
                        draft_id="MRC-API-001",
                        proposed_key="create_user",
                        category="api",
                    )
                ),
            }
        ),
        encoding="utf-8",
    )
    _write_fixable_case_review(
        tmp_path,
        allowed_key="MRC-API-001",
        artifact="qa/results/trace/minimum-coverage-matrix.json",
        case_id=None,
    )
    review_path = tmp_path / "qa/results/review/case-review.json"
    review = json.loads(review_path.read_text(encoding="utf-8"))
    review["auto_fix_plan"][0]["edits"] = ["Set status to covered and fill covered_by_cases."]
    review_path.write_text(json.dumps(review), encoding="utf-8")

    prepared = await run_prepare(cast(TaskHandler, case_repair_prepare), CASE_INPUT, BINDING, tmp_path)

    assert prepared.status == "succeeded", prepared.failure
    request = AgentRunRequest.model_validate(prepared.output)
    business = cast(Mapping[str, object], request.instructions[1].json_content)
    repair = cast(Mapping[str, object], business["review_repair"])
    action = cast(Mapping[str, object], cast(tuple[object, ...], repair["actions"])[0])
    assert action["allowed_paths"] == ("MRC-API-001",)


@pytest.mark.asyncio
async def test_case_repair_finalize_retries_unbound_covered_matrix(
    tmp_path: Path,
) -> None:
    authored = yaml.safe_load((_FIXTURES / "case-authoring-valid.yaml").read_text(encoding="utf-8"))
    project, write_root = dual_roots(tmp_path)
    outputs = _write_case_design_outputs(project, authored)
    artifact = "qa/results/trace/minimum-coverage-matrix.json"
    matrix_path = project / artifact
    rows = json.loads(matrix_path.read_text(encoding="utf-8"))
    rows[0].update(
        {
            "key": None,
            "covered_by_cases": [],
            "status": "skipped_by_scope",
            "skip_reason": "capability_unresolved: frozen key is empty",
        }
    )
    matrix_path.write_text(json.dumps(rows), encoding="utf-8")
    _write_fixable_case_review(project, allowed_key="MRC-API-001", artifact=artifact, case_id=None)
    repair = await _prepared_review_repair(project)
    rows[0].update({"status": "covered", "covered_by_cases": ["TC_MENU_001"], "skip_reason": None})
    staged = write_root / artifact
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_text(json.dumps(rows), encoding="utf-8")

    executed = await _finalize_files(
        cast(TaskHandler, case_repair_finalize),
        cast(JSONValue, {"output_files": outputs}),
        project,
        [
            "qa/.qa.yaml",
            "qa/cases",
            "qa/fixtures",
            "qa/proposal.md",
            "qa/requirement.md",
            "qa/results",
            "qa/tests",
        ],
        change_id="CH-DEMO-001",
        selected_test_families=["api"],
        write_root=write_root,
        review_repair=repair,
    )

    assert executed.status == "failed"
    assert executed.failure is not None
    assert executed.failure.retryable is True
    assert "unresolved MRC rows must remain skipped_by_scope" in executed.failure.message


def test_case_reviewer_applies_frozen_explore_oracle_before_normal_review() -> None:
    reviewer = intake_ops.router.resource_text("ops/case_review/SKILL.md")

    marker = "### Frozen Explore oracle hard gate"
    assert reviewer.index(marker) < reviewer.index("**Before doing any work:**")
    gate = reviewer[reviewer.index(marker) : reviewer.index("**Before doing any work:**")]
    assert "assertion_intent: assert_ideal" in gate
    assert "must not remove its covering case" in gate
    assert "depends only on the frozen obligation `key`" in gate
    assert "keeps `key` empty and changes that row to `covered`" in gate


def test_case_design_repairs_e2e_journey_mapping_from_authenticated_keys() -> None:
    designer = " ".join(intake_ops.router.resource_text("ops/case_design/SKILL.md").split())

    assert "validation_error" in designer
    assert "semicolon-separated" in designer
    assert "E2E cases have no valid journey mapping" in designer
    assert "authenticated journey keys" in designer
    assert "covered_by_cases" in designer
    assert "never invent a journey key" in designer.lower()


def test_case_reviewer_closed_key_repairs_stay_inside_case_design_write_set() -> None:
    designer = " ".join(intake_ops.router.resource_text("ops/case_design/SKILL.md").split())
    reviewer = " ".join(intake_ops.router.resource_text("ops/case_review/SKILL.md").split())

    assert "Do not create a data-knowledge proposal file; it is not an authorized output" in designer
    assert "Do not instruct `aa-case-design` to create a data-knowledge proposal" in reviewer
    assert "mark that exact matrix row `skipped_by_scope`" in reviewer
    assert "Every automatic repair artifact must be an authorized case-design output" in reviewer
    assert "unknown keys require a knowledge proposal" not in reviewer


@pytest.mark.asyncio
async def test_intake_prepare_rejects_missing_requirement(tmp_path: Path) -> None:
    payload = {key: value for key, value in INTAKE_INPUT.items() if key != "requirement"}
    prepared = await run_prepare(cast(TaskHandler, prepare), payload, BINDING, tmp_path)
    assert prepared.status == "failed"
    assert prepared.failure is not None
    assert prepared.failure.kind == "invalid_input"
    assert prepared.failure.retryable is True


@pytest.mark.asyncio
async def test_intake_prepare_embeds_locked_requirement_and_write_rules(tmp_path: Path) -> None:
    prepared = await run_prepare(cast(TaskHandler, prepare), INTAKE_INPUT, BINDING, tmp_path)
    request = AgentRunRequest.model_validate(prepared.output)
    assert request.workspace.agent_profile == "assurance-v1-doc-author"
    assert request.workspace.allowed_outputs == ("qa/.qa.yaml",)
    skill, business = request.instructions
    assert "Capability-owned intake" in (skill.text_content or "")
    assert "Do not ask" in (skill.text_content or "")
    assert "`qa/` is allowed to be missing" in (skill.text_content or "")
    payload = cast(Mapping[str, object], business.json_content)
    assert payload["change_id"] == "RET-dept-management"
    assert payload["requirement"] == "Cover department CRUD and the department tree page."


@pytest.mark.asyncio
async def test_explore_prepare_materializes_deterministic_graph_context(tmp_path: Path) -> None:
    project, write_root = dual_roots(tmp_path)
    requirement = project / "qa/requirement.md"
    requirement.parent.mkdir(parents=True, exist_ok=True)
    requirement.write_text("# Requirement\n\nCover item creation.\n", encoding="utf-8")
    payload = {
        "change_id": "CH-DEMO-001",
        "capability_leafs": list(VALID_LEAFS),
        "artifact_paths": [
            "qa/.qa.yaml",
            "qa/cases",
            "qa/fixtures",
            "qa/proposal.md",
            "qa/requirement.md",
            "qa/results",
            "qa/tests",
        ],
    }

    first = await run_prepare(cast(TaskHandler, explore_prepare), payload, BINDING, project, write_root)
    context_path = write_root / "qa/results/explore/context.json"
    first_bytes = context_path.read_bytes()
    second = await run_prepare(cast(TaskHandler, explore_prepare), payload, BINDING, project, write_root)

    assert first.status == second.status == "succeeded"
    assert context_path.read_bytes() == first_bytes
    context_document = json.loads(first_bytes)
    assert context_document["change_id"] == "CH-DEMO-001"
    assert context_document["requirement_summary"].startswith("# Requirement")
    assert "no_diff: no authenticated diff projection was supplied" in context_document["degraded_reasons"]


@pytest.mark.asyncio
async def test_case_design_prepare_is_canonical_and_provider_neutral(tmp_path: Path) -> None:
    first = await run_prepare(cast(TaskHandler, case_design_prepare), CASE_INPUT, BINDING, tmp_path)
    second = await run_prepare(cast(TaskHandler, case_design_prepare), CASE_INPUT, BINDING, tmp_path)
    request = AgentRunRequest.model_validate(first.output)
    assert request.canonical_bytes() == AgentRunRequest.model_validate(second.output).canonical_bytes()
    assert request.workspace.allowed_outputs == (
        "qa/.qa.yaml",
        "qa/cases/menus/case.yaml",
        "qa/proposal.md",
        "qa/results/trace/minimum-coverage-matrix.json",
    )
    assert not any("**" in path for path in request.workspace.allowed_outputs)
    assert request.workspace.allowed_outputs.count("qa/cases/menus/case.yaml") == 1


@pytest.mark.asyncio
async def test_case_design_prepare_includes_deterministic_validation_feedback(tmp_path: Path) -> None:
    prepared = await run_prepare(
        cast(TaskHandler, case_design_prepare),
        {
            **CASE_INPUT,
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
    notice = request.instructions[1].text_content or ""
    business = cast(Mapping[str, object], request.instructions[2].json_content)
    assert "capability key is not a declared typed leaf" in notice
    assert business["validation_error"] == (
        "capability key is not a declared typed leaf: "
        "entities.dept.constraints.unauthorized_user_management_api_access"
    )


@pytest.mark.asyncio
async def test_case_repair_prepare_builds_a_deterministic_review_repair_contract(
    tmp_path: Path,
) -> None:
    authored = yaml.safe_load((_FIXTURES / "case-authoring-valid.yaml").read_text(encoding="utf-8"))
    outputs = _write_case_design_outputs(tmp_path, authored)
    _write_fixable_case_review(tmp_path, allowed_key="title")

    prepared = await run_prepare(cast(TaskHandler, case_repair_prepare), CASE_INPUT, BINDING, tmp_path)

    assert prepared.status == "succeeded"
    request = AgentRunRequest.model_validate(prepared.output)
    assert "Locator-bounded case repair" in (request.instructions[0].text_content or "")
    business = cast(Mapping[str, object], request.instructions[1].json_content)
    repair = cast(Mapping[str, object], business["review_repair"])
    actions = cast(tuple[object, ...], repair["actions"])
    action = cast(Mapping[str, object], actions[0])
    assert action["finding_id"] == "CR-001"
    assert action["case_id"] == "TC_MENU_001"
    assert action["allowed_paths"] == ("title",)
    assert set(cast(Mapping[str, str], repair["baseline_file_digests"])) == set(outputs)


@pytest.mark.asyncio
async def test_case_design_prepare_ignores_committed_needs_fix_review(tmp_path: Path) -> None:
    authored = yaml.safe_load((_FIXTURES / "case-authoring-valid.yaml").read_text(encoding="utf-8"))
    _write_case_design_outputs(tmp_path, authored)
    _write_fixable_case_review(tmp_path, allowed_key="title")

    prepared = await run_prepare(cast(TaskHandler, case_design_prepare), CASE_INPUT, BINDING, tmp_path)

    assert prepared.status == "succeeded", prepared.failure
    request = AgentRunRequest.model_validate(prepared.output)
    assert "Capability-owned case-design skill" in (request.instructions[0].text_content or "")
    business = cast(Mapping[str, object], request.instructions[1].json_content)
    assert "review_repair" not in business


@pytest.mark.asyncio
@pytest.mark.parametrize("review", [None, "pass"])
async def test_case_repair_prepare_requires_a_committed_needs_fix_review(
    tmp_path: Path, review: str | None
) -> None:
    authored = yaml.safe_load((_FIXTURES / "case-authoring-valid.yaml").read_text(encoding="utf-8"))
    _write_case_design_outputs(tmp_path, authored)
    if review is not None:
        _write_fixable_case_review(tmp_path, allowed_key="title")
        review_path = tmp_path / "qa/results/review/case-review.json"
        document = json.loads(review_path.read_text(encoding="utf-8"))
        document.update(
            {
                "decision": review,
                "findings": [],
                "auto_fix_plan": [],
                "auto_fix_allowed": False,
                "next_action": "proceed",
            }
        )
        review_path.write_text(json.dumps(document), encoding="utf-8")

    prepared = await run_prepare(cast(TaskHandler, case_repair_prepare), CASE_INPUT, BINDING, tmp_path)

    assert prepared.status == "failed"
    assert prepared.failure is not None
    assert "case repair requires" in prepared.failure.message


@pytest.mark.asyncio
async def test_case_repair_prepare_rejects_action_repair_alias(tmp_path: Path) -> None:
    authored = yaml.safe_load((_FIXTURES / "case-authoring-valid.yaml").read_text(encoding="utf-8"))
    _write_case_design_outputs(tmp_path, authored)
    _write_fixable_case_review(tmp_path, allowed_key="title")
    review_path = tmp_path / "qa/results/review/case-review.json"
    review = json.loads(review_path.read_text(encoding="utf-8"))
    review["auto_fix_plan"][0].pop("case_id")
    review["auto_fix_plan"][0]["action"] = review["auto_fix_plan"][0].pop("edits")[0]
    review_path.write_text(json.dumps(review), encoding="utf-8")

    prepared = await run_prepare(cast(TaskHandler, case_repair_prepare), CASE_INPUT, BINDING, tmp_path)

    assert prepared.status == "failed"
    assert prepared.failure is not None
    assert "edits" in (prepared.failure.message or "")


@pytest.mark.asyncio
async def test_case_repair_prepare_accepts_exact_document_section_repair_locator(
    tmp_path: Path,
) -> None:
    authored = yaml.safe_load((_FIXTURES / "case-authoring-valid.yaml").read_text(encoding="utf-8"))
    _write_case_design_outputs(tmp_path, authored)
    _write_fixable_case_review(tmp_path, allowed_key="title")
    review_path = tmp_path / "qa/results/review/case-review.json"
    review = json.loads(review_path.read_text(encoding="utf-8"))
    artifact = "qa/proposal.md"
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

    prepared = await run_prepare(cast(TaskHandler, case_repair_prepare), CASE_INPUT, BINDING, tmp_path)

    assert prepared.status == "succeeded"
    request = AgentRunRequest.model_validate(prepared.output)
    business = cast(Mapping[str, object], request.instructions[1].json_content)
    repair = cast(Mapping[str, object], business["review_repair"])
    action = cast(Mapping[str, object], cast(tuple[object, ...], repair["actions"])[0])
    assert action["allowed_paths"] == ("## Test Conditions",)


@pytest.mark.asyncio
async def test_case_repair_prepare_rejects_bare_proposal_section_locator(tmp_path: Path) -> None:
    authored = yaml.safe_load((_FIXTURES / "case-authoring-valid.yaml").read_text(encoding="utf-8"))
    _write_case_design_outputs(tmp_path, authored)
    artifact = "qa/proposal.md"
    _write_fixable_case_review(
        tmp_path,
        allowed_key="Test Conditions",
        artifact=artifact,
        case_id=None,
    )

    prepared = await run_prepare(cast(TaskHandler, case_repair_prepare), CASE_INPUT, BINDING, tmp_path)

    assert prepared.status == "failed"
    assert prepared.failure is not None
    assert "full level-two Markdown heading" in prepared.failure.message


@pytest.mark.asyncio
async def test_case_repair_prepare_rejects_qa_yaml_automatic_repair(tmp_path: Path) -> None:
    authored = yaml.safe_load((_FIXTURES / "case-authoring-valid.yaml").read_text(encoding="utf-8"))
    _write_case_design_outputs(tmp_path, authored)
    _write_fixable_case_review(
        tmp_path,
        allowed_key="approval.mode",
        artifact="qa/.qa.yaml",
        case_id=None,
    )

    prepared = await run_prepare(cast(TaskHandler, case_repair_prepare), CASE_INPUT, BINDING, tmp_path)

    assert prepared.status == "failed"
    assert prepared.failure is not None
    assert ".qa.yaml cannot be repaired automatically" in prepared.failure.message


@pytest.mark.asyncio
async def test_case_repair_prepare_rejects_mrc_field_selector_locator(tmp_path: Path) -> None:
    authored = yaml.safe_load((_FIXTURES / "case-authoring-valid.yaml").read_text(encoding="utf-8"))
    _write_case_design_outputs(tmp_path, authored)
    _write_fixable_case_review(
        tmp_path,
        allowed_key="MRC-API-001.status",
        artifact="qa/results/trace/minimum-coverage-matrix.json",
        case_id=None,
    )

    prepared = await run_prepare(cast(TaskHandler, case_repair_prepare), CASE_INPUT, BINDING, tmp_path)

    assert prepared.status == "failed"
    assert prepared.failure is not None
    assert "exact mrc_id values" in prepared.failure.message


@pytest.mark.asyncio
async def test_case_repair_prepare_preserves_mrc_locator_baseline_order(tmp_path: Path) -> None:
    authored = yaml.safe_load((_FIXTURES / "case-authoring-valid.yaml").read_text(encoding="utf-8"))
    _write_case_design_outputs(tmp_path, authored)
    artifact = "qa/results/trace/minimum-coverage-matrix.json"
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

    prepared = await run_prepare(cast(TaskHandler, case_repair_prepare), CASE_INPUT, BINDING, tmp_path)

    assert prepared.status == "succeeded"
    request = AgentRunRequest.model_validate(prepared.output)
    business = cast(Mapping[str, object], request.instructions[1].json_content)
    repair = cast(Mapping[str, object], business["review_repair"])
    action = cast(Mapping[str, object], cast(tuple[object, ...], repair["actions"])[0])
    assert action["allowed_paths"] == ("MRC-Z", "MRC-A")


@pytest.mark.asyncio
async def test_case_design_prepare_consumes_typed_current_change_exploration(tmp_path: Path) -> None:
    relative = "qa/results/explore/exploration.json"
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    advisory = _valid_explore_advisory()
    path.write_text(json.dumps(advisory), encoding="utf-8")

    prepared = await run_prepare(cast(TaskHandler, case_design_prepare), CASE_INPUT, BINDING, tmp_path)

    assert prepared.status == "succeeded"
    request = AgentRunRequest.model_validate(prepared.output)
    business = cast(Mapping[str, object], request.instructions[1].json_content)
    exploration = cast(Mapping[str, object], business["exploration"])
    assert exploration["change_id"] == "CH-DEMO-001"
    coverage = tuple(
        cast(Mapping[str, object], item)
        for item in cast(tuple[object, ...], exploration["minimum_required_coverage"])
    )
    assert coverage
    assert coverage[0]["category"] in {"api", "capability", "invariant"}
    assert "statement" in coverage[0]


@pytest.mark.asyncio
async def test_case_design_prepare_consumes_sealed_prepared_exploration(tmp_path: Path) -> None:
    relative = "qa/results/explore/exploration.json"
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_sealed_explore_document()), encoding="utf-8")

    prepared = await run_prepare(cast(TaskHandler, case_design_prepare), CASE_INPUT, BINDING, tmp_path)

    assert prepared.status == "succeeded", prepared.failure
    request = AgentRunRequest.model_validate(prepared.output)
    business = cast(Mapping[str, object], request.instructions[1].json_content)
    exploration = cast(Mapping[str, object], business["exploration"])
    coverage = tuple(
        cast(Mapping[str, object], item)
        for item in cast(tuple[object, ...], exploration["minimum_required_coverage"])
    )
    assert coverage[0]["mrc_id"]
    assert "draft_id" not in coverage[0]


@pytest.mark.asyncio
async def test_case_design_prepare_reads_plan_bound_exploration_for_standalone_case(
    tmp_path: Path,
) -> None:
    prepared = await run_prepare(cast(TaskHandler, case_design_prepare), CASE_INPUT, BINDING, tmp_path)

    assert prepared.status == "succeeded"
    request = AgentRunRequest.model_validate(prepared.output)
    business = cast(Mapping[str, object], request.instructions[1].json_content)
    assert isinstance(business["exploration"], Mapping)


@pytest.mark.asyncio
async def test_case_design_prepare_rejects_mismatched_exploration_identity(tmp_path: Path) -> None:
    relative = "qa/results/explore/exploration.json"
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    advisory = _valid_explore_advisory()
    advisory["change_id"] = "CH-SIBLING"
    path.write_text(json.dumps(advisory), encoding="utf-8")

    prepared = await run_prepare(cast(TaskHandler, case_design_prepare), CASE_INPUT, BINDING, tmp_path)

    assert prepared.status == "failed"
    assert prepared.failure is not None
    assert prepared.failure.kind == "invalid_input"
    assert "change_id" in prepared.failure.message


@pytest.mark.asyncio
async def test_case_review_prepare_locks_exact_current_change_inputs(tmp_path: Path) -> None:
    change_root = tmp_path / "qa"
    (change_root / "results" / "trace").mkdir(parents=True)
    (change_root / "cases/menus").mkdir(parents=True)
    (change_root / ".qa.yaml").write_text("change_id: CH-DEMO-001\n", encoding="utf-8")
    (change_root / "requirement.md").write_text(
        "# Requirement\n\nP95 must be at most 500 ms.\n", encoding="utf-8"
    )
    (change_root / "proposal.md").write_text("# Proposal\n", encoding="utf-8")
    (change_root / "results/trace/minimum-coverage-matrix.json").write_text("[]\n", encoding="utf-8")
    (change_root / "requirement.md").write_text("# Requirement\n", encoding="utf-8")
    (change_root / "cases/menus/case.yaml").write_text(
        "schema_version: '1'\nadded: []\nmodified: []\nremoved: []\n",
        encoding="utf-8",
    )

    prepared = await run_prepare(cast(TaskHandler, case_review_prepare), CASE_REVIEW_INPUT, BINDING, tmp_path)

    assert prepared.status == "succeeded"
    request = AgentRunRequest.model_validate(prepared.output)
    business = cast(Mapping[str, object], request.instructions[1].json_content)
    assert business["case_delta_paths"] == ("qa/cases/menus/case.yaml",)
    assert business["review_input_paths"] == (
        "qa/.qa.yaml",
        "qa/cases/menus/case.yaml",
        "qa/proposal.md",
        "qa/requirement.md",
        "qa/results/trace/minimum-coverage-matrix.json",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("tampered_path", [None, ".qa.yaml", "requirement.md"])
async def test_case_design_commit_refreshes_review_refs_without_accepting_drift(
    tmp_path: Path, tampered_path: str | None
) -> None:
    from assurance_intake.graphs.calls import publish_case_design, select_case_review

    change = "qa"
    content = {
        ".qa.yaml": "change_id: CH-DEMO-001\n",
        "requirement.md": "# Owner requirement\n",
        "proposal.md": "# Proposal\n",
        "results/trace/minimum-coverage-matrix.json": "[]\n",
        "cases/menus/case.yaml": "schema_version: '1'\nadded: []\nmodified: []\nremoved: []\n",
    }
    for relative, text in content.items():
        path = tmp_path / change / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def ref(relative: str) -> dict[str, str]:
        return {
            "path": f"{change}/{relative}",
            "digest": hashlib.sha256((tmp_path / change / relative).read_bytes()).hexdigest(),
        }

    state: dict[str, object] = {
        **CASE_REVIEW_INPUT,
        "allowed_artifact_paths": [
            "qa/.qa.yaml",
            "qa/cases",
            "qa/fixtures",
            "qa/proposal.md",
            "qa/requirement.md",
            "qa/results",
            "qa/tests",
        ],
        "preparation_refs": [ref(".qa.yaml"), ref("requirement.md")],
    }
    original_refs = deepcopy(state["preparation_refs"])
    # Case Design legitimately updates metadata inside its committed write set.
    (tmp_path / change / ".qa.yaml").write_text(content[".qa.yaml"] + "selected_test_families: [api]\n")
    committed_refs = [ref(relative) for relative in content if relative != "requirement.md"]
    update = publish_case_design(state, {"artifacts": committed_refs}, object())
    state.update(update)
    assert update["case_delta_paths"] == ["qa/cases/menus/case.yaml"]
    assert original_refs != [ref(".qa.yaml"), ref("requirement.md")]
    if tampered_path is not None:
        (tmp_path / change / tampered_path).write_text("uncommitted drift\n")

    prepared = await run_prepare(
        cast(TaskHandler, case_review_prepare),
        select_case_review(state).model_dump(mode="json"),
        BINDING,
        tmp_path,
    )
    if tampered_path is None:
        assert prepared.status == "succeeded", prepared.failure
        assert state["preparation_refs"] == [
            ref(relative) for relative in sorted(content) if not relative.endswith("/case.yaml")
        ]
    else:
        assert prepared.status == "failed"
        assert prepared.failure.kind == "invalid_input"
        assert (
            f"evidence digest changed after it was committed: {change}/{tampered_path}"
            in prepared.failure.message
        )


@pytest.mark.asyncio
async def test_case_review_prepare_rejects_missing_locked_input(tmp_path: Path) -> None:
    case = tmp_path / "qa/cases/menus/case.yaml"
    case.parent.mkdir(parents=True, exist_ok=True)
    case.write_text("schema_version: '1'\nadded: []\nmodified: []\nremoved: []\n", encoding="utf-8")
    prepared = await run_prepare(cast(TaskHandler, case_review_prepare), CASE_REVIEW_INPUT, BINDING, tmp_path)

    assert prepared.status == "failed"
    assert prepared.failure is not None
    assert prepared.failure.kind == "invalid_input"
    assert "missing case-review input" in prepared.failure.message


@pytest.mark.asyncio
async def test_case_review_prepare_rejects_intermediate_directory_symlink(tmp_path: Path) -> None:
    change_root = tmp_path / "qa"
    sibling_cases = tmp_path / "outside-menus"
    (change_root / "results" / "trace").mkdir(parents=True)
    (change_root / "cases").mkdir()
    sibling_cases.mkdir(parents=True)
    (change_root / ".qa.yaml").write_text("change_id: CH-DEMO-001\n", encoding="utf-8")
    (change_root / "requirement.md").write_text("# Requirement\n", encoding="utf-8")
    (change_root / "proposal.md").write_text("# Proposal\n", encoding="utf-8")
    (change_root / "results/trace/minimum-coverage-matrix.json").write_text("[]\n", encoding="utf-8")
    (sibling_cases / "case.yaml").write_text(
        "schema_version: '1'\nadded: []\nmodified: []\nremoved: []\n",
        encoding="utf-8",
    )
    (change_root / "cases/menus").symlink_to(sibling_cases, target_is_directory=True)

    prepared = await run_prepare(cast(TaskHandler, case_review_prepare), CASE_REVIEW_INPUT, BINDING, tmp_path)

    assert prepared.status == "failed"
    assert prepared.failure is not None
    assert prepared.failure.kind == "invalid_input"
    assert "must not contain a symlink" in prepared.failure.message


@pytest.mark.asyncio
async def test_case_review_finalize_accepts_mrc_key_that_is_not_a_capability_leaf(
    tmp_path: Path,
) -> None:
    _write_review_matrix(tmp_path, missing=["entities.fake"])
    outcome = await _finalize_review_with_written_cases(
        tmp_path, _case_review_document(missing=["entities.fake"])
    )
    assert outcome.status == "succeeded"


@pytest.mark.asyncio
async def test_prepare_instruction_order_is_skill_then_business(tmp_path: Path) -> None:
    prepared = await run_prepare(cast(TaskHandler, case_design_prepare), CASE_INPUT, BINDING, tmp_path)
    request = AgentRunRequest.model_validate(prepared.output)
    assert len(request.instructions) == 2
    skill, business = request.instructions
    assert skill.media_type == "text/plain"
    assert business.media_type == "application/json"
    assert "Capability-owned case-design skill" in (skill.text_content or "")
    payload = cast(Mapping[str, object], business.json_content)
    leafs = payload["capability_leafs"]
    facts = cast(Mapping[str, object], payload["planning_facts"])
    assert facts["capability_leafs"] == VALID_LEAFS
    assert "unknown" in str(facts["limits"])
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
async def test_case_author_and_reviewer_observe_same_source_snapshot(tmp_path: Path) -> None:
    change = tmp_path / "qa"
    (change / "cases/menus").mkdir(parents=True)
    for relative in (
        ".qa.yaml",
        "requirement.md",
        "proposal.md",
        "results/trace/minimum-coverage-matrix.json",
        "cases/menus/case.yaml",
    ):
        path = change / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("Read app/router.py\n")
    source = tmp_path / "app/router.py"
    source.parent.mkdir()
    source.write_text("def get_items(): pass\n")
    author = await run_prepare(cast(TaskHandler, case_design_prepare), CASE_INPUT, BINDING, tmp_path)
    reviewer = await run_prepare(cast(TaskHandler, case_review_prepare), CASE_REVIEW_INPUT, BINDING, tmp_path)
    assert author.status == reviewer.status == "succeeded"
    author_input = cast(
        Mapping[str, Any], AgentRunRequest.model_validate(author.output).instructions[1].json_content
    )
    review_input = cast(
        Mapping[str, Any], AgentRunRequest.model_validate(reviewer.output).instructions[1].json_content
    )
    assert author_input["planning_facts"] == review_input["planning_facts"]
    assert any(
        file["path"] == "app/router.py" and file["status"] == "observed"
        for file in review_input["planning_facts"]["files"]
    )


@pytest.mark.asyncio
async def test_prepare_rejects_routing_marker_as_invalid_input(tmp_path: Path) -> None:
    binding = {
        **BINDING,
        "execution": {
            **BINDING["execution"],  # type: ignore[arg-type]
            "provider_model": "primary,fallback",
        },
    }
    prepared = await run_prepare(cast(TaskHandler, case_design_prepare), CASE_INPUT, binding, tmp_path)
    assert prepared.status == "failed"
    assert prepared.failure is not None
    assert prepared.failure.kind == "invalid_input"
    assert prepared.failure.retryable is True


@pytest.mark.asyncio
async def test_finalize_rejects_malformed_input(tmp_path: Path) -> None:
    executed = await execute_task(
        cast(TaskHandler, case_review_finalize), {"prepare": {}, "agent_result": {}}, tmp_path
    )
    assert executed.status == "failed"
    assert executed.failure is not None
    assert executed.failure.kind == "invalid_input"
    assert executed.failure.retryable is True


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


def _ref(path: Path, relative: str) -> EvidenceArtifactRefV1:
    return EvidenceArtifactRefV1(
        path=relative,
        digest=hashlib.sha256(path.read_bytes()).hexdigest(),
    )


def _write_json_model(root: Path, relative: str, model: BaseModel) -> None:
    path = root.joinpath(*relative.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json_bytes(model.model_dump(mode="json")) + b"\n")


def _write_case_review_runtime_seal(
    *,
    project: Path,
    write_root: Path,
    document: Mapping[str, object],
    change_id: str,
    coverage_epoch: int,
    review_round: int,
    case_delta_paths: list[str],
    case_refs: list[dict[str, str]],
    preparation_refs: list[dict[str, str]],
    plan: object,
    plan_ref: dict[str, str],
) -> list[str]:
    review_relative = "qa/results/review/case-review.json"
    review_ref = _ref(write_root / review_relative, review_relative)
    refs = [EvidenceArtifactRefV1.model_validate(item) for item in case_refs]
    documents: list[tuple[EvidenceArtifactRefV1, Mapping[str, object]]] = []
    for ref in refs:
        source = yaml.safe_load((project / ref.path).read_bytes())
        assert isinstance(source, Mapping)
        documents.append((ref, source))
    selected = collect_selected_cases(refs, case_delta_paths, documents)
    selection = expected_case_selection(
        change_id=change_id,
        coverage_epoch=coverage_epoch,
        plan=plan,  # type: ignore[arg-type]
        cases=selected,
    )
    selection_relative = f"qa/results/cases/epochs/{coverage_epoch}/selection.json"
    _write_json_model(write_root, selection_relative, selection)
    selection_ref = _ref(write_root.joinpath(*selection_relative.split("/")), selection_relative)
    history = expected_review_history(
        change_id=change_id,
        coverage_epoch=coverage_epoch,
        review_round=review_round,
        document=CaseReviewResultV1.model_validate(document),
        preparation_refs=[EvidenceArtifactRefV1.model_validate(item) for item in preparation_refs],
        case_refs=refs,
        review_ref=review_ref,
    )
    history_relative = f"qa/cases/reviews/epochs/{coverage_epoch}/rounds/{review_round}.json"
    _write_json_model(write_root, history_relative, history)
    reviewed = expected_reviewed_case(
        change_id=change_id,
        coverage_epoch=coverage_epoch,
        plan_digest=str(plan.plan_digest),  # type: ignore[attr-defined]
        plan_ref=EvidenceArtifactRefV1.model_validate(plan_ref),
        preparation_refs=[EvidenceArtifactRefV1.model_validate(item) for item in preparation_refs],
        case_refs=refs,
        review_ref=review_ref,
        selection_ref=selection_ref,
    )
    manifest_relative = "qa/cases/reviewed-case.json"
    _write_json_model(write_root, manifest_relative, reviewed)
    return [manifest_relative, history_relative, selection_relative]


def _install_and_write_case_review_seal(
    *,
    project: Path,
    write_root: Path,
    document: Mapping[str, object],
    case_refs: list[dict[str, str]],
    preparation_refs: list[dict[str, str]],
    case_delta_paths: list[str],
    coverage_epoch: int = 0,
    review_round: int = 0,
    change_id: str = "CH-DEMO-001",
) -> list[dict[str, str]]:
    plan, plan_ref = install_plan(
        project,
        change_id,
        capability_leafs=VALID_LEAFS,
        candidates=("api",),
        proposed=("api",),
    )
    bound = list(preparation_refs)
    if plan_ref not in bound:
        bound.append(plan_ref)
        bound.sort(key=lambda item: (item["path"], item["digest"]))
    _write_case_review_runtime_seal(
        project=project,
        write_root=write_root,
        document=document,
        change_id=change_id,
        coverage_epoch=coverage_epoch,
        review_round=review_round,
        case_delta_paths=case_delta_paths,
        case_refs=case_refs,
        preparation_refs=bound,
        plan=plan,
        plan_ref=plan_ref,
    )
    return bound


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
    path = workspace / "qa/results/trace/minimum-coverage-matrix.json"
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
    validation_error: str | None = None,
    review_repair: object | None = None,
    coverage_epoch: int = 0,
    review_round: int = 0,
    impact_rows: tuple[Mapping[str, object], ...] = (),
    preparation_refs: list[dict[str, str]] | None = None,
    case_refs: list[dict[str, str]] | None = None,
) -> Any:
    result = fake_agent_result(structured_result)
    plan = None
    plan_ref = None
    handler_module = _handler_id(handler)
    if _is_plan_bound(handler_module):
        selected = tuple(cast(Any, selected_test_families or ["api"]))
        plan, plan_ref = install_plan(
            workspace,
            change_id or "CH-DEMO-001",
            capability_leafs=VALID_LEAFS,
            candidates=selected,
            proposed=selected,
            impact_rows=impact_rows,
        )
    bound_preparation_refs = list(preparation_refs or [])
    if plan_ref is not None and plan_ref not in bound_preparation_refs:
        bound_preparation_refs.append(plan_ref)
        bound_preparation_refs.sort(key=lambda item: (item["path"], item["digest"]))
    validation: dict[str, object] = {}
    if validation_error is not None:
        validation = {"validation_error": validation_error}
    locked_input: dict[str, object] = {
        "change_id": change_id or "CH-DEMO-001",
        "requirement": "Cover department CRUD.",
        "candidate_test_families": selected_test_families or ["api"],
        "capability_leafs": list(VALID_LEAFS),
        "artifact_paths": artifact_paths,
        "selected_test_families": selected_test_families or [],
        "case_delta_paths": (
            case_delta_paths
            if case_delta_paths is not None
            else (
                ["qa/cases/menus/case.yaml"]
                if handler_module.endswith(("case-design.finalize", "case-repair.finalize"))
                else []
            )
        ),
        **validation,
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
        cast(JSONValue, {"prepare": locked_input, "agent_result": result.model_dump(mode="json")}),
        workspace,
        write_root=write_root,
    )
    return executed


def _write_case_delta(workspace: Path, document: object) -> str:
    relative = "qa/cases/menus/case.yaml"
    path = workspace / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return relative


async def _finalize_review_with_written_cases(
    workspace: Path,
    document: JSONValue,
    *,
    staged_review: bytes | None = None,
) -> TaskOutcome:
    _, write_root = dual_roots(workspace)
    authored = yaml.safe_load((_FIXTURES / "case-authoring-valid.yaml").read_text(encoding="utf-8"))
    case_relative = _write_case_delta(workspace, authored)
    matrix_relative = "qa/results/trace/minimum-coverage-matrix.json"
    review_relative = "qa/results/review/case-review.json"
    review_path = write_root / review_relative
    review_path.parent.mkdir(parents=True, exist_ok=True)
    review_path.write_bytes(json.dumps(document).encode() if staged_review is None else staged_review)
    summary_relative = "qa/results/review/case-review-summary.md"
    (write_root / summary_relative).write_text("# Case review\n", encoding="utf-8")
    case_refs = [
        {
            "path": case_relative,
            "digest": hashlib.sha256((workspace / case_relative).read_bytes()).hexdigest(),
        }
    ]
    preparation_refs = [
        {
            "path": matrix_relative,
            "digest": hashlib.sha256((workspace / matrix_relative).read_bytes()).hexdigest(),
        },
    ]
    bound_preparation = _install_and_write_case_review_seal(
        project=workspace,
        write_root=write_root,
        document=cast(Mapping[str, object], document),
        case_refs=case_refs,
        preparation_refs=preparation_refs,
        case_delta_paths=[case_relative],
    )
    executed = await _finalize_files(
        cast(TaskHandler, case_review_finalize),
        document,
        workspace,
        list(case_review_outputs("CH-DEMO-001")),
        change_id="CH-DEMO-001",
        write_root=write_root,
        case_delta_paths=[case_relative],
        case_refs=case_refs,
        preparation_refs=bound_preparation,
    )
    return executed.outcome


@pytest.mark.asyncio
@pytest.mark.parametrize("staged_review", [b'{"decision":"reject"}', b"not json", None])
async def test_case_review_rejects_invalid_or_divergent_staged_document(
    tmp_path: Path, staged_review: bytes | None
) -> None:
    _write_review_matrix(tmp_path, missing=[])
    reply = _case_review_document(missing=[])
    assert isinstance(reply, dict)
    if staged_review is None:
        staged_review = json.dumps({**reply, "decision": "reject", "next_action": "stop"}).encode()
    outcome = await _finalize_review_with_written_cases(tmp_path, reply, staged_review=staged_review)
    assert outcome.status == "failed"
    assert outcome.failure is not None
    assert outcome.failure.kind == "invalid_output"
    assert "staged case review" in outcome.failure.message
    _, stage = dual_roots(tmp_path)
    assert (stage / "qa/results/review/case-review.json").read_bytes() == staged_review


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "marker",
    [None, "invalid: [unterminated\n", "null\n", "change_id: 123\n", "change_id: CH-OTHER\n"],
)
async def test_intake_rejects_invalid_or_missing_change_marker(tmp_path: Path, marker: str | None) -> None:
    project, stage = dual_roots(tmp_path)
    (stage / "qa").mkdir(parents=True, exist_ok=True)
    (stage / "qa/requirement.md").write_text("# Requirement\n")
    if marker is not None:
        (stage / "qa/.qa.yaml").write_text(marker)
    result = await _finalize_files(
        cast(TaskHandler, finalize),
        {"output_files": ["qa/requirement.md"] if marker is None else ["qa/.qa.yaml"]},
        project,
        ["qa/.qa.yaml", "qa/requirement.md"],
        change_id="CH-DEMO-001",
        write_root=stage,
    )
    assert result.status == "failed"
    assert result.failure.kind == "invalid_output"
    assert ".qa.yaml" in result.failure.message


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_kind", ["syntax", "missing_fields", "wrong_change"])
async def test_case_design_validates_complete_change_marker(tmp_path: Path, invalid_kind: str) -> None:
    project, stage = dual_roots(tmp_path)
    authored = yaml.safe_load((_FIXTURES / "case-authoring-valid.yaml").read_bytes())
    outputs = _write_case_design_outputs(stage, authored)
    marker = stage / "qa/.qa.yaml"
    if invalid_kind == "syntax":
        marker.write_text("invalid: [unterminated\n")
    elif invalid_kind == "missing_fields":
        marker.write_text("change_id: CH-DEMO-001\n")
    else:
        document = yaml.safe_load((_FIXTURES / "qa-valid.yaml").read_bytes())
        document["change"]["change_id"] = "CH-OTHER"
        marker.write_text(yaml.safe_dump(document))
    result = await _finalize_files(
        cast(TaskHandler, case_design_finalize),
        cast(JSONValue, {"output_files": outputs}),
        project,
        outputs,
        change_id="CH-DEMO-001",
        selected_test_families=["api"],
        write_root=stage,
    )
    assert result.status == "failed"
    assert result.failure.kind == "invalid_output"
    assert ".qa.yaml" in result.failure.message


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["intake", "case-design"])
async def test_finalize_rejects_change_marker_replaced_after_digest_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    project, stage = dual_roots(tmp_path)
    if phase == "case-design":
        authored = yaml.safe_load((_FIXTURES / "case-authoring-valid.yaml").read_bytes())
        outputs = _write_case_design_outputs(stage, authored)
        handler = cast(TaskHandler, case_design_finalize)
        handler_globals = vars(case_delta_domain)
    else:
        (stage / "qa").mkdir(parents=True, exist_ok=True)
        (stage / "qa/.qa.yaml").write_text("change_id: CH-DEMO-001\n")
        (stage / "qa/requirement.md").write_text("# Requirement\n")
        outputs = ["qa/.qa.yaml"]
        handler = cast(TaskHandler, finalize)
        handler_globals = vars(intake_hooks)
    read = handler_globals["read_regular_bytes"]

    def replace_before_validation(workspace: Path, relative: str, *, kind: str) -> bytes:
        if workspace == stage and relative == "qa/.qa.yaml":
            path = workspace / relative
            path.write_bytes(path.read_bytes() + b"# concurrent change\n")
        return read(workspace, relative, kind=kind)

    monkeypatch.setitem(handler_globals, "read_regular_bytes", replace_before_validation)
    result = await _finalize_files(
        handler,
        cast(JSONValue, {"output_files": outputs}),
        project,
        outputs,
        change_id="CH-DEMO-001",
        selected_test_families=["api"],
        write_root=stage,
    )
    assert result.status == "failed"
    if phase == "case-design":
        assert "qa/.qa.yaml changed" in result.failure.message
    else:
        assert "qa/.qa.yaml changed" in result.failure.message


def _write_case_design_outputs(workspace: Path, document: object) -> list[str]:
    change_root = workspace / "qa"
    change_root.mkdir(parents=True, exist_ok=True)
    (change_root / ".qa.yaml").write_bytes((_FIXTURES / "qa-valid.yaml").read_bytes())
    (change_root / "proposal.md").write_text("# Proposal\n", encoding="utf-8")
    relative = _write_case_delta(workspace, document)
    matrix_relative = "qa/results/trace/minimum-coverage-matrix.json"
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
        "qa/.qa.yaml",
        "qa/proposal.md",
        matrix_relative,
        relative,
    ]


def _write_fixable_case_review(
    workspace: Path,
    *,
    allowed_key: str,
    artifact: str = "qa/cases/menus/case.yaml",
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
    path = workspace / "qa/results/review/case-review.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")


async def _prepared_review_repair(workspace: Path) -> Mapping[str, object]:
    prepared = await run_prepare(cast(TaskHandler, case_repair_prepare), CASE_INPUT, BINDING, workspace)
    assert prepared.status == "succeeded"
    request = AgentRunRequest.model_validate(prepared.output)
    dumped = request.model_dump(mode="json")
    instructions = cast(list[dict[str, object]], dumped["instructions"])
    business = cast(dict[str, object], instructions[1]["json_content"])
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
        "minimum_required_coverage": [
            {
                "draft_id": "D-API-001",
                "proposed_key": "create_item",
                "category": "api",
                "layer": "api",
                "statement": "create_item must hold",
                "applicability_conditions": [],
                "impact_row_ids": [],
                "proposed_profile_id": None,
                "prerequisites": [],
                "observation_goals": [],
                "basis_quotes": [],
                "open_questions": [],
            }
        ],
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


def _sealed_explore_document() -> dict[str, Any]:
    advisory = ExploreAdvisoryV1.model_validate(_valid_explore_advisory())
    rows = normalize_obligation_drafts(advisory.minimum_required_coverage, resolved_quotes={})
    return PreparedExploreV1(
        schema_version="1",
        change_id=advisory.change_id,
        context_ref=advisory.context_ref,
        generated_at=advisory.generated_at,
        executive_summary=advisory.executive_summary,
        watchlist=tuple(advisory.watchlist),
        evidence_inventory=advisory.evidence_inventory,
        source_code_evidence=tuple(advisory.source_code_evidence),
        case_design_guidance=advisory.case_design_guidance,
        minimum_required_coverage=rows,
        open_questions_for_case_design=tuple(advisory.open_questions_for_case_design),
        test_strategy=advisory.test_strategy,
    ).model_dump(mode="json")


_EXPLORATION = "qa/results/explore/exploration.json"
_EXPLORATION_DRAFT = "qa/results/explore/exploration-draft.json"
_INVENTORY = "qa/results/explore/impact-inventory.json"
_CONTEXT = "qa/results/explore/context.json"


def _explore_context_document(
    *,
    seeds: tuple[tuple[str, str], ...] = (("CF-001", "app/controllers/item.py"),),
    case_ids: tuple[str, ...] = (),
) -> dict[str, Any]:
    from assurance_intake.ops.explore.models import (
        CandidateCaseV1,
        ExploreContextV1,
        ImpactProjectionV1,
        ImpactSeedV1,
        RequirementReadFactsV1,
    )

    return ExploreContextV1(
        change_id="CH-DEMO-001",
        requirement_summary="# Requirement",
        aggregation_policy={
            "source": "graph-owned-content-snapshot",
            "layers": ["api", "e2e", "fuzz", "performance"],
            "ambient_git_forbidden": True,
        },
        archive_window={"depth": 0, "archives_sampled": [], "newest_archive": None, "oldest_archive": None},
        staleness={"max_age_days": None, "stale": False},
        impact=ImpactProjectionV1(
            diff_base="content-snapshot",
            seeds=tuple(
                ImpactSeedV1(seed_id=seed_id, path=path, reason="requirement_hint") for seed_id, path in seeds
            ),
            candidate_cases=tuple(
                CandidateCaseV1(
                    evidence_id=f"CS-{index:03d}",
                    case_id=case_id,
                    module="menus",
                    path="qa/cases/menus/case.yaml",
                    title=case_id,
                )
                for index, case_id in enumerate(case_ids, start=1)
            ),
            historical_problems=(),
            factory_leafs=(),
        ),
        case_signals=[],
        test_health=[],
        historical_issues=[],
        evidence=[],
        source_catalog=(),
        requirement_read_facts=RequirementReadFactsV1(
            total_bytes=0,
            provided_bytes=0,
            read_state="complete",
        ),
        degraded=True,
        degraded_reasons=["no_diff: no authenticated diff projection was supplied"],
        no_git=True,
    ).model_dump(mode="json")


def _valid_inventory(
    *,
    rows: list[dict[str, Any]] | None = None,
    exclusions: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": "1",
        "change_id": "CH-DEMO-001",
        "context_ref": "explore/context.json",
        "rows": rows if rows is not None else [_inventory_row()],
        "exclusions": exclusions or [],
    }


def _inventory_row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "row_id": "IR-001",
        "change_evidence_ids": ["CF-001"],
        "affected_behavior": {"kind": "api", "key": "POST /api/v1/item/create"},
        "obligation": "duplicate item names must be rejected with 400",
        "expected_basis_ids": ["SC-001"],
        "assets": {"case_ids": [], "factory_leafs": [], "problem_ids": []},
        "disposition": "add",
        "gap_reason": None,
        "confidence": "medium",
    }
    row.update(overrides)
    return row


def _advisory_with_source_evidence() -> dict[str, Any]:
    advisory = _valid_explore_advisory()
    advisory["source_code_evidence"] = [
        {
            "id": "SC-001",
            "source": "source_code",
            "type": "api_route",
            "description": "POST /api/v1/item/create — create_item handler",
            "parse_confidence_cap": "medium",
        }
    ]
    return advisory


def _stage_explore_outputs(
    write_root: Path,
    *,
    advisory: dict[str, Any] | None = None,
    inventory: dict[str, Any] | None = None,
    context: dict[str, Any] | None = None,
) -> dict[str, bytes]:
    files = {
        _CONTEXT: canonical_json_bytes(cast(JSONValue, context or _explore_context_document())) + b"\n",
        _EXPLORATION_DRAFT: json.dumps(advisory or _advisory_with_source_evidence()).encode(),
        _INVENTORY: json.dumps(inventory or _valid_inventory()).encode(),
    }
    for relative, data in files.items():
        path = write_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return files


@pytest.mark.asyncio
async def test_explore_finalize_returns_artifact_digests(tmp_path: Path) -> None:
    project, write_root = dual_roots(tmp_path)
    files = _stage_explore_outputs(write_root)
    result = fake_agent_result({"output_files": [_EXPLORATION_DRAFT, _INVENTORY]})
    executed = await execute_task(
        cast(TaskHandler, explore_finalize),
        {
            "prepare": {
                "change_id": "CH-DEMO-001",
                "capability_leafs": list(VALID_LEAFS),
                "candidate_test_families": ["api"],
                "artifact_paths": [
                    "qa/.qa.yaml",
                    "qa/cases",
                    "qa/fixtures",
                    "qa/proposal.md",
                    "qa/requirement.md",
                    "qa/results",
                    "qa/tests",
                ],
            },
            "agent_result": result.model_dump(mode="json"),
        },
        project,
        write_root=write_root,
    )
    assert executed.status == "succeeded", executed.failure
    official = (write_root / _EXPLORATION).read_bytes()
    assert executed.output == {
        "artifacts": [
            {"path": _EXPLORATION, "digest": hashlib.sha256(official).hexdigest()},
            {"path": _INVENTORY, "digest": hashlib.sha256(files[_INVENTORY]).hexdigest()},
        ]
    }
    contract = AGENT_JOB_CONTRACTS["explore"]
    assert contract.agent_result_model is ArtifactListResultV1
    contract.output_model.model_validate(executed.output)


@pytest.mark.asyncio
async def test_explore_finalize_rejects_placeholder_advisory(tmp_path: Path) -> None:
    project, write_root = dual_roots(tmp_path)
    _stage_explore_outputs(write_root, inventory=_valid_inventory())
    path = write_root / _EXPLORATION_DRAFT
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
        cast(TaskHandler, explore_finalize),
        {"output_files": [_EXPLORATION_DRAFT, _INVENTORY]},
        project,
        [_EXPLORATION_DRAFT, _INVENTORY],
        change_id="CH-DEMO-001",
        write_root=write_root,
    )

    assert executed.status == "failed"
    assert executed.failure is not None
    assert executed.failure.kind == "invalid_output"
    assert "exploration" in executed.failure.message


@pytest.mark.asyncio
async def test_explore_finalize_rejects_a_declared_missing_advisory_as_invalid_output(
    tmp_path: Path,
) -> None:
    project, write_root = dual_roots(tmp_path)
    relative = _EXPLORATION_DRAFT

    executed = await _finalize_files(
        cast(TaskHandler, explore_finalize),
        {"output_files": [_EXPLORATION_DRAFT, _INVENTORY]},
        project,
        [_EXPLORATION_DRAFT, _INVENTORY],
        change_id="CH-DEMO-001",
        write_root=write_root,
    )

    assert executed.status == "failed"
    assert executed.failure is not None
    assert executed.failure.kind == "invalid_output"
    assert executed.failure.retryable is True
    assert executed.failure.message == f"declared output file is missing: {relative}"


async def _finalize_explore(project: Path, write_root: Path) -> Any:
    return await _finalize_files(
        cast(TaskHandler, explore_finalize),
        {"output_files": [_EXPLORATION_DRAFT, _INVENTORY]},
        project,
        [_EXPLORATION_DRAFT, _INVENTORY],
        change_id="CH-DEMO-001",
        write_root=write_root,
    )


@pytest.mark.asyncio
async def test_explore_finalize_requires_both_declared_outputs(tmp_path: Path) -> None:
    project, write_root = dual_roots(tmp_path)
    _stage_explore_outputs(write_root)
    executed = await _finalize_files(
        cast(TaskHandler, explore_finalize),
        {"output_files": [_EXPLORATION_DRAFT]},
        project,
        [_EXPLORATION_DRAFT],
        change_id="CH-DEMO-001",
        write_root=write_root,
    )
    assert executed.status == "failed"
    assert executed.failure is not None
    assert executed.failure.kind == "invalid_output"
    assert "impact-inventory.json" in executed.failure.message


@pytest.mark.asyncio
async def test_explore_finalize_rejects_advisory_evidence_ids_that_do_not_resolve(tmp_path: Path) -> None:
    project, write_root = dual_roots(tmp_path)
    advisory = _advisory_with_source_evidence()
    advisory["case_design_guidance"]["priority_hints"] = [
        {"id": "PH-001", "hint": "assert 400 on duplicates", "confidence": "low", "evidence_ids": ["SC-404"]}
    ]
    _stage_explore_outputs(write_root, advisory=advisory)
    executed = await _finalize_explore(project, write_root)
    assert executed.status == "failed"
    assert executed.failure is not None
    assert executed.failure.kind == "invalid_output"
    assert "exploration.json cites unresolvable evidence ids: ['SC-404']" in executed.failure.message


@pytest.mark.asyncio
async def test_explore_finalize_rejects_inventory_that_leaves_a_seed_unhandled(tmp_path: Path) -> None:
    project, write_root = dual_roots(tmp_path)
    _stage_explore_outputs(
        write_root,
        context=_explore_context_document(
            seeds=(("CF-001", "app/controllers/item.py"), ("CF-002", "app/api/v1/items.py"))
        ),
    )
    executed = await _finalize_explore(project, write_root)
    assert executed.status == "failed"
    assert executed.failure is not None
    assert executed.failure.kind == "invalid_output"
    assert "seeds without an impact row or exclusion: ['CF-002']" in executed.failure.message


@pytest.mark.asyncio
async def test_explore_finalize_accepts_an_explicit_exclusion(tmp_path: Path) -> None:
    project, write_root = dual_roots(tmp_path)
    _stage_explore_outputs(
        write_root,
        context=_explore_context_document(
            seeds=(("CF-001", "app/controllers/item.py"), ("CF-002", "app/api/v1/items.py"))
        ),
        inventory=_valid_inventory(exclusions=[{"seed_id": "CF-002", "reason": "router wiring only"}]),
    )
    executed = await _finalize_explore(project, write_root)
    assert executed.status == "succeeded", executed.failure


@pytest.mark.asyncio
async def test_explore_finalize_rejects_inventory_citing_unknown_case_or_leaf(tmp_path: Path) -> None:
    project, write_root = dual_roots(tmp_path)
    _stage_explore_outputs(
        write_root,
        context=_explore_context_document(case_ids=("TC_MENU_001",)),
        inventory=_valid_inventory(
            rows=[
                _inventory_row(
                    disposition="modify",
                    assets={
                        "case_ids": ["TC_MENU_999"],
                        "factory_leafs": ["capabilities.domain_factories.menu.nope"],
                        "problem_ids": [],
                    },
                )
            ]
        ),
    )
    executed = await _finalize_explore(project, write_root)
    assert executed.status == "failed"
    assert executed.failure is not None
    assert "IR-001: unresolvable assets.case_ids: ['TC_MENU_999']" in executed.failure.message
    assert "assets.factory_leafs outside the typed catalog" in executed.failure.message


@pytest.mark.asyncio
async def test_explore_finalize_rejects_inventory_for_another_change(tmp_path: Path) -> None:
    project, write_root = dual_roots(tmp_path)
    inventory = _valid_inventory()
    inventory["change_id"] = "CH-SIBLING"
    _stage_explore_outputs(write_root, inventory=inventory)
    executed = await _finalize_explore(project, write_root)
    assert executed.status == "failed"
    assert executed.failure is not None
    assert "impact-inventory.json change_id" in executed.failure.message


@pytest.mark.asyncio
async def test_explore_finalize_requires_the_prepared_context_in_staging(tmp_path: Path) -> None:
    project, write_root = dual_roots(tmp_path)
    _stage_explore_outputs(write_root)
    (write_root / _CONTEXT).unlink()
    executed = await _finalize_explore(project, write_root)
    assert executed.status == "failed"
    assert executed.failure is not None
    assert "context.json" in executed.failure.message


@pytest.mark.asyncio
async def test_intake_finalize_accepts_files_under_locked_prefix(tmp_path: Path) -> None:
    project, write_root = dual_roots(tmp_path)
    relative = "qa/cases/system/dept/case.yaml"
    payload = b"# RET-dept-management\n\nCover department CRUD.\n"
    path = write_root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    requirement = b"# Requirement\n"
    (write_root / "qa/requirement.md").write_bytes(requirement)
    marker = b"change_id: CH-DEMO-001\n"
    (write_root / "qa/.qa.yaml").write_bytes(marker)

    executed = await _finalize_files(
        cast(TaskHandler, finalize),
        {"output_files": ["qa/.qa.yaml", relative]},
        project,
        [
            "qa/.qa.yaml",
            "qa/cases",
            "qa/fixtures",
            "qa/proposal.md",
            "qa/requirement.md",
            "qa/results",
            "qa/tests",
        ],
        write_root=write_root,
        change_id="CH-DEMO-001",
    )
    assert executed.status == "succeeded"
    assert executed.output == {
        "artifacts": [
            {"path": "qa/.qa.yaml", "digest": hashlib.sha256(marker).hexdigest()},
            {"path": relative, "digest": hashlib.sha256(payload).hexdigest()},
            {"path": "qa/requirement.md", "digest": hashlib.sha256(requirement).hexdigest()},
        ]
    }


@pytest.mark.asyncio
async def test_intake_finalize_rejects_file_outside_locked_prefix(tmp_path: Path) -> None:
    project, write_root = dual_roots(tmp_path)
    relative = "qa/notes/outside.md"
    path = write_root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"nope\n")
    executed = await _finalize_files(
        cast(TaskHandler, finalize),
        {"output_files": [relative]},
        project,
        [
            "qa/.qa.yaml",
            "qa/cases",
            "qa/fixtures",
            "qa/proposal.md",
            "qa/requirement.md",
            "qa/results",
            "qa/tests",
        ],
        write_root=write_root,
    )
    assert executed.status == "failed"
    assert executed.failure is not None
    assert executed.failure.kind == "invalid_output"
    assert "undeclared" in executed.failure.message


@pytest.mark.asyncio
async def test_intake_finalize_returns_artifact_digests(tmp_path: Path) -> None:
    project, write_root = dual_roots(tmp_path)
    relative = "qa/.qa.yaml"
    path = write_root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = b"change_id: CH-DEMO-001\n"
    path.write_bytes(payload)
    requirement = b"# Requirement\n"
    (write_root / "qa/requirement.md").parent.mkdir(parents=True, exist_ok=True)
    (write_root / "qa/requirement.md").write_bytes(requirement)
    executed = await _finalize_files(
        cast(TaskHandler, finalize),
        {"output_files": [relative]},
        project,
        [relative, "qa/requirement.md"],
        write_root=write_root,
        change_id="CH-DEMO-001",
    )
    assert executed.status == "succeeded"
    assert executed.output == {
        "artifacts": [
            {"path": relative, "digest": hashlib.sha256(payload).hexdigest()},
            {"path": "qa/requirement.md", "digest": hashlib.sha256(requirement).hexdigest()},
        ]
    }
    AGENT_JOB_CONTRACTS["intake"].output_model.model_validate(executed.output)


@pytest.mark.asyncio
async def test_intake_finalize_rejects_stale_canonical_file_when_candidate_is_missing(
    tmp_path: Path,
) -> None:
    project, write_root = dual_roots(tmp_path)
    relative = "qa/requirement.md"
    stale = project / relative
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text("stale canonical content\n", encoding="utf-8")

    executed = await _finalize_files(
        cast(TaskHandler, finalize),
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
        cast(TaskHandler, case_design_finalize),
        cast(JSONValue, {"output_files": outputs}),
        project,
        [
            "qa/.qa.yaml",
            "qa/cases",
            "qa/fixtures",
            "qa/proposal.md",
            "qa/requirement.md",
            "qa/results",
            "qa/tests",
        ],
        change_id="CH-DEMO-001",
        selected_test_families=["api"],
        write_root=write_root,
    )
    assert executed.status == "succeeded", executed.failure
    assert [artifact["path"] for artifact in executed.output["artifacts"]] == sorted(outputs)
    assert not (write_root / "qa/results/case-index.json").exists()
    assert "added" not in executed.output
    AGENT_JOB_CONTRACTS["case-design"].output_model.model_validate(executed.output)


@pytest.mark.asyncio
async def test_case_design_finalize_routes_endpoint_literals_to_validation_repair(
    tmp_path: Path,
) -> None:
    authored = yaml.safe_load((_FIXTURES / "case-authoring-valid.yaml").read_text(encoding="utf-8"))
    authored["added"][0]["steps"] = ["以管理员身份调用POST /api/v1/menu/create 创建菜单"]
    project, write_root = dual_roots(tmp_path)
    outputs = _write_case_design_outputs(write_root, authored)
    executed = await _finalize_files(
        cast(TaskHandler, case_design_finalize),
        cast(JSONValue, {"output_files": outputs}),
        project,
        [
            "qa/.qa.yaml",
            "qa/cases",
            "qa/fixtures",
            "qa/proposal.md",
            "qa/requirement.md",
            "qa/results",
            "qa/tests",
        ],
        change_id="CH-DEMO-001",
        selected_test_families=["api"],
        write_root=write_root,
    )
    assert executed.status == "failed"
    assert executed.failure.kind == "invalid_output"
    error = executed.failure.message
    assert "must not contain an HTTP method + endpoint path" in error
    assert "'POST /api/v1/menu/create'" in error
    assert ".steps:" in error


@pytest.mark.asyncio
async def test_case_design_validation_repair_reads_unchanged_outputs_from_baseline(
    tmp_path: Path,
) -> None:
    valid = yaml.safe_load((_FIXTURES / "case-authoring-valid.yaml").read_text(encoding="utf-8"))
    invalid = deepcopy(valid)
    invalid["added"][0]["trace"] = {"entities.dept.constraints.unauthorized_user_management_api_access": True}
    project, write_root = dual_roots(tmp_path)
    outputs = _write_case_design_outputs(project, invalid)
    _write_case_delta(write_root, valid)

    executed = await _finalize_files(
        cast(TaskHandler, case_design_finalize),
        cast(JSONValue, {"output_files": outputs}),
        project,
        [
            "qa/.qa.yaml",
            "qa/cases",
            "qa/fixtures",
            "qa/proposal.md",
            "qa/requirement.md",
            "qa/results",
            "qa/tests",
        ],
        change_id="CH-DEMO-001",
        selected_test_families=["api"],
        write_root=write_root,
    )

    assert executed.status == "failed"
    assert executed.failure.kind == "invalid_output"
    assert (write_root / "qa/cases/menus/case.yaml").is_file()
    assert not (write_root / "qa/.qa.yaml").exists()
    assert not (write_root / "qa/proposal.md").exists()
    assert not (write_root / "qa/results/trace/minimum-coverage-matrix.json").exists()


@pytest.mark.asyncio
async def test_case_repair_finalize_accepts_only_the_review_locator_change(tmp_path: Path) -> None:
    authored = yaml.safe_load((_FIXTURES / "case-authoring-valid.yaml").read_text(encoding="utf-8"))
    project, write_root = dual_roots(tmp_path)
    outputs = _write_case_design_outputs(project, authored)
    _write_fixable_case_review(project, allowed_key="title")
    repair = await _prepared_review_repair(project)
    authored["added"][0]["title"] = "create menu with validated response"
    _write_case_delta(write_root, authored)

    executed = await _finalize_files(
        cast(TaskHandler, case_repair_finalize),
        cast(JSONValue, {"output_files": outputs}),
        project,
        [
            "qa/.qa.yaml",
            "qa/cases",
            "qa/fixtures",
            "qa/proposal.md",
            "qa/requirement.md",
            "qa/results",
            "qa/tests",
        ],
        change_id="CH-DEMO-001",
        selected_test_families=["api"],
        write_root=write_root,
        review_repair=repair,
    )

    assert executed.status == "succeeded"
    assert executed.output["review_repair"] == repair


@pytest.mark.asyncio
async def test_case_repair_finalize_accepts_exact_review_authorized_case_addition(
    tmp_path: Path,
) -> None:
    authored = yaml.safe_load((_FIXTURES / "case-authoring-valid.yaml").read_text(encoding="utf-8"))
    added_case = deepcopy(authored["added"][0])
    added_case.update(
        {
            "case_id": "TC_MENU_002",
            "title": "no-role superuser is denied",
            "test_condition_id": "COND-2",
            "related_cases": ["TC_MENU_001"],
        }
    )
    project, write_root = dual_roots(tmp_path)
    outputs = _write_case_design_outputs(project, authored)
    _write_fixable_case_review(project, allowed_key="added", case_id="TC_MENU_002")
    review_path = project / "qa/results/review/case-review.json"
    review = json.loads(review_path.read_text(encoding="utf-8"))
    review["auto_fix_plan"][0]["edits"] = ["Add TC_MENU_002 under added."]
    review_path.write_text(json.dumps(review), encoding="utf-8")
    repair = await _prepared_review_repair(project)
    authored["added"].append(added_case)
    _write_case_delta(write_root, authored)

    executed = await _finalize_files(
        cast(TaskHandler, case_repair_finalize),
        cast(JSONValue, {"output_files": outputs}),
        project,
        [
            "qa/.qa.yaml",
            "qa/cases",
            "qa/fixtures",
            "qa/proposal.md",
            "qa/requirement.md",
            "qa/results",
            "qa/tests",
        ],
        change_id="CH-DEMO-001",
        selected_test_families=["api"],
        write_root=write_root,
        review_repair=repair,
    )

    assert executed.status == "succeeded", executed.failure
    assert executed.output["review_repair"] == repair


@pytest.mark.asyncio
async def test_case_repair_finalize_accepts_exact_review_authorized_case_removal(
    tmp_path: Path,
) -> None:
    authored = yaml.safe_load((_FIXTURES / "case-authoring-valid.yaml").read_text(encoding="utf-8"))
    removable_case = deepcopy(authored["added"][0])
    removable_case.update(
        {
            "case_id": "TC_MENU_002",
            "title": "obsolete duplicate menu scenario",
            "test_condition_id": "COND-2",
            "related_cases": ["TC_MENU_001"],
        }
    )
    authored["added"].append(removable_case)
    project, write_root = dual_roots(tmp_path)
    outputs = _write_case_design_outputs(project, authored)
    _write_fixable_case_review(project, allowed_key="added", case_id="TC_MENU_002")
    review_path = project / "qa/results/review/case-review.json"
    review = json.loads(review_path.read_text(encoding="utf-8"))
    review["auto_fix_plan"][0]["edits"] = ["Remove TC_MENU_002 from added."]
    review_path.write_text(json.dumps(review), encoding="utf-8")
    repair = await _prepared_review_repair(project)
    authored["added"] = [authored["added"][0]]
    _write_case_delta(write_root, authored)

    executed = await _finalize_files(
        cast(TaskHandler, case_repair_finalize),
        cast(JSONValue, {"output_files": outputs}),
        project,
        [
            "qa/.qa.yaml",
            "qa/cases",
            "qa/fixtures",
            "qa/proposal.md",
            "qa/requirement.md",
            "qa/results",
            "qa/tests",
        ],
        change_id="CH-DEMO-001",
        selected_test_families=["api"],
        write_root=write_root,
        review_repair=repair,
    )

    assert executed.status == "succeeded", executed.failure
    assert executed.output["review_repair"] == repair


@pytest.mark.asyncio
async def test_case_repair_finalize_rejects_unreviewed_case_addition(tmp_path: Path) -> None:
    authored = yaml.safe_load((_FIXTURES / "case-authoring-valid.yaml").read_text(encoding="utf-8"))
    added_case = deepcopy(authored["added"][0])
    added_case.update(
        {
            "case_id": "TC_MENU_002",
            "title": "unauthorized new scenario",
            "test_condition_id": "COND-2",
            "related_cases": ["TC_MENU_001"],
        }
    )
    project, write_root = dual_roots(tmp_path)
    outputs = _write_case_design_outputs(project, authored)
    _write_fixable_case_review(project, allowed_key="title", case_id="TC_MENU_001")
    repair = await _prepared_review_repair(project)
    authored["added"][0]["title"] = "create menu with validated response"
    authored["added"].append(added_case)
    _write_case_delta(write_root, authored)

    executed = await _finalize_files(
        cast(TaskHandler, case_repair_finalize),
        cast(JSONValue, {"output_files": outputs}),
        project,
        [
            "qa/.qa.yaml",
            "qa/cases",
            "qa/fixtures",
            "qa/proposal.md",
            "qa/requirement.md",
            "qa/results",
            "qa/tests",
        ],
        change_id="CH-DEMO-001",
        selected_test_families=["api"],
        write_root=write_root,
        review_repair=repair,
    )

    assert executed.status == "failed"
    assert executed.failure is not None
    assert "added, removed, moved, or reordered a case" in executed.failure.message


@pytest.mark.asyncio
async def test_case_repair_finalize_rejects_unreviewed_case_removal(tmp_path: Path) -> None:
    authored = yaml.safe_load((_FIXTURES / "case-authoring-valid.yaml").read_text(encoding="utf-8"))
    removable_case = deepcopy(authored["added"][0])
    removable_case.update(
        {
            "case_id": "TC_MENU_002",
            "title": "obsolete duplicate menu scenario",
            "test_condition_id": "COND-2",
            "related_cases": ["TC_MENU_001"],
        }
    )
    authored["added"].append(removable_case)
    project, write_root = dual_roots(tmp_path)
    outputs = _write_case_design_outputs(project, authored)
    _write_fixable_case_review(project, allowed_key="added", case_id="TC_MENU_002")
    review_path = project / "qa/results/review/case-review.json"
    review = json.loads(review_path.read_text(encoding="utf-8"))
    review["auto_fix_plan"][0]["edits"] = ["Remove TC_MENU_002 from added."]
    review_path.write_text(json.dumps(review), encoding="utf-8")
    repair = await _prepared_review_repair(project)
    authored["added"] = [authored["added"][1]]
    _write_case_delta(write_root, authored)

    executed = await _finalize_files(
        cast(TaskHandler, case_repair_finalize),
        cast(JSONValue, {"output_files": outputs}),
        project,
        [
            "qa/.qa.yaml",
            "qa/cases",
            "qa/fixtures",
            "qa/proposal.md",
            "qa/requirement.md",
            "qa/results",
            "qa/tests",
        ],
        change_id="CH-DEMO-001",
        selected_test_families=["api"],
        write_root=write_root,
        review_repair=repair,
    )

    assert executed.status == "failed"
    assert executed.failure is not None
    assert "added, removed, moved, or reordered a case" in executed.failure.message


@pytest.mark.asyncio
async def test_case_repair_finalize_reads_unchanged_review_outputs_from_baseline(
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
        cast(TaskHandler, case_repair_finalize),
        cast(JSONValue, {"output_files": outputs}),
        project,
        [
            "qa/.qa.yaml",
            "qa/cases",
            "qa/fixtures",
            "qa/proposal.md",
            "qa/requirement.md",
            "qa/results",
            "qa/tests",
        ],
        change_id="CH-DEMO-001",
        selected_test_families=["api"],
        write_root=write_root,
        review_repair=repair,
    )

    assert executed.status == "succeeded"
    assert executed.output["review_repair"] == repair
    assert [artifact["path"] for artifact in executed.output["artifacts"]] == sorted(outputs)
    assert (write_root / "qa/cases/menus/case.yaml").is_file()
    assert not (write_root / "qa/.qa.yaml").exists()
    assert not (write_root / "qa/proposal.md").exists()
    assert not (write_root / "qa/results/trace/minimum-coverage-matrix.json").exists()


@pytest.mark.asyncio
async def test_case_repair_finalize_accepts_only_the_named_proposal_section(tmp_path: Path) -> None:
    authored = yaml.safe_load((_FIXTURES / "case-authoring-valid.yaml").read_text(encoding="utf-8"))
    project, write_root = dual_roots(tmp_path)
    outputs = _write_case_design_outputs(project, authored)
    proposal = project / "qa/proposal.md"
    proposal.write_text(
        "# Proposal\n\n## Data Needs\n- old need\n\n## Other\n- unchanged\n",
        encoding="utf-8",
    )
    artifact = "qa/proposal.md"
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
        cast(TaskHandler, case_repair_finalize),
        cast(JSONValue, {"output_files": outputs}),
        project,
        [
            "qa/.qa.yaml",
            "qa/cases",
            "qa/fixtures",
            "qa/proposal.md",
            "qa/requirement.md",
            "qa/results",
            "qa/tests",
        ],
        change_id="CH-DEMO-001",
        selected_test_families=["api"],
        write_root=write_root,
        review_repair=repair,
    )

    assert executed.status == "succeeded"
    assert executed.output["review_repair"] == repair


@pytest.mark.asyncio
async def test_case_repair_finalize_rejects_proposal_change_outside_named_section(
    tmp_path: Path,
) -> None:
    authored = yaml.safe_load((_FIXTURES / "case-authoring-valid.yaml").read_text(encoding="utf-8"))
    project, write_root = dual_roots(tmp_path)
    outputs = _write_case_design_outputs(project, authored)
    proposal = project / "qa/proposal.md"
    proposal.write_text(
        "# Proposal\n\n## Data Needs\n- old need\n\n## Other\n- unchanged\n",
        encoding="utf-8",
    )
    artifact = "qa/proposal.md"
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
        cast(TaskHandler, case_repair_finalize),
        cast(JSONValue, {"output_files": outputs}),
        project,
        [
            "qa/.qa.yaml",
            "qa/cases",
            "qa/fixtures",
            "qa/proposal.md",
            "qa/requirement.md",
            "qa/results",
            "qa/tests",
        ],
        change_id="CH-DEMO-001",
        selected_test_families=["api"],
        write_root=write_root,
        review_repair=repair,
    )

    assert executed.status == "failed"
    assert executed.failure is not None
    assert "outside named section" in executed.failure.message


@pytest.mark.asyncio
async def test_case_repair_finalize_treats_h1_as_end_of_named_proposal_section(
    tmp_path: Path,
) -> None:
    authored = yaml.safe_load((_FIXTURES / "case-authoring-valid.yaml").read_text(encoding="utf-8"))
    project, write_root = dual_roots(tmp_path)
    outputs = _write_case_design_outputs(project, authored)
    proposal = project / "qa/proposal.md"
    proposal.write_text(
        "# Proposal\n\n## Data Needs\n- old need\n\n# Appendix\n- unchanged\n",
        encoding="utf-8",
    )
    artifact = "qa/proposal.md"
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
        cast(TaskHandler, case_repair_finalize),
        cast(JSONValue, {"output_files": outputs}),
        project,
        [
            "qa/.qa.yaml",
            "qa/cases",
            "qa/fixtures",
            "qa/proposal.md",
            "qa/requirement.md",
            "qa/results",
            "qa/tests",
        ],
        change_id="CH-DEMO-001",
        selected_test_families=["api"],
        write_root=write_root,
        review_repair=repair,
    )

    assert executed.status == "failed"
    assert executed.failure is not None
    assert "outside named section" in executed.failure.message


@pytest.mark.asyncio
async def test_case_repair_finalize_ignores_heading_inside_proposal_fence(tmp_path: Path) -> None:
    authored = yaml.safe_load((_FIXTURES / "case-authoring-valid.yaml").read_text(encoding="utf-8"))
    project, write_root = dual_roots(tmp_path)
    outputs = _write_case_design_outputs(project, authored)
    proposal = project / "qa/proposal.md"
    proposal.write_text(
        "# Proposal\n\n```md\n## Data Needs\nexample\n```\n\n## Data Needs\n- old need\n",
        encoding="utf-8",
    )
    artifact = "qa/proposal.md"
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
        cast(TaskHandler, case_repair_finalize),
        cast(JSONValue, {"output_files": outputs}),
        project,
        [
            "qa/.qa.yaml",
            "qa/cases",
            "qa/fixtures",
            "qa/proposal.md",
            "qa/requirement.md",
            "qa/results",
            "qa/tests",
        ],
        change_id="CH-DEMO-001",
        selected_test_families=["api"],
        write_root=write_root,
        review_repair=repair,
    )

    assert executed.status == "succeeded"


@pytest.mark.asyncio
async def test_case_repair_finalize_accepts_only_named_mrc_rows(tmp_path: Path) -> None:
    authored = yaml.safe_load((_FIXTURES / "case-authoring-valid.yaml").read_text(encoding="utf-8"))
    project, write_root = dual_roots(tmp_path)
    outputs = _write_case_design_outputs(project, authored)
    artifact = "qa/results/trace/minimum-coverage-matrix.json"
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
        cast(TaskHandler, case_repair_finalize),
        cast(JSONValue, {"output_files": outputs}),
        project,
        [
            "qa/.qa.yaml",
            "qa/cases",
            "qa/fixtures",
            "qa/proposal.md",
            "qa/requirement.md",
            "qa/results",
            "qa/tests",
        ],
        change_id="CH-DEMO-001",
        selected_test_families=["api"],
        write_root=write_root,
        review_repair=repair,
    )

    assert executed.status == "succeeded"
    assert executed.output["review_repair"] == repair


@pytest.mark.asyncio
async def test_case_repair_finalize_rejects_mrc_change_outside_named_rows(tmp_path: Path) -> None:
    authored = yaml.safe_load((_FIXTURES / "case-authoring-valid.yaml").read_text(encoding="utf-8"))
    project, write_root = dual_roots(tmp_path)
    outputs = _write_case_design_outputs(project, authored)
    artifact = "qa/results/trace/minimum-coverage-matrix.json"
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
        cast(TaskHandler, case_repair_finalize),
        cast(JSONValue, {"output_files": outputs}),
        project,
        [
            "qa/.qa.yaml",
            "qa/cases",
            "qa/fixtures",
            "qa/proposal.md",
            "qa/requirement.md",
            "qa/results",
            "qa/tests",
        ],
        change_id="CH-DEMO-001",
        selected_test_families=["api"],
        write_root=write_root,
        review_repair=repair,
    )

    assert executed.status == "failed"
    assert executed.failure is not None
    assert "outside named MRC rows" in executed.failure.message


@pytest.mark.asyncio
async def test_case_repair_finalize_rejects_review_receipt_outside_frozen_outputs(
    tmp_path: Path,
) -> None:
    authored = yaml.safe_load((_FIXTURES / "case-authoring-valid.yaml").read_text(encoding="utf-8"))
    project, write_root = dual_roots(tmp_path)
    outputs = _write_case_design_outputs(project, authored)
    _write_fixable_case_review(project, allowed_key="title")
    repair = await _prepared_review_repair(project)
    authored["added"][0]["title"] = "create menu with validated response"
    _write_case_delta(write_root, authored)
    extra = "qa/results/repair-notes.txt"
    extra_path = write_root / extra
    extra_path.parent.mkdir(parents=True, exist_ok=True)
    extra_path.write_text("unauthorized expansion\n", encoding="utf-8")

    executed = await _finalize_files(
        cast(TaskHandler, case_repair_finalize),
        cast(JSONValue, {"output_files": sorted([*outputs, extra])}),
        project,
        [
            "qa/.qa.yaml",
            "qa/cases",
            "qa/fixtures",
            "qa/proposal.md",
            "qa/requirement.md",
            "qa/results",
            "qa/tests",
        ],
        change_id="CH-DEMO-001",
        selected_test_families=["api"],
        write_root=write_root,
        review_repair=repair,
    )

    assert executed.status == "failed"
    assert executed.failure is not None
    assert "frozen case-design outputs" in executed.failure.message


@pytest.mark.asyncio
async def test_case_repair_finalize_rejects_non_regular_staged_review_output(
    tmp_path: Path,
) -> None:
    authored = yaml.safe_load((_FIXTURES / "case-authoring-valid.yaml").read_text(encoding="utf-8"))
    project, write_root = dual_roots(tmp_path)
    outputs = _write_case_design_outputs(project, authored)
    _write_fixable_case_review(project, allowed_key="title")
    repair = await _prepared_review_repair(project)
    authored["added"][0]["title"] = "create menu with validated response"
    _write_case_delta(write_root, authored)
    (write_root / "qa/proposal.md").mkdir(parents=True)

    executed = await _finalize_files(
        cast(TaskHandler, case_repair_finalize),
        cast(JSONValue, {"output_files": outputs}),
        project,
        [
            "qa/.qa.yaml",
            "qa/cases",
            "qa/fixtures",
            "qa/proposal.md",
            "qa/requirement.md",
            "qa/results",
            "qa/tests",
        ],
        change_id="CH-DEMO-001",
        selected_test_families=["api"],
        write_root=write_root,
        review_repair=repair,
    )

    assert executed.status == "failed"
    assert executed.failure is not None
    assert "staged a non-target output" in executed.failure.message


@pytest.mark.asyncio
async def test_case_repair_finalize_accepts_exact_dotted_trace_leaf_repair(tmp_path: Path) -> None:
    authored = yaml.safe_load((_FIXTURES / "case-authoring-valid.yaml").read_text(encoding="utf-8"))
    authored["added"][0]["trace"]["auth.session.create"] = {"covered": True}
    project, write_root = dual_roots(tmp_path)
    outputs = _write_case_design_outputs(project, authored)
    _write_fixable_case_review(project, allowed_key="trace.entities.item.create")
    repair = await _prepared_review_repair(project)
    authored["added"][0]["trace"].pop("entities.item.create")
    _write_case_delta(write_root, authored)

    executed = await _finalize_files(
        cast(TaskHandler, case_repair_finalize),
        cast(JSONValue, {"output_files": outputs}),
        project,
        [
            "qa/.qa.yaml",
            "qa/cases",
            "qa/fixtures",
            "qa/proposal.md",
            "qa/requirement.md",
            "qa/results",
            "qa/tests",
        ],
        change_id="CH-DEMO-001",
        selected_test_families=["api"],
        write_root=write_root,
        review_repair=repair,
    )

    assert executed.status == "succeeded"
    assert executed.output["review_repair"] == repair


@pytest.mark.asyncio
async def test_case_repair_finalize_rejects_review_repair_that_rewrites_non_target_output(
    tmp_path: Path,
) -> None:
    authored = yaml.safe_load((_FIXTURES / "case-authoring-valid.yaml").read_text(encoding="utf-8"))
    project, write_root = dual_roots(tmp_path)
    outputs = _write_case_design_outputs(project, authored)
    _write_fixable_case_review(project, allowed_key="title")
    repair = await _prepared_review_repair(project)
    authored["added"][0]["title"] = "create menu with validated response"
    _write_case_delta(write_root, authored)
    (write_root / "qa/proposal.md").write_text("# Replanned proposal\n", encoding="utf-8")

    executed = await _finalize_files(
        cast(TaskHandler, case_repair_finalize),
        cast(JSONValue, {"output_files": outputs}),
        project,
        [
            "qa/.qa.yaml",
            "qa/cases",
            "qa/fixtures",
            "qa/proposal.md",
            "qa/requirement.md",
            "qa/results",
            "qa/tests",
        ],
        change_id="CH-DEMO-001",
        selected_test_families=["api"],
        write_root=write_root,
        review_repair=repair,
    )

    assert executed.status == "failed"
    assert executed.failure is not None
    assert "staged a non-target output" in executed.failure.message


@pytest.mark.asyncio
async def test_case_repair_finalize_rejects_review_repair_outside_allowed_case_fields(
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
        cast(TaskHandler, case_repair_finalize),
        cast(JSONValue, {"output_files": outputs}),
        project,
        [
            "qa/.qa.yaml",
            "qa/cases",
            "qa/fixtures",
            "qa/proposal.md",
            "qa/requirement.md",
            "qa/results",
            "qa/tests",
        ],
        change_id="CH-DEMO-001",
        selected_test_families=["api"],
        write_root=write_root,
        review_repair=repair,
    )

    assert executed.status == "failed"
    assert executed.failure is not None
    assert "outside allowed_paths" in executed.failure.message


@pytest.mark.asyncio
async def test_case_repair_finalize_rejects_invalid_first_review_repair_without_rewriting(
    tmp_path: Path,
) -> None:
    authored = yaml.safe_load((_FIXTURES / "case-authoring-valid.yaml").read_text(encoding="utf-8"))
    project, write_root = dual_roots(tmp_path)
    outputs = _write_case_design_outputs(project, authored)
    case_relative = "qa/cases/menus/case.yaml"
    baseline = (project / case_relative).read_bytes()
    _write_fixable_case_review(project, allowed_key="title")
    repair = await _prepared_review_repair(project)
    authored["added"][0]["title"] = "create menu with validated response"
    authored["added"][0]["objective"] = "unauthorized replanning"
    _write_case_delta(write_root, authored)

    executed = await _finalize_files(
        cast(TaskHandler, case_repair_finalize),
        cast(JSONValue, {"output_files": outputs}),
        project,
        [
            "qa/.qa.yaml",
            "qa/cases",
            "qa/fixtures",
            "qa/proposal.md",
            "qa/requirement.md",
            "qa/results",
            "qa/tests",
        ],
        change_id="CH-DEMO-001",
        selected_test_families=["api"],
        write_root=write_root,
        review_repair=repair,
    )

    assert executed.status == "failed"
    assert executed.failure is not None
    assert executed.failure.kind == "invalid_output"
    assert "outside allowed_paths" in executed.failure.message
    assert (project / case_relative).read_bytes() == baseline
    # The finalizer has no runtime write authority.  A failed Attempt does not
    # promote these bytes; the next repair Attempt gets a fresh workspace.
    assert (write_root / case_relative).read_bytes() != baseline


@pytest.mark.asyncio
async def test_case_repair_finalize_rejects_review_repair_that_adds_case_top_level_data(
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
        cast(TaskHandler, case_repair_finalize),
        cast(JSONValue, {"output_files": outputs}),
        project,
        [
            "qa/.qa.yaml",
            "qa/cases",
            "qa/fixtures",
            "qa/proposal.md",
            "qa/requirement.md",
            "qa/results",
            "qa/tests",
        ],
        change_id="CH-DEMO-001",
        selected_test_families=["api"],
        write_root=write_root,
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
        cast(TaskHandler, case_design_finalize),
        cast(JSONValue, {"output_files": outputs}),
        project,
        [
            "qa/.qa.yaml",
            "qa/cases",
            "qa/fixtures",
            "qa/proposal.md",
            "qa/requirement.md",
            "qa/results",
            "qa/tests",
        ],
        change_id="CH-DEMO-001",
        selected_test_families=["api"],
        write_root=write_root,
    )

    assert executed.status == "failed"
    assert executed.failure.kind == "invalid_output"
    assert "capability key is not a declared typed leaf" in executed.failure.message


@pytest.mark.asyncio
async def test_case_design_finalize_rejects_unlocked_module_or_sibling_case(tmp_path: Path) -> None:
    authored = cast(
        JSONValue,
        yaml.safe_load((_FIXTURES / "case-authoring-valid.yaml").read_text(encoding="utf-8")),
    )
    project, write_root = dual_roots(tmp_path)
    outputs = _write_case_design_outputs(write_root, authored)

    for locked in (
        ["qa/cases/roles/case.yaml"],
        ["qa/cases/menus/other/case.yaml"],
    ):
        executed = await _finalize_files(
            cast(TaskHandler, case_design_finalize),
            cast(JSONValue, {"output_files": outputs}),
            project,
            [
                "qa/.qa.yaml",
                "qa/cases",
                "qa/fixtures",
                "qa/proposal.md",
                "qa/requirement.md",
                "qa/results",
                "qa/tests",
            ],
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
    matrix = "qa/results/trace/minimum-coverage-matrix.json"
    outputs.remove(matrix)

    executed = await _finalize_files(
        cast(TaskHandler, case_design_finalize),
        cast(JSONValue, {"output_files": outputs}),
        project,
        [
            "qa/.qa.yaml",
            "qa/cases",
            "qa/fixtures",
            "qa/proposal.md",
            "qa/requirement.md",
            "qa/results",
            "qa/tests",
        ],
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
        cast(TaskHandler, case_design_finalize),
        cast(JSONValue, {"output_files": outputs}),
        project,
        [
            "qa/.qa.yaml",
            "qa/cases",
            "qa/fixtures",
            "qa/proposal.md",
            "qa/requirement.md",
            "qa/results",
            "qa/tests",
        ],
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
        cast(TaskHandler, case_design_finalize),
        cast(JSONValue, authored),
        project,
        [
            "qa/.qa.yaml",
            "qa/cases",
            "qa/fixtures",
            "qa/proposal.md",
            "qa/requirement.md",
            "qa/results",
            "qa/tests",
        ],
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
        "qa/.qa.yaml",
        "qa/proposal.md",
        "qa/results/trace/minimum-coverage-matrix.json",
    ]
    assert set(catalog) == set(outputs) - {outputs[-1]}

    executed = await _finalize_files(
        cast(TaskHandler, case_design_finalize),
        cast(JSONValue, {"output_files": catalog}),
        project,
        [
            "qa/.qa.yaml",
            "qa/cases",
            "qa/fixtures",
            "qa/proposal.md",
            "qa/requirement.md",
            "qa/results",
            "qa/tests",
        ],
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
        cast(TaskHandler, case_design_finalize),
        cast(JSONValue, {"output_files": outputs}),
        project,
        [
            "qa/.qa.yaml",
            "qa/cases",
            "qa/fixtures",
            "qa/proposal.md",
            "qa/requirement.md",
            "qa/results",
            "qa/tests",
        ],
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
    executed = await _finalize_review_with_written_cases(tmp_path, _case_review_document(missing=[]))
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
    change_root = project / "qa"
    requirement = change_root / "requirement.md"
    requirement.write_text("# Requirement\n", encoding="utf-8")
    case_path = change_root / "cases/menus/case.yaml"
    case_path.parent.mkdir(parents=True, exist_ok=True)
    case_path.write_bytes((_FIXTURES / "case-authoring-valid.yaml").read_bytes())
    review_document = _case_review_document(missing=[])
    review_path = write_root / "qa/results/review/case-review.json"
    review_path.parent.mkdir(parents=True, exist_ok=True)
    review_path.write_text(json.dumps(review_document), encoding="utf-8")
    (review_path.parent / "case-review-summary.md").write_text("# Case review\n", encoding="utf-8")
    preparation_refs = [
        {
            "path": requirement.relative_to(project).as_posix(),
            "digest": hashlib.sha256(requirement.read_bytes()).hexdigest(),
        },
        {
            "path": "qa/results/trace/minimum-coverage-matrix.json",
            "digest": hashlib.sha256(
                (change_root / "results/trace/minimum-coverage-matrix.json").read_bytes()
            ).hexdigest(),
        },
    ]
    case_refs = [
        {
            "path": case_path.relative_to(project).as_posix(),
            "digest": hashlib.sha256(case_path.read_bytes()).hexdigest(),
        }
    ]
    case_delta_paths = [case_path.relative_to(project).as_posix()]
    bound_preparation = _install_and_write_case_review_seal(
        project=project,
        write_root=write_root,
        document=cast(Mapping[str, object], review_document),
        case_refs=case_refs,
        preparation_refs=preparation_refs,
        case_delta_paths=case_delta_paths,
    )

    executed = await _finalize_files(
        cast(TaskHandler, case_review_finalize),
        review_document,
        project,
        [
            "qa/.qa.yaml",
            "qa/cases",
            "qa/fixtures",
            "qa/proposal.md",
            "qa/requirement.md",
            "qa/results",
            "qa/tests",
        ],
        change_id="CH-DEMO-001",
        case_delta_paths=case_delta_paths,
        preparation_refs=bound_preparation,
        case_refs=case_refs,
        write_root=write_root,
    )

    assert executed.status == "succeeded", executed.failure
    output = cast(dict[str, object], executed.output)
    reviewed = cast(dict[str, object], output["reviewed_case"])
    assert reviewed["case_refs"] == case_refs
    manifest = write_root / "qa/cases/reviewed-case.json"
    assert json.loads(manifest.read_bytes()) == reviewed


@pytest.mark.asyncio
async def test_case_review_finalize_preserves_each_epoch_history_and_updates_latest(
    tmp_path: Path,
) -> None:
    project, write_root = dual_roots(tmp_path)
    _write_review_matrix(project, missing=[])
    change_root = project / "qa"
    requirement = change_root / "requirement.md"
    requirement.write_text("# Requirement\n", encoding="utf-8")
    case_path = change_root / "cases/menus/case.yaml"
    case_path.parent.mkdir(parents=True, exist_ok=True)
    case_path.write_bytes((_FIXTURES / "case-authoring-valid.yaml").read_bytes())
    review_document = _case_review_document(missing=[])
    review_path = write_root / "qa/results/review/case-review.json"
    review_path.parent.mkdir(parents=True, exist_ok=True)
    review_path.write_text(json.dumps(review_document), encoding="utf-8")
    (review_path.parent / "case-review-summary.md").write_text("# Case review\n", encoding="utf-8")
    preparation_refs = [
        {
            "path": requirement.relative_to(project).as_posix(),
            "digest": hashlib.sha256(requirement.read_bytes()).hexdigest(),
        },
        {
            "path": "qa/results/trace/minimum-coverage-matrix.json",
            "digest": hashlib.sha256(
                (change_root / "results/trace/minimum-coverage-matrix.json").read_bytes()
            ).hexdigest(),
        },
    ]
    case_refs = [
        {
            "path": case_path.relative_to(project).as_posix(),
            "digest": hashlib.sha256(case_path.read_bytes()).hexdigest(),
        }
    ]
    case_delta_paths = [case_path.relative_to(project).as_posix()]
    artifact_paths = [
        "qa/.qa.yaml",
        "qa/cases",
        "qa/fixtures",
        "qa/proposal.md",
        "qa/requirement.md",
        "qa/results",
        "qa/tests",
    ]
    bound_preparation = _install_and_write_case_review_seal(
        project=project,
        write_root=write_root,
        document=cast(Mapping[str, object], review_document),
        case_refs=case_refs,
        preparation_refs=preparation_refs,
        case_delta_paths=case_delta_paths,
        coverage_epoch=0,
    )

    first = await _finalize_files(
        cast(TaskHandler, case_review_finalize),
        review_document,
        project,
        artifact_paths,
        change_id="CH-DEMO-001",
        coverage_epoch=0,
        review_round=0,
        case_delta_paths=case_delta_paths,
        preparation_refs=bound_preparation,
        case_refs=case_refs,
        write_root=write_root,
    )
    assert first.status == "succeeded", first.failure
    first_history_path = write_root / "qa/cases/reviews/epochs/0/rounds/0.json"
    first_history_bytes = first_history_path.read_bytes()
    resumed = await _finalize_files(
        cast(TaskHandler, case_review_finalize),
        review_document,
        project,
        artifact_paths,
        change_id="CH-DEMO-001",
        coverage_epoch=0,
        review_round=0,
        case_delta_paths=case_delta_paths,
        preparation_refs=bound_preparation,
        case_refs=case_refs,
        write_root=write_root,
    )
    _install_and_write_case_review_seal(
        project=project,
        write_root=write_root,
        document=cast(Mapping[str, object], review_document),
        case_refs=case_refs,
        preparation_refs=bound_preparation,
        case_delta_paths=case_delta_paths,
        coverage_epoch=1,
    )
    second = await _finalize_files(
        cast(TaskHandler, case_review_finalize),
        review_document,
        project,
        artifact_paths,
        change_id="CH-DEMO-001",
        coverage_epoch=1,
        review_round=0,
        case_delta_paths=case_delta_paths,
        preparation_refs=bound_preparation,
        case_refs=case_refs,
        write_root=write_root,
    )

    assert first.status == resumed.status == second.status == "succeeded"
    assert first_history_path.read_bytes() == first_history_bytes
    first_history = write_root / "qa/cases/reviews/epochs/0/rounds/0.json"
    second_history = write_root / "qa/cases/reviews/epochs/1/rounds/0.json"
    assert json.loads(first_history.read_bytes())["coverage_epoch"] == 0
    assert json.loads(second_history.read_bytes())["coverage_epoch"] == 1
    manifest = json.loads((write_root / "qa/cases/reviewed-case.json").read_bytes())
    assert manifest["coverage_epoch"] == 1
    assert cast(dict[str, object], second.output)["history_ref"] == {
        "path": "qa/cases/reviews/epochs/1/rounds/0.json",
        "digest": hashlib.sha256(second_history.read_bytes()).hexdigest(),
    }


@pytest.mark.asyncio
async def test_case_review_finalize_preserves_raw_review_bytes(
    tmp_path: Path,
) -> None:
    project, write_root = dual_roots(tmp_path)
    _write_review_matrix(project, missing=[])
    case_relative = _write_case_delta(
        project,
        yaml.safe_load((_FIXTURES / "case-authoring-valid.yaml").read_text(encoding="utf-8")),
    )
    review_document = _case_review_document(missing=[])
    review_path = write_root / "qa/results/review/case-review.json"
    review_path.parent.mkdir(parents=True, exist_ok=True)
    review_path.write_text(json.dumps(review_document), encoding="utf-8")
    (review_path.parent / "case-review-summary.md").write_text("# Case review\n", encoding="utf-8")
    case_refs = [
        {
            "path": case_relative,
            "digest": hashlib.sha256((project / case_relative).read_bytes()).hexdigest(),
        }
    ]
    bound_preparation = _install_and_write_case_review_seal(
        project=project,
        write_root=write_root,
        document=cast(Mapping[str, object], review_document),
        case_refs=case_refs,
        preparation_refs=[
            {
                "path": "qa/results/trace/minimum-coverage-matrix.json",
                "digest": hashlib.sha256(
                    (project / "qa/results/trace/minimum-coverage-matrix.json").read_bytes()
                ).hexdigest(),
            }
        ],
        case_delta_paths=[case_relative],
    )
    before = {
        relative: (write_root / relative).read_bytes()
        for relative in case_review_outputs("CH-DEMO-001")
        if relative != "qa/results/review/case-review-summary.md"
    }
    executed = await _finalize_files(
        cast(TaskHandler, case_review_finalize),
        review_document,
        project,
        list(case_review_outputs("CH-DEMO-001")),
        change_id="CH-DEMO-001",
        case_delta_paths=[case_relative],
        preparation_refs=bound_preparation,
        case_refs=case_refs,
        write_root=write_root,
    )
    assert executed.status == "succeeded", executed.failure
    for relative, data in before.items():
        assert (write_root / relative).read_bytes() == data


@pytest.mark.asyncio
async def test_case_review_finalize_generates_host_seals(
    tmp_path: Path,
) -> None:
    from tests.capabilities.finalize_phase import checked_finalize

    project, write_root = dual_roots(tmp_path)
    _write_review_matrix(project, missing=[])
    case_relative = _write_case_delta(
        project,
        yaml.safe_load((_FIXTURES / "case-authoring-valid.yaml").read_text(encoding="utf-8")),
    )
    review_document = _case_review_document(missing=[])
    review_path = write_root / "qa/results/review/case-review.json"
    review_path.parent.mkdir(parents=True, exist_ok=True)
    review_path.write_text(json.dumps(review_document), encoding="utf-8")
    (review_path.parent / "case-review-summary.md").write_text("# Case review\n", encoding="utf-8")
    case_refs = [
        {
            "path": case_relative,
            "digest": hashlib.sha256((project / case_relative).read_bytes()).hexdigest(),
        }
    ]
    bound_preparation = _install_and_write_case_review_seal(
        project=project,
        write_root=write_root,
        document=cast(Mapping[str, object], review_document),
        case_refs=case_refs,
        preparation_refs=[
            {
                "path": "qa/results/trace/minimum-coverage-matrix.json",
                "digest": hashlib.sha256(
                    (project / "qa/results/trace/minimum-coverage-matrix.json").read_bytes()
                ).hexdigest(),
            }
        ],
        case_delta_paths=[case_relative],
    )
    for relative in (
        "qa/results/cases/epochs/0/selection.json",
        "qa/cases/reviews/epochs/0/rounds/0.json",
        "qa/cases/reviewed-case.json",
    ):
        (write_root / relative).unlink()
    executed = await checked_finalize(
        AGENT_JOB_CONTRACTS["case-review"],
        write_root,
        lambda: _finalize_files(
            cast(TaskHandler, case_review_finalize),
            review_document,
            project,
            list(case_review_outputs("CH-DEMO-001")),
            change_id="CH-DEMO-001",
            case_delta_paths=[case_relative],
            preparation_refs=bound_preparation,
            case_refs=case_refs,
            write_root=write_root,
        ),
    )
    assert executed.status == "succeeded", executed.failure
    selection_path = write_root / "qa/results/cases/epochs/0/selection.json"
    selection = json.loads(selection_path.read_bytes())
    assert selection["cases"][0]["case_id"] == "TC_MENU_001"
    reviewed = json.loads((write_root / "qa/cases/reviewed-case.json").read_bytes())
    assert reviewed["selection_ref"]["digest"] == hashlib.sha256(selection_path.read_bytes()).hexdigest()
    assert reviewed["review_ref"]["digest"] == hashlib.sha256(review_path.read_bytes()).hexdigest()
    assert (
        json.loads((write_root / "qa/cases/reviews/epochs/0/rounds/0.json").read_bytes())["outcome"] == "pass"
    )


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
                    "locator": {"artifact": "qa/results/trace/minimum-coverage-matrix.json"},
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
        cast(TaskHandler, case_review_finalize),
        cast(JSONValue, document),
        tmp_path,
        [],
        change_id="CH-DEMO-001",
        case_delta_paths=["qa/cases/menus/case.yaml"],
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
    document = json.loads((tmp_path / "qa/results/review/case-review.json").read_text(encoding="utf-8"))
    document["findings"][0]["locator"]["key"] = None

    executed = await _finalize_files(
        cast(TaskHandler, case_review_finalize),
        cast(JSONValue, document),
        tmp_path,
        [],
        change_id="CH-DEMO-001",
        case_delta_paths=["qa/cases/menus/case.yaml"],
    )

    assert executed.status == "failed"
    assert executed.failure is not None
    assert "exact locator key" in executed.failure.message


@pytest.mark.asyncio
async def test_case_review_finalize_rejects_case_id_as_a_mutable_field_locator(
    tmp_path: Path,
) -> None:
    _write_review_matrix(tmp_path, missing=[])
    _write_fixable_case_review(tmp_path, allowed_key="case_id")
    document = json.loads((tmp_path / "qa/results/review/case-review.json").read_text(encoding="utf-8"))

    outcome = await _finalize_review_with_written_cases(tmp_path, cast(JSONValue, document))

    assert outcome.status == "failed"
    assert outcome.failure is not None
    assert "case_id identifies the case and cannot be an automatic repair field" in (outcome.failure.message)


@pytest.mark.asyncio
async def test_case_review_finalize_replaces_projection_drift_from_authenticated_matrix(
    tmp_path: Path,
) -> None:
    _write_review_matrix(tmp_path, missing=["skipped_item"])
    outcome = await _finalize_review_with_written_cases(
        tmp_path, _case_review_document(missing=["entities.item"])
    )
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
    relative = "qa/results/explore/exploration.json"
    path = project / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_valid_explore_advisory()), encoding="utf-8")
    executed = await _finalize_files(
        cast(TaskHandler, explore_finalize),
        {"output_files": [relative]},
        project,
        [],
        change_id="CH-DEMO-001",
        write_root=write_root,
    )
    assert executed.status == "failed"
    assert executed.failure is not None
    assert executed.failure.kind == "invalid_input"
    assert executed.failure.retryable is True


@pytest.mark.asyncio
async def test_explore_finalize_rejects_path_escape(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (tmp_path / "secret.json").write_bytes(b'{"secret":true}')
    executed = await _finalize_files(
        cast(TaskHandler, explore_finalize),
        {"output_files": ["../secret.json"]},
        workspace,
        ["qa/results/explore/exploration.json"],
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
    canonical = project / "qa/proposal.md"
    canonical.parent.mkdir(parents=True, exist_ok=True)
    original = b"# Canonical proposal\n"
    canonical.write_bytes(original)
    _write_case_design_outputs(write_root, authored)
    executed = await _finalize_files(
        cast(TaskHandler, case_design_finalize),
        cast(JSONValue, authored),
        project,
        [
            "qa/.qa.yaml",
            "qa/cases",
            "qa/fixtures",
            "qa/proposal.md",
            "qa/requirement.md",
            "qa/results",
            "qa/tests",
        ],
        change_id="CH-DEMO-001",
        selected_test_families=["api"],
        write_root=write_root,
    )
    assert executed.status == "failed"
    assert executed.failure is not None
    assert executed.failure.kind == "invalid_output"
    assert canonical.read_bytes() == original


def _case_impact_row(
    row_id: str = "IR-001",
    *,
    disposition: str = "add",
    gap_reason: str | None = None,
    case_module: str | None = None,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "row_id": row_id,
        "change_evidence_ids": ["CF-001"],
        "affected_behavior": {"kind": "api", "key": "POST /items"},
        "obligation": "duplicate item names must be rejected",
        "expected_basis_ids": [],
        "assets": {"case_ids": [], "factory_leafs": [], "problem_ids": []},
        "disposition": disposition,
        "gap_reason": gap_reason,
        "confidence": "medium",
    }
    if case_module is not None:
        row["case_module"] = case_module
    return row


@pytest.mark.asyncio
async def test_case_design_prepare_embeds_the_frozen_inventory(tmp_path: Path) -> None:
    prepared = await run_prepare(
        cast(TaskHandler, case_design_prepare),
        CASE_INPUT,
        BINDING,
        tmp_path,
        impact_rows=(_case_impact_row(),),
    )
    assert prepared.status == "succeeded"
    request = AgentRunRequest.model_validate(prepared.output)
    business = cast(Mapping[str, object], request.instructions[1].json_content)
    inventory = cast(Mapping[str, object], business["impact_inventory"])
    rows = cast(list[Mapping[str, object]], inventory["rows"])
    assert rows[0]["row_id"] == "IR-001"
    assert rows[0]["disposition"] == "add"


@pytest.mark.asyncio
async def test_case_design_prepare_infers_case_paths_from_inventory(tmp_path: Path) -> None:
    payload = {**CASE_INPUT, "case_delta_paths": []}
    prepared = await run_prepare(
        cast(TaskHandler, case_design_prepare),
        payload,
        BINDING,
        tmp_path,
        impact_rows=(_case_impact_row(case_module="system/dept"),),
    )
    assert prepared.status == "succeeded"
    request = AgentRunRequest.model_validate(prepared.output)
    business = cast(Mapping[str, object], request.instructions[1].json_content)
    assert business["case_delta_paths"] == ("qa/cases/system/dept/case.yaml",)
    assert request.workspace.allowed_outputs == (
        "qa/.qa.yaml",
        "qa/cases/system/dept/case.yaml",
        "qa/proposal.md",
        "qa/results/trace/minimum-coverage-matrix.json",
    )


@pytest.mark.asyncio
async def test_case_design_prepare_locks_every_inferred_module(tmp_path: Path) -> None:
    payload = {**CASE_INPUT, "case_delta_paths": ["qa/cases/menus/case.yaml"]}
    prepared = await run_prepare(
        cast(TaskHandler, case_design_prepare),
        payload,
        BINDING,
        tmp_path,
        impact_rows=(
            _case_impact_row(case_module="system/dept"),
            _case_impact_row(row_id="IR-002", case_module="system/user"),
        ),
    )
    assert prepared.status == "succeeded"
    request = AgentRunRequest.model_validate(prepared.output)
    business = cast(Mapping[str, object], request.instructions[1].json_content)
    assert business["case_delta_paths"] == (
        "qa/cases/system/dept/case.yaml",
        "qa/cases/system/user/case.yaml",
    )
    assert "qa/cases/menus/case.yaml" not in request.workspace.allowed_outputs
    assert "qa/cases/system/dept/case.yaml" in request.workspace.allowed_outputs
    assert "qa/cases/system/user/case.yaml" in request.workspace.allowed_outputs


@pytest.mark.asyncio
async def test_case_design_prepare_derives_module_when_explore_omits_case_module(
    tmp_path: Path,
) -> None:
    payload = {**CASE_INPUT, "case_delta_paths": []}
    prepared = await run_prepare(
        cast(TaskHandler, case_design_prepare),
        payload,
        BINDING,
        tmp_path,
        impact_rows=(_case_impact_row(),),
    )
    assert prepared.status == "succeeded"
    request = AgentRunRequest.model_validate(prepared.output)
    assert "qa/cases/items/case.yaml" in request.workspace.allowed_outputs


@pytest.mark.asyncio
async def test_case_design_prepare_rejects_empty_lock_without_inferred_module(
    tmp_path: Path,
) -> None:
    payload = {**CASE_INPUT, "case_delta_paths": []}
    prepared = await run_prepare(cast(TaskHandler, case_design_prepare), payload, BINDING, tmp_path)
    assert prepared.status == "failed"
    assert prepared.failure is not None
    assert prepared.failure.kind == "invalid_input"
    assert "does not imply any case module" in prepared.failure.message


@pytest.mark.asyncio
async def test_case_design_finalize_locks_inferred_case_module(tmp_path: Path) -> None:
    project, write_root = dual_roots(tmp_path)
    authored = yaml.safe_load((_FIXTURES / "case-authoring-valid.yaml").read_text(encoding="utf-8"))
    authored["added"][0]["impact_rows"] = ["IR-001"]
    outputs = _write_case_design_outputs(write_root, authored)
    inferred = "qa/cases/system/dept/case.yaml"
    source = write_root / "qa/cases/menus/case.yaml"
    dest = write_root / inferred
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(source.read_bytes())
    source.unlink()
    outputs = [inferred if path.endswith("/case.yaml") else path for path in outputs]
    executed = await _finalize_files(
        cast(TaskHandler, case_design_finalize),
        cast(JSONValue, {"output_files": outputs}),
        project,
        [
            "qa/.qa.yaml",
            "qa/cases",
            "qa/fixtures",
            "qa/proposal.md",
            "qa/requirement.md",
            "qa/results",
            "qa/tests",
        ],
        change_id="CH-DEMO-001",
        selected_test_families=["api"],
        write_root=write_root,
        case_delta_paths=[],
        impact_rows=(_case_impact_row(case_module="system/dept"),),
    )
    assert executed.status == "succeeded", executed.failure
    assert [artifact["path"] for artifact in executed.output["artifacts"]] == sorted(outputs)


@pytest.mark.asyncio
async def test_case_design_finalize_rejects_uncovered_add_rows(tmp_path: Path) -> None:
    project, write_root = dual_roots(tmp_path)
    authored = yaml.safe_load((_FIXTURES / "case-authoring-valid.yaml").read_text(encoding="utf-8"))
    outputs = _write_case_design_outputs(write_root, authored)
    executed = await _finalize_files(
        cast(TaskHandler, case_design_finalize),
        cast(JSONValue, {"output_files": outputs}),
        project,
        [
            "qa/.qa.yaml",
            "qa/cases",
            "qa/fixtures",
            "qa/proposal.md",
            "qa/requirement.md",
            "qa/results",
            "qa/tests",
        ],
        change_id="CH-DEMO-001",
        selected_test_families=["api"],
        write_root=write_root,
        impact_rows=(_case_impact_row(case_module="menus"),),
    )
    assert executed.status == "failed"
    assert executed.failure is not None
    assert executed.failure.kind == "invalid_output"
    assert "add/modify inventory rows have no covering case: ['IR-001']" in executed.failure.message


@pytest.mark.asyncio
async def test_case_design_finalize_accepts_a_covering_added_case(tmp_path: Path) -> None:
    project, write_root = dual_roots(tmp_path)
    authored = yaml.safe_load((_FIXTURES / "case-authoring-valid.yaml").read_text(encoding="utf-8"))
    authored["added"][0]["impact_rows"] = ["IR-001"]
    outputs = _write_case_design_outputs(write_root, authored)
    executed = await _finalize_files(
        cast(TaskHandler, case_design_finalize),
        cast(JSONValue, {"output_files": outputs}),
        project,
        [
            "qa/.qa.yaml",
            "qa/cases",
            "qa/fixtures",
            "qa/proposal.md",
            "qa/requirement.md",
            "qa/results",
            "qa/tests",
        ],
        change_id="CH-DEMO-001",
        selected_test_families=["api"],
        write_root=write_root,
        impact_rows=(_case_impact_row(case_module="menus"),),
    )
    assert executed.status == "succeeded", executed.failure


@pytest.mark.asyncio
async def test_case_design_finalize_rejects_add_row_cited_from_modified(tmp_path: Path) -> None:
    project, write_root = dual_roots(tmp_path)
    authored = yaml.safe_load((_FIXTURES / "case-authoring-valid.yaml").read_text(encoding="utf-8"))
    added = authored.pop("added")
    authored["added"] = []
    authored["modified"] = added
    authored["modified"][0]["impact_rows"] = ["IR-001"]
    outputs = _write_case_design_outputs(write_root, authored)
    executed = await _finalize_files(
        cast(TaskHandler, case_design_finalize),
        cast(JSONValue, {"output_files": outputs}),
        project,
        [
            "qa/.qa.yaml",
            "qa/cases",
            "qa/fixtures",
            "qa/proposal.md",
            "qa/requirement.md",
            "qa/results",
            "qa/tests",
        ],
        change_id="CH-DEMO-001",
        selected_test_families=["api"],
        write_root=write_root,
        impact_rows=(_case_impact_row(case_module="menus"),),
    )
    assert executed.status == "failed"
    assert executed.failure is not None
    assert "inventory row cited in the wrong section" in executed.failure.message


@pytest.mark.asyncio
async def test_case_design_finalize_does_not_require_coverage_for_open_rows(tmp_path: Path) -> None:
    project, write_root = dual_roots(tmp_path)
    authored = yaml.safe_load((_FIXTURES / "case-authoring-valid.yaml").read_text(encoding="utf-8"))
    outputs = _write_case_design_outputs(write_root, authored)
    executed = await _finalize_files(
        cast(TaskHandler, case_design_finalize),
        cast(JSONValue, {"output_files": outputs}),
        project,
        [
            "qa/.qa.yaml",
            "qa/cases",
            "qa/fixtures",
            "qa/proposal.md",
            "qa/requirement.md",
            "qa/results",
            "qa/tests",
        ],
        change_id="CH-DEMO-001",
        selected_test_families=["api"],
        write_root=write_root,
        impact_rows=(_case_impact_row(disposition="capability_gap", gap_reason="no factory yet"),),
    )
    assert executed.status == "succeeded", executed.failure
