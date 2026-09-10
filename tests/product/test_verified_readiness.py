"""Readiness checks probe a provided SUT URL; they do not own the process."""

from __future__ import annotations

import os
from pathlib import Path

import pytest


class Secrets:
    def __init__(self, values):
        self.values = values

    def resolve(self, handle):
        return self.values[handle]


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


def test_exclusive_collector_receipt_is_removed():
    import assurance_execution.contracts.readiness as readiness

    assert not hasattr(readiness, "CollectorReadinessReceiptV1")
