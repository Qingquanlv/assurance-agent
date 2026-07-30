"""review/*-plan-checks.json — 确定性 plan check 的证据文档（spec C2）。

check 只回答「事实是否成立」；block / require_human / warn 的处置由 policy 决定，
因此本文档没有 severity 字段。
"""

from collections.abc import Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

from assurance_agent.artifacts.models.assurance import (
    KNOWN_PLAN_CHECK_IDS,
    PLAN_CHECK_IDS,
    LayerName,
)

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
    def validate_cases(self) -> "LayerApplicability":
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
    def validate_evidence(self) -> "CheckEvidence":
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

    schema_version: Literal["1", "2"] = "2"
    layer: LayerName | None = None
    applicability: LayerApplicability | None = None
    status: CheckStatus
    checks: tuple[CheckEvidence, ...] = ()

    @model_validator(mode="after")
    def validate_versioned_document(self) -> "PlanCheckDocument":
        if self.schema_version == "1":
            if self.layer is not None or self.applicability is not None:
                raise ValueError("version 1 documents do not contain applicability data")
            if self.status == "not_applicable" or any(
                check.status == "not_applicable" for check in self.checks
            ):
                raise ValueError("version 1 documents only support pass and fail")
            return self

        if self.layer is None or self.applicability is None:
            raise ValueError("version 2 documents require layer and applicability")
        if self.layer != self.applicability.layer:
            raise ValueError("document layer must match applicability layer")

        check_ids = tuple(check.check_id for check in self.checks)
        if len(check_ids) != len(PLAN_CHECK_IDS) or set(check_ids) != KNOWN_PLAN_CHECK_IDS:
            raise ValueError("version 2 documents require every known check exactly once")

        expected_status: CheckStatus
        if not self.applicability.applicable:
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
    ) -> "PlanCheckDocument":
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
