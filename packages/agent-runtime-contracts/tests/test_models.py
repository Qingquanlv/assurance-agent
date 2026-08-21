from __future__ import annotations

import ast
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from agent_runtime_contracts import (
    AgentRunRequest,
    AgentRunResult,
    FrozenExecutionSelection,
    InstructionPart,
    ResultContract,
)
from agent_runtime_contracts.schema import canonical_digest


_REPO_ROOT = Path(__file__).resolve().parents[3]
_PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "agent_runtime_contracts"
_ENGINE_ROOT = _REPO_ROOT / "packages" / "graph-engine" / "graph_engine"
_SHA_A = "1" * 64
_SHA_B = "2" * 64
_SHA_C = "3" * 64
_SHA_D = "4" * 64
_FORBIDDEN_GRAPH_ENGINE_PREFIXES = (
    "graph_engine.runtime",
    "graph_engine.composition",
    "graph_engine.graph",
    "graph_engine.canonical",
    "graph_engine.frozen_json",
    "graph_engine.errors",
    "graph_engine.identifiers",
)
_FORBIDDEN_PEER_PREFIXES = (
    "agent_runtime_opencode",
    "agent_runtime_cursor",
    "assurance_agent",
    "assurance_kernel",
)
_FORBIDDEN_RESULT_FIELDS = (
    "candidate_tree_id",
    "write_set_digest",
    "transcript",
    "model_history",
    "tokens",
    "cost",
    "events",
    "secret",
    "secrets",
)


def _selection(
    *,
    provider_model: str = "provider_default",
    **overrides: object,
) -> FrozenExecutionSelection:
    payload: dict[str, object] = {
        "provider_model": provider_model,
        "worker_profile": "fixture-v1",
        "permission_profile_digest": _SHA_B,
        "limits": {"max_seconds": 120},
    }
    payload.update(overrides)
    return FrozenExecutionSelection.model_validate(payload)


def _request() -> AgentRunRequest:
    return AgentRunRequest(
        schema_version="1",
        instructions=(InstructionPart.text("text/plain", "write result.json"),),
        result_contract=ResultContract(
            schema_id="fixture.result.v1",
            schema_digest=_SHA_A,
            extraction_mode="structured",
        ),
        execution=_selection(),
        request_policy_digest=_SHA_C,
        request_config_digest=_SHA_D,
    )


def test_agent_run_request_is_strict_frozen_and_canonical() -> None:
    request = AgentRunRequest(
        schema_version="1",
        instructions=(InstructionPart.text("text/plain", "write result.json"),),
        result_contract=ResultContract(
            schema_id="fixture.result.v1",
            schema_digest="1" * 64,
            extraction_mode="structured",
        ),
        execution=FrozenExecutionSelection(
            provider_model="provider_default",
            worker_profile="fixture-v1",
            permission_profile_digest="2" * 64,
            limits={"max_seconds": 120},  # type: ignore[arg-type]
        ),
        request_policy_digest="3" * 64,
        request_config_digest="4" * 64,
    )
    assert AgentRunRequest.model_validate_json(request.canonical_bytes()).canonical_bytes() == (
        request.canonical_bytes()
    )
    with pytest.raises(ValidationError, match="extra"):
        AgentRunRequest.model_validate({**request.model_dump(), "fallback_model": "x"})


def test_agent_run_request_rejects_mutation() -> None:
    request = _request()
    with pytest.raises(ValidationError, match="frozen"):
        request.schema_version = "2"  # type: ignore[misc]


