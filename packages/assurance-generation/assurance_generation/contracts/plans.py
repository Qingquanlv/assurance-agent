"""Deterministic plan-check evidence documents."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

from pathlib import PurePosixPath

from pydantic import BaseModel, ConfigDict, ValidationInfo, field_validator, model_validator

from assurance_generation.contracts.families import KNOWN_PLAN_CHECK_IDS, PLAN_CHECK_IDS, LayerName
from assurance_intake.contracts import NonEmptyStr, RiskTier

_FROZEN = ConfigDict(frozen=True, extra="forbid")

CheckStatus = Literal["pass", "fail", "not_applicable"]
LayerApplicabilityReason = Literal["automated_cases_present", "no_automated_cases"]
CheckApplicabilityReason = Literal["layer_not_applicable", "check_not_in_profile"]


class Finding(BaseModel):
    model_config = _FROZEN

    locator: str
    actual: str
    expected: str


class LayerApplicability(BaseModel):
    model_config = _FROZEN

    layer: LayerName
    applicable: bool
    reason_code: LayerApplicabilityReason
    case_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_cases(self) -> LayerApplicability:
        if self.applicable:
            if self.reason_code != "automated_cases_present":
                raise ValueError("applicable layers require automated_cases_present")
            if not self.case_ids:
                raise ValueError("applicable layers require at least one case ID")
            if self.case_ids != tuple(sorted(set(self.case_ids))):
                raise ValueError("case IDs must be sorted and unique")
        else:
            if self.reason_code != "no_automated_cases":
                raise ValueError("inapplicable layers require no_automated_cases")
            if self.case_ids:
                raise ValueError("inapplicable layers cannot contain case IDs")
        return self


class CheckEvidence(BaseModel):
    model_config = _FROZEN

    check_id: str
    status: CheckStatus
    findings: tuple[Finding, ...] = ()
    refs: tuple[str, ...] = ()
    applicability_reason: CheckApplicabilityReason | None = None

    @model_validator(mode="after")
    def validate_evidence(self) -> CheckEvidence:
        if self.status == "fail" and not self.findings:
            raise ValueError("failed checks require findings")
        if self.status != "fail" and self.findings:
            raise ValueError("only failed checks can contain findings")
        if self.status == "not_applicable" and self.applicability_reason is None:
            raise ValueError("not applicable checks require an applicability reason")
        if self.status != "not_applicable" and self.applicability_reason is not None:
            raise ValueError("only not applicable checks can have an applicability reason")
        return self


class PlanCheckDocument(BaseModel):
    model_config = _FROZEN

    schema_version: Literal["2"] = "2"
    layer: LayerName
    applicability: LayerApplicability
    status: CheckStatus
    checks: tuple[CheckEvidence, ...] = ()

    @model_validator(mode="after")
    def validate_versioned_document(self) -> PlanCheckDocument:
        if self.layer != self.applicability.layer:
            raise ValueError("document layer must match applicability layer")

        check_ids = tuple(check.check_id for check in self.checks)
        if len(check_ids) != len(PLAN_CHECK_IDS) or set(check_ids) != KNOWN_PLAN_CHECK_IDS:
            raise ValueError("version 2 documents require every known check exactly once")

        expected_status: CheckStatus
        if not self.applicability.applicable:
            if any(
                check.status != "not_applicable" or check.applicability_reason != "layer_not_applicable"
                for check in self.checks
            ):
                raise ValueError(
                    "inapplicable layers require every check to be not_applicable "
                    "with applicability_reason=layer_not_applicable"
                )
            expected_status = "not_applicable"
        elif any(check.status == "fail" for check in self.checks):
            expected_status = "fail"
        else:
            expected_status = "pass"
        if self.status != expected_status:
            raise ValueError("document status must match applicability and check statuses")
        return self

    @classmethod
    def from_checks(
        cls,
        *,
        layer: LayerName,
        applicability: LayerApplicability,
        checks: Sequence[CheckEvidence],
    ) -> PlanCheckDocument:
        ordered = tuple(checks)
        if not applicability.applicable:
            status: CheckStatus = "not_applicable"
        elif any(check.status == "fail" for check in ordered):
            status = "fail"
        else:
            status = "pass"
        return cls(
            schema_version="2",
            layer=layer,
            applicability=applicability,
            status=status,
            checks=ordered,
        )


def canonical_relative_path(path: str) -> str:
    posix = PurePosixPath(path)
    if (
        posix.is_absolute()
        or "\\" in path
        or posix.as_posix() != path
        or any(part in {"", ".", ".."} for part in posix.parts)
    ):
        raise ValueError(f"path must be canonical and relative: {path}")
    return path


class PlanCoverageRow(BaseModel):
    model_config = _FROZEN

    case_id: NonEmptyStr
    operation: NonEmptyStr
    risk: RiskTier
    required_capabilities: tuple[NonEmptyStr, ...]


class FuzzStrategyV1(BaseModel):
    model_config = _FROZEN

    endpoint: NonEmptyStr
    property_name: NonEmptyStr


class PerformanceScenarioV1(BaseModel):
    model_config = _FROZEN

    scenario_id: NonEmptyStr
    capability: NonEmptyStr
    endpoint: NonEmptyStr
    p95_ms: float
    error_rate_max: float


class PlanResultV1(BaseModel):
    """Typed four-family plan result consumed by review, codegen, and validators."""

    model_config = _FROZEN

    schema_version: Literal["1"]
    family: LayerName
    change_id: NonEmptyStr
    case_ids: tuple[NonEmptyStr, ...]
    required_capabilities: tuple[NonEmptyStr, ...]
    coverage: tuple[PlanCoverageRow, ...]
    output_files: tuple[NonEmptyStr, ...]
    fuzz_strategy: FuzzStrategyV1 | None = None
    performance_scenarios: tuple[PerformanceScenarioV1, ...] = ()

    @field_validator("output_files")
    @classmethod
    def _output_files(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(canonical_relative_path(item) for item in value)

    @model_validator(mode="after")
    def _validate_plan_result(self, info: ValidationInfo) -> PlanResultV1:
        context = info.context or {}
        leafs = context.get("capability_leafs")
        if not isinstance(leafs, frozenset) or any(not isinstance(item, str) for item in leafs):
            raise ValueError("capability_leafs context must be a frozenset of declared typed leaves")
        ids = tuple(sorted(set(self.case_ids)))
        if self.case_ids != ids:
            raise ValueError("case_ids must be sorted and unique")
        if not self.coverage:
            raise ValueError("plan coverage must include operation and risk partitions")
        covered = tuple(row.case_id for row in self.coverage)
        if covered != self.case_ids:
            raise ValueError("coverage must include every case_id exactly once in case_ids order")
        for key in self.required_capabilities:
            if key not in leafs:
                raise ValueError(f"unknown capability leaf: {key}")
        for row in self.coverage:
            for key in row.required_capabilities:
                if key not in leafs:
                    raise ValueError(f"unknown capability leaf: {key}")
        if self.family == "fuzz":
            if self.fuzz_strategy is None:
                raise ValueError("fuzz plan requires endpoint/property strategy")
        elif self.fuzz_strategy is not None:
            raise ValueError("fuzz_strategy is only valid for fuzz plans")
        if self.family == "performance":
            if not self.performance_scenarios:
                raise ValueError("performance plan requires scenario identity and numeric thresholds")
        elif self.performance_scenarios:
            raise ValueError("performance_scenarios is only valid for performance plans")
        return self
