"""Per-target execution result models (CLI-internal `free`-grade artifacts).

These are written to execution/runs/<batch-id>/*-result.json and the top-level
latest pointers. They are NOT in the M2 artifact registry: the registry only
owns cross-consumer contracts (manifest, failure-analysis, quality-gate, report).
Reuses M2 shared types so coverage/performance shapes stay consistent.

``property_tests`` is the §5.1 third identity bucket: executed tests that carry
``@pytest.mark.property(...)`` and no case_id. They are mutually exclusive with
``cases`` and ``unmapped_tests``.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from assurance_agent.artifacts.models import (
    CoverageThreshold,
    GateStatus,
    PerformanceScenarioVerdict,
)

ExecutionStatus = Literal["passed", "failed", "skipped"]
PytestTarget = Literal["api", "e2e", "fuzz"]


class CaseResult(BaseModel):
    case_id: str
    status: ExecutionStatus
    file: str
    test_name: str
    duration_ms: int
    message: str
    raw_log_ref: str = ""
    trace: str = ""
    screenshot: str = ""
    video: str = ""


class PropertyTestResult(BaseModel):
    """One executed property test (design §5.1).

    Carries outcome and batch_id so A2 aliveness can be judged without re-reading
    the raw pytest report. ``constraint_keys`` is the marker's closed-key args.
    """

    nodeid: str
    file: str
    constraint_keys: tuple[str, ...] = ()
    outcome: ExecutionStatus
    batch_id: str


class ResultSource(BaseModel):
    model_config = ConfigDict(extra="allow")

    framework: str
    raw_log: str
    report_json: str = ""


class TargetResult(BaseModel):
    schema_version: Literal["1.0"] = "1.0"
    change_id: str
    batch_id: str
    target: PytestTarget
    status: ExecutionStatus
    command: str
    source: ResultSource
    total: int
    passed: int
    failed: int
    skipped: int
    cases: list[CaseResult]
    unmapped_tests: list[CaseResult]
    property_tests: list[PropertyTestResult] = Field(default_factory=list)


class CoverageResult(BaseModel):
    schema_version: Literal["1.0"] = "1.0"
    change_id: str
    batch_id: str
    kind: Literal["coverage"] = "coverage"
    available: bool
    line_coverage: float
    branch_coverage: float
    threshold: CoverageThreshold
    status: GateStatus
    skip_reason: str = ""
    uncovered_critical_files: list[dict] = Field(default_factory=list)
    source: dict[str, str] = Field(default_factory=dict)


class PerformanceResult(BaseModel):
    schema_version: Literal["1.0"] = "1.0"
    change_id: str
    batch_id: str
    kind: Literal["performance"] = "performance"
    available: bool
    status: Literal["PASS", "FAIL", "SKIPPED"]
    scenarios: list[PerformanceScenarioVerdict]
    command: str = ""
    source: dict[str, str] = Field(default_factory=dict)