def test_instruction_part_permits_exactly_one_text_or_json_form() -> None:
    text_part = InstructionPart.text("text/plain", "write result.json")
    assert text_part.media_type == "text/plain"
    assert text_part.text_content == "write result.json"
    assert text_part.json_content is None
    assert text_part.digest == canonical_digest("write result.json")

    json_part = InstructionPart.from_json({"task": "write result.json"})
    assert json_part.media_type == "application/json"
    assert json_part.text_content is None
    assert json_part.json_content == {"task": "write result.json"}
    assert json_part.digest == canonical_digest({"task": "write result.json"})

    with pytest.raises(ValidationError, match="exactly one"):
        InstructionPart(media_type="text/plain", digest=canonical_digest("x"))
    with pytest.raises(ValidationError, match="exactly one"):
        InstructionPart(
            media_type="text/plain",
            text_content="hello",
            json_content={"x": 1},
            digest=canonical_digest("hello"),
        )


def test_instruction_part_rejects_unknown_media_type_and_bad_digest() -> None:
    with pytest.raises(ValidationError):
        InstructionPart.text("text/markdown", "write result.json")
    with pytest.raises(ValidationError, match="canonical"):
        InstructionPart(
            media_type="text/plain",
            text_content="write result.json",
            digest=_SHA_A,
        )


def test_frozen_execution_selection_accepts_exact_or_provider_default() -> None:
    defaulted = _selection()
    assert defaulted.provider_model == "provider_default"
    exact = _selection(provider_model="openai/gpt-4.1")
    assert exact.provider_model == "openai/gpt-4.1"


@pytest.mark.parametrize(
    "payload",
    [
        {"candidates": ["openai/gpt-4.1", "anthropic/claude"]},
        {"fallback_model": "openai/gpt-4.1"},
        {"fallbacks": ["openai/gpt-4.1"]},
        {"routing": "cheapest"},
        {"routing_expression": "cost < 1"},
    ],
)
def test_frozen_execution_selection_rejects_candidates_routing_and_fallbacks(
    payload: dict[str, object],
) -> None:
    body = _selection().model_dump()
    body.update(payload)
    with pytest.raises(ValidationError, match="extra"):
        FrozenExecutionSelection.model_validate(body)


@pytest.mark.parametrize(
    "provider_model",
    [
        "openai/gpt-4.1,anthropic/claude",
        "openai/gpt-4.1|anthropic/claude",
        "route:cheapest",
        "fallback:openai/gpt-4.1",
        ["openai/gpt-4.1", "anthropic/claude"],
    ],
)
def test_frozen_execution_selection_rejects_non_exact_provider_models(
    provider_model: object,
) -> None:
    with pytest.raises(ValidationError):
        _selection(provider_model=provider_model)  # type: ignore[arg-type]


def test_frozen_execution_selection_rejects_unknown_limit_fields() -> None:
    with pytest.raises(ValidationError, match="extra"):
        _selection(limits={"max_seconds": 120, "max_cost": 1})


def test_result_contract_extraction_mode_is_closed() -> None:
    with pytest.raises(ValidationError):
        ResultContract.model_validate(
            {
                "schema_id": "fixture.result.v1",
                "schema_digest": _SHA_A,
                "extraction_mode": "transcript",
            }
        )


def test_agent_run_result_authenticates_digests_and_forbids_provider_payloads() -> None:
    structured = {"status": "ok", "artifact": "result.json"}
    result = AgentRunResult.model_validate(
        {
            "structured_result": structured,
            "result_digest": canonical_digest(structured),
            "evidence_digest": _SHA_B,
            "provider_diff_digest": _SHA_C,
            "adapter_id": "agent-runtime-fixture",
            "adapter_version": "1.0.0",
            "diagnostics": ("idle",),
        }
    )
    assert result.schema_version == "1"
    assert AgentRunResult.model_validate_json(result.canonical_bytes()).canonical_bytes() == (
        result.canonical_bytes()
    )
    with pytest.raises(ValidationError, match="canonical"):
        AgentRunResult.model_validate(
            {
                "structured_result": structured,
                "result_digest": _SHA_A,
                "evidence_digest": _SHA_B,
                "adapter_id": "agent-runtime-fixture",
                "adapter_version": "1.0.0",
            }
        )
    dumped = result.model_dump()
    for field_name in _FORBIDDEN_RESULT_FIELDS:
        with pytest.raises(ValidationError, match="extra"):
            AgentRunResult.model_validate({**dumped, field_name: "leak"})


