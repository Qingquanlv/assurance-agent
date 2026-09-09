"""Private prepare/finalize request models owned by assurance-generation."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any

from pydantic import Field, field_validator, model_validator

from agent_runtime_contracts import AgentRunResult, FrozenExecutionSelection
from graph_engine.plugin_api import FrozenModel

from assurance_generation.contracts.plans import canonical_relative_path
from assurance_generation.contracts.execution_plan import CasePlanContextV1, ValidationProfile
from assurance_intake.contracts import EvidenceArtifactRefV1, ReviewedCaseV1, RiskTier
from assurance_intake.contracts.verification import AssertionSourcesV1
from assurance_intake.contracts.workflow import require_same_plan

_SHA256 = r"^[0-9a-f]{64}$"


def _sorted_unique(values: tuple[str, ...], *, label: str) -> tuple[str, ...]:
    cleaned = tuple(item.strip() for item in values)
    if any(not item for item in cleaned):
        raise ValueError(f"{label} must be a non-empty string")
    return tuple(sorted(set(cleaned)))


def _canonical_relative_paths(values: tuple[str, ...]) -> tuple[str, ...]:
    paths = tuple(canonical_relative_path(path) for path in _sorted_unique(values, label="artifact path"))
    for path in paths:
        if len(path) >= 2 and path[1] == ":":
            raise ValueError("artifact path must be canonical and relative")
    return paths


def _canonical_write_root(path: str) -> str:
    stripped = path.rstrip("/")
    if not stripped:
        raise ValueError("write root must be a non-empty canonical relative prefix")
    canonical_relative_path(stripped)
    if path != stripped and not path.endswith("/"):
        raise ValueError("write root must be a canonical relative prefix")
    return path


class AgentBindingDataV1(FrozenModel):
    agent_profile: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    execution: FrozenExecutionSelection
    request_policy_digest: str = Field(pattern=_SHA256)
    request_config_digest: str = Field(pattern=_SHA256)


class FamilyConstraintsV1(FrozenModel):
    write_roots: tuple[str, ...]
    operations: tuple[str, ...]
    risks: tuple[RiskTier, ...]

    @field_validator("write_roots")
    @classmethod
    def _write_roots(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        roots = tuple(_canonical_write_root(item) for item in value)
        if not roots:
            raise ValueError("write_roots must be non-empty")
        return roots

    @field_validator("operations")
    @classmethod
    def _operations(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        operations = _sorted_unique(value, label="operation")
        if not operations:
            raise ValueError("operations must be non-empty")
        return operations

    @field_validator("risks")
    @classmethod
    def _risks(cls, value: tuple[RiskTier, ...]) -> tuple[RiskTier, ...]:
        if not value:
            raise ValueError("risks must be non-empty")
        return tuple(sorted(set(value)))


class PlanInputV1(FrozenModel):
    change_id: str = Field(min_length=1)
    plan_digest: str = Field(pattern=_SHA256)
    plan_ref: EvidenceArtifactRefV1
    capability_leafs: tuple[str, ...]
    artifact_paths: tuple[str, ...]
    reviewed_cases: dict[str, Any] | None = None
    reviewed_plan: dict[str, Any] | None = None
    family_constraints: FamilyConstraintsV1 | None = None
    coverage_epoch: int = Field(default=0, ge=0)
    local_round: int = Field(default=0, ge=0)
    reviewed_case: ReviewedCaseV1 | None = None
    case_plan_context: CasePlanContextV1 | None = None
    assertion_sources: AssertionSourcesV1 | None = None
    validation_profile: ValidationProfile | None = None
    case_execution_plan_ref: EvidenceArtifactRefV1 | None = None
    case_execution_plan_digest: str | None = Field(default=None, pattern=_SHA256)

    @field_validator("capability_leafs")
    @classmethod
    def _capability_leafs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _sorted_unique(value, label="capability leaf")

    @field_validator("artifact_paths")
    @classmethod
    def _artifact_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _canonical_relative_paths(value)

    @field_validator("reviewed_cases")
    @classmethod
    def _reviewed_cases(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is not None and not value:
            raise ValueError("reviewed_cases must be a mapping")
        return value

    @model_validator(mode="after")
    def _reviewed_case_uses_plan(self) -> PlanInputV1:
        if self.reviewed_case is not None:
            require_same_plan(
                self.plan_digest,
                self.plan_ref,
                self.reviewed_case.plan_digest,
                self.reviewed_case.plan_ref,
            )
        machine_inputs = (
            self.case_plan_context,
            self.assertion_sources,
            self.validation_profile,
        )
        if any(value is not None for value in machine_inputs):
            if any(value is None for value in machine_inputs):
                raise ValueError("machine plan inputs must be supplied together")
            assert self.case_plan_context is not None
            if self.case_plan_context.change_id != self.change_id:
                raise ValueError("machine plan context change_id does not match plan input")
            require_same_plan(
                self.plan_digest,
                self.plan_ref,
                self.case_plan_context.plan_digest,
                self.case_plan_context.plan_ref,
            )
            if self.reviewed_case is not None and self.reviewed_case != self.case_plan_context.reviewed_case:
                raise ValueError("machine plan context does not match reviewed_case")
        has_machine_ref = self.case_execution_plan_ref is not None
        has_machine_digest = self.case_execution_plan_digest is not None
        if has_machine_ref != has_machine_digest:
            raise ValueError("case execution plan ref and digest must be supplied together")
        if self.case_execution_plan_ref is not None:
            expected = f"qa/changes/{self.change_id}/plans/api-case-execution-plan.json"
            if self.case_execution_plan_ref.path != expected:
                raise ValueError("case execution plan ref does not match current change")
            if self.case_execution_plan_ref.digest != self.case_execution_plan_digest:
                raise ValueError("case execution plan ref and digest do not match")
        return self


class AgentFinalizeInputV1(FrozenModel):
    agent_result: AgentRunResult
    plan_digest: str = Field(pattern=_SHA256)
    plan_ref: EvidenceArtifactRefV1
    change_id: str | None = Field(default=None, min_length=1)
    capability_leafs: tuple[str, ...]
    required_capabilities: tuple[str, ...] | None = None
    reviewed_mapping: dict[str, Any] | None = None
    artifact_paths: tuple[str, ...]
    allowed_paths: tuple[str, ...] = ()
    coverage_epoch: int = Field(default=0, ge=0)
    local_round: int = Field(default=0, ge=0)
    reviewed_case: ReviewedCaseV1 | None = None
    case_plan_context: CasePlanContextV1 | None = None
    assertion_sources: AssertionSourcesV1 | None = None
    validation_profile: ValidationProfile | None = None
    case_execution_plan_ref: EvidenceArtifactRefV1 | None = None
    case_execution_plan_digest: str | None = Field(default=None, pattern=_SHA256)

    @field_validator("capability_leafs")
    @classmethod
    def _capability_leafs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _sorted_unique(value, label="capability leaf")

    @field_validator("required_capabilities")
    @classmethod
    def _required_capabilities(cls, value: tuple[str, ...] | None) -> tuple[str, ...] | None:
        if value is None:
            return None
        return _sorted_unique(value, label="required capability")

    @field_validator("artifact_paths")
    @classmethod
    def _artifact_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _canonical_relative_paths(value)

    @field_validator("allowed_paths")
    @classmethod
    def _allowed_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _canonical_relative_paths(value)

    @model_validator(mode="after")
    def _reviewed_case_uses_plan(self) -> AgentFinalizeInputV1:
        if self.reviewed_case is not None:
            require_same_plan(
                self.plan_digest,
                self.plan_ref,
                self.reviewed_case.plan_digest,
                self.reviewed_case.plan_ref,
            )
        machine_inputs = (self.case_plan_context, self.assertion_sources)
        if any(value is not None for value in machine_inputs):
            if any(value is None for value in machine_inputs) or self.validation_profile is None:
                raise ValueError("machine plan inputs must be supplied together")
            assert self.case_plan_context is not None
            if self.change_id is not None and self.case_plan_context.change_id != self.change_id:
                raise ValueError("machine plan context change_id does not match finalize input")
            require_same_plan(
                self.plan_digest,
                self.plan_ref,
                self.case_plan_context.plan_digest,
                self.case_plan_context.plan_ref,
            )
            if self.reviewed_case is not None and self.reviewed_case != self.case_plan_context.reviewed_case:
                raise ValueError("machine plan context does not match reviewed_case")
        has_machine_ref = self.case_execution_plan_ref is not None
        has_machine_digest = self.case_execution_plan_digest is not None
        if has_machine_ref != has_machine_digest:
            raise ValueError("case execution plan ref and digest must be supplied together")
        if self.case_execution_plan_ref is not None:
            if self.change_id is not None:
                expected = f"qa/changes/{self.change_id}/plans/api-case-execution-plan.json"
                if self.case_execution_plan_ref.path != expected:
                    raise ValueError("case execution plan ref does not match current change")
            if self.case_execution_plan_ref.digest != self.case_execution_plan_digest:
                raise ValueError("case execution plan ref and digest do not match")
        verified_codegen = self.validation_profile is not None and self.case_plan_context is None
        if verified_codegen and (self.reviewed_case is None or self.case_execution_plan_ref is None):
            raise ValueError("verified codegen requires ReviewedCase and case execution plan")
        return self


class CodegenInputV1(FrozenModel):
    change_id: str = Field(min_length=1)
    plan_digest: str = Field(pattern=_SHA256)
    plan_ref: EvidenceArtifactRefV1
    capability_leafs: tuple[str, ...]
    artifact_paths: tuple[str, ...] = ()
    reviewed_plan: dict[str, Any]
    reviewed_cases: dict[str, Any] | None = None
    family_constraints: FamilyConstraintsV1 | None = None
    coverage_epoch: int = Field(default=0, ge=0)
    local_round: int = Field(default=0, ge=0)
    reviewed_case: ReviewedCaseV1 | None = None
    validation_profile: ValidationProfile | None = None
    case_execution_plan_ref: EvidenceArtifactRefV1 | None = None
    case_execution_plan_digest: str | None = Field(default=None, pattern=_SHA256)

    @field_validator("capability_leafs")
    @classmethod
    def _capability_leafs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _sorted_unique(value, label="capability leaf")

    @field_validator("artifact_paths")
    @classmethod
    def _artifact_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _canonical_relative_paths(value)

    @field_validator("reviewed_plan")
    @classmethod
    def _reviewed_plan(cls, value: dict[str, Any]) -> dict[str, Any]:
        if not value:
            raise ValueError("reviewed_plan must be a mapping")
        return value

    @field_validator("reviewed_cases")
    @classmethod
    def _reviewed_cases(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is not None and not value:
            raise ValueError("reviewed_cases must be a mapping")
        return value

    @model_validator(mode="after")
    def _reviewed_case_uses_plan(self) -> CodegenInputV1:
        if self.reviewed_case is not None:
            require_same_plan(
                self.plan_digest,
                self.plan_ref,
                self.reviewed_case.plan_digest,
                self.reviewed_case.plan_ref,
            )
        has_ref = self.case_execution_plan_ref is not None
        has_digest = self.case_execution_plan_digest is not None
        if has_ref != has_digest:
            raise ValueError("case execution plan ref and digest must be supplied together")
        if self.case_execution_plan_ref is not None:
            expected = f"qa/changes/{self.change_id}/plans/api-case-execution-plan.json"
            if self.case_execution_plan_ref.path != expected:
                raise ValueError("case execution plan ref does not match current change")
            if self.case_execution_plan_ref.digest != self.case_execution_plan_digest:
                raise ValueError("case execution plan ref and digest do not match")
        verified = self.validation_profile is not None
        if verified != (self.reviewed_case is not None and self.case_execution_plan_ref is not None):
            raise ValueError(
                "verified codegen requires validation profile, ReviewedCase, and case execution plan"
            )
        return self


def under_write_root(path: str, roots: tuple[str, ...]) -> bool:
    relative = PurePosixPath(path).as_posix()
    for root in roots:
        prefix = root if root.endswith("/") else f"{root}/"
        if relative == root.rstrip("/") or relative.startswith(prefix):
            return True
    return False
