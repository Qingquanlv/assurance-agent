"""Contracts for the frozen intake assurance plan."""

from __future__ import annotations

from pathlib import PurePosixPath
import hashlib
import json
from typing import Literal, Self, cast

from pydantic import ConfigDict, Field, ValidationInfo, field_validator, model_validator
from pydantic_core import to_jsonable_python

from graph_engine.identifiers import IdentifierError, validate_qualified_id
from graph_engine.plugin_api import FrozenModel
from graph_engine.canonical import JSONValue, canonical_digest, canonical_json_bytes

from assurance_intake.contracts.common import TestFamily, validate_family_tuple
from assurance_intake.contracts.quality_goals import (
    PreparedQualityGoalV1,
    validate_resource_digests,
)
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1

_SHA256 = r"^[0-9a-f]{64}$"
_REASON_ORDER = {
    "accepted_proposal": 0,
    "fallback_all_candidates": 1,
    "required_retained": 2,
    "candidate_outside_allowed": 3,
    "proposed_outside_candidate": 4,
}


def _canonical_segment(value: str, label: str) -> str:
    posix = PurePosixPath(value)
    if posix.is_absolute() or len(posix.parts) != 1 or value in {"", ".", ".."}:
        raise ValueError(f"{label} must be a canonical path segment")
    return value


def _qualified_id(value: str, label: str) -> str:
    try:
        return validate_qualified_id(value)
    except IdentifierError as error:
        raise ValueError(f"{label} must be qualified") from error


class TestFamilyPolicyV1(FrozenModel):
    required: tuple[TestFamily, ...]
    allowed: tuple[TestFamily, ...] = Field(min_length=1)

    @field_validator("required", "allowed")
    @classmethod
    def _families(cls, value: tuple[TestFamily, ...]) -> tuple[TestFamily, ...]:
        return validate_family_tuple(value)

    @model_validator(mode="after")
    def _required_is_allowed(self) -> Self:
        if not set(self.required) <= set(self.allowed):
            raise ValueError("required test families must be allowed")
        return self


class PlanBudgetsV1(FrozenModel):
    review_rounds: int = Field(ge=0)
    coverage_rounds: int = Field(ge=0)
    healing_rounds: int = Field(ge=0)
    execution_retries: int = Field(ge=0)


class BusinessVerificationPolicyV1(FrozenModel):
    validation_profile: Literal["api_db.v1", "api_db_trace.v1"]
    resource_id: Literal["assurance.product.configuration.verification-policy"]
    digest: str = Field(pattern=_SHA256)


ResolutionReasonCode = Literal[
    "accepted_proposal",
    "fallback_all_candidates",
    "required_retained",
    "candidate_outside_allowed",
    "proposed_outside_candidate",
]
FallbackDetail = Literal["empty_proposal", "outside_candidate", "empty_after_policy"]


class ResolutionReasonV1(FrozenModel):
    reason_code: ResolutionReasonCode
    family: TestFamily | None = None
    detail: FallbackDetail | None = None
    summary: str = Field(min_length=1)

    @model_validator(mode="after")
    def _shape_matches_reason(self) -> Self:
        global_reason = self.reason_code in {"accepted_proposal", "fallback_all_candidates"}
        if global_reason and self.family is not None:
            raise ValueError("global resolution reason cannot name a family")
        if not global_reason and self.family is None:
            raise ValueError("family-specific resolution reason requires a family")
        if self.reason_code == "fallback_all_candidates":
            if self.detail is None:
                raise ValueError("fallback reason requires a detail")
        elif self.detail is not None:
            raise ValueError("only fallback reason can have a detail")
        return self


def resolution_reason_sort_key(reason: ResolutionReasonV1) -> tuple[int, int]:
    family_rank = -1 if reason.family is None else ("api", "e2e", "fuzz", "performance").index(reason.family)
    return _REASON_ORDER[reason.reason_code], family_rank


