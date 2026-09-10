"""Create the stopped SQLite seed used by the managed User benchmark SUT."""

from __future__ import annotations

import ast
import json
import os
import re
import sqlite3
from contextvars import ContextVar
from pathlib import Path
from typing import Any


_DISABLED_PASSWORD = "!managed-runtime-secret-required!"
_REQUEST_EXECUTION_ID: ContextVar[str | None] = ContextVar("aa_sut_execution_id", default=None)


def bind_request_execution_id(execution_id: str | None) -> None:
    """Bind the current request identity so child spans inherit aa.execution_id."""

    _REQUEST_EXECUTION_ID.set(execution_id)


def _migration_sql(migration_file: Path) -> str:
    module = ast.parse(Path(migration_file).read_text(encoding="utf-8"))
    for item in module.body:
        if isinstance(item, ast.AsyncFunctionDef) and item.name == "upgrade":
            for statement in item.body:
                if isinstance(statement, ast.Return) and isinstance(statement.value, ast.Constant):
                    if isinstance(statement.value.value, str):
                        return statement.value.value
    raise RuntimeError("managed migration does not contain a static upgrade SQL payload")


def initialize_database(db_file: Path, migration_file: Path | None = None) -> None:
    """Create only the schema and administrator needed before SUT startup."""
    path = Path(db_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        if migration_file is not None:
            connection.executescript(_migration_sql(migration_file))
        else:
            connection.executescript(
                """
            CREATE TABLE IF NOT EXISTS "user" (
                "id" INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
                "created_at" TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                "updated_at" TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                "username" VARCHAR(20) NOT NULL UNIQUE,
                "alias" VARCHAR(30),
                "email" VARCHAR(255) NOT NULL UNIQUE,
                "phone" VARCHAR(20),
                "password" VARCHAR(128),
                "is_active" INT NOT NULL DEFAULT 1,
                "is_superuser" INT NOT NULL DEFAULT 0,
                "last_login" TIMESTAMP,
                "dept_id" INT
            );
            """
            )
        connection.execute(
            """
            INSERT OR IGNORE INTO "user"
                (username, email, password, is_active, is_superuser, dept_id)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("admin", "admin@benchmark.invalid", _DISABLED_PASSWORD, 1, 1, None),
        )


def install_runtime_password(db_file: Path, password: str) -> None:
    """Replace the disabled seed value inside the locked runtime environment."""
    from argon2 import PasswordHasher

    encoded = PasswordHasher().hash(password)
    with sqlite3.connect(db_file) as connection:
        updated = connection.execute(
            """
            UPDATE "user" SET password = ?
            WHERE username = ? AND email = ? AND password = ? AND is_superuser = 1
            """,
            (encoded, "admin", "admin@benchmark.invalid", _DISABLED_PASSWORD),
        )
        if updated.rowcount != 1:
            raise RuntimeError("managed disabled administrator identity does not match")


_OLD_SYSTEM = ("db.system", "db.statement", "db.operation", "db.sql.table", "db.name")
_NEW_SYSTEM = ("db.system.name", "db.query.text", "db.operation.name", "db.collection.name", "db.namespace")
_SQL_TABLE = re.compile(
    r"""(?ix)^\s*(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM|SELECT\b.+?\bFROM)\s+[`"'[]?([A-Za-z_][\w]*)"""
)
_SQL_OPERATION = re.compile(r"(?i)^\s*(INSERT|UPDATE|DELETE|SELECT|CREATE|ALTER|DROP)\b")
_SENSITIVE = re.compile(r"(?i).*(password|passwd|secret|authorization|token|statement|query\.text).*")


def normalize_db_attributes(attributes: dict[str, Any]) -> dict[str, str]:
    """Map official Tortoise old/new semconv onto a fixed table/operation pair."""
    keys = set(attributes)
    old = any(key in keys for key in _OLD_SYSTEM)
    new = any(key in keys for key in _NEW_SYSTEM)
    if old and not new:
        semconv = "1.11.0"
        statement = str(attributes.get("db.statement") or "")
        table = str(attributes.get("db.sql.table") or "")
        operation = str(attributes.get("db.operation") or "")
    elif new and not old:
        semconv = "1.24.0"
        statement = str(attributes.get("db.query.text") or "")
        table = str(attributes.get("db.collection.name") or "")
        operation = str(attributes.get("db.operation.name") or "")
    else:
        raise ValueError("unknown db semconv")
    if not table:
        matched = _SQL_TABLE.search(statement)
        table = matched.group(1) if matched else ""
    if not operation:
        matched = _SQL_OPERATION.search(statement)
        operation = matched.group(1).upper() if matched else ""
    if not table or not operation:
        raise ValueError("unknown db semconv")
    return {"table": table, "operation": operation.upper(), "semconv": semconv}


class _NormalizeAndScrubProcessor:
    def __init__(self, downstream=None) -> None:  # noqa: ANN001
        self._downstream = downstream

    def on_start(self, span, parent_context=None) -> None:  # noqa: ANN001
        return None

    def on_end(self, span) -> None:  # noqa: ANN001
        attributes = getattr(span, "_attributes", None)
        if attributes is None:
            if self._downstream is not None:
                self._downstream.on_end(span)
            return
        scope = getattr(span, "instrumentation_scope", None)
        name = getattr(scope, "name", "") if scope is not None else ""
        current = dict(attributes)
        failed: ValueError | None = None
        if name == "opentelemetry.instrumentation.tortoiseorm":
            try:
                normalized = normalize_db_attributes(current)
            except ValueError as error:
                failed = error
            else:
                attributes["aa.db.table"] = normalized["table"]
                attributes["aa.db.operation"] = normalized["operation"]
                attributes["aa.db.semconv"] = normalized["semconv"]
        for key in list(attributes):
            if _SENSITIVE.match(str(key)) and not str(key).startswith("aa.db."):
                del attributes[key]
        if failed is not None:
            raise failed
        fault = os.environ.get("AA_SUT_FAULT", "none")
        inherited = _REQUEST_EXECUTION_ID.get()
        if inherited and "aa.execution_id" not in attributes:
            attributes["aa.execution_id"] = inherited
        if fault == "stale-trace":
            attributes["aa.execution_id"] = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        if fault == "drop-business-span":
            route = str(attributes.get("http.route") or getattr(span, "name", "") or "")
            if name == "opentelemetry.instrumentation.fastapi" or route.endswith("/api/v1/user/create"):
                return
            if getattr(span, "name", "") == "user.create.completed":
                return
        if fault == "drop-write-span" and name == "opentelemetry.instrumentation.tortoiseorm":
            if attributes.get("aa.db.table") == "user" and attributes.get("aa.db.operation") == "INSERT":
                return
        if self._downstream is not None:
            self._downstream.on_end(span)

    def shutdown(self) -> None:
        if self._downstream is not None:
            self._downstream.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:  # noqa: ARG002
        if self._downstream is not None:
            return bool(self._downstream.force_flush(timeout_millis))
        return True


def _otlp_span_kind(kind: object) -> int:
    value = int(kind.value) if hasattr(kind, "value") else int(kind)  # type: ignore[arg-type]
    if 0 <= value <= 4:
        return value + 1
    return value


class OTLPFileExporter:
    """Write one ExportTraceServiceRequest-shaped JSON line per completed span."""

    def __init__(self, output_path: str) -> None:
        self._jsonl_path = Path(output_path)
        self._jsonl_path.parent.mkdir(parents=True, exist_ok=True)

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
        resource_attrs = []
        resource = getattr(span, "resource", None)
        attributes = getattr(resource, "attributes", None) if resource is not None else None
        if attributes:
            resource_attrs = self._encode_attrs(dict(attributes))
        if not resource_attrs:
            resource_attrs = self._encode_attrs(
                {
                    "service.name": "user-oracle-sut",
                    "service.instance.id": os.environ.get("AA_SUT_INSTANCE_ID", "unknown"),
                }
            )
        return {
            "resourceSpans": [
                {
                    "resource": {"attributes": resource_attrs},
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
            elif isinstance(value, (list, tuple)):
                encoded.append(
                    {
                        "key": key,
                        "value": {"arrayValue": {"values": [{"stringValue": str(item)} for item in value]}},
                    }
                )
            else:
                encoded.append({"key": key, "value": {"stringValue": str(value)}})
        return encoded


def install_otel():
    """Install official instrumentors and a Demoso-style in-process file exporter."""
    output = os.environ.get("AA_SUT_OTEL_FILE")
    if not output:
        return None
    from opentelemetry import trace
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.sampling import ALWAYS_ON, ALWAYS_OFF, ParentBased

    sampler_name = os.environ.get("AA_SUT_OTEL_SAMPLER", "always_on")
    if sampler_name == "always_on":
        sampler = ParentBased(ALWAYS_ON)
    elif sampler_name == "always_off":
        sampler = ParentBased(ALWAYS_OFF)
    else:
        raise ValueError(f"unsupported OTel sampler: {sampler_name}")
    provider = TracerProvider(
        sampler=sampler,
        resource=Resource.create(
            {
                "service.name": "user-oracle-sut",
                "service.instance.id": os.environ.get("AA_SUT_INSTANCE_ID", "unknown"),
            }
        ),
    )
    provider.add_span_processor(_NormalizeAndScrubProcessor(SimpleSpanProcessor(OTLPFileExporter(output))))
    trace.set_tracer_provider(provider)
    return provider


def instrument_sut(app, provider) -> None:
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.instrumentation.tortoiseorm import TortoiseORMInstrumentor

    def _server_request_hook(span, scope) -> None:  # noqa: ANN001
        fault = os.environ.get("AA_SUT_FAULT", "none")
        if fault == "broken-context":
            bind_request_execution_id(None)
            return
        for key, value in scope.get("headers") or ():
            name = key.decode("latin-1").lower() if isinstance(key, bytes) else str(key).lower()
            if name == "aa-execution-id":
                text = value.decode("latin-1") if isinstance(value, bytes) else str(value)
                if fault == "stale-trace":
                    text = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
                span.set_attribute("aa.execution_id", text)
                bind_request_execution_id(text)
                return

    TortoiseORMInstrumentor().instrument(tracer_provider=provider, capture_parameters=False)
    FastAPIInstrumentor.instrument_app(
        app, tracer_provider=provider, server_request_hook=_server_request_hook
    )

    @app.post("/internal/otel/flush")
    def _flush_otel() -> dict[str, str]:
        from opentelemetry import trace

        current = trace.get_tracer_provider()
        flushed = True
        if hasattr(current, "force_flush"):
            flushed = bool(current.force_flush(timeout_millis=5000))
        receipt = {"state": "flushed" if flushed else "incomplete", "schema_version": "1"}
        path = os.environ.get("AA_SUT_OTEL_FLUSH_RECEIPT")
        if path:
            Path(path).write_text(json.dumps(receipt, sort_keys=True) + "\n", encoding="utf-8")
        return receipt


def flush_and_shutdown() -> dict[str, str]:
    from opentelemetry import trace

    provider = trace.get_tracer_provider()
    flushed = True
    if hasattr(provider, "force_flush"):
        flushed = bool(provider.force_flush(timeout_millis=5000))
    if hasattr(provider, "shutdown"):
        provider.shutdown()
    receipt = {
        "state": "flushed" if flushed else "incomplete",
        "schema_version": "1",
    }
    path = os.environ.get("AA_SUT_OTEL_FLUSH_RECEIPT")
    if path:
        Path(path).write_text(json.dumps(receipt, sort_keys=True) + "\n", encoding="utf-8")
    return receipt


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("db_file", type=Path)
    parser.add_argument("--migration", type=Path)
    parser.add_argument("--password-env")
    arguments = parser.parse_args()
    initialize_database(arguments.db_file, arguments.migration)
    if arguments.password_env:
        import os

        install_runtime_password(arguments.db_file, os.environ[arguments.password_env])
