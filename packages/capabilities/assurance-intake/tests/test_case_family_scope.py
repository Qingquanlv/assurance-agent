from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any, Literal, cast

import pytest
import yaml
from pydantic import BaseModel

from agent_runtime_contracts import AgentRunResult
from graph_engine.canonical import JSONValue, canonical_digest, canonical_json_bytes
from graph_engine.plugin_api import TaskOutcome

from assurance_intake.contracts.plan import ResolvedAssurancePlan
from assurance_intake.contracts.cases import MinimumCoverageMatrixAuthoring
from assurance_intake.contracts.review import CaseReviewResultV1
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1
from assurance_intake.operations.prepare import case_review_outputs
from assurance_intake.operations.case_review_seal import (
    collect_selected_cases,
    expected_case_selection,
    expected_review_history,
    expected_reviewed_case,
)
from assurance_intake.agent_ops.case_design import finalize as case_design_finalize
from assurance_intake.agent_ops.case_review import finalize as case_review_finalize
from tests.acg_plan_fixture import install_plan
from tests.product.test_change_local_output_routing import dual_roots, execute_task


_CHANGE = "CH-DEMO-001"
_ROOT = "qa"
_CASE = "qa/cases/menus/case.yaml"
_MATRIX = "qa/results/trace/minimum-coverage-matrix.json"
_LEAFS = ("entities.item.create",)
_FIXTURE = Path(__file__).parent / "fixtures/case-authoring-valid.yaml"
_Phase = Literal["design", "review"]


def test_matrix_keeps_multiple_unresolved_mrc_rows_by_id() -> None:
    matrix = MinimumCoverageMatrixAuthoring.model_validate(
        [
            {
                "mrc_id": mrc_id,
                "key": None,
                "required": True,
                "covered_by_cases": [],
                "status": "skipped_by_scope",
                "skip_reason": "capability_unresolved",
                "category": "negative",
                "layer": "api",
            }
            for mrc_id in ("MRC-NEGATIVE-009", "MRC-NEGATIVE-012")
        ]
    )
    assert [row.mrc_id for row in matrix.root] == ["MRC-NEGATIVE-009", "MRC-NEGATIVE-012"]


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ("design", "review"))
@pytest.mark.parametrize("matrix_mode", ("valid", "dropped", "rebound"))
async def test_case_finalization_preserves_unresolved_row_by_mrc_id(
    tmp_path: Path, phase: _Phase, matrix_mode: str
) -> None:
    project, stage = dual_roots(tmp_path)
    drafts: list[Mapping[str, object]] = [
        {
            "draft_id": mrc_id,
            "proposed_key": key,
            "category": category,
            "layer": "api",
            "statement": "required behavior must be verified",
            "applicability_conditions": [],
            "impact_row_ids": [],
            "proposed_profile_id": None,
            "prerequisites": [],
            "observation_goals": [],
            "basis_quotes": [],
            "open_questions": [],
        }
        for mrc_id, key, category in (
            ("MRC-API-001", "create_menu", "api"),
            ("MRC-NEGATIVE-009", None, "negative"),
        )
    ]
    plan, plan_ref = install_plan(project, _CHANGE, capability_leafs=_LEAFS, minimum_required_coverage=drafts)
    outputs = _write_outputs(project, e2e_required=None)
    matrix_path = project / _MATRIX
    matrix = json.loads(matrix_path.read_bytes())
    if matrix_mode != "dropped":
        matrix.append(
            {
                "mrc_id": "MRC-NEGATIVE-009",
                "key": None if matrix_mode == "valid" else "entities.item.create",
                "required": True,
                "covered_by_cases": [],
                "status": "skipped_by_scope",
                "skip_reason": "capability_unresolved",
                "category": "negative",
                "layer": "api",
            }
        )
    matrix_path.write_text(json.dumps(matrix), encoding="utf-8")

    outcome = await _finalize(
        phase,
        project,
        stage,
        plan,
        plan_ref,
        outputs,
        input_refs=_refs(project, outputs),
        write_runtime_seal=phase == "review",
    )

    if matrix_mode != "valid":
        assert outcome.status == "failed"
        assert outcome.failure is not None
        assert "unresolved MRC" in outcome.failure.message
        return
    assert outcome.status == "succeeded", outcome.failure
    assert isinstance(outcome.output, dict)
    if phase == "review":
        assert outcome.output["reviewed_case"]


