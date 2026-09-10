"""Real Tortoise/SQLite OTel compatibility — no hand-made in-memory spans."""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import uuid
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

import pytest


REPO = Path(__file__).resolve().parents[3]
BENCHMARK = REPO / "benchmark" / "assurance-product"
FIXTURE = BENCHMARK / "fixtures" / "user-oracle"
HARNESS_PATH = BENCHMARK / "user_oracle_harness.py"
CANARY_PASSWORD = "CANARY_otel_secret_7f3a9c2e1b90"
CANARY_TOKEN_HINT = "CANARY_auth_header_should_not_leak"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_harness():
    return _load(HARNESS_PATH, "user_oracle_harness")


def _set_runtime_secrets(monkeypatch: pytest.MonkeyPatch, password: str = "runtime-only") -> None:
    for name in ("AA_SUT_ADMIN_PASSWORD", "AA_SUT_RESET_PASSWORD", "AA_SUT_SECRET_KEY"):
        monkeypatch.setenv(name, password)


def _serve(harness, tmp_path: Path, *, fault: str = "none", otel: bool = True) -> dict[str, Any]:
    project = tmp_path / "project"
    harness.materialize_project(project_dir=project, fault=fault)
    otel_file = tmp_path / "observed.otlp.jsonl" if otel else None
    return harness.serve(project, otel_file=otel_file)


def _json(url: str, *, method: str = "GET", payload: dict[str, Any] | None = None, token: str | None = None):
    data = None if payload is None else json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["token"] = token
    request = Request(url, data=data, headers=headers, method=method)
    with urlopen(request, timeout=5) as response:  # noqa: S310
        return json.loads(response.read().decode())


def _login(base_url: str, password: str) -> str:
    body = _json(
        f"{base_url}/api/v1/base/access_token",
        method="POST",
        payload={"username": "admin", "password": password},
    )
    return str(body["data"]["access_token"])


def _create_user(
    base_url: str, token: str, username: str, *, password: str = CANARY_PASSWORD
) -> dict[str, Any]:
    return _json(
        f"{base_url}/api/v1/user/create",
        method="POST",
        token=token,
        payload={
            "email": f"{username}@example.com",
            "username": username,
            "password": password,
            "is_active": True,
            "is_superuser": False,
            "dept_id": None,
            "role_ids": [],
        },
    )


def test_verify_otel_compatibility_command_is_declared() -> None:
    harness = _load_harness()
    parser = harness._parser()
    names = set(parser._subparsers._group_actions[0].choices)  # type: ignore[attr-defined]
    assert "verify-otel-compatibility" in names


def test_runtime_lock_records_hashed_otel_deps_without_collector() -> None:
    harness = _load_harness()
    locked = harness.verify_runtime_lock(FIXTURE)
    assert "collector.yaml" not in locked["files"]
    assert not (FIXTURE / "collector.yaml").exists()
    lock = json.loads((FIXTURE / "runtime-lock.json").read_text())
    assert "collector" not in lock
    requirements = (FIXTURE / "requirements.in").read_text()
    assert "opentelemetry-sdk" in requirements
    assert "opentelemetry-instrumentation-fastapi" in requirements
    assert "opentelemetry-instrumentation-tortoiseorm" in requirements
    lock_text = (FIXTURE / "requirements.lock").read_text()
    assert "--hash=sha256:" in lock_text
    assert "opentelemetry-sdk" in lock_text
    assert "otelcol" not in lock_text.lower()
    assert "github.com/open-telemetry/opentelemetry-collector-releases" not in json.dumps(lock)


