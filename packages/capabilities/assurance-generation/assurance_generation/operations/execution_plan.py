"""Compile authenticated business assertions into a closed execution plan."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, cast

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic import field_validator

from graph_engine.canonical import JSONValue, canonical_digest, canonical_json_bytes

from assurance_generation.contracts.execution_plan import (
    ACTUAL_BINDING_ADAPTER,
    TRACE_OBLIGATIONS,
    ActualBindingV1,
    CaseExecutionPlanV1,
    CaseExecutionPlanSetV1,
    CasePlanContextV1,
    CompletionV1,
    ExecutionBindingsV1,
    HttpActionBindingV1,
    HttpActionV1,
    InitialStateV1,
    InputKey,
    ObligationBindingV1,
    SqliteUserOracleV1,
    TraceNotRequiredV1,
    TraceRequirementsV1,
    ValidationProfile,
    required_obligations,
    validate_case_plan_sources,
    validate_user_assertions,
)
from assurance_intake.contracts.verification import AssertionSourcesV1, BusinessAssertionV1
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1


class PlanNotReady(ValueError):
    """The formal execution plan cannot be closed before starting an action."""


class _CaseV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str
    case_id: str = Field(min_length=1)
    revision: str = Field(min_length=1)
    spec_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    inputs: dict[InputKey, Any]
    assertions: tuple[BusinessAssertionV1, ...] = Field(min_length=1)

    @field_validator("inputs")
    @classmethod
    def _complete_inputs(cls, value: dict[InputKey, Any]) -> dict[InputKey, Any]:
        expected = {"username", "email", "is_active", "is_superuser", "dept_id"}
        if set(value) != expected:
            raise ValueError("User inputs must be complete")
        return value


def _case_payload(document: object, case_id: str) -> dict[str, object]:
    if not isinstance(document, dict):
        raise PlanNotReady("case document must be a mapping")
    if document.get("case_id") == case_id:
        return cast(dict[str, object], document)
    candidates: list[object] = []
    for section in ("added", "modified"):
        value = document.get(section, [])
        if isinstance(value, list):
            candidates.extend(value)
    matches = [item for item in candidates if isinstance(item, dict) and item.get("case_id") == case_id]
    if len(matches) != 1:
        raise PlanNotReady(f"case document must contain {case_id} exactly once")
    return cast(dict[str, object], matches[0])


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
    return _case_payload(document, case_id)


def _assert_context(plan: CaseExecutionPlanV1, context: CasePlanContextV1) -> None:
    if (
        plan.change_id != context.change_id
        or plan.coverage_epoch != context.coverage_epoch
        or plan.plan_digest != context.plan_digest
        or plan.plan_ref != context.plan_ref
        or plan.reviewed_case != context.reviewed_case
        or plan.verification_policy_digest != context.verification_policy_digest
        or plan.technical_config_digest != context.technical_config_digest
        or plan.sut_digest != context.sut_digest
    ):
        raise PlanNotReady("case execution plan identity does not match authenticated context")


_EXPECTED_LOCATORS: dict[str, tuple[str, str | None]] = {
    "api.http_status": ("http_status", None),
    "api.code": ("http_json_field", "code"),
    "user.row_count": ("sqlite_user_row_count", None),
    "user.username": ("sqlite_user_field", "username"),
    "user.email": ("sqlite_user_field", "email"),
    "user.is_active": ("sqlite_user_field", "is_active"),
    "user.is_superuser": ("sqlite_user_field", "is_superuser"),
    "user.dept_id": ("sqlite_user_field", "dept_id"),
}
_RUNTIME_KINDS = {
    "action.finished": "http_action",
    "initial.user_absent": "sqlite_user_absence",
    "oracle.executed": "sqlite_user_observation",
    "trace.http": "trace_http",
    "trace.user_write": "trace_user_write",
    "trace.user_completed": "trace_checkpoint",
    "trace.drained": "trace_drain",
}


def _parse_bindings(raw: dict[str, object], *, required: frozenset[str]) -> dict[str, ActualBindingV1]:
    missing = sorted(required - raw.keys())
    if missing:
        raise PlanNotReady(f"missing required obligation binding: {missing[0]}")
    allowed = required | TRACE_OBLIGATIONS
    unknown = sorted(raw.keys() - allowed)
    if unknown:
        raise PlanNotReady(f"unknown obligation binding: {unknown[0]}")
    parsed: dict[str, ActualBindingV1] = {}
    for obligation_id in sorted(required):
        try:
            binding = ACTUAL_BINDING_ADAPTER.validate_python(raw[obligation_id])
        except ValidationError as error:
            raise PlanNotReady(f"invalid binding for {obligation_id}: {error}") from error
        locator = _EXPECTED_LOCATORS.get(obligation_id)
        expected_kind, expected_field = (
            locator if locator is not None else (_RUNTIME_KINDS[obligation_id], None)
        )
        if binding.kind != expected_kind:
            raise PlanNotReady(
                f"binding for {obligation_id} uses {binding.kind!r}, expected {expected_kind!r}"
            )
        if expected_field is not None and getattr(binding, "field", None) != expected_field:
            raise PlanNotReady(f"binding for {obligation_id} has the wrong actual field")
        parsed[obligation_id] = binding
    return parsed


def compile_case_plan(
    case: dict[str, object],
    sources: AssertionSourcesV1,
    bindings: dict[str, object],
    validation_profile: str,
    *,
    context: CasePlanContextV1,
) -> CaseExecutionPlanV1:
    try:
        parsed = _CaseV1.model_validate(case)
        assertion_ids = tuple(assertion.assertion_id for assertion in parsed.assertions)
        validate_user_assertions(parsed.assertions)
        required_set = required_obligations(frozenset(assertion_ids), validation_profile)
    except (ValidationError, ValueError) as error:
        raise PlanNotReady(str(error)) from error
    actuals = _parse_bindings(bindings, required=required_set)
    if len(context.reviewed_case.case_refs) != 1:
        raise PlanNotReady("single-case execution plans require exactly one authenticated case_ref")
    action_binding = actuals["action.finished"]
    if not isinstance(action_binding, HttpActionBindingV1):
        raise PlanNotReady("action.finished must use the installed HTTP action binding")
    required = tuple(sorted(required_set))
    assertion_by_id = {assertion.assertion_id: assertion for assertion in parsed.assertions}
    obligation_bindings = tuple(
        ObligationBindingV1(
            obligation_id=obligation_id,
            actual=actuals[obligation_id],
            expected_id=obligation_id if obligation_id in assertion_by_id else None,
            comparator=(
                assertion_by_id[obligation_id].comparator if obligation_id in assertion_by_id else None
            ),
        )
        for obligation_id in required
    )
    trace = (
        TraceRequirementsV1(
            status="required",
            http_obligation="trace.http",
            user_write_obligation="trace.user_write",
            checkpoint_obligation="trace.user_completed",
            checkpoint_id="user.create.completed",
            checkpoint_version="1",
            drain_obligation="trace.drained",
            require_same_action_and_sut=True,
        )
        if validation_profile == "api_db_trace.v1"
        else TraceNotRequiredV1(status="not_required")
    )
    assertions = tuple(sorted(parsed.assertions, key=lambda item: item.assertion_id))
    try:
        plan = CaseExecutionPlanV1(
            change_id=context.change_id,
            case_id=parsed.case_id,
            revision=parsed.revision,
            coverage_epoch=context.coverage_epoch,
            plan_digest=context.plan_digest,
            plan_ref=context.plan_ref,
            reviewed_case=context.reviewed_case,
            case_ref=context.reviewed_case.case_refs[0],
            spec_digest=parsed.spec_digest,
            assertion_sources_digest=canonical_digest(cast(JSONValue, sources.model_dump(mode="json"))),
            verification_policy_digest=context.verification_policy_digest,
            technical_config_digest=context.technical_config_digest,
            sut_digest=context.sut_digest,
            validation_profile=cast(ValidationProfile, validation_profile),
            inputs=parsed.inputs,
            assertions=assertions,
            action=HttpActionV1(
                action_key=action_binding.action_key,
                binding_id=action_binding.binding_id,
                binding_version=action_binding.binding_version,
                method=action_binding.method,
                path=action_binding.path,
                request_fields=action_binding.request_fields,
                credential_ref=action_binding.credential_ref,
                response_obligations=("api.code", "api.http_status"),
            ),
            initial_state=InitialStateV1(
                obligation_id="initial.user_absent",
                oracle_key="created_user",
                lookup_inputs=("username", "email"),
                expected_row_count=0,
            ),
            oracle=SqliteUserOracleV1(
                oracle_key="created_user",
                binding_id="assurance.execution.sqlite.user.v1",
                binding_version="1",
                lookup_inputs=("username", "email"),
                fields=("username", "email", "is_active", "is_superuser", "dept_id"),
                assertion_obligations=(
                    "user.dept_id",
                    "user.email",
                    "user.is_active",
                    "user.is_superuser",
                    "user.row_count",
                    "user.username",
                ),
            ),
            trace=trace,
            completion=CompletionV1(
                action_semantics="synchronous",
                http_timeout_seconds=10,
                oracle_timeout_seconds=2,
                telemetry_timeout_seconds=10,
                missing_evidence="incomplete",
                obligations=required,
            ),
            required=required,
            bindings=obligation_bindings,
        )
        validate_case_plan_sources(
            plan,
            case_id=parsed.case_id,
            revision=parsed.revision,
            spec_digest=parsed.spec_digest,
            assertions=assertions,
            sources=sources,
        )
    except (ValidationError, ValueError) as error:
        raise PlanNotReady(str(error)) from error
    return plan


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
    candidate = write_root.joinpath(*bindings_path.split("/"))
    try:
        raw = json.loads(candidate.read_text(encoding="utf-8"))
        bindings = ExecutionBindingsV1.model_validate(raw)
    except (OSError, UnicodeError, json.JSONDecodeError, ValidationError) as error:
        raise PlanNotReady(f"execution bindings are invalid: {error}") from error
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
    for plan in plan_set.cases:
        _assert_context(plan, context)
        if plan.validation_profile != validation_profile:
            raise PlanNotReady("case execution plan validation profile does not match")
        case = _read_case(
            project_root.joinpath(*plan.case_ref.path.split("/")),
            case_id=plan.case_id,
            expected_digest=plan.case_ref.digest,
        )
        try:
            parsed = _CaseV1.model_validate(case)
            validate_case_plan_sources(
                plan,
                case_id=parsed.case_id,
                revision=parsed.revision,
                spec_digest=parsed.spec_digest,
                assertions=tuple(sorted(parsed.assertions, key=lambda item: item.assertion_id)),
                sources=sources,
            )
        except (ValidationError, ValueError) as error:
            raise PlanNotReady(str(error)) from error
    return plan_set


__all__ = [
    "PlanNotReady",
    "compile_case_plan",
    "compile_case_plan_artifact",
    "validate_case_plan_artifact",
]
