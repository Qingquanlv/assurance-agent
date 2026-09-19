"""Contracts for the frozen intake assurance plan."""

from __future__ import annotations

from pathlib import PurePosixPath
import hashlib
import json
from typing import Literal, Self, cast

from pydantic import Field, field_validator, model_validator
from pydantic_core import to_jsonable_python

from graph_engine.identifiers import IdentifierError, validate_qualified_id
from graph_engine.plugin_api import FrozenModel
from graph_engine.canonical import JSONValue, canonical_digest, canonical_json_bytes

from assurance_intake.contracts.common import TestFamily, validate_family_tuple
from assurance_intake.contracts.impact import INVENTORY_PATH, impact_row_identity
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
    "impact_retained": 5,
    "impact_family_unavailable": 6,
    "impact_pending_confirmation": 7,
}
_GLOBAL_REASONS = frozenset({"accepted_proposal", "fallback_all_candidates", "impact_pending_confirmation"})


def bind_impact_row_ids(
    *,
    plan_digest: str,
    inventory_ref: EvidenceArtifactRefV1,
    row_ids: tuple[str, ...],
) -> tuple[tuple[str, str, str], ...]:
    """Bind local IR-* ids to the plan and authenticated inventory digest."""
    return tuple(
        impact_row_identity(
            plan_digest=plan_digest,
            inventory_digest=inventory_ref.digest,
            row_id=row_id,
        )
        for row_id in row_ids
    )


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


ResolutionReasonCode = Literal[
    "accepted_proposal",
    "fallback_all_candidates",
    "required_retained",
    "candidate_outside_allowed",
    "proposed_outside_candidate",
    "impact_retained",
    "impact_family_unavailable",
    "impact_pending_confirmation",
]
FallbackDetail = Literal["empty_proposal", "outside_candidate", "empty_after_policy"]


class ResolutionReasonV1(FrozenModel):
    reason_code: ResolutionReasonCode
    family: TestFamily | None = None
    detail: FallbackDetail | None = None
    summary: str = Field(min_length=1)

    @model_validator(mode="after")
    def _shape_matches_reason(self) -> Self:
        global_reason = self.reason_code in _GLOBAL_REASONS
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
    schema_version: Literal["1"] = "1"
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
    impact_inventory_ref: EvidenceArtifactRefV1
    plan_digest: str = Field(pattern=_SHA256)
    resolution_reasons: tuple[ResolutionReasonV1, ...]

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
    def _plan_invariants(self) -> Self:
        if not set(self.selected_test_families) <= set(self.candidate_test_families):
            raise ValueError("selected_test_families must be candidates")
        if not set(self.quality_goal.required_test_families) <= set(self.selected_test_families):
            raise ValueError("selected_test_families must retain required goal families")
        expected_exploration = "qa/results/explore/exploration.json"
        if self.exploration_ref.path != expected_exploration:
            raise ValueError("exploration_ref must bind the current change")
        if self.impact_inventory_ref.path != INVENTORY_PATH:
            raise ValueError("impact_inventory_ref must bind the current change")
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
        payload = self.model_dump(mode="json")
        payload.pop("plan_digest")
        if self.plan_digest != canonical_digest(cast(JSONValue, payload)):
            raise ValueError("plan_digest does not match the plan projection")
        return self


class ResolvePlanInputV1(FrozenModel):
    change_id: str = Field(min_length=1)
    requirement_digest: str = Field(pattern=_SHA256)
    candidate_test_families: tuple[TestFamily, ...] = Field(min_length=1)
    budgets: PlanBudgetsV1
    policy_resource_id: str
    policy_digest: str = Field(pattern=_SHA256)
    family_policy: TestFamilyPolicyV1
    exploration_ref: EvidenceArtifactRefV1
    impact_inventory_ref: EvidenceArtifactRefV1
    source_resource_digests: tuple[tuple[str, str], ...]
    capability_leafs: tuple[str, ...]

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
        expected = "qa/results/explore/exploration.json"
        if self.exploration_ref.path != expected:
            raise ValueError("exploration_ref must bind the current change")
        if self.impact_inventory_ref.path != INVENTORY_PATH:
            raise ValueError("impact_inventory_ref must bind the current change")
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
    projection = cast(JSONValue, to_jsonable_python(payload))
    digest = canonical_digest(projection)
    return ResolvedAssurancePlan.model_validate({**payload, "plan_digest": digest})


def plan_bytes(plan: ResolvedAssurancePlan) -> bytes:
    return canonical_json_bytes(cast(JSONValue, plan.model_dump(mode="json")))


def plan_artifact_ref(plan: ResolvedAssurancePlan) -> EvidenceArtifactRefV1:
    data = plan_bytes(plan)
    return EvidenceArtifactRefV1(
        path=(f"qa/results/plan/{plan.plan_digest}/resolved-assurance-plan.json"),
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
    "FallbackDetail",
    "LoadPlanInputV1",
    "PlanBudgetsV1",
    "PreparedQualityGoalV1",
    "ResolutionReasonCode",
    "ResolutionReasonV1",
    "ResolvePlanInputV1",
    "ResolvePlanOutputV1",
    "ResolvedAssurancePlan",
    "TestFamilyPolicyV1",
    "bind_impact_row_ids",
    "decode_plan",
    "plan_artifact_ref",
    "plan_bytes",
    "seal_plan",
    "resolution_reason_sort_key",
]
