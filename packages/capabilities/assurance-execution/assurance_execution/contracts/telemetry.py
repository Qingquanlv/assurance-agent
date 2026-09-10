"""Pure OTLP normalize/correlate rules shared by producer and assessment replay."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Literal, Self

from pydantic import Field, model_validator

from graph_engine.plugin_api import FrozenModel

from assurance_execution.contracts.verification import ObservationV1, VerificationManifestV1
from assurance_generation.contracts.execution_plan import CaseExecutionPlanV1, TraceRequirementsV1
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1

_SHA256 = r"^[0-9a-f]{64}$"
_KIND_NAMES = {1: "INTERNAL", 2: "SERVER", 3: "CLIENT", 4: "PRODUCER", 5: "CONSUMER"}
_STATUS_NAMES = {0: "UNSET", 1: "OK", 2: "ERROR"}
_USER_WRITE_SCOPE = "opentelemetry.instrumentation.tortoiseorm"
_HTTP_SCOPE = "opentelemetry.instrumentation.fastapi"
_ORACLE_SCOPE = "assurance.execution.oracle"
_ORACLE_SERVICE = "oracle"
ALLOWED_SUT_IDENTITY_HEADERS = frozenset({"aa-execution-id"})
TELEMETRY_OTLP_NAME = "telemetry.otlp.jsonl"
TELEMETRY_COMPLETION_NAME = "telemetry-completion.json"


class StageCompletionV1(FrozenModel):
    state: Literal["complete", "error", "timeout", "incomplete"]
    reason: str | None = Field(default=None, min_length=1)
    receipt_ref: EvidenceArtifactRefV1 | None = None

    @model_validator(mode="after")
    def _reason_matches_state(self) -> Self:
        if (self.reason is None) != (self.state == "complete"):
            raise ValueError("stage errors require a reason and complete stages must not carry one")
        return self


class TelemetryArchiveV1(FrozenModel):
    state: Literal["complete", "error", "incomplete"]
    reason: str | None = Field(default=None, min_length=1)
    path: Literal["telemetry.otlp.jsonl"]
    digest: str = Field(pattern=_SHA256)
    size: int = Field(ge=0)

    @model_validator(mode="after")
    def _reason_matches_state(self) -> Self:
        if (self.reason is None) != (self.state == "complete"):
            raise ValueError("archive errors require a reason and complete archives must not carry one")
        return self


class TelemetryCompletionV1(FrozenModel):
    schema_version: Literal["1"] = "1"
    execution_id: str
    sut_instance_id: str
    driver_flush: StageCompletionV1
    sut_flush: StageCompletionV1
    collector_drain: StageCompletionV1
    archive: TelemetryArchiveV1
    state: Literal["complete", "incomplete"]

    @model_validator(mode="after")
    def _closed_completion(self) -> Self:
        stages_ok = (
            self.driver_flush.state == "complete"
            and self.sut_flush.state == "complete"
            and self.collector_drain.state == "complete"
            and self.archive.state == "complete"
        )
        if self.state == "complete" and not stages_ok:
            raise ValueError("complete telemetry requires every staged flush, drain, and archive")
        if self.state == "incomplete" and stages_ok:
            raise ValueError("incomplete telemetry cannot report every stage complete")
        return self


def _attr_value(raw: object) -> object:
    if not isinstance(raw, Mapping):
        return raw
    if "stringValue" in raw:
        return raw["stringValue"]
    if "boolValue" in raw:
        return raw["boolValue"]
    if "intValue" in raw:
        try:
            return int(raw["intValue"])
        except (TypeError, ValueError):
            return raw["intValue"]
    if "doubleValue" in raw:
        return raw["doubleValue"]
    if "arrayValue" in raw or "kvlistValue" in raw or "bytesValue" in raw:
        return raw
    return next(iter(raw.values()), None) if raw else None


def _resource_attributes(resource: Mapping[str, Any]) -> dict[str, object]:
    attributes: dict[str, object] = {}
    for item in resource.get("attributes") or ():
        if isinstance(item, Mapping) and "key" in item:
            attributes[str(item["key"])] = _attr_value(item.get("value"))
    return attributes


def _normalize_span(
    span: Mapping[str, Any],
    *,
    instrumentation: str,
    service_name: str,
    service_instance_id: str,
) -> dict[str, Any]:
    attributes = {
        str(item["key"]): _attr_value(item.get("value"))
        for item in span.get("attributes") or ()
        if isinstance(item, Mapping) and "key" in item
    }
    status = span.get("status") if isinstance(span.get("status"), Mapping) else {}
    kind = span.get("kind")
    return {
        "trace_id": str(span.get("traceId") or ""),
        "span_id": str(span.get("spanId") or ""),
        "parent_span_id": str(span.get("parentSpanId") or ""),
        "name": str(span.get("name") or ""),
        "kind": _KIND_NAMES.get(kind, kind if isinstance(kind, str) else ""),
        "status": _STATUS_NAMES.get(status.get("code"), "UNSET") if isinstance(status, Mapping) else "UNSET",
        "instrumentation": instrumentation,
        "service_name": service_name,
        "service_instance_id": service_instance_id,
        "role": attributes.get("aa.role"),
        "execution_id": attributes.get("aa.execution_id"),
        "attributes": attributes,
    }


def _identity_payload(span: Mapping[str, Any]) -> str:
    return json.dumps(span, sort_keys=True, separators=(",", ":"), default=str)


def parse_otlp_records(raw: bytes | str) -> tuple[dict[str, Any], ...]:
    """Flatten OTLP JSON/JSONL and reject conflicting duplicate span identities."""

    text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
    if not text.strip():
        return ()
    records: list[dict[str, Any]] = []
    try:
        loaded = json.loads(text)
    except json.JSONDecodeError:
        loaded = None
    if isinstance(loaded, dict):
        records.append(loaded)
    elif loaded is not None:
        raise ValueError("truncated OTLP export")
    else:
        decoder = json.JSONDecoder()
        index = 0
        while index < len(text):
            while index < len(text) and text[index].isspace():
                index += 1
            if index >= len(text):
                break
            try:
                value, index = decoder.raw_decode(text, index)
            except json.JSONDecodeError as error:
                raise ValueError("truncated OTLP export") from error
            if not isinstance(value, dict):
                raise ValueError("truncated OTLP export")
            records.append(value)
    spans: list[dict[str, Any]] = []
    seen: dict[tuple[str, str], str] = {}
    for record in records:
        resources = record.get("resourceSpans") or ([record] if "scopeSpans" in record else [])
        if not isinstance(resources, list):
            raise ValueError("truncated OTLP export")
        for resource in resources:
            if not isinstance(resource, Mapping):
                raise ValueError("truncated OTLP export")
            resource_attrs = _resource_attributes(resource.get("resource") or {})
            service = str(resource_attrs.get("service.name") or "")
            instance = str(resource_attrs.get("service.instance.id") or "")
            for scope in resource.get("scopeSpans") or ():
                if not isinstance(scope, Mapping):
                    raise ValueError("truncated OTLP export")
                instrumentation = str((scope.get("scope") or {}).get("name") or "")
                for span in scope.get("spans") or ():
                    if not isinstance(span, Mapping):
                        raise ValueError("truncated OTLP export")
                    normalized = _normalize_span(
                        span,
                        instrumentation=instrumentation,
                        service_name=service,
                        service_instance_id=instance,
                    )
                    if not normalized["trace_id"] or not normalized["span_id"]:
                        raise ValueError("OTLP span is missing required identity fields")
                    key = (normalized["trace_id"], normalized["span_id"])
                    fingerprint = _identity_payload(normalized)
                    previous = seen.get(key)
                    if previous is None:
                        seen[key] = fingerprint
                        spans.append(normalized)
                    elif previous != fingerprint:
                        raise ValueError("conflicting duplicate trace_id/span_id")
    return tuple(spans)


def apply_sut_request_identity(span: Any, headers: Mapping[str, str]) -> None:
    """Copy only the frozen test-identity header onto a SUT server span."""

    allowed = {
        key.lower(): value for key, value in headers.items() if key.lower() in ALLOWED_SUT_IDENTITY_HEADERS
    }
    execution_id = allowed.get("aa-execution-id")
    if execution_id:
        span.set_attribute("aa.execution_id", execution_id)


def _ancestors(span: Mapping[str, Any], by_id: Mapping[str, Mapping[str, Any]]) -> set[str]:
    seen: set[str] = set()
    current = str(span.get("parent_span_id") or "")
    while current and current not in seen:
        seen.add(current)
        parent = by_id.get(current)
        if parent is None:
            break
        current = str(parent.get("parent_span_id") or "")
    return seen


def _same_trace_connected(
    child: Mapping[str, Any], ancestor: Mapping[str, Any], by_id: Mapping[str, Mapping[str, Any]]
) -> bool:
    if child.get("trace_id") != ancestor.get("trace_id"):
        return False
    if child.get("span_id") == ancestor.get("span_id"):
        return True
    return ancestor.get("span_id") in _ancestors(child, by_id)


def _is_oracle(span: Mapping[str, Any]) -> bool:
    role = str(span.get("role") or (span.get("attributes") or {}).get("aa.role") or "").lower()
    return (
        role == "oracle"
        or str(span.get("instrumentation") or "") == _ORACLE_SCOPE
        or str(span.get("service_name") or "") == _ORACLE_SERVICE
    )


def _bound_spans(
    spans: tuple[dict[str, Any], ...],
    manifest: VerificationManifestV1,
) -> tuple[dict[str, Any], ...]:
    return tuple(
        span
        for span in spans
        if span.get("execution_id") == manifest.execution_id
        and span.get("service_instance_id") == manifest.sut.instance_id
    )


def _http_create(span: Mapping[str, Any]) -> bool:
    attributes = span.get("attributes") or {}
    haystack = " ".join(
        str(part)
        for part in (
            span.get("name"),
            attributes.get("http.route"),
            attributes.get("http.url"),
            attributes.get("http.target"),
        )
        if part
    )
    return "/api/v1/user/create" in haystack


def _observation(
    *,
    execution_id: str,
    obligation_id: str,
    state: Literal["observed", "missing"],
    evidence_ref: EvidenceArtifactRefV1 | None,
    actual: bool | None = None,
    reason: str | None = None,
) -> ObservationV1:
    return ObservationV1(
        execution_id=execution_id,
        obligation_id=obligation_id,
        state=state,
        actual=actual if state == "observed" else None,
        evidence_ref=evidence_ref if state == "observed" else None,
        reason=None if state == "observed" else reason,
    )


def check_trace_requirements(
    plan: CaseExecutionPlanV1,
    manifest: VerificationManifestV1,
    spans: tuple[dict[str, Any], ...] | list[dict[str, Any]],
    completion: TelemetryCompletionV1,
) -> tuple[ObservationV1, ...]:
    """Normalize correlated User-trace facts. Business expected comparison stays in quality."""

    if plan.validation_profile != "api_db_trace.v1" or not isinstance(plan.trace, TraceRequirementsV1):
        return ()
    evidence_ref = EvidenceArtifactRefV1(
        path=f"{manifest.evidence_root}/{TELEMETRY_OTLP_NAME}",
        digest=completion.archive.digest,
    )
    bound = _bound_spans(tuple(spans), manifest)
    by_id = {str(span["span_id"]): span for span in bound if span.get("span_id")}
    driver = next(
        (
            span
            for span in bound
            if span.get("kind") == "CLIENT"
            and span.get("instrumentation") == "assurance.execution.http-driver"
            and _http_create(span)
        ),
        None,
    )
    server = next(
        (
            span
            for span in bound
            if span.get("kind") == "SERVER"
            and span.get("instrumentation") == _HTTP_SCOPE
            and _http_create(span)
            and driver is not None
            and _same_trace_connected(span, driver, by_id)
        ),
        None,
    )
    write = next(
        (
            span
            for span in bound
            if span.get("kind") == "CLIENT"
            and span.get("instrumentation") == _USER_WRITE_SCOPE
            and not _is_oracle(span)
            and (span.get("attributes") or {}).get("aa.db.table") == "user"
            and str((span.get("attributes") or {}).get("aa.db.operation") or "").upper() == "INSERT"
            and span.get("status") in {"UNSET", "OK"}
            and server is not None
            and _same_trace_connected(span, server, by_id)
        ),
        None,
    )
    completed = next(
        (
            span
            for span in bound
            if span.get("name") == plan.trace.checkpoint_id
            and (span.get("attributes") or {}).get("user.username") == plan.inputs["username"]
            and server is not None
            and _same_trace_connected(span, server, by_id)
        ),
        None,
    )
    drained = (
        completion.state == "complete"
        and completion.execution_id == manifest.execution_id
        and completion.sut_instance_id == manifest.sut.instance_id
        and completion.collector_drain.state == "complete"
        and completion.archive.state == "complete"
        and completion.archive.size > 0
    )
    facts = (
        ("trace.http", server is not None, "trace_http_missing"),
        ("trace.user_write", write is not None, "trace_user_write_missing"),
        ("trace.user_completed", completed is not None, "trace_user_completed_missing"),
        ("trace.drained", drained, "trace_drain_incomplete"),
    )
    required = set(plan.required)
    return tuple(
        _observation(
            execution_id=manifest.execution_id,
            obligation_id=obligation_id,
            state="observed" if holds else "missing",
            evidence_ref=evidence_ref,
            actual=True if holds else None,
            reason=None if holds else reason,
        )
        for obligation_id, holds, reason in facts
        if obligation_id in required
    )


__all__ = [
    "ALLOWED_SUT_IDENTITY_HEADERS",
    "TELEMETRY_COMPLETION_NAME",
    "TELEMETRY_OTLP_NAME",
    "StageCompletionV1",
    "TelemetryArchiveV1",
    "TelemetryCompletionV1",
    "apply_sut_request_identity",
    "check_trace_requirements",
    "parse_otlp_records",
]
