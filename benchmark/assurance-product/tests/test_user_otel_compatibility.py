"""Real Tortoise/SQLite OTel compatibility — no hand-made in-memory spans."""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import time
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


def test_runtime_lock_records_real_collector_and_hashed_otel_deps() -> None:
    harness = _load_harness()
    locked = harness.verify_runtime_lock(FIXTURE)
    assert (FIXTURE / "collector.yaml").is_file()
    assert "collector.yaml" in locked["files"]
    requirements = (FIXTURE / "requirements.in").read_text()
    assert "opentelemetry-sdk" in requirements
    assert "opentelemetry-instrumentation-fastapi" in requirements
    assert "opentelemetry-instrumentation-tortoiseorm" in requirements
    assert "opentelemetry-exporter-otlp-proto-http" in requirements
    lock_text = (FIXTURE / "requirements.lock").read_text()
    assert "--hash=sha256:" in lock_text
    assert "opentelemetry-sdk" in lock_text
    collector = json.loads((FIXTURE / "runtime-lock.json").read_text())["collector"]
    assert collector["distribution"] == "otelcol-contrib"
    assert collector["version"] != "latest"
    assert str(collector["artifact_digest"]).startswith("sha256:")
    assert "placeholder" not in str(collector).lower()
    assert collector["export_protocol"] == "http/protobuf"
    assert collector["sampler"] == "always_on"
    assert collector["health_extension"] == "health_check"
    assert collector["capture_parameters"] is False


