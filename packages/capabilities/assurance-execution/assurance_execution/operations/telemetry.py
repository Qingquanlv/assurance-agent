"""Bounded OTLP reads, driver context, and sealed telemetry completion."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from opentelemetry import trace
from opentelemetry.propagate import inject
from opentelemetry.trace import SpanKind, set_span_in_context

from assurance_execution.contracts.telemetry import (
    TELEMETRY_COMPLETION_NAME,
    TELEMETRY_OTLP_NAME,
    TelemetryCompletionV1,
    parse_otlp_records,
)
from assurance_execution.operations.record_publication import publish_record

_MAX_OTLP_BYTES = 12 * 1024 * 1024


def load_otlp_records(
    path: Path, execution_id: str, *, trace_id: str | None = None
) -> tuple[dict[str, Any], ...]:
    """Read a bounded raw OTLP file and return the pulled request chain."""

    if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
        raise ValueError("OTLP export must be a regular single-link file")
    if path.stat().st_size > _MAX_OTLP_BYTES:
        raise ValueError("OTLP export exceeds the bounded read size")
    spans = parse_otlp_records(path.read_bytes())
    if trace_id:
        return tuple(
            span
            for span in spans
            if span.get("trace_id") == trace_id and span.get("execution_id") == execution_id
        )
    return tuple(span for span in spans if span.get("execution_id") == execution_id)


def observed_otlp_path(run_root: Path) -> Path:
    """Return the in-process SUT/driver OTLP JSONL path under the run root."""

    otel = Path(run_root) / "otel"
    observed = otel / "observed.otlp.jsonl"
    if observed.is_file():
        return observed
    traces = otel / "traces.jsonl"
    if traces.is_file():
        return traces
    return observed


_DRIVER_EXPORT_FILE: str | None = None


def _otlp_span_kind(kind: object) -> int:
    value = int(kind.value) if hasattr(kind, "value") else int(kind)  # type: ignore[arg-type]
    # SDK SpanKind is 0=INTERNAL..4=CONSUMER; OTLP JSON is 1=INTERNAL..5=CONSUMER.
    if 0 <= value <= 4:
        return value + 1
    return value


class _OTLPFileExporter:
    def __init__(self, output_path: Path, *, service_name: str, instance_id: str) -> None:
        self._jsonl_path = Path(output_path)
        self._jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        self._service_name = service_name
        self._instance_id = instance_id

    def export(self, spans) -> object:  # noqa: ANN001
        from opentelemetry.sdk.trace.export import SpanExportResult

        lines = []
        for span in spans:
            record = self._span_to_otlp(span)
            if record:
                lines.append(json.dumps(record, ensure_ascii=False))
        if lines:
            with open(self._jsonl_path, "a", encoding="utf-8") as handle:
                handle.write("\n".join(lines) + "\n")
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        return None

    def force_flush(self, timeout_millis: int = 30_000) -> bool:  # noqa: ARG002
        return True

    def _span_to_otlp(self, span) -> dict[str, Any] | None:  # noqa: ANN001
        ctx = span.get_span_context()
        if not ctx or not ctx.is_valid:
            return None
        parent_span_id = format(span.parent.span_id, "016x") if span.parent else ""
        scope_name = ""
        if getattr(span, "instrumentation_scope", None):
            scope_name = span.instrumentation_scope.name or ""
        span_record: dict[str, Any] = {
            "traceId": format(ctx.trace_id, "032x"),
            "spanId": format(ctx.span_id, "016x"),
            "name": span.name,
            "kind": _otlp_span_kind(span.kind),
            "startTimeUnixNano": str(span.start_time or 0),
            "endTimeUnixNano": str(span.end_time or 0),
            "attributes": self._encode_attrs(dict(span.attributes or {})),
            "status": {"code": int(span.status.status_code.value) if span.status else 0},
        }
        if parent_span_id:
            span_record["parentSpanId"] = parent_span_id
        return {
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": self._encode_attrs(
                            {
                                "service.name": self._service_name,
                                "service.instance.id": self._instance_id,
                            }
                        )
                    },
                    "scopeSpans": [{"scope": {"name": scope_name}, "spans": [span_record]}],
                }
            ]
        }

    @staticmethod
    def _encode_attrs(attrs: dict[str, Any]) -> list[dict[str, Any]]:
        encoded: list[dict[str, Any]] = []
        for key, value in attrs.items():
            if isinstance(value, bool):
                encoded.append({"key": key, "value": {"boolValue": value}})
            elif isinstance(value, int):
                encoded.append({"key": key, "value": {"intValue": str(value)}})
            elif isinstance(value, float):
                encoded.append({"key": key, "value": {"doubleValue": value}})
            else:
                encoded.append({"key": key, "value": {"stringValue": str(value)}})
        return encoded


def _ensure_driver_provider(otlp_file: Path | None = None, sut_instance_id: str | None = None) -> None:
    global _DRIVER_EXPORT_FILE
    current = trace.get_tracer_provider()
    add_span_processor = getattr(current, "add_span_processor", None)
    if not callable(add_span_processor):
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider

        attributes = {"service.name": "assurance-execution-driver"}
        if sut_instance_id:
            attributes["service.instance.id"] = sut_instance_id
        current = TracerProvider(resource=Resource.create(attributes))
        trace.set_tracer_provider(current)
        add_span_processor = current.add_span_processor
        _DRIVER_EXPORT_FILE = None
    target = str(otlp_file) if otlp_file is not None else None
    if not target or target == _DRIVER_EXPORT_FILE:
        return
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    add_span_processor(
        SimpleSpanProcessor(
            _OTLPFileExporter(
                Path(target),
                service_name="assurance-execution-driver",
                instance_id=sut_instance_id or "unknown",
            )
        )
    )
    _DRIVER_EXPORT_FILE = target


def driver_trace_headers(
    execution_id: str, *, otlp_file: Path | None = None, sut_instance_id: str | None = None
) -> dict[str, str]:
    """Create a parent CLIENT span and inject W3C context plus the execution identity."""

    _ensure_driver_provider(otlp_file, sut_instance_id)
    tracer = trace.get_tracer("assurance.execution.http-driver")
    span = tracer.start_span("POST /api/v1/user/create", kind=SpanKind.CLIENT)
    span.set_attribute("aa.execution_id", execution_id)
    headers: dict[str, str] = {}
    inject(headers, context=set_span_in_context(span))
    headers["aa-execution-id"] = execution_id
    span.end()
    return headers


def start_driver_client_span(
    execution_id: str,
    url: str,
    *,
    otlp_file: Path | None = None,
    sut_instance_id: str | None = None,
):
    """Open the parent HTTP CLIENT span for one frozen action."""

    _ensure_driver_provider(otlp_file, sut_instance_id)
    tracer = trace.get_tracer("assurance.execution.http-driver")
    span = tracer.start_span("POST /api/v1/user/create", kind=SpanKind.CLIENT)
    span.set_attribute("aa.execution_id", execution_id)
    span.set_attribute("http.method", "POST")
    span.set_attribute("http.url", url)
    headers: dict[str, str] = {}
    inject(headers, context=set_span_in_context(span))
    headers["aa-execution-id"] = execution_id
    return span, headers


def flush_driver_provider() -> dict[str, Any]:
    provider = trace.get_tracer_provider()
    flushed = True
    force_flush = getattr(provider, "force_flush", None)
    if callable(force_flush):
        flushed = bool(force_flush(timeout_millis=5000))
    return {
        "state": "complete" if flushed else "incomplete",
        **({} if flushed else {"reason": "driver_flush_failed"}),
    }


def flush_sut_provider(base_url: str, receipt_dir: Path | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"state": "incomplete", "reason": "sut_flush_failed"}
    try:
        import httpx

        with httpx.Client(follow_redirects=False, timeout=2, trust_env=False) as client:
            response = client.post(base_url.rstrip("/") + "/internal/otel/flush")
            if response.status_code == 200:
                body = response.json()
                if body.get("state") in {"flushed", "complete"}:
                    payload = {"state": "complete"}
    except Exception:
        pass
    if payload.get("state") != "complete" and receipt_dir is not None:
        receipt = Path(receipt_dir) / "flush-receipt.json"
        if not receipt.is_file():
            receipt = Path(receipt_dir) / "otel-flush-receipt.json"
        if receipt.is_file():
            try:
                body = json.loads(receipt.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                body = {}
            if body.get("state") == "flushed":
                payload = {"state": "complete"}
    return payload


def _filter_otlp_by_trace_id(data: bytes, trace_id: str | None) -> bytes:
    if not trace_id:
        return data
    text = data.decode("utf-8")
    kept: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            raise ValueError("OTLP export is truncated or unreadable") from None
        if not isinstance(record, dict):
            raise ValueError("OTLP export is truncated or unreadable")
        filtered = _keep_trace_record(record, trace_id)
        if filtered is not None:
            kept.append(json.dumps(filtered, ensure_ascii=False))
    return (("\n".join(kept) + "\n") if kept else "").encode("utf-8")


def _keep_trace_record(record: dict[str, Any], trace_id: str) -> dict[str, Any] | None:
    resources = []
    for resource in record.get("resourceSpans") or ():
        if not isinstance(resource, dict):
            continue
        scopes = []
        for scope in resource.get("scopeSpans") or ():
            if not isinstance(scope, dict):
                continue
            spans = [
                span
                for span in scope.get("spans") or ()
                if isinstance(span, dict) and span.get("traceId") == trace_id
            ]
            if spans:
                scopes.append({**scope, "spans": spans})
        if scopes:
            resources.append({**resource, "scopeSpans": scopes})
    if not resources:
        return None
    return {**record, "resourceSpans": resources}


def seal_telemetry_artifacts(
    *,
    evidence_root: Path,
    source_otlp: Path,
    execution_id: str,
    sut_instance_id: str,
    driver_flush: dict[str, Any],
    sut_flush: dict[str, Any],
    trace_id: str | None = None,
) -> dict[str, str]:
    """Copy the flushed JSONL (filtered by request trace_id) into the evidence root."""

    if source_otlp.is_symlink() or not source_otlp.is_file() or source_otlp.stat().st_nlink != 1:
        raise ValueError("OTLP export must be a regular single-link file")
    if source_otlp.stat().st_size > _MAX_OTLP_BYTES:
        raise ValueError("OTLP export exceeds the bounded archive size")
    data = _filter_otlp_by_trace_id(source_otlp.read_bytes(), trace_id)
    try:
        parse_otlp_records(data)
    except ValueError as error:
        raise ValueError("OTLP export is truncated or unreadable") from error
    evidence_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination = evidence_root / TELEMETRY_OTLP_NAME
    publish_record(destination, data)
    digest = hashlib.sha256(data).hexdigest()
    complete = driver_flush.get("state") == "complete" and sut_flush.get("state") == "complete" and bool(data)
    archive_state = "complete" if complete else "incomplete"
    archive_reason = None if complete else "telemetry_archive_incomplete"
    document = TelemetryCompletionV1.model_validate(
        {
            "schema_version": "1",
            "execution_id": execution_id,
            "sut_instance_id": sut_instance_id,
            "driver_flush": driver_flush,
            "sut_flush": sut_flush,
            "file_export": {"state": "complete"}
            if complete
            else {"state": "incomplete", "reason": archive_reason},
            "archive": {
                "state": archive_state,
                "reason": archive_reason,
                "path": TELEMETRY_OTLP_NAME,
                "digest": digest,
                "size": len(data),
            },
            "state": "complete" if complete else "incomplete",
        }
    )
    completion_path = evidence_root / TELEMETRY_COMPLETION_NAME
    publish_record(
        completion_path,
        (json.dumps(document.model_dump(mode="json"), indent=2, sort_keys=True) + "\n").encode(),
    )
    return {
        "otlp_path": str(destination),
        "completion_path": str(completion_path),
        "digest": digest,
    }


def seal_incomplete_telemetry(
    *,
    evidence_root: Path,
    execution_id: str,
    sut_instance_id: str,
    driver_flush: dict[str, Any],
    sut_flush: dict[str, Any],
    reason: str,
) -> dict[str, str]:
    """Write a staged incomplete completion when flush or the OTLP file is missing."""

    evidence_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    digest = hashlib.sha256(b"").hexdigest()
    document = TelemetryCompletionV1.model_validate(
        {
            "schema_version": "1",
            "execution_id": execution_id,
            "sut_instance_id": sut_instance_id,
            "driver_flush": driver_flush,
            "sut_flush": sut_flush,
            "file_export": {"state": "incomplete", "reason": reason},
            "archive": {
                "state": "incomplete",
                "reason": reason,
                "path": TELEMETRY_OTLP_NAME,
                "digest": digest,
                "size": 0,
            },
            "state": "incomplete",
        }
    )
    completion_path = evidence_root / TELEMETRY_COMPLETION_NAME
    publish_record(
        completion_path,
        (json.dumps(document.model_dump(mode="json"), indent=2, sort_keys=True) + "\n").encode(),
    )
    return {"completion_path": str(completion_path), "digest": digest}


__all__ = [
    "driver_trace_headers",
    "flush_driver_provider",
    "flush_sut_provider",
    "load_otlp_records",
    "observed_otlp_path",
    "seal_incomplete_telemetry",
    "seal_telemetry_artifacts",
    "start_driver_client_span",
]