def test_api_db_v1_start_does_not_launch_collector(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    harness = _load_harness()
    _set_runtime_secrets(monkeypatch)
    started = _serve(harness, tmp_path, otel=False)
    try:
        assert started.get("otel_file") is None
        assert "collector" not in started
        assert os.environ.get("AA_SUT_OTEL_ENDPOINT") in {None, ""}
        assert os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") in {None, ""}
    finally:
        harness.stop_served(started["pid"])


def test_real_user_save_emits_fastapi_server_and_sqlite_client_spans(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _load_harness()
    _set_runtime_secrets(monkeypatch)
    started = _serve(harness, tmp_path)
    try:
        token = _login(started["base_url"], "runtime-only")
        created = _create_user(started["base_url"], token, "otel_user")
        assert created.get("code") == 200
        with sqlite3.connect(started["sqlite_path"]) as connection:
            rows = connection.execute(
                'SELECT username FROM "user" WHERE username = ?', ("otel_user",)
            ).fetchall()
        assert rows == [("otel_user",)]
        otlp_path = Path(started["otel_file"])
        flush = harness.flush_otel(otlp_path=otlp_path)
        assert flush["state"] == "flushed"
        assert otlp_path.name in {"observed.otlp.jsonl", "traces.jsonl"}
        records = harness.load_otlp_file(otlp_path)
        spans = harness.flatten_spans(records)
        assert any(
            span["kind"] == "SERVER"
            and "/api/v1/user/create" in str(span.get("name", ""))
            or span["kind"] == "SERVER"
            and any("/api/v1/user/create" in str(value) for value in span["attributes"].values())
            for span in spans
        )
        client = [
            span
            for span in spans
            if span["kind"] == "CLIENT"
            and span["instrumentation"] == "opentelemetry.instrumentation.tortoiseorm"
            and span["normalized"]["table"] == "user"
            and span["normalized"]["operation"] == "INSERT"
        ]
        assert client, [
            {
                "name": span["name"],
                "kind": span["kind"],
                "instrumentation": span["instrumentation"],
                "normalized": span["normalized"],
                "semconv": span["semconv"],
                "keys": sorted(span["attributes"]),
            }
            for span in spans
        ]
        assert all(span["semconv"] in {"1.11.0", "1.24.0"} for span in client)
        completed = [span for span in spans if span["name"] == "user.create.completed"]
        assert len(completed) == 1
        assert completed[0]["attributes"]["user.username"] == "otel_user"
        raw = otlp_path.read_text(encoding="utf-8")
        receipt = json.dumps(started)
        assert CANARY_PASSWORD not in raw
        assert CANARY_PASSWORD not in receipt
        assert "INSERT INTO" not in raw
        assert "db.statement" not in raw
        assert "db.query.text" not in raw
    finally:
        harness.stop_served(started["pid"])


def test_transaction_variant_emits_real_sqlite_client_spans(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _load_harness()
    _set_runtime_secrets(monkeypatch)
    started = _serve(harness, tmp_path, fault="rollback-success")
    try:
        token = _login(started["base_url"], "runtime-only")
        created = _create_user(started["base_url"], token, "tx_user")
        assert created.get("code") == 200
        otlp_path = Path(started["otel_file"])
        harness.flush_otel(otlp_path=otlp_path)
        spans = harness.flatten_spans(harness.load_otlp_file(otlp_path))
        client = [
            span
            for span in spans
            if span["kind"] == "CLIENT"
            and span["instrumentation"] == "opentelemetry.instrumentation.tortoiseorm"
        ]
        assert client
        assert any(span["normalized"]["table"] == "user" for span in client)
    finally:
        harness.stop_served(started["pid"])


def test_injected_traceparent_file_contains_that_trace_server_and_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from assurance_execution.operations.telemetry import driver_trace_headers

    harness = _load_harness()
    _set_runtime_secrets(monkeypatch)
    execution_id = str(uuid.uuid4())
    started = _serve(harness, tmp_path)
    try:
        headers = driver_trace_headers(execution_id)
        assert "traceparent" in headers
        trace_id = headers["traceparent"].split("-")[1]
        token = _login(started["base_url"], "runtime-only")
        payload = {
            "email": "inject_user@example.com",
            "username": "inject_user",
            "password": CANARY_PASSWORD,
            "is_active": True,
            "is_superuser": False,
            "dept_id": None,
            "role_ids": [],
        }
        request = Request(
            f"{started['base_url']}/api/v1/user/create",
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                "token": token,
                "traceparent": headers["traceparent"],
                "aa-execution-id": execution_id,
            },
            method="POST",
        )
        with urlopen(request, timeout=5) as response:  # noqa: S310
            created = json.loads(response.read().decode())
        assert created.get("code") == 200
        harness.flush_otel(otlp_path=Path(started["otel_file"]))
        records = harness.load_otlp_file(Path(started["otel_file"]))
        pulled = [span for span in harness.flatten_spans(records) if span.get("trace_id") == trace_id]
        assert any(
            span["kind"] == "SERVER"
            and (
                "/api/v1/user/create" in str(span.get("name", ""))
                or any("/api/v1/user/create" in str(value) for value in span["attributes"].values())
            )
            for span in pulled
        )
        write = [
            span
            for span in pulled
            if span["kind"] == "CLIENT"
            and span["instrumentation"] == "opentelemetry.instrumentation.tortoiseorm"
            and span["normalized"]["table"] == "user"
            and span["normalized"]["operation"] == "INSERT"
        ]
        assert write, pulled
    finally:
        harness.stop_served(started["pid"])


def test_new_attempt_uses_separate_otel_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    harness = _load_harness()
    _set_runtime_secrets(monkeypatch)
    first = _serve(harness, tmp_path / "a")
    second = _serve(harness, tmp_path / "b")
    try:
        assert first["otel_file"] != second["otel_file"]
        assert first["base_url"] != second["base_url"]
        assert "collector" not in first
        assert "collector" not in second
    finally:
        harness.stop_served(first["pid"])
        harness.stop_served(second["pid"])


def test_unknown_semconv_fails_preflight() -> None:
    harness = _load_harness()
    with pytest.raises(ValueError, match="semconv"):
        harness.normalize_db_span(
            {
                "attributes": {"mystery.db": "sqlite"},
                "instrumentation": "opentelemetry.instrumentation.tortoiseorm",
            }
        )


def test_processor_stamps_request_execution_id_on_child_spans() -> None:
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    bootstrap = _load(FIXTURE / "bootstrap.py", "user_oracle_bootstrap_identity")
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(bootstrap._NormalizeAndScrubProcessor(SimpleSpanProcessor(exporter)))
    execution_id = "11111111-1111-4111-8111-111111111111"
    bootstrap.bind_request_execution_id(execution_id)
    try:
        write = provider.get_tracer("opentelemetry.instrumentation.tortoiseorm")
        done = provider.get_tracer("user.oracle")
        with write.start_as_current_span("user INSERT") as span:
            span.set_attribute("db.system.name", "sqlite")
            span.set_attribute("db.collection.name", "user")
            span.set_attribute("db.operation.name", "INSERT")
        with done.start_as_current_span("user.create.completed") as span:
            span.set_attribute("user.username", "otel_user")
        exported = {item.name: dict(item.attributes or {}) for item in exporter.get_finished_spans()}
        assert exported["user INSERT"]["aa.execution_id"] == execution_id
        assert exported["user INSERT"]["aa.db.table"] == "user"
        assert exported["user INSERT"]["aa.db.operation"] == "INSERT"
        assert exported["user.create.completed"]["aa.execution_id"] == execution_id
    finally:
        bootstrap.bind_request_execution_id(None)
        provider.shutdown()


def test_unknown_semconv_fails_on_live_processor_export_path() -> None:
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    bootstrap = _load(FIXTURE / "bootstrap.py", "user_oracle_bootstrap")
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(bootstrap._NormalizeAndScrubProcessor())
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("opentelemetry.instrumentation.tortoiseorm")
    try:
        with pytest.raises(ValueError, match="semconv"):
            with tracer.start_as_current_span("INSERT") as span:
                span.set_attribute("db.system", "sqlite")
                span.set_attribute("db.system.name", "sqlite")
        exported = exporter.get_finished_spans()
        assert not any((item.attributes or {}).get("aa.db.semconv") == "unknown" for item in exported)
        assert not any(
            item.instrumentation_scope is not None
            and item.instrumentation_scope.name == "opentelemetry.instrumentation.tortoiseorm"
            for item in exported
        )
    finally:
        provider.shutdown()
