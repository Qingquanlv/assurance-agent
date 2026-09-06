from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from typing import Any, Literal, cast

import pytest
import yaml

from agent_runtime_contracts import AgentRunResult
from graph_engine.canonical import JSONValue, canonical_digest
from graph_engine.plugin_api import TaskOutcome

from assurance_intake.contracts.plan import ResolvedAssurancePlan
from assurance_intake.operations.finalize import (
    CaseDesignFinalizeHandler,
    CaseReviewFinalizeHandler,
)
from tests.acg_plan_fixture import install_plan
from tests.product.test_change_local_output_routing import dual_roots, execute_task


_CHANGE = "CH-DEMO-001"
_ROOT = f"qa/changes/{_CHANGE}"
_CASE = f"{_ROOT}/cases/menus/case.yaml"
_MATRIX = f"{_ROOT}/trace/minimum-coverage-matrix.json"
_LEAFS = ("entities.item.create",)
_FIXTURE = Path(__file__).parent / "fixtures/case-authoring-valid.yaml"
_Phase = Literal["design", "review"]


def _write_outputs(
    root: Path,
    *,
    e2e_required: bool | None,
    matrix_e2e_required: bool = True,
) -> tuple[str, ...]:
    authored = yaml.safe_load(_FIXTURE.read_text(encoding="utf-8"))
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
        rows.append(
            {
                "mrc_id": "MRC-E2E-001",
                "key": "manage_menu",
                "required": matrix_e2e_required,
                "covered_by_cases": ["TC_MENU_002"],
                "status": "covered",
                "category": "e2e",
                "layer": "e2e",
            }
        )
    documents = {
        f"{_ROOT}/.qa.yaml": "approval:\n  mode: autonomous\n",
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


async def _finalize(
    phase: _Phase,
    project: Path,
    stage: Path,
    plan: ResolvedAssurancePlan,
    plan_ref: dict[str, str],
    outputs: tuple[str, ...],
    *,
    coverage_epoch: int = 0,
    input_refs: list[dict[str, str]] | None = None,
) -> TaskOutcome:
    if phase == "design":
        structured: dict[str, Any] = {"output_files": list(outputs)}
        artifacts = outputs
        selected = ["api"]
        handler = CaseDesignFinalizeHandler()
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
            f"{_ROOT}/review/case-review-summary.md",
            f"{_ROOT}/review/case-review.json",
        )
        review_path = stage / artifacts[1]
        review_path.parent.mkdir(parents=True, exist_ok=True)
        review_path.write_text(json.dumps(structured), encoding="utf-8")
        (stage / artifacts[0]).write_text("# Review\n", encoding="utf-8")
        selected = []
        handler = CaseReviewFinalizeHandler()
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
    outputs = _write_outputs(source, e2e_required=True)
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
        phase, project, stage, plan, plan_ref, outputs, input_refs=_refs(source, outputs)
    )

    assert outcome.status == "succeeded", outcome.failure
    assert isinstance(outcome.output, dict)
    assert outcome.output["validation_status" if phase == "design" else "decision"] == "pass"


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
