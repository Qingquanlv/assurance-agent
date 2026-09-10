"""Compatibility surface: telemetry contracts live in assurance-telemetry."""

from __future__ import annotations

from typing import Any

from assurance_execution.contracts.verification import ObservationV1
from assurance_generation.contracts.execution_plan import CaseExecutionPlanV1
from assurance_telemetry.contracts.telemetry import (
    ALLOWED_SUT_IDENTITY_HEADERS,
    TELEMETRY_COMPLETION_NAME,
    TELEMETRY_OTLP_NAME,
    StageCompletionV1,
    TelemetryArchiveV1,
    TelemetryCompletionV1,
    TelemetryScopeV1,
    apply_sut_request_identity,
    check_trace_requirements as _check_trace_requirements,
    parse_otlp_records,
    truncated_trace_observations as _truncated_trace_observations,
)


def check_trace_requirements(
    plan: CaseExecutionPlanV1,
    manifest: Any,
    spans: tuple[dict[str, Any], ...] | list[dict[str, Any]],
    completion: TelemetryCompletionV1,
) -> tuple[ObservationV1, ...]:
    return tuple(
        ObservationV1.model_validate(item.model_dump())
        for item in _check_trace_requirements(plan, manifest, spans, completion)
    )


def truncated_trace_observations(
    plan: CaseExecutionPlanV1,
    execution_id: str,
) -> tuple[ObservationV1, ...]:
    return tuple(
        ObservationV1.model_validate(item.model_dump())
        for item in _truncated_trace_observations(plan, execution_id)
    )


__all__ = [
    "ALLOWED_SUT_IDENTITY_HEADERS",
    "TELEMETRY_COMPLETION_NAME",
    "TELEMETRY_OTLP_NAME",
    "StageCompletionV1",
    "TelemetryArchiveV1",
    "TelemetryCompletionV1",
    "TelemetryScopeV1",
    "apply_sut_request_identity",
    "check_trace_requirements",
    "parse_otlp_records",
    "truncated_trace_observations",
]
