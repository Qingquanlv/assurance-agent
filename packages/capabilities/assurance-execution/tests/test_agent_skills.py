from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any, cast

import pytest

from agent_runtime_contracts import AgentRunRequest
from graph_engine.canonical import JSONValue, canonical_json_bytes
from assurance_execution.contracts import ExecutionEvidenceV1
from assurance_execution.operations.agent_skills import (
    ExecuteFinalizeHandler,
    ExecutePrepareHandler,
    RunFinalizeHandler,
    RunPrepareHandler,
)
from assurance_execution.resource_loader import resource_bytes
from execution_fixtures import (  # pyright: ignore[reportMissingImports]
    BINDING,
    VALID_LEAFS,
    as_object,
    closed_mapping,
    execute_task,
    fake_agent_result,
    run_request,
)

_RESOURCES = Path(__file__).resolve().parent.parent / "assurance_execution" / "resources"
_FORBIDDEN = (
    "assurance_agent",
    "opencode",
    "cursor",
    "workflow-state.json",
    "aa risk",
    "claude code",
    "codex",
)
_TOKEN = re.compile(
    r"assurance_agent|opencode|\bcursor\b|workflow-state\.json|aa risk|claude code|\bcodex\b",
    re.IGNORECASE,
)


def _skill_input() -> dict[str, Any]:
    payload = run_request(selected=["tests/generated_test.py"])
    payload["artifact_paths"] = []
    return payload


