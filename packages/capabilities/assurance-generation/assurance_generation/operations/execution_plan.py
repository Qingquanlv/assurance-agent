"""Compile and authenticate committed case execution plan artifacts."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import cast


from graph_engine.canonical import JSONValue, canonical_json_bytes

from assurance_generation.contracts.compiler import (
    PlanNotReady,
    compile_case_plan,
)
from assurance_generation.contracts.execution_plan import (
    CaseExecutionPlanSetV1,
    CasePlanContextV1,
    ExecutionBindingsV1,
)
from assurance_intake.contracts.verification import AssertionSourcesV1
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1
from assurance_intake.contracts.plan import decode_plan, ResolvedAssurancePlan
from assurance_intake.contracts.reviewed_case import authenticated_file, authenticate_reviewed_case
from assurance_generation.contracts.agent import PlanInputV1, AgentFinalizeInputV1
from assurance_generation.contracts.execution_plan import ValidationProfile
from assurance_generation.contracts.admission import (
    _assertion_sources,
    _reviewed_documents,
    _authenticate_review_projection,
    _regular_file,
)


def frozen_validation_profile(
    project_root: Path, payload: PlanInputV1 | AgentFinalizeInputV1
) -> ValidationProfile | None:
    """The frozen root plan, never omission of caller fields, selects verification."""
    path = project_root / payload.plan_ref.path
    if (
        not path.exists()
        and payload.reviewed_case is None
        and payload.validation_profile is None
        and payload.assertion_sources is None
        and not payload.source_artifacts
    ):
        return None
    try:
        root = decode_plan(authenticated_file(project_root, payload.plan_ref).read_bytes(), payload.plan_ref)
        if root.plan_digest != payload.plan_digest or (
            payload.change_id is not None and root.change_id != payload.change_id
        ):
            raise ValueError("root plan identity differs from generation input")
        profile = root.verification_policy.validation_profile if root.verification_policy else None
        if payload.validation_profile is not None and payload.validation_profile != profile:
            raise ValueError("validation profile differs from the frozen root plan")
        return profile
    except (OSError, ValueError) as error:
        raise PlanNotReady(str(error)) from error


def authenticated_plan_specification(
    project_root: Path, payload: PlanInputV1 | AgentFinalizeInputV1
) -> tuple[ResolvedAssurancePlan, dict[str, dict[str, object]], dict[str, AssertionSourcesV1], str]:
    """Reuse the same specification authentication as downstream admission."""
    try:
        profile = frozen_validation_profile(project_root, payload)
        if profile is None or payload.reviewed_case is None:
            raise ValueError("verified planning requires ReviewedCase and assertion sources")
        reviewed = authenticate_reviewed_case(
            payload.reviewed_case,
            project_root,
            change_id=payload.reviewed_case.change_id,
            coverage_epoch=payload.coverage_epoch,
            require_acceptance_record=True,
        )
        root = decode_plan(authenticated_file(project_root, payload.plan_ref).read_bytes(), payload.plan_ref)
        cases, _ = _reviewed_documents(project_root, reviewed, capability_leafs=payload.capability_leafs)
        sources = _assertion_sources(project_root, reviewed=reviewed, root_plan=root, cases=cases)
        if len(cases) != 1:
            raise ValueError("single-case execution plans require exactly one authenticated case")
        if payload.assertion_sources is not None and payload.assertion_sources not in sources.values():
            raise ValueError("assertion sources differ from authenticated sources")
        return root, cases, sources, _authenticate_review_projection(project_root, reviewed)
    except (OSError, ValueError) as error:
        raise PlanNotReady(str(error)) from error


def _compile_candidate(
    *, project_root: Path, bindings_data: bytes, payload: PlanInputV1 | AgentFinalizeInputV1
) -> CaseExecutionPlanSetV1:
    root, cases, sources, sut_digest = authenticated_plan_specification(project_root, payload)
    assert root.verification_policy is not None and payload.reviewed_case is not None
    try:
        bindings = ExecutionBindingsV1.model_validate_json(bindings_data)
        context = CasePlanContextV1(
            change_id=root.change_id,
            coverage_epoch=payload.coverage_epoch,
            plan_digest=root.plan_digest,
            plan_ref=payload.plan_ref,
            reviewed_case=payload.reviewed_case,
            verification_policy_digest=root.verification_policy.digest,
            technical_config_digest=hashlib.sha256(bindings_data).hexdigest(),
            sut_digest=sut_digest,
        )
        plan = compile_case_plan(
            cases[bindings.case_id],
            sources[bindings.case_id],
            cast(dict[str, object], bindings.model_dump(mode="python")["bindings"]),
            root.verification_policy.validation_profile,
            context=context,
        )
    except (KeyError, ValueError) as error:
        raise PlanNotReady(f"execution bindings cannot compile: {error}") from error
    return CaseExecutionPlanSetV1(change_id=root.change_id, cases=(plan,))


def compile_case_plan_artifact(
    *,
    project_root: Path,
    write_root: Path,
    bindings_data: bytes,
    output_path: str,
    payload: AgentFinalizeInputV1,
) -> tuple[CaseExecutionPlanSetV1, EvidenceArtifactRefV1]:
    """Hash and compile the exact authenticated candidate image from this attempt."""
    plan_set = _compile_candidate(project_root=project_root, bindings_data=bindings_data, payload=payload)
    data = canonical_json_bytes(cast(JSONValue, plan_set.model_dump(mode="json"))) + b"\n"
    destination = write_root.joinpath(*output_path.split("/"))
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(data)
    return plan_set, EvidenceArtifactRefV1(path=output_path, digest=hashlib.sha256(data).hexdigest())


def validate_case_plan_artifact(
    *, project_root: Path, artifact_ref: EvidenceArtifactRefV1, payload: AgentFinalizeInputV1
) -> CaseExecutionPlanSetV1:
    """Reauthenticate committed inputs and compare exact deterministic compiler output."""
    expected_path = f"qa/changes/{payload.change_id}/plans/api-case-execution-plan.json"
    if artifact_ref.path != expected_path:
        raise PlanNotReady("case execution plan ref does not match authenticated context")
    try:
        data = _regular_file(project_root, artifact_ref.path).read_bytes()
        if hashlib.sha256(data).hexdigest() != artifact_ref.digest:
            raise PlanNotReady("case execution plan artifact digest does not match")
        candidate = f"qa/changes/{payload.change_id}/plans/api-execution-bindings.json"
        bindings_data = _regular_file(project_root, candidate).read_bytes()
        expected = _compile_candidate(project_root=project_root, bindings_data=bindings_data, payload=payload)
        expected_data = canonical_json_bytes(cast(JSONValue, expected.model_dump(mode="json"))) + b"\n"
        if data != expected_data:
            raise PlanNotReady("case execution plan does not match deterministic compiler output")
        return expected
    except (OSError, ValueError) as error:
        raise PlanNotReady(str(error)) from error


__all__ = ["PlanNotReady", "compile_case_plan", "compile_case_plan_artifact", "validate_case_plan_artifact"]