def _write_outputs(
    root: Path,
    *,
    e2e_required: bool | None,
    matrix_e2e_required: bool = True,
    include_e2e_matrix: bool = True,
    include_e2e_metadata: bool = True,
    case_status: Literal["active", "draft"] = "active",
) -> tuple[str, ...]:
    authored = yaml.safe_load(_FIXTURE.read_text(encoding="utf-8"))
    authored["added"][0]["status"] = case_status
    rows = [
        {
            "mrc_id": "MRC-API-001",
            "key": "create_menu",
            "required": True,
            "covered_by_cases": ["TC_MENU_001"],
            "status": "covered",
            "category": "api",
            "layer": "api",
        }
    ]
    if e2e_required is not None:
        e2e = deepcopy(authored["added"][0])
        e2e.update({"case_id": "TC_MENU_002", "type": "E2E"})
        e2e["risk"]["level"] = "low"
        e2e["automation"].update({"required": e2e_required, "framework": "pytest-playwright"})
        authored["added"].append(e2e)
        if include_e2e_matrix:
            e2e_row = {
                "mrc_id": "MRC-E2E-001",
                "key": "manage_menu",
                "required": matrix_e2e_required,
                "covered_by_cases": ["TC_MENU_002"],
                "status": "covered",
            }
            if include_e2e_metadata:
                e2e_row.update({"category": "e2e", "layer": "e2e"})
            rows.append(e2e_row)
    documents = {
        f"{_ROOT}/.qa.yaml": (_FIXTURE.parent / "qa-valid.yaml").read_text(encoding="utf-8"),
        f"{_ROOT}/proposal.md": "# Menu coverage\n",
        _CASE: yaml.safe_dump(authored),
        _MATRIX: json.dumps(rows),
    }
    for relative, data in documents.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data, encoding="utf-8")
    return tuple(sorted(documents))


def _refs(root: Path, paths: tuple[str, ...]) -> list[dict[str, str]]:
    return [
        {"path": relative, "digest": hashlib.sha256((root / relative).read_bytes()).hexdigest()}
        for relative in paths
    ]


def _write_json_model(root: Path, relative: str, model: BaseModel) -> None:
    path = root.joinpath(*relative.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json_bytes(model.model_dump(mode="json")) + b"\n")


