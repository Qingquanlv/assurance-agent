from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest

from agent_runtime_contracts.schema import canonical_digest
from agent_runtime_fixture import EXPECTED_AGENT_RUN_REQUEST_BYTES

_REPO = Path(__file__).resolve().parents[2]
_BENCH = _REPO / "benchmark" / "agent-runtime-phase3"
_MANIFEST = _BENCH / "manifest.json"
_RUN_ITEM = _BENCH / "run_item.py"
_PACKAGING_SMOKE = _REPO / "scripts" / "assurance_capability_wheel_smoke_test.sh"
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
    items = document["items"]
    assert {item["adapter"] for item in items} == {"opencode", "cursor"}
    driver = _load_run_item()
    for adapter in ("opencode", "cursor"):
        meta = adapters[adapter]
        assert meta["source_digest"] == driver._tree_digest(_REPO / meta["source_root"])


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


def _load_run_item() -> ModuleType:
    spec = importlib.util.spec_from_file_location("phase3_run_item", _RUN_ITEM)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    import sys

    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _assert_driver_fail_closed(action: Callable[[], int]) -> None:
    try:
        code = action()
    except SystemExit as error:
        assert error.code not in (0, None)
        return
    except (FileNotFoundError, OSError, json.JSONDecodeError, ValueError):
        return
    assert code != 0


def test_packaging_smoke_forbids_contracts_and_graph_engine_as_aa_runtime_deps() -> None:
    script = _PACKAGING_SMOKE.read_text(encoding="utf-8")
    # Requires-Dist and installed distribution names (hyphenated).
    assert script.count("agent-runtime-contracts") >= 2
    assert script.count("graph-engine") >= 2
    # find_spec import names (underscored).
    assert "agent_runtime_contracts" in script
    assert "graph_engine" in script


def test_run_item_driver_rejects_fallback_and_invented_credentials() -> None:
    source = _RUN_ITEM.read_text(encoding="utf-8")
    assert "manifest.json" in source
    assert "fallback" not in source.lower()
    assert "pytest.skip" not in source
    assert "invent credentials" in source.lower()
    assert "fail-closed" in source.lower()
    assert "_reject_credentials_in_text" in source


def test_run_item_fails_closed_when_manifest_is_missing(tmp_path: Path) -> None:
    driver = _load_run_item()
    missing = tmp_path / "manifest.json"
    output = tmp_path / "output"
    output.mkdir()
    _assert_driver_fail_closed(
        lambda: driver.main(["--adapter", "opencode", "--manifest", str(missing), "--output", str(output)])
    )


def test_run_item_fails_closed_when_source_digest_drifted(tmp_path: Path) -> None:
    driver = _load_run_item()
    document = json.loads(_MANIFEST.read_text(encoding="utf-8"))
    document["fixture"]["source_digest"] = "0" * 64
    drifted = tmp_path / "manifest.json"
    drifted.write_text(json.dumps(document), encoding="utf-8")
    output = tmp_path / "output"
    output.mkdir()
    _assert_driver_fail_closed(
        lambda: driver.main(["--adapter", "cursor", "--manifest", str(drifted), "--output", str(output)])
    )


def test_run_item_fails_closed_when_cursor_secret_is_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver = _load_run_item()
    version = "phase3-fail-closed"
    executable = tmp_path / "cursor-agent"
    executable.write_text(f"#!/bin/sh\necho '{version}'\n", encoding="utf-8")
    executable.chmod(0o755)
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    monkeypatch.setattr(driver, "_load_cursor_api_key_from_keychain", lambda: None)
    code = driver._check_cursor(
        {
            "executable": str(executable),
            "executable_digest": hashlib.sha256(executable.read_bytes()).hexdigest(),
            "external_tool_version": version,
            "secret_env": "CURSOR_API_KEY",
        }
    )
    assert code == 1


def test_run_item_allows_opencode_without_token_when_session_is_reachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver = _load_run_item()
    monkeypatch.delenv("OPENCODE_PHASE3_TOKEN", raising=False)

    def _fetch_json(url: str) -> object:
        if url.endswith("/global/health"):
            return {"version": "1.18.4"}
        if url.endswith("/config"):
            return {"model": "provider_default", "mcp": {}, "tools": []}
        if url.endswith("/session"):
            return []
        raise ValueError(f"unexpected url: {url}")

    monkeypatch.setattr(driver, "_fetch_json", _fetch_json)
    code = driver._check_opencode(
        {
            "endpoint": "http://127.0.0.1:4096",
            "external_tool_version": "1.18.4",
            "protocol_profile": "opencode-http-v1",
            "secret_env": "OPENCODE_PHASE3_TOKEN",
        }
    )
    assert code == 0
    assert os.environ.get("OPENCODE_PHASE3_TOKEN") == ""


def test_run_item_fails_closed_when_opencode_session_is_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver = _load_run_item()

    def _fetch_json(url: str) -> object:
        if url.endswith("/global/health"):
            return {"version": "1.18.4"}
        if url.endswith("/config"):
            return {"model": "provider_default"}
        if url.endswith("/session"):
            raise OSError("connection refused")
        raise ValueError(f"unexpected url: {url}")

    monkeypatch.setattr(driver, "_fetch_json", _fetch_json)
    monkeypatch.delenv("OPENCODE_PHASE3_TOKEN", raising=False)
    code = driver._check_opencode(
        {
            "endpoint": "http://127.0.0.1:4096",
            "external_tool_version": "1.18.4",
            "protocol_profile": "opencode-http-v1",
            "secret_env": "OPENCODE_PHASE3_TOKEN",
        }
    )
    assert code == 1
