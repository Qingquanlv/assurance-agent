"""Readiness checks use real owned processes; test listeners do not qualify OTel."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


class Secrets:
    def __init__(self, values):
        self.values = values

    def resolve(self, handle):
        return self.values[handle]


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@pytest.fixture(scope="module")
def live_sut(tmp_path_factory):
    from assurance_execution.operations.managed_sut import ManagedUserSutHost
    from assurance_execution.operations.verification_manifest import build_managed_sut_authority

    workspace = tmp_path_factory.mktemp("readiness-sut")
    source = Path(__file__).resolve().parents[2]
    secrets = Secrets(
        {
            key: b"readiness-test-only"
            for key in ("managed-sut.admin-password", "managed-sut.reset-password", "managed-sut.secret-key")
        }
    )
    host = ManagedUserSutHost(source_root=source, secret_port=secrets)
    frozen = host._call("freeze", ["--project-dir", str(workspace / "project")])
    workspace = workspace / "project"
    from tests.verified_generation_fixture import accepted_verified_execution_input

    accepted = accepted_verified_execution_input(workspace, reviewed_source_path="app/controllers/user.py")
    assert accepted.verification is not None
    prepared = host.prepare(
        workspace_root=workspace,
        project_dir=workspace / "attempt-source",
        run_root=workspace / "run",
        frozen_artifact=workspace / ".aa/user-oracle",
        frozen_artifact_digest=frozen["frozen_artifact_digest"],
    )
    started = host.start(workspace_root=workspace, prepare_receipt=workspace / "run/harness-prepare.json")
    token = workspace / "run/.ownership-token"
    details = token.stat()
    authority = build_managed_sut_authority(
        run_root=workspace / "run",
        ownership_token_path=token,
        ownership_token_device=details.st_dev,
        ownership_token_inode=details.st_ino,
        ownership_token_digest="sha256:" + digest(token),
        prepare_receipt_digest=digest(workspace / "run/harness-prepare.json"),
        start_receipt_digest=digest(workspace / "run/owned-process.json"),
        authorization_scope_digest="a" * 64,
        activity_receipt_digest="b" * 64,
    )
    secrets.values["sut.authority"] = authority.model_dump_json().encode()
    selection = {
        "workspace_root": str(workspace),
        "configuration_digest": "c" * 64,
        "execution_id": "00000000-0000-4000-8000-000000000001",
        "authorization_scope_digest": "a" * 64,
        "activity_receipt_digest": "b" * 64,
        "verification": {
            "validation_profile": "api_db.v1",
            "case_execution_plan_ref": accepted.verification.case_execution_plan_ref.model_dump(mode="json"),
            "nodeid": "tests/test_case.py::test_case",
            "business_activation": {"kind": "trigger", "value": "execution"},
            "sut_instance_id": started["instance_id"],
            "sut_base_url": started["base_url"],
            "managed_sqlite_path": prepared["sqlite_path"],
            "observer_sqlite_path": prepared["sqlite_path"],
            "user_inputs": {"username": "readiness", "email": "readiness@example.test"},
            "managed_sut_prepare_receipt_ref": {
                "path": "run/harness-prepare.json",
                "digest": authority.prepare_receipt_digest,
            },
            "managed_sut_start_receipt_ref": {
                "path": "run/owned-process.json",
                "digest": authority.start_receipt_digest,
            },
            "managed_sut_authority_handle": "sut.authority",
        },
    }
    try:
        yield host, secrets, selection, started
    finally:
        if not (workspace / "run/stopped-process.json").exists():
            host.stop(
                workspace_root=workspace,
                receipt_path=workspace / "run/owned-process.json",
                instance_id=started["instance_id"],
            )


def test_current_managed_sut_readiness_is_read_only(live_sut):
    from assurance_execution.contracts.readiness import ManagedSutReadinessSelectionV1
    from assurance_execution.operations.managed_sut import authenticate_managed_sut_readiness

    host, secrets, document, _ = live_sut
    selection = ManagedSutReadinessSelectionV1.model_validate(document)
    workspace = Path(selection.workspace_root)
    paths = [
        workspace / "run/harness-prepare.json",
        workspace / "run/owned-process.json",
        workspace / "run/.ownership-token",
    ]
    before = [p.read_bytes() for p in paths]
    authenticate_managed_sut_readiness(selection, source_root=host.source_root, secret_port=secrets)
    assert before == [p.read_bytes() for p in paths]


@pytest.mark.parametrize(
    "field,value",
    [
        ("sut_instance_id", "wrong"),
        ("sut_base_url", "http://127.0.0.1:1"),
        ("managed_sqlite_path", "/tmp/wrong.sqlite3"),
    ],
)
def test_managed_readiness_rejects_wrong_selection(live_sut, field, value):
    from assurance_execution.contracts.readiness import ManagedSutReadinessSelectionV1
    from assurance_execution.operations.managed_sut import authenticate_managed_sut_readiness

    host, secrets, document, _ = live_sut
    changed = {**document, "verification": {**document["verification"], field: value}}
    with pytest.raises(ValueError):
        authenticate_managed_sut_readiness(
            ManagedSutReadinessSelectionV1.model_validate(changed),
            source_root=host.source_root,
            secret_port=secrets,
        )


@pytest.mark.parametrize("name", ["harness-prepare.json", "owned-process.json", "sut/app/settings/config.py"])
def test_managed_readiness_rejects_tampered_receipts_or_runtime(live_sut, name):
    from assurance_execution.contracts.readiness import ManagedSutReadinessSelectionV1
    from assurance_execution.operations.managed_sut import authenticate_managed_sut_readiness

    host, secrets, document, _ = live_sut
    path = Path(document["workspace_root"]) / "run" / name
    if not path.exists():
        path = next((Path(document["workspace_root"]) / "run/sut/app").rglob("*.py"))
    original = path.read_bytes()
    original_mode = path.stat().st_mode & 0o777
    path.chmod(0o644)
    try:
        path.write_bytes(original + b"\n# changed")
        with pytest.raises(ValueError):
            authenticate_managed_sut_readiness(
                ManagedSutReadinessSelectionV1.model_validate(document),
                source_root=host.source_root,
                secret_port=secrets,
            )
    finally:
        path.write_bytes(original)
        path.chmod(original_mode)


def test_trace_boolean_is_not_a_readiness_receipt():
    from assurance_execution.contracts.readiness import CollectorReadinessReceiptV1

    with pytest.raises(ValueError):
        CollectorReadinessReceiptV1.model_validate({"collector_ready": True, "otel_ready": True})


def collector_listener(tmp_path):
    import subprocess
    import sys
    import time
    from assurance_execution.operations.readiness import process_birth_identity

    # A real test-only readiness endpoint. It makes no OTel protocol/compatibility claim.
    body = json.dumps(
        {"probe_nonce": "7" * 64, "execution_id": "00000000-0000-4000-8000-000000000001"}
    ).encode()
    response_file = tmp_path / "response.json"
    response_file.write_bytes(body)
    script = tmp_path / "listener.py"
    script.write_text(
        "from http.server import HTTPServer, BaseHTTPRequestHandler\nfrom pathlib import Path\nclass Handler(BaseHTTPRequestHandler):\n def do_GET(self):\n  self.send_response(200); self.end_headers(); self.wfile.write("
        + "Path("
        + repr(str(response_file))
        + ").read_bytes()"
        + ")\n def log_message(self, *args): pass\ns = HTTPServer(('127.0.0.1', 0), Handler)\nPath("
        + repr(str(tmp_path / "port"))
        + ").write_text(str(s.server_port))\ns.serve_forever()\n"
    )
    process = subprocess.Popen([sys.executable, "-B", str(script)])
    try:
        deadline = time.monotonic() + 5
        while not (tmp_path / "port").exists():
            if time.monotonic() > deadline:
                raise RuntimeError("test listener failed to start")
            time.sleep(0.01)
        config = tmp_path / "collector-config.yaml"
        config.write_text("test-listener-only: true\n")
        dependency = tmp_path / "otel-dependencies.lock"
        dependency.write_text("test listener; does not establish OTel compatibility\n")
        qualification = tmp_path / "otel-qualification.json"
        qualification.write_text("test-only authenticated receipt fixture\n")
        now = datetime.now(timezone.utc)
        receipt = {
            "schema_version": "1",
            "validation_profile": "api_db_trace.v1",
            "sut_instance_id": "sut",
            "execution_id": "00000000-0000-4000-8000-000000000001",
            "configuration_digest": "c" * 64,
            "authorization_scope_digest": "a" * 64,
            "activity_receipt_digest": "b" * 64,
            "collector_endpoint": "http://127.0.0.1:" + (tmp_path / "port").read_text() + "/ready",
            "collector_pid": process.pid,
            "collector_process_birth_identity": process_birth_identity(process.pid),
            "collector_artifact": {"path": str(script), "digest": digest(script)},
            "collector_config": {"path": str(config), "digest": digest(config)},
            "otel_dependencies": {"path": str(dependency), "digest": digest(dependency)},
            "otel_qualification": {"path": str(qualification), "digest": digest(qualification)},
            "issued_at": now.isoformat(),
            "checked_at": now.isoformat(),
            "expires_at": (now + timedelta(seconds=30)).isoformat(),
            "probe_nonce": "7" * 64,
            "endpoint_response_digest": hashlib.sha256(body).hexdigest(),
        }
        yield receipt, process
    finally:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=5)


@pytest.fixture
def collector_receipt(tmp_path):
    yield from collector_listener(tmp_path)


@pytest.mark.parametrize(
    "drift",
    [
        None,
        "stale",
        "stopped",
        "birth",
        "instance",
        "execution",
        "config",
        "authorization",
        "artifact",
        "dependency",
        "qualification",
    ],
)
def test_collector_readiness_requires_current_bound_receipt(collector_receipt, drift):
    from assurance_execution.contracts.readiness import CollectorReadinessReceiptV1
    from assurance_execution.operations.readiness import authenticate_collector_readiness

    document, process = collector_receipt
    expected = {
        key: document[key]
        for key in (
            "sut_instance_id",
            "execution_id",
            "configuration_digest",
            "authorization_scope_digest",
            "activity_receipt_digest",
        )
    }
    if drift == "stale":
        document["expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    elif drift == "stopped":
        process.terminate()
        process.wait(timeout=5)
    elif drift == "birth":
        document["collector_process_birth_identity"] = "sha256:" + "0" * 64
    elif drift in {"instance", "execution", "config", "authorization"}:
        key = {
            "instance": "sut_instance_id",
            "execution": "execution_id",
            "config": "configuration_digest",
            "authorization": "authorization_scope_digest",
        }[drift]
        expected[key] = "mismatch"
    elif drift is not None:
        key = {
            "artifact": "collector_artifact",
            "dependency": "otel_dependencies",
            "qualification": "otel_qualification",
        }[drift]
        Path(document[key]["path"]).write_text("changed")
    if drift is None:
        authenticate_collector_readiness(CollectorReadinessReceiptV1.model_validate(document), **expected)
    else:
        with pytest.raises(ValueError):
            authenticate_collector_readiness(CollectorReadinessReceiptV1.model_validate(document), **expected)


def test_dynamic_host_readiness_with_real_sut_and_authenticated_host_receipts(
    live_sut, collector_receipt, monkeypatch, tmp_path
):
    from assurance_product.verification_execution import VerificationConfiguration, _readiness_binding
    from assurance_product.runtime_ports import AuthorizedSecretResolver
    from assurance_execution.operations.readiness import authenticate_host_readiness

    def preflight_verification(config, authorization, config_digest):
        authenticate_host_readiness(
            _readiness_binding(config, config_digest),
            source_root=host.source_root,
            secret_port=AuthorizedSecretResolver(authorization),
        )

    from graph_engine.attempts.secret_sources import (
        InvocationRuntimeAuthorization,
        SecretSourceBinding,
        runtime_authorization_digest,
    )

    host, secrets, selection, _ = live_sut
    collector, _ = collector_receipt

    config = VerificationConfiguration.model_validate(
        {
            "validation_profile": "api_db.v1",
            "host": {
                "sut_source_root": str(host.source_root),
                "managed_sut_readiness_handle": "sut.selection",
                "managed_sut_authority_handle": "sut.authority",
                "credential_handle": "sut.credential",
            },
        }
    )
    monkeypatch.setenv("AA_READINESS_SELECTION", json.dumps(selection))
    monkeypatch.setenv("AA_READINESS_AUTHORITY", secrets.values["sut.authority"].decode())
    monkeypatch.setenv("AA_READINESS_CREDENTIAL", "host-only-test-credential")
    monkeypatch.setenv("AA_READINESS_COLLECTOR", json.dumps({"collector_ready": True, "otel_ready": True}))
    sources = tuple(
        SecretSourceBinding("sut." + key, "environment", "AA_READINESS_" + key.upper())
        for key in ("selection", "authority", "credential", "collector")
    )
    authorization = InvocationRuntimeAuthorization(
        schema_version="1", secret_sources=sources, digest=runtime_authorization_digest(sources)
    )
    preflight_verification(config, authorization, "c" * 64)
    with pytest.raises(ValueError, match="selection|invalid"):
        preflight_verification(config, authorization, "f" * 64)
    selected_trace = {
        **selection,
        "verification": {**selection["verification"], "validation_profile": "api_db_trace.v1"},
    }
    monkeypatch.setenv("AA_READINESS_SELECTION", json.dumps(selected_trace))
    trace = config.model_copy(update={"validation_profile": "api_db_trace.v1"})
    with pytest.raises(ValueError, match="Collector/OTel"):
        preflight_verification(trace, authorization, "c" * 64)
    trace = trace.model_copy(
        update={"host": trace.host.model_copy(update={"collector_readiness_handle": "sut.collector"})}
    )
    with pytest.raises(ValueError, match="selection|invalid"):
        preflight_verification(trace, authorization, "c" * 64)
    collector["sut_instance_id"] = selected_trace["verification"]["sut_instance_id"]
    monkeypatch.setenv("AA_READINESS_COLLECTOR", json.dumps(collector))
    preflight_verification(trace, authorization, "c" * 64)


def test_managed_sut_with_missing_live_marker_is_not_ready(live_sut):
    from assurance_execution.contracts.readiness import ManagedSutReadinessSelectionV1
    from assurance_execution.operations.managed_sut import authenticate_managed_sut_readiness

    host, secrets, document, started = live_sut
    marker = Path(started["live_marker"])
    raw = marker.read_bytes()
    marker.unlink()
    try:
        with pytest.raises(ValueError):
            authenticate_managed_sut_readiness(
                ManagedSutReadinessSelectionV1.model_validate(document),
                source_root=host.source_root,
                secret_port=secrets,
            )
    finally:
        marker.write_bytes(raw)


@pytest.mark.parametrize("boundary", ["selection", "authority", "collector"])
def test_public_readiness_errors_never_disclose_secret_documents(live_sut, monkeypatch, caplog, boundary):
    import logging
    import traceback
    from assurance_product.verification_execution import VerificationConfiguration, _readiness_binding
    from assurance_product.runtime_ports import AuthorizedSecretResolver
    from assurance_execution.operations.readiness import authenticate_host_readiness
    from graph_engine.attempts.secret_sources import (
        InvocationRuntimeAuthorization,
        SecretSourceBinding,
        runtime_authorization_digest,
    )

    host, secrets, original, _ = live_sut
    selection = {
        **original,
        "verification": {**original["verification"], "validation_profile": "api_db_trace.v1"},
    }
    canary = "PRIVATE_RECEIPT_CANARY_" + boundary + "_fa912dc0"
    malformed = json.dumps({"unexpected": canary})
    documents = {
        "selection": json.dumps(selection),
        "authority": secrets.values["sut.authority"].decode(),
        "collector": malformed,
        "credential": "test-only",
    }
    documents[boundary] = malformed
    for key, value in documents.items():
        monkeypatch.setenv("AA_PRIVATE_" + key.upper(), value)
    sources = tuple(
        SecretSourceBinding("sut." + key, "environment", "AA_PRIVATE_" + key.upper()) for key in documents
    )
    auth = InvocationRuntimeAuthorization(
        schema_version="1", secret_sources=sources, digest=runtime_authorization_digest(sources)
    )
    config = VerificationConfiguration.model_validate(
        {
            "validation_profile": "api_db_trace.v1",
            "host": {
                "sut_source_root": str(host.source_root),
                "managed_sut_readiness_handle": "sut.selection",
                "managed_sut_authority_handle": "sut.authority",
                "collector_readiness_handle": "sut.collector",
                "credential_handle": "sut.credential",
            },
        }
    )
    with pytest.raises(ValueError) as caught:
        authenticate_host_readiness(
            _readiness_binding(config, "c" * 64),
            source_root=host.source_root,
            secret_port=AuthorizedSecretResolver(auth),
        )
    logging.getLogger(__name__).error(
        "preflight failed", exc_info=(type(caught.value), caught.value, caught.value.__traceback__)
    )
    rendered = caplog.text + "".join(traceback.format_exception(caught.value))
    error = caught.value
    while error is not None:
        rendered += str(error)
        error = error.__cause__ or error.__context__
    assert "invalid" in rendered
    assert canary not in rendered
    assert malformed not in rendered
    assert "input_value" not in rendered


def test_stopped_managed_sut_is_not_ready(live_sut):
    from assurance_execution.contracts.readiness import ManagedSutReadinessSelectionV1
    from assurance_execution.operations.managed_sut import authenticate_managed_sut_readiness

    host, secrets, document, started = live_sut
    workspace = Path(document["workspace_root"])
    host.stop(
        workspace_root=workspace,
        receipt_path=workspace / "run/owned-process.json",
        instance_id=started["instance_id"],
    )
    with pytest.raises(ValueError):
        authenticate_managed_sut_readiness(
            ManagedSutReadinessSelectionV1.model_validate(document),
            source_root=host.source_root,
            secret_port=secrets,
        )


@pytest.mark.parametrize(
    "member", [".aa/user-oracle/bootstrap.py", "run/sut/extra.py", "run/sut/app/models/admin.py"]
)
def test_execution_admission_rechecks_artifact_after_real_lifecycle(live_sut, member):
    from assurance_execution.contracts.agent import VerifiedExecutionPrepareV1
    from assurance_execution.operations.managed_sut import authenticate_managed_sut_receipts

    _, secrets, document, _ = live_sut
    workspace = Path(document["workspace_root"])
    profile = VerifiedExecutionPrepareV1.model_validate(document["verification"])

    def admit():
        return authenticate_managed_sut_receipts(
            workspace,
            profile,
            secret_port=secrets,
            authorization_scope_digest=document["authorization_scope_digest"],
            activity_receipt_digest=document["activity_receipt_digest"],
        )

    admit()
    path = workspace / member
    original = path.read_bytes() if path.exists() else None
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
    if path.exists():
        path.chmod(0o644)
    try:
        path.write_bytes((original or b"") + b"\n# drift after valid lifecycle\n")
        with pytest.raises(ValueError, match="NOT_READY"):
            admit()
    finally:
        if original is None:
            path.unlink()
        else:
            path.write_bytes(original)
            path.chmod(mode)


def test_root_static_db_preflight_needs_no_instance_or_readiness_selection(tmp_path, monkeypatch):
    from tests.product.test_user_oracle_full_workflow import load
    from assurance_product.verification_execution import VerificationConfiguration, preflight_verification
    from assurance_execution.operations.verified_process import DockerVerificationHost
    from graph_engine.attempts.secret_sources import (
        InvocationRuntimeAuthorization,
        SecretSourceBinding,
        runtime_authorization_digest,
    )

    project = tmp_path / "project"
    selected = load("user_oracle_harness").materialize_project(project_dir=project)
    private = tmp_path / "private"
    private.mkdir(mode=0o700)

    config = VerificationConfiguration.model_validate(
        {
            "validation_profile": "api_db.v1",
            "host": {
                "sut_source_root": str(Path(__file__).resolve().parents[2]),
                "managed_sut_authority_handle": "sut.authority",
                "credential_handle": "sut.credential",
            },
        }
    )
    values = {
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
        ),
        "sut.credential": "test-only-password",
        **{
            handle: "test-only-password"
            for handle in (
                "managed-sut.admin-password",
                "managed-sut.reset-password",
                "managed-sut.secret-key",
            )
        },
    }
    sources = tuple(
        SecretSourceBinding(handle, "environment", f"AA_STATIC_{index}")
        for index, handle in enumerate(values)
    )
    for index, value in enumerate(values.values()):
        monkeypatch.setenv(f"AA_STATIC_{index}", value)
    auth = InvocationRuntimeAuthorization(
        schema_version="1", secret_sources=sources, digest=runtime_authorization_digest(sources)
    )
    monkeypatch.setattr(
        DockerVerificationHost, "preflight", lambda self: pytest.fail("unexpected OCI dependency")
    )
    preflight_verification(config, auth, "c" * 64, workspace_root=project)
    assert not (project / ".aa/managed-user").exists()
    assert list(private.iterdir()) == []
    import traceback

    monkeypatch.setenv("AA_STATIC_0", json.dumps({"unexpected": "R4_STATIC_SECRET_CANARY"}))
    with pytest.raises(ValueError) as caught:
        preflight_verification(config, auth, "c" * 64, workspace_root=project)
    assert "NOT_READY" in str(caught.value)
    assert "R4_STATIC_SECRET_CANARY" not in "".join(traceback.format_exception(caught.value))
    monkeypatch.setenv("AA_STATIC_0", values["sut.authority"])
    trace = config.model_copy(update={"validation_profile": "api_db_trace.v1"})
    with pytest.raises(ValueError, match="Collector/OTel"):
        preflight_verification(trace, auth, "c" * 64, workspace_root=project)
    trace = trace.model_copy(
        update={"host": trace.host.model_copy(update={"collector_readiness_handle": "sut.collector"})}
    )
    trace_sources = (*sources, SecretSourceBinding("sut.collector", "environment", "AA_STATIC_COLLECTOR"))
    trace_auth = InvocationRuntimeAuthorization(
        schema_version="1", secret_sources=trace_sources, digest=runtime_authorization_digest(trace_sources)
    )
    monkeypatch.setenv("AA_STATIC_COLLECTOR", json.dumps({"collector_ready": True, "otel_ready": True}))
    with pytest.raises(ValueError, match="NOT_READY"):
        preflight_verification(trace, trace_auth, "c" * 64, workspace_root=project)
    qualification_document: dict[str, object] = {
        "validation_profile": "api_db_trace.v1",
        "configuration_digest": "c" * 64,
    }
    for member in ("collector_artifact", "collector_config", "otel_dependencies", "otel_qualification"):
        artifact = tmp_path / member
        artifact.write_text("test-only artifact digest fixture; no real OTel qualification claim")
        qualification_document[member] = {"path": str(artifact), "digest": digest(artifact)}
    monkeypatch.setenv("AA_STATIC_COLLECTOR", json.dumps(qualification_document))
    preflight_verification(trace, trace_auth, "c" * 64, workspace_root=project)
    assert not (project / ".aa/managed-user").exists()
    with pytest.raises(ValueError, match="NOT_READY"):
        preflight_verification(trace, trace_auth, "f" * 64, workspace_root=project)
    (tmp_path / "otel_dependencies").write_text("changed")
    with pytest.raises(ValueError, match="NOT_READY"):
        preflight_verification(trace, trace_auth, "c" * 64, workspace_root=project)
    (project / ".aa/user-oracle/bootstrap.py").chmod(0o600)
    (project / ".aa/user-oracle/bootstrap.py").write_text("tampered")
    with pytest.raises(ValueError, match="NOT_READY"):
        preflight_verification(config, auth, "c" * 64, workspace_root=project)