def _write_review_seal(
    *,
    project: Path,
    stage: Path,
    document: dict[str, Any],
    plan: ResolvedAssurancePlan,
    plan_ref: dict[str, str],
    input_refs: list[dict[str, str]],
    coverage_epoch: int,
) -> None:
    review_relative = "qa/results/review/case-review.json"
    review_ref = EvidenceArtifactRefV1(
        path=review_relative,
        digest=hashlib.sha256((stage / review_relative).read_bytes()).hexdigest(),
    )
    case_refs = [EvidenceArtifactRefV1.model_validate(item) for item in input_refs if item["path"] == _CASE]
    if not case_refs:
        return
    source = yaml.safe_load((project / _CASE).read_bytes())
    if not isinstance(source, dict):
        return
    selected = collect_selected_cases(case_refs, [_CASE], [(case_refs[0], source)])
    selection = expected_case_selection(
        change_id=_CHANGE,
        coverage_epoch=coverage_epoch,
        plan=plan,
        cases=selected,
    )
    selection_relative = f"qa/results/cases/epochs/{coverage_epoch}/selection.json"
    _write_json_model(stage, selection_relative, selection)
    selection_ref = EvidenceArtifactRefV1(
        path=selection_relative,
        digest=hashlib.sha256((stage / selection_relative).read_bytes()).hexdigest(),
    )
    preparation_refs = [
        EvidenceArtifactRefV1.model_validate(item)
        for item in sorted(
            [plan_ref, *(ref for ref in input_refs if ref["path"] != _CASE)],
            key=lambda ref: ref["path"],
        )
    ]
    history = expected_review_history(
        change_id=_CHANGE,
        coverage_epoch=coverage_epoch,
        review_round=0,
        document=CaseReviewResultV1.model_validate(document),
        preparation_refs=preparation_refs,
        case_refs=case_refs,
        review_ref=review_ref,
    )
    _write_json_model(
        stage,
        f"qa/cases/reviews/epochs/{coverage_epoch}/rounds/0.json",
        history,
    )
    reviewed = expected_reviewed_case(
        change_id=_CHANGE,
        coverage_epoch=coverage_epoch,
        plan_digest=plan.plan_digest,
        plan_ref=EvidenceArtifactRefV1.model_validate(plan_ref),
        preparation_refs=preparation_refs,
        case_refs=case_refs,
        review_ref=review_ref,
        selection_ref=selection_ref,
    )
    _write_json_model(stage, "qa/cases/reviewed-case.json", reviewed)


async def _finalize(
    phase: _Phase,
    project: Path,
    stage: Path,
    plan: ResolvedAssurancePlan,
    plan_ref: dict[str, str],
    outputs: tuple[str, ...],
    *,
    coverage_epoch: int = 0,
    validation_attempt: int | None = None,
    input_refs: list[dict[str, str]] | None = None,
    write_runtime_seal: bool = False,
) -> TaskOutcome:
    if phase == "design":
        structured: dict[str, Any] = {"output_files": list(outputs)}
        artifacts = outputs
        selected = list(plan.selected_test_families)
        handler = case_design_finalize
    else:
        structured = {
            "schema_version": "1.0",
            "review_type": "case",
            "change_id": _CHANGE,
            "decision": "pass",
            "findings": [],
            "auto_fix_plan": [],
            "next_action": "continue",
            "auto_fix_allowed": False,
            "human_review_required": False,
            "risk_level": "low",
            "minimum_coverage": {
                "total_required": 1,
                "covered": 1,
                "skipped_by_scope": 0,
                "missing": [],
            },
            "source_verification": {
                "independent": True,
                "reviewed_source_files": ["src/menu.py"],
                "verified_claims": [{"claim": "menu creation persists", "evidence_files": ["src/menu.py"]}],
            },
        }
        artifacts = (
            "qa/results/review/case-review-summary.md",
            "qa/results/review/case-review.json",
        )
        artifacts = (
            case_review_outputs(_CHANGE, coverage_epoch=coverage_epoch)
            if write_runtime_seal
            else (
                "qa/results/review/case-review-summary.md",
                "qa/results/review/case-review.json",
            )
        )
        review_path = stage / "qa/results/review/case-review.json"
        review_path.parent.mkdir(parents=True, exist_ok=True)
        review_path.write_text(json.dumps(structured), encoding="utf-8")
        (stage / "qa/results/review/case-review-summary.md").write_text("# Review\n", encoding="utf-8")
        if write_runtime_seal:
            _write_review_seal(
                project=project,
                stage=stage,
                document=structured,
                plan=plan,
                plan_ref=plan_ref,
                input_refs=input_refs or [],
                coverage_epoch=coverage_epoch,
            )
        selected = []
        handler = case_review_finalize
    result = AgentRunResult(
        result_payload=cast(JSONValue, structured),
        result_digest=canonical_digest(cast(JSONValue, structured)),
        evidence_digest="a" * 64,
        adapter_id="test.agent",
        adapter_version="1.0.0",
    )
    request = {
        "agent_result": result.model_dump(mode="json"),
        "change_id": _CHANGE,
        "capability_leafs": list(_LEAFS),
        "artifact_paths": list(artifacts),
        "selected_test_families": selected,
        "case_delta_paths": [_CASE],
        "plan_digest": plan.plan_digest,
        "plan_ref": plan_ref,
        "coverage_epoch": coverage_epoch,
        "preparation_refs": sorted(
            [plan_ref, *(ref for ref in input_refs or [] if ref["path"] != _CASE)],
            key=lambda ref: ref["path"],
        ),
        "case_refs": [ref for ref in input_refs or [] if ref["path"] == _CASE],
    }
    if validation_attempt is not None:
        request["validation_attempt"] = validation_attempt
    executed = await execute_task(
        handler,
        cast(JSONValue, request),
        workspace=project,
        write_root=stage,
    )
    return executed.outcome


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["design", "review"])
@pytest.mark.parametrize("coverage_epoch", [0, 1])
async def test_finalize_blocks_required_case_outside_frozen_scope(
    tmp_path: Path,
    phase: _Phase,
    coverage_epoch: int,
) -> None:
    project, stage = dual_roots(tmp_path)
    plan, plan_ref = install_plan(project, _CHANGE, capability_leafs=_LEAFS)
    plan_path = project / plan_ref["path"]
    frozen = plan_path.read_bytes()
    source = stage if phase == "design" else project
    outputs = _write_outputs(source, e2e_required=True, matrix_e2e_required=False)
    inputs = {relative: (source / relative).read_bytes() for relative in outputs}

    outcome = await _finalize(
        phase,
        project,
        stage,
        plan,
        plan_ref,
        outputs,
        coverage_epoch=coverage_epoch,
        input_refs=_refs(source, outputs),
    )

    assert outcome.status == "failed"
    assert outcome.failure is not None
    assert outcome.failure.kind == "invalid_output"
    assert "family scope conflict" in outcome.failure.message
    assert "e2e" in outcome.failure.message
    assert "TC_MENU_002" in outcome.failure.message
    assert plan_path.read_bytes() == frozen
    assert {relative: (source / relative).read_bytes() for relative in outputs} == inputs
    assert not (stage / f"{_ROOT}/cases/reviewed-case.json").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["design", "review"])
