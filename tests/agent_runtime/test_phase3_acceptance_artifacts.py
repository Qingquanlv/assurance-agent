from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_runtime_contracts.schema import canonical_digest
from agent_runtime_fixture import EXPECTED_AGENT_RUN_REQUEST_BYTES

_REPO = Path(__file__).resolve().parents[2]
_BENCH = _REPO / "benchmark" / "agent-runtime-phase3"
_MANIFEST = _BENCH / "manifest.json"
_REQUIRED = (
    "fixture",
    "graph",
    "canonical_request",
    "workspace",
    "result_schema",
    "expected_output",
    "adapters",
    "success",
)


def test_committed_live_manifest_pins_the_required_release_fields() -> None:
    document = json.loads(_MANIFEST.read_text(encoding="utf-8"))
    missing = [key for key in _REQUIRED if key not in document]
    assert missing == []
    fixture = document["fixture"]
    assert fixture["distribution"] == "agent-runtime-fixture"
    assert len(fixture["source_digest"]) == 64
    assert len(fixture["wheel_sha256"]) == 64
    graph = document["graph"]
    assert graph["entrypoint"] == "run"
    assert graph["graph_id"] == "root"
    request = document["canonical_request"]
    assert bytes(request["canonical_bytes"], "utf-8") == EXPECTED_AGENT_RUN_REQUEST_BYTES
    assert request["digest"] == canonical_digest(json.loads(EXPECTED_AGENT_RUN_REQUEST_BYTES))
    assert document["workspace"]["write_set"] == ["result.json"]
    assert document["expected_output"]["path"] == "result.json"
    assert document["expected_output"]["digest"] == canonical_digest(
        {"artifact": "result.json", "status": "ok"}
    )
    adapters = document["adapters"]
    assert adapters["opencode"]["protocol_profile"] == "opencode-http-v1"
    assert adapters["cursor"]["protocol_profile"] == "confined_process"
    assert adapters["opencode"]["external_tool_version"]
    assert adapters["cursor"]["external_tool_version"]
    assert adapters["cursor"]["executable"].startswith("/")
    assert document["success"]["status"] == "succeeded"


@pytest.mark.parametrize("name", ["run-opencode.sh", "run-cursor.sh"])
def test_live_scripts_consume_only_the_committed_manifest_and_fail_closed(name: str) -> None:
    script = (_BENCH / name).read_text(encoding="utf-8")
    assert "manifest.json" in script
    assert "${FOO:-" not in script and "${VAR:-" not in script
    assert ":-" not in script
    assert "fallback" not in script.lower()
    assert "pytest.skip" not in script
    assert "exit 0" not in script
    assert "set -euo pipefail" in script
    assert "fail-closed" in script
