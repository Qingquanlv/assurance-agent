"""Case and change-authoring contracts owned by assurance-intake."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, model_validator

from assurance_intake.contracts.common import CaseId, NonEmptyStr, RiskTier

CasePriority = Literal["P0", "P1", "P2", "P3"]
CaseSeverity = Literal["blocker", "critical", "major", "minor"]


class CaseRisk(BaseModel):
    """The author's own risk assessment of a case."""

    model_config = ConfigDict(extra="forbid")

    level: RiskTier
    likelihood: int | None = None
    impact: int | None = None
    rationale: str = ""


class _CaseEntryBase(BaseModel):
    case_id: CaseId
    title: NonEmptyStr
    status: Literal["draft", "active", "deprecated"]
    priority: CasePriority
    severity: CaseSeverity
    type: Literal["API", "E2E", "Fuzz", "Performance"]
    module: NonEmptyStr


class CaseEntry(_CaseEntryBase):
    risk: CaseRisk | None = None


class CaseRemoval(BaseModel):
    case_id: CaseId


class CaseAutomationAuthoring(BaseModel):
    """Automation intent consumed by layer applicability and code generation."""

    required: bool
    framework: Literal["pytest", "pytest-playwright", "schemathesis", "locust"]
    status: Literal["not_automated", "planned", "automated", "flaky", "deprecated"]
    confirmed_by: str | None = None
    confirmed_at: str | None = None
    fuzz: dict[str, object] | None = None
    performance: dict[str, object] | None = None


class CaseRegressionAuthoring(BaseModel):
    candidate: bool
    tier: NonEmptyStr
    rationale: NonEmptyStr
    selection_reason: list[str]
    maintenance_rule: NonEmptyStr


class CaseEntryAuthoring(_CaseEntryBase):
    """Fields required from newly authored cases, beyond legacy compatibility."""

    requirement_id: NonEmptyStr
    feature_name: NonEmptyStr
    test_condition_id: NonEmptyStr
    design_technique: NonEmptyStr
    objective: NonEmptyStr
    summary: NonEmptyStr
    preconditions: list[object]
    test_data: list[object]
    steps: list[object] = Field(min_length=1)
    assertions: list[object] = Field(min_length=1)
    postconditions: list[object]
    edge_cases: list[object]
    related_cases: list[str]
    risk: CaseRisk
    automation: CaseAutomationAuthoring
    regression: CaseRegressionAuthoring
    trace: dict[str, object]

    @model_validator(mode="after")
    def _validate_automation_contract(self) -> CaseEntryAuthoring:
        expected_framework = {
            "API": "pytest",
            "E2E": "pytest-playwright",
            "Fuzz": "schemathesis",
            "Performance": "locust",
        }[self.type]
        if self.automation.framework != expected_framework:
            raise ValueError(f"{self.type} automation.framework must be {expected_framework!r}")
        if self.risk.likelihood is None or self.risk.impact is None or not self.risk.rationale.strip():
            raise ValueError("risk.likelihood, risk.impact, and risk.rationale are required")
        if self.risk.level in {"high", "critical"} and not self.automation.required:
            raise ValueError("high/critical risk cases require automation.required=true")
        if self.type == "Fuzz":
            fuzz = self.automation.fuzz
            endpoints = fuzz.get("endpoints") if isinstance(fuzz, Mapping) else None
            if not isinstance(endpoints, list) or not endpoints:
                raise ValueError("Fuzz automation.fuzz.endpoints must be a non-empty list")
            if not self.related_cases:
                raise ValueError("Fuzz related_cases must reference a non-Fuzz case")
        if self.type == "Performance":
            performance = self.automation.performance
            scenario = performance.get("scenario") if isinstance(performance, Mapping) else None
            thresholds = scenario.get("thresholds") if isinstance(scenario, Mapping) else None
            for field in ("capability", "endpoint"):
                value = scenario.get(field) if isinstance(scenario, Mapping) else None
                if not isinstance(value, str) or not value.strip():
                    raise ValueError(
                        f"Performance automation.performance.scenario.{field} must be a non-empty string"
                    )
            for field in ("p95_ms", "error_rate_max"):
                value = thresholds.get(field) if isinstance(thresholds, Mapping) else None
                if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise ValueError(
                        f"Performance automation.performance.scenario.thresholds.{field} must be numeric"
                    )
        return self