async def test_finalize_blocks_required_matrix_for_optional_unselected_case(
    tmp_path: Path,
    phase: _Phase,
) -> None:
    project, stage = dual_roots(tmp_path)
    plan, plan_ref = install_plan(project, _CHANGE, capability_leafs=_LEAFS)
    source = stage if phase == "design" else project
    outputs = _write_outputs(source, e2e_required=False)

    outcome = await _finalize(
        phase, project, stage, plan, plan_ref, outputs, input_refs=_refs(source, outputs)
    )

    assert outcome.status == "failed"
    assert outcome.failure is not None
    assert "family scope conflict" in outcome.failure.message
    assert "e2e" in outcome.failure.message
    assert "MRC-E2E-001" in outcome.failure.message


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["design", "review"])
async def test_finalize_allows_optional_case_outside_frozen_scope(
    tmp_path: Path,
    phase: _Phase,
) -> None:
    project, stage = dual_roots(tmp_path)
    plan, plan_ref = install_plan(project, _CHANGE, capability_leafs=_LEAFS)
    source = stage if phase == "design" else project
    outputs = _write_outputs(source, e2e_required=False, matrix_e2e_required=False)

    outcome = await _finalize(
        phase,
        project,
        stage,
        plan,
        plan_ref,
        outputs,
        input_refs=_refs(source, outputs),
        write_runtime_seal=phase == "review",
    )

    assert outcome.status == "succeeded", outcome.failure
    assert isinstance(outcome.output, dict)
    assert outcome.output["validation_status" if phase == "design" else "decision"] == "pass"