def _resource_files() -> Iterator[Path]:
    for path in sorted(_RESOURCES.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts:
            yield path


def _valid_evidence() -> dict[str, Any]:
    return {
        "change_id": "CH-DEMO-001",
        "batch_id": "20260822T000000Z",
        "selected_targets": {"api": True, "e2e": False, "fuzz": False, "performance": False},
        "mapping": closed_mapping(["tests/generated_test.py"]),
        "mapping_digest": "a" * 64,
        "baseline_tree_id": "b" * 64,
        "runner_profile_digest": "c" * 64,
        "receipt_digest": "d" * 64,
        "receipt": {
            "command": ["pytest", "tests/generated_test.py"],
            "exit_code": 0,
            "collected": 1,
            "passed": 1,
            "failed": 0,
            "skipped": 0,
        },
        "results": [{"test": "tests/generated_test.py", "status": "passed", "duration_ms": 1, "message": ""}],
    }


@pytest.mark.asyncio
async def test_execute_prepare_is_canonical_and_provider_neutral(tmp_path: Path) -> None:
    first = await execute_task(ExecutePrepareHandler(), _skill_input(), tmp_path, binding_data=BINDING)
    second = await execute_task(ExecutePrepareHandler(), _skill_input(), tmp_path, binding_data=BINDING)
    assert first.status == "succeeded"
    assert AgentRunRequest.model_validate(first.output).canonical_bytes() == (
        AgentRunRequest.model_validate(second.output).canonical_bytes()
    )


@pytest.mark.asyncio
async def test_prepare_instruction_order_is_skill_persona_business(tmp_path: Path) -> None:
    prepared = await execute_task(RunPrepareHandler(), _skill_input(), tmp_path, binding_data=BINDING)
    request = AgentRunRequest.model_validate(prepared.output)
    assert len(request.instructions) == 3
    skill, persona, business = request.instructions
    assert skill.media_type == "text/plain"
    assert persona.media_type == "text/plain"
    assert business.media_type == "application/json"
    assert "Capability-owned run skill" in (skill.text_content or "")
    assert "Executor persona" in (persona.text_content or "")
    payload = cast(Mapping[str, object], business.json_content)
    assert payload["change_id"] == "CH-DEMO-001"
    encoded = request.canonical_bytes().decode("utf-8").lower()
    assert "opencode" not in encoded
    assert "cursor" not in encoded
    assert "assurance_agent" not in encoded
    assert request.execution.provider_model == "test-model"


@pytest.mark.asyncio
async def test_prepare_rejects_routing_marker_as_invalid_input(tmp_path: Path) -> None:
    binding = {
        **BINDING,
        "execution": {
            **BINDING["execution"],  # type: ignore[arg-type]
            "provider_model": "primary,fallback",
        },
    }
    prepared = await execute_task(ExecutePrepareHandler(), _skill_input(), tmp_path, binding_data=binding)
    assert prepared.status == "failed"
    assert prepared.failure is not None
    assert prepared.failure.kind == "invalid_input"
    assert prepared.failure.retryable is False


@pytest.mark.asyncio
async def test_finalize_rejects_malformed_input(tmp_path: Path) -> None:
    executed = await execute_task(RunFinalizeHandler(), {"agent_result": {}}, tmp_path)
    assert executed.status == "failed"
    assert executed.failure is not None
    assert executed.failure.kind == "invalid_input"
    assert executed.failure.retryable is False


@pytest.mark.asyncio
async def test_run_finalize_rejects_result_outside_mapping(tmp_path: Path) -> None:
    raw = _valid_evidence()
    raw["results"] = [{"test": "tests/legacy_test.py", "status": "passed", "duration_ms": 1, "message": ""}]
    outcome = await execute_task(RunFinalizeHandler(), fake_agent_result(raw), tmp_path)
    assert outcome.status == "failed"
    assert outcome.failure is not None
    assert outcome.failure.kind == "invalid_output"
    assert outcome.failure.retryable is True
    assert "outside the closed mapping" in outcome.failure.message


@pytest.mark.asyncio
async def test_execute_finalize_accepts_typed_evidence(tmp_path: Path) -> None:
    outcome = await execute_task(ExecuteFinalizeHandler(), fake_agent_result(_valid_evidence()), tmp_path)
    assert outcome.status == "succeeded"
    results = as_object(outcome.output)["results"]
    assert as_object(results[0])["test"] == "tests/generated_test.py"


def test_execution_resources_forbid_legacy_and_provider_names() -> None:
    required = (
        "skills/aa-execute/SKILL.md",
        "skills/aa-run/SKILL.md",
        "personas/executor.md",
        "result-contracts/execution.v1.schema.json",
    )
    missing = [item for item in required if not (_RESOURCES / item).is_file()]
    assert missing == []
    hits = [
        path.relative_to(_RESOURCES).as_posix()
        for path in _resource_files()
        if _TOKEN.search(path.read_text(encoding="utf-8"))
    ]
    assert hits == []
    lowered = "\n".join(path.read_text(encoding="utf-8").lower() for path in _resource_files())
    for token in _FORBIDDEN:
        assert token not in lowered


def test_result_contract_matches_capability_schema() -> None:
    assert resource_bytes("result-contracts/execution.v1.schema.json") == canonical_json_bytes(
        cast(JSONValue, ExecutionEvidenceV1.model_json_schema())
    )
    assert VALID_LEAFS


def test_execution_skills_keep_tool_environments_outside_candidate_and_use_family_runners() -> None:
    execute = (_RESOURCES / "skills/aa-execute/SKILL.md").read_text(encoding="utf-8")
    run = (_RESOURCES / "skills/aa-run/SKILL.md").read_text(encoding="utf-8")

    for skill in (execute, run):
        normalized = " ".join(skill.split())
        assert "Do not create or update `.venv`" in normalized
        assert "uv run --isolated pytest" in normalized
        assert "uv run --isolated locust --headless" in normalized
        assert "PYTHONDONTWRITEBYTECODE=1" in normalized
        assert "HYPOTHESIS_STORAGE_DIRECTORY=/tmp/aa-hypothesis-<batch_id>" in normalized
        assert "-p no:cacheprovider" in normalized
        assert "--output=/tmp/aa-playwright-<batch_id>" in normalized
        assert "Never" in normalized and "Locust file" in normalized and "pytest" in normalized


def test_cache_safe_pytest_recipe_does_not_dirty_candidate(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "test_sample.py").write_text("def test_sample():\n    assert True\n", encoding="utf-8")
    external_hypothesis = tmp_path / "external-hypothesis"
    environment = {
        **os.environ,
        "PYTHONDONTWRITEBYTECODE": "1",
        "HYPOTHESIS_STORAGE_DIRECTORY": str(external_hypothesis),
    }

    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "test_sample.py"],
        cwd=candidate,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert not (candidate / ".pytest_cache").exists()
    assert not list(candidate.rglob("__pycache__"))
    assert not (candidate / ".hypothesis").exists()