CaseEntryT = TypeVar("CaseEntryT", bound=_CaseEntryBase)


class _CaseYamlBase(BaseModel, Generic[CaseEntryT]):
    schema_version: NonEmptyStr
    added: list[CaseEntryT]
    modified: list[CaseEntryT]
    removed: list[CaseRemoval]


class CaseYaml(_CaseYamlBase[CaseEntry]):
    pass


class CaseYamlAuthoring(_CaseYamlBase[CaseEntryAuthoring]):
    """New case output obligations that are stricter than historical reads."""

    model_config = ConfigDict(
        json_schema_extra={
            "prompt_notes": [
                "Every added/modified case requires complete risk, automation, regression, "
                "trace, steps, assertions, and test-design metadata; do not emit a minimal "
                "legacy-compatible case",
                "Set automation.required=true for every layer selected for automated execution; "
                "otherwise applicability deterministically skips that layer",
                "Performance entries require automation.performance.scenario.capability "
                "and automation.performance.scenario.endpoint as non-empty strings, plus numeric "
                "thresholds.p95_ms and thresholds.error_rate_max",
            ]
        }
    )

    @model_validator(mode="after")
    def _require_authored_cases(self) -> CaseYamlAuthoring:
        if not self.added and not self.modified:
            raise ValueError("case authoring must add or modify at least one case")
        return self

    @model_validator(mode="before")
    @classmethod
    def _require_performance_execution_identity(cls, value: object) -> object:
        if not isinstance(value, Mapping):
            return value
        for section in ("added", "modified"):
            entries = value.get(section)
            if not isinstance(entries, list):
                continue
            for index, entry in enumerate(entries):
                if not isinstance(entry, Mapping) or entry.get("type") != "Performance":
                    continue
                automation = entry.get("automation")
                performance = automation.get("performance") if isinstance(automation, Mapping) else None
                scenario = performance.get("scenario") if isinstance(performance, Mapping) else None
                for field in ("capability", "endpoint"):
                    field_value = scenario.get(field) if isinstance(scenario, Mapping) else None
                    if not isinstance(field_value, str) or not field_value.strip():
                        raise ValueError(
                            f"{section}[{index}] Performance automation.performance.scenario."
                            f"{field} must be a non-empty string"
                        )
        return value

    @model_validator(mode="after")
    def _require_typed_capability_leafs(self, info: ValidationInfo) -> CaseYamlAuthoring:
        context = info.context or {}
        leafs = context.get("capability_leafs")
        if not isinstance(leafs, frozenset) or any(not isinstance(item, str) for item in leafs):
            raise ValueError("capability_leafs context must be a frozenset of declared typed leaves")
        for entry in (*self.added, *self.modified):
            for key in entry.trace:
                if key not in leafs:
                    raise ValueError(f"capability key is not a declared typed leaf: {key}")
        return self


class QaChange(BaseModel):
    change_id: NonEmptyStr
    requirement_id: NonEmptyStr
    feature_name: NonEmptyStr
    status: NonEmptyStr


class QaCaseTarget(BaseModel):
    module: NonEmptyStr
    change_case_file: NonEmptyStr
    target_case_file: NonEmptyStr


class QaTargets(BaseModel):
    cases: list[QaCaseTarget]


class QaWorkflow(BaseModel):
    current_step: str | None = None
    next_step: str | None = None


class QaApproval(BaseModel):
    """Approval metadata consumed by case-design gate."""

    mode: Literal["interactive", "autonomous"]
    approved_by: NonEmptyStr
    approved_approach: NonEmptyStr
    approved_at: NonEmptyStr


class QaYaml(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    schema_version: NonEmptyStr
    schema_: NonEmptyStr = Field(alias="schema")
    created_at: NonEmptyStr
    change: QaChange
    targets: QaTargets
    approval: QaApproval
    workflow: QaWorkflow | None = None
