"""Compile and authenticate committed case execution plan artifacts."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import cast

import yaml
from pydantic import ValidationError

from graph_engine.canonical import JSONValue, canonical_json_bytes

from assurance_generation.contracts.compiler import (
    PlanNotReady,
    assert_case_plan_context,
    case_specification,
    compile_case_plan,
)
from assurance_generation.contracts.execution_plan import (
    CaseExecutionPlanSetV1,
    CasePlanContextV1,
    ExecutionBindingsV1,
)
from assurance_intake.contracts.verification import AssertionSourcesV1
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1


def _read_case(path: Path, *, case_id: str, expected_digest: str) -> dict[str, object]:
    try:
        data = path.read_bytes()
    except OSError as error:
        raise PlanNotReady(f"case input is unreadable: {path}") from error
    if hashlib.sha256(data).hexdigest() != expected_digest:
        raise PlanNotReady(f"case input digest does not match ReviewedCase: {path}")
    try:
        document = yaml.safe_load(data)
    except yaml.YAMLError as error:
        raise PlanNotReady(f"case input is invalid YAML: {path}") from error
    return case_specification(document, case_id)


def _read_bindings(root: Path, relative: str) -> ExecutionBindingsV1:
    path = root.joinpath(*relative.split("/"))
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return ExecutionBindingsV1.model_validate(raw)
    except (OSError, UnicodeError, json.JSONDecodeError, ValidationError) as error:
        raise PlanNotReady(f"execution bindings are invalid: {error}") from error


def compile_case_plan_artifact(
    *,
    project_root: Path,
    write_root: Path,
    bindings_path: str,
    output_path: str,
    sources: AssertionSourcesV1,
    validation_profile: str,
    context: CasePlanContextV1,
) -> tuple[CaseExecutionPlanSetV1, EvidenceArtifactRefV1]:
    """Compile one candidate locator document and write the formal canonical plan set."""
    bindings = _read_bindings(write_root, bindings_path)
    if bindings.case_id != sources.case_id:
        raise PlanNotReady("execution bindings case_id does not match assertion sources")
    case_ref = context.reviewed_case.case_refs[0]
    case = _read_case(
        project_root.joinpath(*case_ref.path.split("/")),
        case_id=bindings.case_id,
        expected_digest=case_ref.digest,
    )
    plan = compile_case_plan(
        case,
        sources,
        cast(dict[str, object], bindings.model_dump(mode="python")["bindings"]),
        validation_profile,
        context=context,
    )
    plan_set = CaseExecutionPlanSetV1(change_id=context.change_id, cases=(plan,))
    data = canonical_json_bytes(cast(JSONValue, plan_set.model_dump(mode="json"))) + b"\n"
    destination = write_root.joinpath(*output_path.split("/"))
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    return plan_set, EvidenceArtifactRefV1(path=output_path, digest=digest)


def validate_case_plan_artifact(
    *,
    project_root: Path,
    artifact_ref: EvidenceArtifactRefV1,
    sources: AssertionSourcesV1,
    validation_profile: str,
    context: CasePlanContextV1,
) -> CaseExecutionPlanSetV1:
    """Recheck formal bytes and their complete external specification binding."""
    expected_path = f"qa/changes/{context.change_id}/plans/api-case-execution-plan.json"
    if artifact_ref.path != expected_path:
        raise PlanNotReady("case execution plan ref does not match authenticated context")
    path = project_root.joinpath(*artifact_ref.path.split("/"))
    try:
        data = path.read_bytes()
    except OSError as error:
        raise PlanNotReady(f"case execution plan is unreadable: {artifact_ref.path}") from error
    if hashlib.sha256(data).hexdigest() != artifact_ref.digest:
        raise PlanNotReady("case execution plan artifact digest does not match")
    try:
        plan_set = CaseExecutionPlanSetV1.model_validate_json(data)
    except ValidationError as error:
        raise PlanNotReady(f"case execution plan closure is invalid: {error}") from error
    if plan_set.change_id != context.change_id:
        raise PlanNotReady("case execution plan change_id does not match authenticated context")
    if len(plan_set.cases) != 1:
        raise PlanNotReady("single-case execution plans require exactly one formal case")
    plan = plan_set.cases[0]
    assert_case_plan_context(plan, context)
    if plan.validation_profile != validation_profile:
        raise PlanNotReady("case execution plan validation profile does not match")
    candidate_path = f"qa/changes/{context.change_id}/plans/api-execution-bindings.json"
    bindings = _read_bindings(project_root, candidate_path)
    if bindings.case_id != sources.case_id:
        raise PlanNotReady("execution bindings case_id does not match assertion sources")
    if len(context.reviewed_case.case_refs) != 1:
        raise PlanNotReady("single-case execution plans require exactly one authenticated case_ref")
    case_ref = context.reviewed_case.case_refs[0]
    case = _read_case(
        project_root.joinpath(*case_ref.path.split("/")),
        case_id=bindings.case_id,
        expected_digest=case_ref.digest,
    )
    expected_plan = compile_case_plan(
        case,
        sources,
        cast(dict[str, object], bindings.model_dump(mode="python")["bindings"]),
        validation_profile,
        context=context,
    )
    expected_set = CaseExecutionPlanSetV1(change_id=context.change_id, cases=(expected_plan,))
    expected_data = canonical_json_bytes(cast(JSONValue, expected_set.model_dump(mode="json"))) + b"\n"
    if data != expected_data:
        raise PlanNotReady("case execution plan does not match deterministic compiler output")
    return plan_set


__all__ = [
    "PlanNotReady",
    "compile_case_plan",
    "compile_case_plan_artifact",
    "validate_case_plan_artifact",
]