def test_api_db_v1_start_does_not_launch_collector(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    harness = _load_harness()
    workspace = tmp_path / "worktree"
    workspace.mkdir()
    prepared = harness.prepare(
        workspace_root=workspace,
        project_dir=workspace / "project",
        run_root=workspace / "runs" / "api-db",
    )
    _set_runtime_secrets(monkeypatch)
    started = harness.start(
        workspace_root=workspace,
        prepare_receipt=Path(prepared["run_root"]) / "harness-prepare.json",
    )
    try:
        assert "collector" not in started
        assert not (Path(prepared["run_root"]) / "otel").exists()
        assert os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") in {None, ""}
    finally:
        harness.stop(
            workspace_root=workspace,
            receipt_path=Path(prepared["run_root"]) / "owned-process.json",
            instance_id=started["instance_id"],
        )


def test_real_user_save_emits_fastapi_server_and_sqlite_client_spans(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _load_harness()
    workspace = tmp_path / "worktree"
    workspace.mkdir()
    prepared = harness.prepare(
        workspace_root=workspace,
        project_dir=workspace / "project",
        run_root=workspace / "runs" / "normal",
    )
    _set_runtime_secrets(monkeypatch)
    execution_id = str(uuid.uuid4())
    started = harness.start(
        workspace_root=workspace,
        prepare_receipt=Path(prepared["run_root"]) / "harness-prepare.json",
        validation_profile="api_db_trace.v1",
        execution_id=execution_id,
    )
    try:
        token = _login(started["base_url"], "runtime-only")
        created = _create_user(started["base_url"], token, "otel_user")
        assert created.get("code") == 200
        with sqlite3.connect(started["sqlite_path"]) as connection:
            rows = connection.execute(
                'SELECT username FROM "user" WHERE username = ?', ("otel_user",)
            ).fetchall()
        assert rows == [("otel_user",)]
        flush = harness.flush_otel(run_root=Path(prepared["run_root"]))
        assert flush["state"] == "flushed"
        records = harness.load_otlp_file(Path(started["collector"]["otlp_path"]))
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
        raw = Path(started["collector"]["otlp_path"]).read_text(encoding="utf-8")
        receipt = json.dumps(started)
        assert CANARY_PASSWORD not in raw
        assert CANARY_PASSWORD not in receipt
        assert "INSERT INTO" not in raw
        assert "db.statement" not in raw
        assert "db.query.text" not in raw
    finally:
        harness.stop(
            workspace_root=workspace,
            receipt_path=Path(prepared["run_root"]) / "owned-process.json",
            instance_id=started["instance_id"],
        )


def test_transaction_variant_emits_real_sqlite_client_spans(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _load_harness()
    workspace = tmp_path / "worktree"
    workspace.mkdir()
    prepared = harness.prepare(
        workspace_root=workspace,
        project_dir=workspace / "project",
        run_root=workspace / "runs" / "tx",
        fault="rollback-success",
    )
    _set_runtime_secrets(monkeypatch)
    started = harness.start(
        workspace_root=workspace,
        prepare_receipt=Path(prepared["run_root"]) / "harness-prepare.json",
        validation_profile="api_db_trace.v1",
        execution_id=str(uuid.uuid4()),
    )
    try:
        token = _login(started["base_url"], "runtime-only")
        created = _create_user(started["base_url"], token, "tx_user")
        assert created.get("code") == 200
        harness.flush_otel(run_root=Path(prepared["run_root"]))
        spans = harness.flatten_spans(harness.load_otlp_file(Path(started["collector"]["otlp_path"])))
        client = [
            span
            for span in spans
            if span["kind"] == "CLIENT"
            and span["instrumentation"] == "opentelemetry.instrumentation.tortoiseorm"
        ]
        assert client
        assert any(span["normalized"]["table"] == "user" for span in client)
    finally:
        harness.stop(
            workspace_root=workspace,
            receipt_path=Path(prepared["run_root"]) / "owned-process.json",
            instance_id=started["instance_id"],
        )


def test_collector_start_failure_does_not_execute_business_post(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _load_harness()
    workspace = tmp_path / "worktree"
    workspace.mkdir()
    prepared = harness.prepare(
        workspace_root=workspace,
        project_dir=workspace / "project",
        run_root=workspace / "runs" / "fail-collector",
    )
    _set_runtime_secrets(monkeypatch)
    posts: list[str] = []
    original_urlopen = urlopen

    def counting_urlopen(request, *args, **kwargs):
        url = request.full_url if hasattr(request, "full_url") else str(request)
        if "/users/create" in url or "/access_token" in url:
            posts.append(url)
        return original_urlopen(request, *args, **kwargs)

    monkeypatch.setattr(harness, "ensure_collector_artifact", lambda: Path("/missing/otelcol-contrib"))
    with pytest.raises(ValueError, match="Collector"):
        harness.start(
            workspace_root=workspace,
            prepare_receipt=Path(prepared["run_root"]) / "harness-prepare.json",
            validation_profile="api_db_trace.v1",
            execution_id=str(uuid.uuid4()),
        )
    assert posts == []
    assert not (Path(prepared["run_root"]) / "owned-process.json").exists()


def test_incomplete_drain_keeps_diagnostics(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    harness = _load_harness()
    workspace = tmp_path / "worktree"
    workspace.mkdir()
    prepared = harness.prepare(
        workspace_root=workspace,
        project_dir=workspace / "project",
        run_root=workspace / "runs" / "drain",
    )
    _set_runtime_secrets(monkeypatch)
    started = harness.start(
        workspace_root=workspace,
        prepare_receipt=Path(prepared["run_root"]) / "harness-prepare.json",
        validation_profile="api_db_trace.v1",
        execution_id=str(uuid.uuid4()),
    )
    run = Path(prepared["run_root"])
    try:
        token = _login(started["base_url"], "runtime-only")
        _create_user(started["base_url"], token, "drain_user")
        os.kill(started["pid"], 9)
        time.sleep(0.2)
        stopped = harness.stop_collector(run_root=run, drain_timeout_s=0.0)
        assert stopped["drain_state"] == "incomplete"
        assert (run / "otel" / "diagnostics.json").is_file()
        assert Path(started["collector"]["otlp_path"]).exists()
        assert (run / "otel" / "collector.log").exists()
    finally:
        try:
            harness.stop(
                workspace_root=workspace,
                receipt_path=run / "owned-process.json",
                instance_id=started["instance_id"],
            )
        except ValueError:
            pass


def test_same_attempt_recover_does_not_rebuild_collector(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from assurance_execution.operations.user_attempt import recover_user_attempt, start_user_attempt
    from tests.verified_generation_fixture import accepted_verified_execution_input
    from tests.product.test_verified_readiness import Secrets

    project = tmp_path / "project"
    harness = _load_harness()
    selected = harness.materialize_project(project_dir=project)
    root = accepted_verified_execution_input(
        project,
        reviewed_source_path="app/controllers/user.py",
        validation_profile="api_db_trace.v1",
    ).model_copy(update={"verification": None})
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    secrets = Secrets(
        {
            "sut.authority": json.dumps(
                {
                    "kind": "user-invocation-host.v1",
                    "authority_root": str(private),
                    "fault": "none",
                    "frozen_artifact_ref": {
                        "path": ".aa/user-oracle/runtime-lock.json",
                        "digest": selected["frozen_artifact_digest"].removeprefix("sha256:"),
                    },
                }
            ).encode(),
            **{
                handle: b"R4-private-password"
                for handle in (
                    "sut.credential",
                    "managed-sut.admin-password",
                    "managed-sut.reset-password",
                    "managed-sut.secret-key",
                )
            },
        }
    )
    kwargs = dict(
        source_root=REPO,
        workspace_root=project,
        attempt_key=SimpleNamespace(digest="a" * 64),
        invocation_id="inv",
        task_id="a" * 64,
        graph_instance_id="graph",
        node_id="execution.execute",
        authorization_scope_digest="b" * 64,
        secrets=secrets,
        authority_handle="sut.authority",
        credential_handle="sut.credential",
    )
    first = start_user_attempt(root, **kwargs)
    try:
        recovered = recover_user_attempt(
            root,
            workspace_root=project,
            source_root=REPO,
            attempt_key=SimpleNamespace(digest="a" * 64),
            secrets=secrets,
            authority_handle="sut.authority",
        )
        assert recovered is not None
        assert recovered.verification.sut_instance_id == first.verification.sut_instance_id
        assert recovered.collector["otlp_path"] == first.collector["otlp_path"]
        assert recovered.collector["endpoint"] == first.collector["endpoint"]
        assert recovered.collector["pid"] == first.collector["pid"]
    finally:
        first.stop()


def test_new_attempt_uses_new_files_and_dynamic_endpoints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _load_harness()
    workspace = tmp_path / "worktree"
    workspace.mkdir()
    first_prep = harness.prepare(
        workspace_root=workspace,
        project_dir=workspace / "project-a",
        run_root=workspace / "runs" / "a",
    )
    second_prep = harness.prepare(
        workspace_root=workspace,
        project_dir=workspace / "project-b",
        run_root=workspace / "runs" / "b",
    )
    _set_runtime_secrets(monkeypatch)
    first = harness.start(
        workspace_root=workspace,
        prepare_receipt=Path(first_prep["run_root"]) / "harness-prepare.json",
        validation_profile="api_db_trace.v1",
        execution_id=str(uuid.uuid4()),
    )
    second = harness.start(
        workspace_root=workspace,
        prepare_receipt=Path(second_prep["run_root"]) / "harness-prepare.json",
        validation_profile="api_db_trace.v1",
        execution_id=str(uuid.uuid4()),
    )
    try:
        assert first["collector"]["otlp_path"] != second["collector"]["otlp_path"]
        assert first["collector"]["otlp_endpoint"] != second["collector"]["otlp_endpoint"]
        assert first["collector"]["health_endpoint"] != second["collector"]["health_endpoint"]
        assert first["base_url"] != second["base_url"]
        locked = json.loads((FIXTURE / "runtime-lock.json").read_text())["collector"]
        assert first["collector"]["sampler"] == locked["sampler"]
        assert second["collector"]["export_protocol"] == locked["export_protocol"]
    finally:
        harness.stop(
            workspace_root=workspace,
            receipt_path=Path(first_prep["run_root"]) / "owned-process.json",
            instance_id=first["instance_id"],
        )
        harness.stop(
            workspace_root=workspace,
            receipt_path=Path(second_prep["run_root"]) / "owned-process.json",
            instance_id=second["instance_id"],
        )


def test_unknown_semconv_fails_preflight() -> None:
    harness = _load_harness()
    with pytest.raises(ValueError, match="semconv"):
        harness.normalize_db_span(
            {
                "attributes": {"mystery.db": "sqlite"},
                "instrumentation": "opentelemetry.instrumentation.tortoiseorm",
            }
        )


def test_collector_health_body_matches_probe_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from assurance_execution.operations.readiness import authenticate_collector_readiness
    from assurance_execution.contracts.readiness import CollectorReadinessReceiptV1

    harness = _load_harness()
    workspace = tmp_path / "worktree"
    workspace.mkdir()
    prepared = harness.prepare(
        workspace_root=workspace,
        project_dir=workspace / "project",
        run_root=workspace / "runs" / "health",
    )
    _set_runtime_secrets(monkeypatch)
    execution_id = "00000000-0000-4000-8000-000000000099"
    started = harness.start(
        workspace_root=workspace,
        prepare_receipt=Path(prepared["run_root"]) / "harness-prepare.json",
        validation_profile="api_db_trace.v1",
        execution_id=execution_id,
    )
    try:
        receipt = CollectorReadinessReceiptV1.model_validate(started["collector"]["readiness"])
        authenticate_collector_readiness(
            receipt,
            sut_instance_id=started["instance_id"],
            execution_id=execution_id,
            configuration_digest=receipt.configuration_digest,
            authorization_scope_digest=receipt.authorization_scope_digest,
            activity_receipt_digest=receipt.activity_receipt_digest,
        )
        with urlopen(receipt.collector_endpoint, timeout=2) as response:  # noqa: S310
            body = json.loads(response.read().decode())
        assert body == {"probe_nonce": receipt.probe_nonce, "execution_id": execution_id}
    finally:
        harness.stop(
            workspace_root=workspace,
            receipt_path=Path(prepared["run_root"]) / "owned-process.json",
            instance_id=started["instance_id"],
        )
