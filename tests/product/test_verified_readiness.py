"""Readiness checks probe a provided SUT URL; they do not own the process."""

from __future__ import annotations

import hashlib
import json
import os
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
    import importlib.util
    import httpx
    from assurance_execution.contracts.verification import UserAttemptAuthorityV1
    from tests.verified_generation_fixture import accepted_verified_execution_input

    workspace = tmp_path_factory.mktemp("readiness-sut") / "project"
    repo = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location(
        "user_oracle_harness", repo / "benchmark/assurance-product/user_oracle_harness.py"
    )
    assert spec and spec.loader
    harness = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(harness)
    harness.materialize_project(project_dir=workspace)
    password = "readiness-test-only"
    os.environ["AA_SUT_ADMIN_PASSWORD"] = password
    os.environ["AA_SUT_RESET_PASSWORD"] = password
    os.environ["AA_SUT_SECRET_KEY"] = password
    started = harness.serve(workspace)
    accepted = accepted_verified_execution_input(workspace, reviewed_source_path="app/controllers/user.py")
    assert accepted.verification is not None
    authority = UserAttemptAuthorityV1(
        authorization_scope_digest="a" * 64,
        activity_receipt_digest="b" * 64,
        journal_key=os.urandom(32).hex(),
        sut_base_url=started["base_url"],
        sqlite_path=started["sqlite_path"],
        instance_id=started["instance_id"],
    )
    secrets = Secrets({"sut.authority": authority.model_dump_json().encode()})
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
            "managed_sqlite_path": started["sqlite_path"],
            "observer_sqlite_path": started["sqlite_path"],
            "user_inputs": {"username": "readiness", "email": "readiness@example.test"},
            "managed_sut_authority_handle": "sut.authority",
        },
    }
    try:
        response = httpx.post(
            started["base_url"] + "/api/v1/base/access_token",
            json={"username": "admin", "password": password},
            trust_env=False,
        )
        response.raise_for_status()
        yield secrets, selection, started
    finally:
        harness.stop_served(started["pid"])


def test_current_sut_readiness_is_http_probe(live_sut):
    from assurance_execution.contracts.readiness import ManagedSutReadinessSelectionV1
    from assurance_execution.operations.managed_sut import authenticate_managed_sut_readiness

    secrets, document, _ = live_sut
    authenticate_managed_sut_readiness(
        ManagedSutReadinessSelectionV1.model_validate(document),
        secret_port=secrets,
    )


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

    secrets, document, _ = live_sut
    changed = {**document, "verification": {**document["verification"], field: value}}
    with pytest.raises(ValueError):
        authenticate_managed_sut_readiness(
            ManagedSutReadinessSelectionV1.model_validate(changed),
            secret_port=secrets,
        )


def test_trace_boolean_is_not_a_readiness_receipt():
    from assurance_execution.contracts.readiness import CollectorReadinessReceiptV1

    with pytest.raises(ValueError):
        CollectorReadinessReceiptV1.model_validate({"collector_ready": True, "otel_ready": True})


def collector_listener(tmp_path):
    import subprocess
    import sys
    import time
    from assurance_execution.operations.readiness import process_birth_identity

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


def test_collector_readiness_requires_current_bound_receipt(collector_receipt):
    from assurance_execution.contracts.readiness import CollectorReadinessReceiptV1
    from assurance_execution.operations.readiness import authenticate_collector_readiness

    document, _ = collector_receipt
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
    authenticate_collector_readiness(CollectorReadinessReceiptV1.model_validate(document), **expected)
