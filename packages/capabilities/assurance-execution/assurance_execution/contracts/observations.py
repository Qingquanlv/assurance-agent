"""Host-side observation and collector documents. The SUT plugin speaks JSON only."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import AwareDatetime, Field, field_validator, model_validator

from graph_engine.plugin_api import FrozenModel

from assurance_execution.contracts.execution import ExecutionFamily
from assurance_generation.contracts.plans import ObligationMethodPlanV1
from assurance_intake.contracts.obligations import VerificationRequirementV1
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1

_SHA256 = r"^[0-9a-f]{64}$"


class ExecutionIdentityV1(FrozenModel):
    plan_digest: str = Field(pattern=_SHA256)
    method_plan_refs: tuple[EvidenceArtifactRefV1, ...]
    mapping_digest: str = Field(pattern=_SHA256)
    batch_id: str = Field(min_length=1)
    baseline_tree_id: str = Field(pattern=_SHA256)
    runner_profile_digest: str = Field(pattern=_SHA256)


class RuntimeObservationV1(FrozenModel):
    observation_id: str = Field(min_length=1)
    observation_key: str = Field(min_length=1)
    mrc_id: str = Field(min_length=1)
    requirement_id: str = Field(min_length=1)
    test_nodeid: str = Field(min_length=1)
    assertion_id: str = Field(min_length=1)
    step_id: str = Field(min_length=1)
    sequence_id: str = Field(min_length=1)
    sequence_index: int = Field(ge=0)
    actual_status: int = Field(ge=100, le=599)
    predicate_passed: bool
    observed_at: AwareDatetime
    prerequisite_refs: tuple[EvidenceArtifactRefV1, ...]


class SubjectBindingV1(FrozenModel):
    kind: Literal["local", "remote"]
    expected_identity: str = Field(min_length=1)
    observed_identity: str | None
    evidence_ref: EvidenceArtifactRefV1 | None
    status: Literal["matched", "mismatch", "unavailable"]


class ObservationBundleV1(FrozenModel):
    schema_version: Literal["1"]
    identity: ExecutionIdentityV1
    subject: SubjectBindingV1
    observations: tuple[RuntimeObservationV1, ...]
    collection_errors: tuple[str, ...]


class ObservationRunContextV1(FrozenModel):
    identity: ExecutionIdentityV1
    requirements: tuple[VerificationRequirementV1, ...]
    method_plans: tuple[ObligationMethodPlanV1, ...]
    allowed_origins: tuple[str, ...]
    timeout_seconds: int = Field(gt=0)
    max_response_bytes: int = Field(gt=0)


class PytestPhaseV1(FrozenModel):
    outcome: Literal["passed", "failed", "skipped"]
    duration: float = Field(ge=0)
    longrepr: str | None

    @field_validator("duration")
    @classmethod
    def _finite(cls, value: float) -> float:
        if value != value or value == float("inf"):
            raise ValueError("duration must be finite")
        return value


class PytestTestV1(FrozenModel):
    nodeid: str = Field(min_length=1)
    outcome: Literal["passed", "failed", "skipped"]
    setup: PytestPhaseV1 | None
    call: PytestPhaseV1 | None
    teardown: PytestPhaseV1 | None


class PytestSummaryV1(FrozenModel):
    collected: int = Field(ge=0)
    passed: int = Field(ge=0)
    failed: int = Field(ge=0)
    skipped: int = Field(ge=0)


class PytestReportV1(FrozenModel):
    summary: PytestSummaryV1
    tests: tuple[PytestTestV1, ...]

    @model_validator(mode="after")
    def _counts_match_tests(self) -> Self:
        nodeids = [item.nodeid for item in self.tests]
        if len(nodeids) != len(set(nodeids)):
            raise ValueError("pytest report nodeids must be unique")
        counts = {"passed": 0, "failed": 0, "skipped": 0}
        for item in self.tests:
            counts[item.outcome] += 1
        if (
            counts["passed"] != self.summary.passed
            or counts["failed"] != self.summary.failed
            or counts["skipped"] != self.summary.skipped
        ):
            raise ValueError("pytest summary counts must match test outcomes")
        return self


class CollectorDocumentV1(FrozenModel):
    protocol_version: Literal["1"]
    collection_token: str = Field(min_length=1)
    identity: ExecutionIdentityV1
    complete: bool
    pytest_exitstatus: int
    collected_nodeids: tuple[str, ...]
    collection_errors: tuple[str, ...]
    report: PytestReportV1
    observations: tuple[RuntimeObservationV1, ...]

    @model_validator(mode="after")
    def _complete_covers_collection(self) -> Self:
        if self.report.summary.collected != len(self.collected_nodeids):
            raise ValueError("collected count must match collected_nodeids")
        if self.complete:
            reported = {item.nodeid for item in self.report.tests}
            if reported != set(self.collected_nodeids) or len(self.report.tests) != self.report.summary.collected:
                raise ValueError("complete collector must cover every collected nodeid")
        return self


class ExecutionEventV1(FrozenModel):
    event_seq: int = Field(ge=0)
    producer: Literal["runner", "pytest", "collector"]
    producer_seq: int = Field(ge=0)
    event: Literal[
        "family_blocked",
        "process_started",
        "process_exited",
        "timed_out",
        "interrupted",
        "test_started",
        "test_finished",
        "request_started",
        "request_finished",
        "request_error",
        "collection_error",
    ]
    family: ExecutionFamily
    timestamp: AwareDatetime
    test_nodeid: str | None = None
    observation_id: str | None = None
    step_id: str | None = None
    phase: Literal["setup", "call", "teardown"] | None = None
    http_method: str | None = None
    route: str | None = None
    exit_code: int | None = None
    status: str | None = None
    duration_ms: float | None = None
