"""Mandatory real OCI qualification; missing Docker/record is a failure, never skipped.

Prepare explicitly: uv run python scripts/build_verification_runner.py
Then: uv run pytest packages/capabilities/assurance-execution/tests/test_verified_runner_qualification.py -q
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from assurance_execution.contracts.verification import VerifiedExecutionResultV1
from assurance_execution.operations.verified_execution import VerifiedExecutionHandler
from assurance_execution.operations.verified_process import DockerVerificationHost
from test_verified_execution import handler_case, managed_sut  # pyright: ignore[reportMissingImports] # noqa: F401

REPO = Path(__file__).resolve().parents[4]


@pytest.fixture
def qualified_runner():
    lock = json.loads(
        (REPO / "benchmark/assurance-product/fixtures/user-oracle/runner-lock.json").read_bytes()
    )
    host = DockerVerificationHost(source_root=REPO, qualification_path=REPO / lock["qualification_path"])
    host.preflight()
    return host


def test_real_oci_isolation_and_parent_http_sqlite(qualified_runner, managed_sut, monkeypatch):  # noqa: F811
    workspace, _, started, _ = managed_sut
    canary = "DO_NOT_EXPOSE_TASK4_AUTH_CANARY"
    monkeypatch.setenv("TASK4_SECRET_CANARY", canary)
    evidence = workspace / "host-evidence-canary"
    evidence.write_text(canary)
    source = f'''from pathlib import Path
import os
import socket
import sys
import pytest
from assurance_execution.bridge import execute_case

def test_case():
    assert os.getuid() == 65534
    assert sys.version_info[:3] == (3, 11, 14)
    assert "{canary}" not in repr(dict(os.environ))
    assert not Path("/var/run/docker.sock").exists()
    with pytest.raises(OSError):
        Path({str(evidence)!r}).read_bytes()
    with pytest.raises(OSError):
        Path({str(evidence)!r}).write_text("forged")
    with pytest.raises(OSError):
        Path(__file__).write_text("forged")
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", {int(started["base_url"].rsplit(":", 1)[1])}), timeout=.5)
    execute_case("TC_USER_CREATE_001")
'''
    request, context, _, _ = handler_case(managed_sut, "oci", source)
    result = asyncio.run(VerifiedExecutionHandler(process_host=qualified_runner).execute(request, context))
    assert VerifiedExecutionResultV1.model_validate(result.output).evidence.state == "collected"
    assert evidence.read_text() == canary
    assert canary not in result.model_dump_json()