def test_agent_run_result_bounds_and_redacts_diagnostics() -> None:
    structured = {"status": "ok"}
    result = AgentRunResult.model_validate(
        {
            "structured_result": structured,
            "result_digest": canonical_digest(structured),
            "evidence_digest": _SHA_B,
            "adapter_id": "agent-runtime-fixture",
            "adapter_version": "1.0.0",
            "diagnostics": (
                "Authorization: Bearer sk-secret-canary",
                "x" * 512,
            ),
        }
    )
    assert "sk-secret-canary" not in result.diagnostics[0]
    assert "[redacted]" in result.diagnostics[0]
    assert len(result.diagnostics[1]) <= 240
    overflow = tuple(f"note-{index}" for index in range(17))
    with pytest.raises(ValidationError, match="bound"):
        AgentRunResult.model_validate(
            {
                "structured_result": structured,
                "result_digest": canonical_digest(structured),
                "evidence_digest": _SHA_B,
                "adapter_id": "agent-runtime-fixture",
                "adapter_version": "1.0.0",
                "diagnostics": overflow,
            }
        )


def _imported_modules(tree: ast.AST) -> tuple[str, ...]:
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            names.append(node.module)
    return tuple(names)


def test_contracts_source_imports_only_plugin_api_primitives() -> None:
    for path in sorted(_PACKAGE_ROOT.rglob("*.py")):
        imported = _imported_modules(ast.parse(path.read_text(encoding="utf-8")))
        for module_name in imported:
            assert not module_name.startswith(_FORBIDDEN_PEER_PREFIXES), (path, module_name)
            assert not module_name.startswith(_FORBIDDEN_GRAPH_ENGINE_PREFIXES), (path, module_name)
            if module_name == "graph_engine" or module_name.startswith("graph_engine."):
                assert module_name == "graph_engine.plugin_api", (path, module_name)


def test_graph_engine_does_not_import_agent_runtime_contracts() -> None:
    for path in sorted(_ENGINE_ROOT.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        assert "agent_runtime_contracts" not in text, path


def test_isolated_wheel_import_does_not_load_adapters_or_assurance(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    venv = tmp_path / "venv"
    dist.mkdir()
    for package in ("graph-engine", "agent-runtime-contracts"):
        subprocess.run(
            [
                "uv",
                "build",
                "--offline",
                "--wheel",
                "--package",
                package,
                "--out-dir",
                str(dist),
            ],
            cwd=_REPO_ROOT,
            check=True,
        )
    engine_wheel = next(dist.glob("graph_engine-*.whl"))
    contracts_wheel = next(dist.glob("agent_runtime_contracts-*.whl"))
    subprocess.run(["uv", "venv", "--offline", "--python", "3.11", str(venv)], check=True)
    subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--offline",
            "--python",
            str(venv / "bin" / "python"),
            "--find-links",
            str(dist),
            str(engine_wheel),
            str(contracts_wheel),
        ],
        check=True,
    )
    script = r"""
import importlib.util
import sys

import agent_runtime_contracts
from agent_runtime_contracts import AgentRunRequest
from graph_engine.plugin_api import FrozenModel

assert issubclass(AgentRunRequest, FrozenModel)
for package in (
    "agent_runtime_opencode",
    "agent_runtime_cursor",
    "assurance_agent",
    "assurance_kernel",
):
    assert importlib.util.find_spec(package) is None, package
    assert not any(name == package or name.startswith(package + ".") for name in sys.modules), package
"""
    completed = subprocess.run(
        [str(venv / "bin" / "python"), "-c", script],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0
    assert "graph_engine.plugin_api" not in completed.stderr