class ResolvedAssurancePlan(FrozenModel):
    schema_version: Literal["1", "2"] = "1"
    change_id: str = Field(min_length=1)
    requirement_digest: str = Field(pattern=_SHA256)
    gdt: Literal["in-execution"] = "in-execution"
    gpm: Literal["select"] = "select"
    candidate_test_families: tuple[TestFamily, ...]
    proposed_test_families: tuple[TestFamily, ...]
    selected_test_families: tuple[TestFamily, ...] = Field(min_length=1)
    quality_goal: PreparedQualityGoalV1
    resolved_budgets: PlanBudgetsV1
    policy_resource_id: str
    policy_digest: str = Field(pattern=_SHA256)
    exploration_ref: EvidenceArtifactRefV1
    plan_digest: str = Field(pattern=_SHA256)
    resolution_reasons: tuple[ResolutionReasonV1, ...]
    verification_policy: BusinessVerificationPolicyV1 | None = None

    @field_validator("change_id")
    @classmethod
    def _change_id(cls, value: str) -> str:
        return _canonical_segment(value, "change_id")

    @field_validator(
        "candidate_test_families",
        "proposed_test_families",
        "selected_test_families",
    )
    @classmethod
    def _families(cls, value: tuple[TestFamily, ...]) -> tuple[TestFamily, ...]:
        return validate_family_tuple(value)

    @field_validator("policy_resource_id")
    @classmethod
    def _policy_resource_id(cls, value: str) -> str:
        return _qualified_id(value, "policy_resource_id")

    @model_validator(mode="after")
    def _plan_invariants(self, info: ValidationInfo) -> Self:
        if (self.schema_version == "2") != (self.verification_policy is not None):
            raise ValueError("plan schema version 2 requires an explicit verification policy")
        if not set(self.selected_test_families) <= set(self.candidate_test_families):
            raise ValueError("selected_test_families must be candidates")
        if not set(self.quality_goal.required_test_families) <= set(self.selected_test_families):
            raise ValueError("selected_test_families must retain required goal families")
        expected_exploration = f"qa/changes/{self.change_id}/explore/exploration.json"
        if self.exploration_ref.path != expected_exploration:
            raise ValueError("exploration_ref must bind the current change")
        if self.quality_goal.obligations_ref != self.exploration_ref:
            raise ValueError("quality goal obligations must bind exploration_ref")
        ordered = tuple(
            sorted(
                self.resolution_reasons,
                key=resolution_reason_sort_key,
            )
        )
        if self.resolution_reasons != ordered or len(set(self.resolution_reasons)) != len(
            self.resolution_reasons
        ):
            raise ValueError("resolution_reasons must be sorted and unique")
        if isinstance(info.context, dict) and info.context.get("skip_plan_digest") is True:
            return self
        payload = self.model_dump(mode="json", exclude_none=True)
        payload.pop("plan_digest")
        expected_digest = canonical_digest(cast(JSONValue, payload))
        if self.plan_digest != expected_digest:
            raise ValueError(
                f"plan_digest does not match the plan projection: {self.plan_digest} != {expected_digest}"
            )
        return self


class _ResolvedAssurancePlanVersion(FrozenModel):
    """Published schema projection shared by immutable plan versions."""

    model_config = ConfigDict(extra="forbid", frozen=True, title="ResolvedAssurancePlan")

    change_id: str = Field(min_length=1)
    requirement_digest: str = Field(pattern=_SHA256)
    gdt: Literal["in-execution"] = "in-execution"
    gpm: Literal["select"] = "select"
    candidate_test_families: tuple[TestFamily, ...]
    proposed_test_families: tuple[TestFamily, ...]
    selected_test_families: tuple[TestFamily, ...] = Field(min_length=1)
    quality_goal: PreparedQualityGoalV1
    resolved_budgets: PlanBudgetsV1
    policy_resource_id: str
    policy_digest: str = Field(pattern=_SHA256)
    exploration_ref: EvidenceArtifactRefV1
    plan_digest: str = Field(pattern=_SHA256)
    resolution_reasons: tuple[ResolutionReasonV1, ...]


class ResolvedAssurancePlanV1(_ResolvedAssurancePlanVersion):
    schema_version: Literal["1"] = "1"


class ResolvedAssurancePlanV2(_ResolvedAssurancePlanVersion):
    schema_version: Literal["2"] = "2"
    verification_policy: BusinessVerificationPolicyV1


class ResolvePlanInputV1(FrozenModel):
    change_id: str = Field(min_length=1)
    requirement_digest: str = Field(pattern=_SHA256)
    candidate_test_families: tuple[TestFamily, ...] = Field(min_length=1)
    budgets: PlanBudgetsV1
    policy_resource_id: str
    policy_digest: str = Field(pattern=_SHA256)
    family_policy: TestFamilyPolicyV1
    exploration_ref: EvidenceArtifactRefV1
    source_resource_digests: tuple[tuple[str, str], ...]
    capability_leafs: tuple[str, ...]
    verification_policy: BusinessVerificationPolicyV1 | None = None

    @field_validator("candidate_test_families")
    @classmethod
    def _candidate_test_families(cls, value: tuple[TestFamily, ...]) -> tuple[TestFamily, ...]:
        return validate_family_tuple(value)

    @field_validator("policy_resource_id")
    @classmethod
    def _policy_resource_id(cls, value: str) -> str:
        return _qualified_id(value, "policy_resource_id")

    @field_validator("change_id")
    @classmethod
    def _change_id(cls, value: str) -> str:
        return _canonical_segment(value, "change_id")

    @field_validator("source_resource_digests")
    @classmethod
    def _source_resource_digests(cls, value: tuple[tuple[str, str], ...]) -> tuple[tuple[str, str], ...]:
        return validate_resource_digests(value)

    @field_validator("capability_leafs")
    @classmethod
    def _capability_leafs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))):
            raise ValueError("capability_leafs must be sorted and unique")
        return value

    @model_validator(mode="after")
    def _scope_is_possible(self) -> Self:
        if not set(self.family_policy.required) <= set(self.candidate_test_families):
            raise ValueError("policy required families must be candidates")
        if not set(self.candidate_test_families) & set(self.family_policy.allowed):
            raise ValueError("candidate and policy allowed families must intersect")
        expected = f"qa/changes/{self.change_id}/explore/exploration.json"
        if self.exploration_ref.path != expected:
            raise ValueError("exploration_ref must bind the current change")
        return self