@pytest.mark.asyncio
async def test_case_design_requests_repair_before_quality_when_e2e_case_has_no_journey_row(
    tmp_path: Path,
) -> None:
    project, stage = dual_roots(tmp_path)
    plan, plan_ref = install_plan(
        project,
        _CHANGE,
        capability_leafs=_LEAFS,
        journeys=("manage_menu",),
        candidates=("api", "e2e"),
        proposed=("api", "e2e"),
    )
    outputs = _write_outputs(
        stage,
        e2e_required=True,
        include_e2e_matrix=False,
    )

    outcome = await _finalize(
        "design",
        project,
        stage,
        plan,
        plan_ref,
        outputs,
        validation_attempt=0,
    )

    assert outcome.status == "succeeded", outcome.failure
    assert isinstance(outcome.output, dict)
    assert outcome.output["validation_status"] == "needs_fix"
    error = outcome.output["validation_error"]
    assert isinstance(error, str)
    assert "TC_MENU_002" in error
    assert "manage_menu" in error


@pytest.mark.asyncio
async def test_case_design_infers_e2e_mapping_from_authenticated_journey_key(
    tmp_path: Path,
) -> None:
    project, stage = dual_roots(tmp_path)
    plan, plan_ref = install_plan(
        project,
        _CHANGE,
        capability_leafs=_LEAFS,
        journeys=("manage_menu",),
        candidates=("api", "e2e"),
        proposed=("api", "e2e"),
    )
    outputs = _write_outputs(
        stage,
        e2e_required=True,
        include_e2e_metadata=False,
    )

    outcome = await _finalize("design", project, stage, plan, plan_ref, outputs)

    assert outcome.status == "succeeded", outcome.failure
    assert isinstance(outcome.output, dict)
    assert outcome.output["validation_status"] == "pass"


@pytest.mark.asyncio
async def test_case_design_reports_family_and_journey_errors_in_one_repair_attempt(
    tmp_path: Path,
) -> None:
    project, stage = dual_roots(tmp_path)
    plan, plan_ref = install_plan(
        project,
        _CHANGE,
        capability_leafs=_LEAFS,
        journeys=("manage_menu",),
        candidates=("api", "e2e"),
        proposed=("api", "e2e"),
    )
    outputs = _write_outputs(
        stage,
        e2e_required=True,
        include_e2e_matrix=False,
        case_status="draft",
    )

    outcome = await _finalize(
        "design",
        project,
        stage,
        plan,
        plan_ref,
        outputs,
        validation_attempt=0,
    )

    assert outcome.status == "succeeded", outcome.failure
    assert isinstance(outcome.output, dict)
    assert outcome.output["validation_status"] == "needs_fix"
    error = outcome.output["validation_error"]
    assert isinstance(error, str)
    assert "missing required automated cases" in error
    assert "api, e2e" in error
    assert "TC_MENU_002" in error
    assert "manage_menu" in error


@pytest.mark.asyncio
@pytest.mark.parametrize("changed_input", [_CASE, _MATRIX])
async def test_review_rejects_changed_inputs_that_hide_scope_conflicts(
    tmp_path: Path,
    changed_input: str,
) -> None:
    project, stage = dual_roots(tmp_path)
    plan, plan_ref = install_plan(project, _CHANGE, capability_leafs=_LEAFS)
    outputs = _write_outputs(project, e2e_required=True)
    refs = _refs(project, outputs)
    original = {relative: (project / relative).read_bytes() for relative in outputs}
    _write_outputs(project, e2e_required=None)
    for relative, data in original.items():
        if relative != changed_input:
            (project / relative).write_bytes(data)

    outcome = await _finalize("review", project, stage, plan, plan_ref, outputs, input_refs=refs)

    assert outcome.status == "failed"
    assert outcome.failure is not None
    assert "digest" in outcome.failure.message
    assert changed_input in outcome.failure.message
    assert not (stage / f"{_ROOT}/cases/reviewed-case.json").exists()
