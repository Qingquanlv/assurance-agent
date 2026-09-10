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


def load_otlp_records(path: Path, execution_id: str) -> tuple[dict[str, Any], ...]:
    """Read a bounded raw OTLP file and return normalized spans for one execution."""

    if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
        raise ValueError("OTLP export must be a regular single-link file")
    if path.stat().st_size > _MAX_OTLP_BYTES:
        raise ValueError("OTLP export exceeds the bounded read size")
    spans = parse_otlp_records(path.read_bytes())
    return tuple(span for span in spans if span.get("execution_id") == execution_id)


def collector_otlp_endpoint(run_root: Path) -> str | None:
    """Return the Attempt Collector OTLP HTTP endpoint recorded on the host."""

    receipt_path = Path(run_root) / "otel" / "collector-process.json"
    if not receipt_path.is_file() or receipt_path.is_symlink():
        return None
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(receipt, dict):
        return None
    endpoint = receipt.get("otlp_endpoint")
    return str(endpoint) if isinstance(endpoint, str) and endpoint else None


_DRIVER_EXPORT_ENDPOINT: str | None = None


def _ensure_driver_provider(otlp_endpoint: str | None = None, sut_instance_id: str | None = None) -> None:
    global _DRIVER_EXPORT_ENDPOINT
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
        _DRIVER_EXPORT_ENDPOINT = None
    if not otlp_endpoint or otlp_endpoint == _DRIVER_EXPORT_ENDPOINT:
        return
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    traces = otlp_endpoint.rstrip("/") + "/v1/traces"
    add_span_processor(SimpleSpanProcessor(OTLPSpanExporter(endpoint=traces)))
    _DRIVER_EXPORT_ENDPOINT = otlp_endpoint


def driver_trace_headers(
    execution_id: str, *, otlp_endpoint: str | None = None, sut_instance_id: str | None = None
) -> dict[str, str]:
    """Create a parent CLIENT span and inject W3C context plus the execution identity."""

    _ensure_driver_provider(otlp_endpoint, sut_instance_id)
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
    otlp_endpoint: str | None = None,
    sut_instance_id: str | None = None,
):
    """Open the parent HTTP CLIENT span for one frozen action."""

    _ensure_driver_provider(otlp_endpoint, sut_instance_id)
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
    shutdown = getattr(provider, "shutdown", None)
    if callable(shutdown):
        try:
            shutdown()
        except Exception:  # noqa: BLE001
            flushed = False
    return {
        "state": "complete" if flushed else "incomplete",
        **({} if flushed else {"reason": "driver_flush_failed"}),
    }


def flush_sut_provider(base_url: str, run_root: Path) -> dict[str, Any]:
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
    if payload.get("state") != "complete":
        receipt = Path(run_root) / "otel" / "flush-receipt.json"
        if receipt.is_file():
            try:
                body = json.loads(receipt.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                body = {}
            if body.get("state") == "flushed":
                payload = {"state": "complete"}
    return payload


def drain_owned_collector(run_root: Path, timeout_s: float = 5.0) -> dict[str, Any]:
    import os
    import signal
    import time

    payload: dict[str, Any]
    receipt_path = Path(run_root) / "otel" / "collector-process.json"
    if not receipt_path.is_file():
        return {"state": "incomplete", "reason": "collector_receipt_missing"}
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        pid = int(receipt["pid"])
    except (OSError, KeyError, TypeError, ValueError):
        return {"state": "incomplete", "reason": "collector_receipt_invalid"}
    try:
        os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return {"state": "complete"}
            time.sleep(0.05)
        os.kill(pid, signal.SIGKILL)
        payload = {"state": "timeout", "reason": "drain_timeout"}
    except ProcessLookupError:
        payload = {"state": "complete"}
    except OSError:
        payload = {"state": "incomplete", "reason": "collector_drain_failed"}
    return payload


def seal_telemetry_artifacts(
    *,
    evidence_root: Path,
    source_otlp: Path,
    execution_id: str,
    sut_instance_id: str,
    driver_flush: dict[str, Any],
    sut_flush: dict[str, Any],
    collector_drain: dict[str, Any],
) -> dict[str, str]:
    """Copy the Collector file into the evidence root, then write the staged completion."""

    if source_otlp.is_symlink() or not source_otlp.is_file() or source_otlp.stat().st_nlink != 1:
        raise ValueError("Collector OTLP export must be a regular single-link file")
    if source_otlp.stat().st_size > _MAX_OTLP_BYTES:
        raise ValueError("Collector OTLP export exceeds the bounded archive size")
    data = source_otlp.read_bytes()
    try:
        parse_otlp_records(data)
    except ValueError as error:
        raise ValueError("Collector OTLP export is truncated or unreadable") from error
    evidence_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination = evidence_root / TELEMETRY_OTLP_NAME
    publish_record(destination, data)
    digest = hashlib.sha256(data).hexdigest()
    stages = (driver_flush, sut_flush, collector_drain)
    complete = all(item.get("state") == "complete" for item in stages)
    archive_state = "complete" if complete else "incomplete"
    archive_reason = (
        None if complete else str(collector_drain.get("reason") or "telemetry_archive_incomplete")
    )
    document = TelemetryCompletionV1.model_validate(
        {
            "schema_version": "1",
            "execution_id": execution_id,
            "sut_instance_id": sut_instance_id,
            "driver_flush": driver_flush,
            "sut_flush": sut_flush,
            "collector_drain": collector_drain,
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
    collector_drain: dict[str, Any],
    reason: str,
) -> dict[str, str]:
    """Write a staged incomplete completion when export or drain cannot produce OTLP."""

    evidence_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    digest = hashlib.sha256(b"").hexdigest()
    document = TelemetryCompletionV1.model_validate(
        {
            "schema_version": "1",
            "execution_id": execution_id,
            "sut_instance_id": sut_instance_id,
            "driver_flush": driver_flush,
            "sut_flush": sut_flush,
            "collector_drain": collector_drain,
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
    "collector_otlp_endpoint",
    "drain_owned_collector",
    "driver_trace_headers",
    "flush_driver_provider",
    "flush_sut_provider",
    "load_otlp_records",
    "seal_incomplete_telemetry",
    "seal_telemetry_artifacts",
    "start_driver_client_span",
]