class LoadPlanInputV1(FrozenModel):
    change_id: str = Field(min_length=1)
    requirement_digest: str = Field(pattern=_SHA256)
    resolved_plan_ref: EvidenceArtifactRefV1
    budgets: PlanBudgetsV1
    policy_resource_id: str
    policy_digest: str = Field(pattern=_SHA256)
    source_resource_digests: tuple[tuple[str, str], ...]
    capability_leafs: tuple[str, ...]
    verification_policy: BusinessVerificationPolicyV1 | None = None

    @field_validator("change_id")
    @classmethod
    def _change_id(cls, value: str) -> str:
        return _canonical_segment(value, "change_id")

    @field_validator("policy_resource_id")
    @classmethod
    def _policy_resource_id(cls, value: str) -> str:
        return _qualified_id(value, "policy_resource_id")

    @field_validator("source_resource_digests")
    @classmethod
    def _source_resource_digests(cls, value: tuple[tuple[str, str], ...]) -> tuple[tuple[str, str], ...]:
        return validate_resource_digests(value)

    @field_validator("capability_leafs")
    @classmethod
    def _capability_leafs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))):
            raise ValueError("capability_leafs must be sorted and unique")
        return value


class ResolvePlanOutputV1(FrozenModel):
    plan: ResolvedAssurancePlan
    plan_ref: EvidenceArtifactRefV1

    @model_validator(mode="after")
    def _ref_matches_plan(self) -> Self:
        if self.plan_ref != plan_artifact_ref(self.plan):
            raise ValueError("plan_ref does not match the resolved plan")
        return self


def seal_plan(payload: dict[str, object]) -> ResolvedAssurancePlan:
    if "plan_digest" in payload:
        raise ValueError("unsealed plan payload cannot supply plan_digest")
    normalized = {key: value for key, value in payload.items() if value is not None}
    unsealed = ResolvedAssurancePlan.model_validate(
        {**normalized, "plan_digest": "0" * 64},
        context={"skip_plan_digest": True},
    )
    projection = cast(
        JSONValue,
        to_jsonable_python(unsealed.model_dump(mode="json", exclude={"plan_digest"}, exclude_none=True)),
    )
    digest = canonical_digest(projection)
    return ResolvedAssurancePlan.model_validate({**normalized, "plan_digest": digest})


def plan_bytes(plan: ResolvedAssurancePlan) -> bytes:
    return canonical_json_bytes(cast(JSONValue, plan.model_dump(mode="json", exclude_none=True)))


def plan_artifact_ref(plan: ResolvedAssurancePlan) -> EvidenceArtifactRefV1:
    data = plan_bytes(plan)
    return EvidenceArtifactRefV1(
        path=(f"qa/changes/{plan.change_id}/plan/{plan.plan_digest}/resolved-assurance-plan.json"),
        digest=hashlib.sha256(data).hexdigest(),
    )


def decode_plan(data: bytes, ref: EvidenceArtifactRefV1) -> ResolvedAssurancePlan:
    try:
        payload = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("plan is not valid JSON") from error
    if not isinstance(payload, dict):
        raise ValueError("plan document must be an object")
    if canonical_json_bytes(cast(JSONValue, payload)) != data:
        raise ValueError("plan bytes must be canonical JSON")
    if hashlib.sha256(data).hexdigest() != ref.digest:
        raise ValueError("plan_ref digest does not match plan bytes")
    plan = ResolvedAssurancePlan.model_validate(payload)
    if plan_artifact_ref(plan) != ref:
        raise ValueError("plan_ref path does not match plan_digest")
    return plan


__all__ = [
    "BusinessVerificationPolicyV1",
    "FallbackDetail",
    "LoadPlanInputV1",
    "PlanBudgetsV1",
    "PreparedQualityGoalV1",
    "ResolutionReasonCode",
    "ResolutionReasonV1",
    "ResolvePlanInputV1",
    "ResolvePlanOutputV1",
    "ResolvedAssurancePlan",
    "ResolvedAssurancePlanV1",
    "ResolvedAssurancePlanV2",
    "TestFamilyPolicyV1",
    "decode_plan",
    "plan_artifact_ref",
    "plan_bytes",
    "seal_plan",
    "resolution_reason_sort_key",
]
