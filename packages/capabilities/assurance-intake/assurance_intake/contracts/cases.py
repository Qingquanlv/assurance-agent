"""Case and change-authoring contracts owned by assurance-intake."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Generic, Literal, Self, TypeVar

from pydantic import BaseModel, ConfigDict, Field, RootModel, ValidationInfo, model_validator

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


class CaseRemoval(BaseModel):
    case_id: CaseId


class FuzzEndpointAuthoring(BaseModel):
    """Concrete HTTP operation selected for generated fuzz coverage."""

    model_config = ConfigDict(extra="forbid")

    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]
    path: str = Field(min_length=1, pattern=r"^/")


class FuzzAutomationAuthoring(BaseModel):
    """Executable fuzz strategy; exposed directly in the agent result schema."""

    model_config = ConfigDict(extra="forbid")

    endpoints: list[FuzzEndpointAuthoring] = Field(min_length=1)
    property: NonEmptyStr
    expectations: list[NonEmptyStr] = Field(min_length=1)


class PerformanceThresholdsAuthoring(BaseModel):
    model_config = ConfigDict(extra="forbid")

    p95_ms: float = Field(gt=0)
    error_rate_max: float = Field(ge=0, le=1)


class PerformanceLoadAuthoring(BaseModel):
    """Absolute, reproducible load shape for a performance scenario."""

    model_config = ConfigDict(extra="forbid")

    concurrency: int = Field(gt=0)
    spawn_rate_per_second: float = Field(gt=0)
    duration_seconds: int = Field(gt=0)


class PerformanceScenarioAuthoring(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capability: NonEmptyStr
    endpoint: str = Field(
        min_length=1,
        pattern=r"^(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS) /\S*$",
    )
    load: PerformanceLoadAuthoring
    thresholds: PerformanceThresholdsAuthoring


class PerformanceAutomationAuthoring(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenario: PerformanceScenarioAuthoring


class CaseAutomationAuthoring(BaseModel):
    """Automation intent consumed by layer applicability and code generation."""

    required: bool
    framework: Literal["pytest", "pytest-playwright", "schemathesis", "locust"]
    status: Literal["not_automated", "planned", "automated", "flaky", "deprecated"]
    confirmed_by: str | None = None
    confirmed_at: str | None = None
    fuzz: FuzzAutomationAuthoring | None = None
    performance: PerformanceAutomationAuthoring | None = None


class CaseRegressionAuthoring(BaseModel):
    candidate: bool
    tier: NonEmptyStr
    rationale: NonEmptyStr
    selection_reason: list[str]
    maintenance_rule: NonEmptyStr


class CaseTraceCoverage(BaseModel):
    """Typed proof that an authored case covers a declared capability leaf."""

    model_config = ConfigDict(extra="forbid")

    covered: Literal[True]


class MinimumCoverageMatrixRowAuthoring(BaseModel):
    """One case-design mapping from an MRC obligation to authored cases."""

    model_config = ConfigDict(extra="forbid")

    mrc_id: NonEmptyStr
    key: NonEmptyStr
    required: bool = True
    covered_by_cases: list[CaseId] = Field(default_factory=list)
    status: Literal["covered", "skipped_by_scope"] = "covered"
    skip_reason: str | None = None
    category: Literal["api", "e2e", "e2e_if_enabled", "negative", "data_integrity"] | None = None
    layer: Literal["api", "e2e", "both"] | None = None

    @model_validator(mode="after")
    def _require_status_evidence(self) -> Self:
        if self.status == "covered":
            if not self.covered_by_cases:
                raise ValueError("covered MRC rows require covered_by_cases")
            if self.skip_reason is not None:
                raise ValueError("covered MRC rows cannot declare skip_reason")
        else:
            if self.covered_by_cases:
                raise ValueError("skipped_by_scope MRC rows cannot declare covered_by_cases")
            if self.skip_reason is None or not self.skip_reason.strip():
                raise ValueError("skipped_by_scope MRC rows require skip_reason")
        return self


class MinimumCoverageMatrixAuthoring(RootModel[list[MinimumCoverageMatrixRowAuthoring]]):
    """Authenticated case-design MRC matrix with unique row identities."""

    @model_validator(mode="after")
    def _require_rows_and_unique_identity(self) -> Self:
        if not self.root:
            raise ValueError("minimum coverage matrix must contain at least one row")
        for field in ("mrc_id", "key"):
            values = [getattr(row, field) for row in self.root]
            duplicates = sorted(value for value in set(values) if values.count(value) > 1)
            if duplicates:
                raise ValueError(f"duplicate {field} values are not allowed: {duplicates!r}")
        return self


class CaseEntryAuthoring(_CaseEntryBase):
    """Fields required on every current-schema case document."""

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
    trace: dict[str, CaseTraceCoverage] = Field(min_length=1)

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
            if self.automation.fuzz is None:
                raise ValueError("Fuzz automation.fuzz is required")
            if self.automation.performance is not None:
                raise ValueError("Fuzz cases cannot declare automation.performance")
            if not self.related_cases:
                raise ValueError("Fuzz related_cases must reference a non-Fuzz case")
        elif self.type == "Performance":
            if self.automation.performance is None:
                raise ValueError("Performance automation.performance is required")
            if self.automation.fuzz is not None:
                raise ValueError("Performance cases cannot declare automation.fuzz")
        elif self.automation.fuzz is not None or self.automation.performance is not None:
            raise ValueError(f"{self.type} cases cannot declare fuzz/performance automation")
        return self


CaseEntryT = TypeVar("CaseEntryT", bound=_CaseEntryBase)


class _CaseYamlBase(BaseModel, Generic[CaseEntryT]):
    schema_version: Literal["1.0"]
    added: list[CaseEntryT]
    modified: list[CaseEntryT]
    removed: list[CaseRemoval]


class CaseYamlAuthoring(_CaseYamlBase[CaseEntryAuthoring]):
    """Current case document; the read model equals this authoring contract."""

    model_config = ConfigDict(
        json_schema_extra={
            "prompt_notes": [
                "Every added/modified case requires complete risk, automation, regression, "
                "trace, steps, assertions, and test-design metadata",
                "Set automation.required=true for every layer selected for automated execution; "
                "otherwise applicability deterministically skips that layer",
                "Performance entries require automation.performance.scenario.capability "
                "and automation.performance.scenario.endpoint, an explicit load with positive "
                "concurrency/spawn_rate_per_second/duration_seconds, plus numeric thresholds",
                "Fuzz entries require concrete HTTP method/path endpoints, a named property, and "
                "non-empty oracle expectations",
                "Every trace must contain at least one exact capability leaf supplied by the graph",
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
            if entry.type == "Performance" and entry.automation.performance is not None:
                capability = entry.automation.performance.scenario.capability
                if capability not in leafs:
                    raise ValueError(f"capability key is not a declared typed leaf: {capability}")
        return self


CaseEntry = CaseEntryAuthoring
CaseYaml = CaseYamlAuthoring


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

    schema_version: Literal["1.0"]
    schema_: NonEmptyStr = Field(alias="schema")
    created_at: NonEmptyStr
    change: QaChange
    targets: QaTargets
    approval: QaApproval
    workflow: QaWorkflow | None = None
