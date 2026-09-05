from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections.abc import Iterator, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from agent_runtime_contracts import AgentRunRequest
from graph_engine.canonical import JSONValue, canonical_json_bytes
from graph_engine.frozen_json import thaw_json
from assurance_execution.contracts import ExecutionAgentResultV1
from assurance_execution.operations import agent_skills
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
    reviewed_cases,
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


def _prepare_input(project: Path) -> dict[str, Any]:
    change_id = "CH-DEMO-001"
    target = "tests/api/test_generated.py"
    content = b"def test_tc_a_001__ok():\n    assert True\n"
    generated = project / "qa" / "changes" / change_id / "generated" / "api" / "files" / target
    generated.parent.mkdir(parents=True, exist_ok=True)
    generated.write_bytes(content)
    manifest = project / "qa" / "changes" / change_id / "codegen" / "api-generated-files.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        json.dumps(
            {
                "schema_version": "1",
                "change_id": change_id,
                "layer": "api",
                "files": [
                    {
                        "repo_path": target,
                        "disposition": "generated",
                        "role": "test_entry",
                        "case_ids": ["TC_A"],
                    }
                ],
                "mapping": {
                    "schema_version": "1",
                    "layer": "api",
                    "entries": [
                        {
                            "case_id": "TC_A",
                            "symbol": "test_tc_a_001__ok",
                            "target_file": target,
                        }
                    ],
                },
                "required_capabilities": [],
            }
        ),
        encoding="utf-8",
    )
    cases = project / "qa" / "changes" / change_id / "cases" / "items" / "case.yaml"
    cases.parent.mkdir(parents=True, exist_ok=True)
    cases.write_text(yaml.safe_dump(reviewed_cases(), sort_keys=False), encoding="utf-8")
    return {
        "change_id": change_id,
        "selected_test_families": ["api"],
        "capability_leafs": list(VALID_LEAFS),
    }


def _business_payload(request: AgentRunRequest) -> dict[str, object]:
    value = request.instructions[-1].json_content
    assert isinstance(value, Mapping)
    thawed = thaw_json(value)
    assert isinstance(thawed, dict)
    return thawed


async def _prepared_finalize_payload(project: Path) -> tuple[dict[str, object], dict[str, Any]]:
    prepared = await execute_task(
        ExecutePrepareHandler(),
        _prepare_input(project),
        project,
        binding_data=BINDING,
    )
    assert prepared.status == "succeeded"
    business = _business_payload(AgentRunRequest.model_validate(prepared.output))
    mapping = cast(dict[str, Any], business["mapping"])
    selected = cast(list[str], mapping["selected"])
    assert len(selected) == 1
    evidence = _valid_evidence()
    receipt = cast(dict[str, object], evidence["receipt"])
    command = cast(list[dict[str, object]], receipt["commands"])[0]
    evidence.update(
        {
            "change_id": business["change_id"],
            "batch_id": business["batch_id"],
            "selected_targets": business["selected_targets"],
            "mapping": mapping,
            "baseline_tree_id": business["baseline_tree_id"],
            "runner_profile_digest": business["runner_profile_digest"],
            "receipt": {
                "commands": [
                    {
                        **command,
                        "command": ["pytest", selected[0]],
                    }
                ]
            },
            "results": [
                {
                    "test": selected[0],
                    "status": "passed",
                    "duration_ms": 1,
                    "message": "",
                }
            ],
        }
    )
    agent_result = cast(dict[str, object], fake_agent_result(evidence)["agent_result"])
    return {**business, "agent_result": agent_result}, evidence


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
        "baseline_tree_id": "b" * 64,
        "runner_profile_digest": "c" * 64,
        "receipt": {
            "commands": [
                {
                    "family": "api",
                    "command": ["pytest", "tests/generated_test.py"],
                    "exit_code": 0,
                    "collected": 1,
                    "passed": 1,
                    "failed": 0,
                    "skipped": 0,
                }
            ]
        },
        "results": [{"test": "tests/generated_test.py", "status": "passed", "duration_ms": 1, "message": ""}],
    }


@pytest.mark.asyncio
async def test_execute_prepare_is_canonical_and_provider_neutral(tmp_path: Path) -> None:
    payload = _prepare_input(tmp_path)
    first = await execute_task(ExecutePrepareHandler(), payload, tmp_path, binding_data=BINDING)
    second = await execute_task(ExecutePrepareHandler(), payload, tmp_path, binding_data=BINDING)
    assert first.status == "succeeded"
    assert AgentRunRequest.model_validate(first.output).canonical_bytes() == (
        AgentRunRequest.model_validate(second.output).canonical_bytes()
    )


@pytest.mark.asyncio
async def test_prepare_instruction_order_is_skill_persona_business(tmp_path: Path) -> None:
    prepared = await execute_task(
        RunPrepareHandler(),
        _prepare_input(tmp_path),
        tmp_path,
        binding_data=BINDING,
    )
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
    assert payload["runner_profile_digest"] == (
        "b7a0e4a77bf9eeb220b6094c28389aed65e2f9e94f055a60e4e8231dfa5bcd61"
    )
    encoded = request.canonical_bytes().decode("utf-8").lower()
    assert "opencode" not in encoded
    assert "cursor" not in encoded
    assert "assurance_agent" not in encoded
    assert request.execution.provider_model == "test-model"


@pytest.mark.asyncio
async def test_execute_prepare_materializes_an_authenticated_attempt_local_view(
    tmp_path: Path,
) -> None:
    payload = _prepare_input(tmp_path)

    prepared = await execute_task(ExecutePrepareHandler(), payload, tmp_path, binding_data=BINDING)

    assert prepared.status == "succeeded"
    request = AgentRunRequest.model_validate(prepared.output)
    business = _business_payload(request)
    view_root = business["execution_view_root"]
    view_digest = business["execution_view_digest"]
    assert isinstance(view_root, str)
    assert isinstance(view_digest, str) and len(view_digest) == 64
    executed_at = datetime.fromisoformat(cast(str, business["executed_at"]).replace("Z", "+00:00"))
    assert executed_at.utcoffset() is not None
    assert view_root.startswith("qa/changes/CH-DEMO-001/.staging/attempt-1/")
    assert "/qa/changes/CH-DEMO-001/.staging/execution/" in view_root
    materialized = tmp_path.joinpath(*view_root.split("/"), "tests/api/test_generated.py")
    assert materialized.read_bytes() == b"def test_tc_a_001__ok():\n    assert True\n"
    assert request.workspace.allowed_outputs == ()
    assert request.workspace.read_roots == (view_root,)


@pytest.mark.asyncio
async def test_execute_prepare_replay_authenticates_the_existing_attempt_view(tmp_path: Path) -> None:
    payload = _prepare_input(tmp_path)

    first = await execute_task(ExecutePrepareHandler(), payload, tmp_path, binding_data=BINDING)
    second = await execute_task(ExecutePrepareHandler(), payload, tmp_path, binding_data=BINDING)

    assert first.status == second.status == "succeeded"
    first_request = AgentRunRequest.model_validate(first.output)
    second_request = AgentRunRequest.model_validate(second.output)
    assert "execution_view_root" in _business_payload(first_request)
    assert _business_payload(first_request) == _business_payload(second_request)
    assert first_request.canonical_bytes() == second_request.canonical_bytes()


@pytest.mark.asyncio
async def test_prepare_baseline_ignores_dependency_and_runtime_noise(tmp_path: Path) -> None:
    payload = _prepare_input(tmp_path)
    first = await execute_task(ExecutePrepareHandler(), payload, tmp_path, binding_data=BINDING)
    assert first.status == "succeeded"
    first_request = AgentRunRequest.model_validate(first.output)

    dependency = tmp_path / "dependency-cache"
    dependency.mkdir()
    (dependency / "package.js").write_text("runtime dependency\n", encoding="utf-8")
    (tmp_path / "node_modules").symlink_to(dependency, target_is_directory=True)
    (tmp_path / "db.sqlite3").write_bytes(b"runtime database\n")
    app_logs = tmp_path / "app" / "logs"
    app_logs.mkdir(parents=True)
    (app_logs / "server.log").write_text("runtime log\n", encoding="utf-8")
    (tmp_path / "app" / "runtime.sqlite3").write_bytes(b"runtime database\n")
    runtime = tmp_path / "qa" / "changes" / "CH-DEMO-001" / ".runtime" / "langgraph"
    runtime.mkdir(parents=True)
    (runtime / "checkpoints.sqlite3").write_bytes(b"runtime checkpoint\n")
    evaluation = tmp_path / "eval-out"
    evaluation.mkdir()
    (evaluation / "result.json").write_text("{}\n", encoding="utf-8")

    second = await execute_task(ExecutePrepareHandler(), payload, tmp_path, binding_data=BINDING)

    assert second.status == "succeeded"
    second_request = AgentRunRequest.model_validate(second.output)
    assert (
        _business_payload(first_request)["baseline_tree_id"]
        == _business_payload(second_request)["baseline_tree_id"]
    )
    assert first_request.canonical_bytes() == second_request.canonical_bytes()


@pytest.mark.asyncio
async def test_prepare_baseline_binds_closed_source_and_execution_inputs(tmp_path: Path) -> None:
    payload = _prepare_input(tmp_path)
    tracked = (
        tmp_path / "app" / "main.py",
        tmp_path / "migrations" / "001_initial.py",
        tmp_path / "web" / "src" / "App.vue",
        tmp_path / "web" / "build" / "proxy.ts",
        tmp_path / "web" / ".env.test",
        tmp_path / "pyproject.toml",
        tmp_path / "uv.lock",
        tmp_path / "web" / "package.json",
        tmp_path / "tests" / "config.py",
        tmp_path
        / "qa"
        / "changes"
        / "CH-DEMO-001"
        / "generated"
        / "api"
        / "files"
        / "tests"
        / "api"
        / "test_generated.py",
    )
    baselines: list[object] = []
    initial = await execute_task(ExecutePrepareHandler(), payload, tmp_path, binding_data=BINDING)
    assert initial.status == "succeeded"
    baselines.append(_business_payload(AgentRunRequest.model_validate(initial.output))["baseline_tree_id"])

    for index, path in enumerate(tracked):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"tracked-{index}\n", encoding="utf-8")
        prepared = await execute_task(ExecutePrepareHandler(), payload, tmp_path, binding_data=BINDING)
        assert prepared.status == "succeeded"
        baselines.append(
            _business_payload(AgentRunRequest.model_validate(prepared.output))["baseline_tree_id"]
        )

    assert len(set(baselines)) == len(baselines)


@pytest.mark.asyncio
async def test_prepare_baseline_rejects_a_symlink_in_an_included_source_root(
    tmp_path: Path,
) -> None:
    payload = _prepare_input(tmp_path)
    target = tmp_path / "outside.py"
    target.write_text("VALUE = 1\n", encoding="utf-8")
    source = tmp_path / "app" / "main.py"
    source.parent.mkdir()
    source.symlink_to(target)

    prepared = await execute_task(ExecutePrepareHandler(), payload, tmp_path, binding_data=BINDING)

    assert prepared.status == "failed"
    assert prepared.failure is not None
    assert prepared.failure.kind == "invalid_input"
    assert "symbolic link" in prepared.failure.message


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["hardlink", "fifo"])
async def test_prepare_baseline_rejects_non_regular_included_source(
    tmp_path: Path,
    kind: str,
) -> None:
    payload = _prepare_input(tmp_path)
    source = tmp_path / "app" / "main.py"
    source.parent.mkdir()
    if kind == "hardlink":
        target = tmp_path / "outside.py"
        target.write_text("VALUE = 1\n", encoding="utf-8")
        os.link(target, source)
    else:
        os.mkfifo(source)

    prepared = await execute_task(ExecutePrepareHandler(), payload, tmp_path, binding_data=BINDING)

    assert prepared.status == "failed"
    assert prepared.failure is not None
    assert prepared.failure.kind == "invalid_input"
    assert "non-regular file" in prepared.failure.message


@pytest.mark.asyncio
async def test_prepare_baseline_enforces_file_count_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _prepare_input(tmp_path)
    app = tmp_path / "app"
    app.mkdir()
    (app / "one.py").write_text("ONE = 1\n", encoding="utf-8")
    (app / "two.py").write_text("TWO = 2\n", encoding="utf-8")
    monkeypatch.setattr(agent_skills, "_MAX_BASELINE_FILES", 1)

    prepared = await execute_task(ExecutePrepareHandler(), payload, tmp_path, binding_data=BINDING)

    assert prepared.status == "failed"
    assert prepared.failure is not None
    assert prepared.failure.kind == "invalid_input"
    assert "file count limit" in prepared.failure.message


@pytest.mark.asyncio
async def test_execute_prepare_replay_rejects_a_drifted_execution_view(tmp_path: Path) -> None:
    payload = _prepare_input(tmp_path)
    first = await execute_task(ExecutePrepareHandler(), payload, tmp_path, binding_data=BINDING)
    business = _business_payload(AgentRunRequest.model_validate(first.output))
    view = tmp_path.joinpath(*cast(str, business["execution_view_root"]).split("/"))
    (view / "tests/api/test_generated.py").write_text("def test_drift(): pass\n", encoding="utf-8")

    replay = await execute_task(ExecutePrepareHandler(), payload, tmp_path, binding_data=BINDING)

    assert replay.status == "failed"
    assert replay.failure is not None
    assert replay.failure.kind == "invalid_input"
    assert "execution view digest drifted" in replay.failure.message


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
    payload, raw = await _prepared_finalize_payload(tmp_path)
    raw["results"] = [{"test": "tests/legacy_test.py", "status": "passed", "duration_ms": 1, "message": ""}]
    payload["agent_result"] = fake_agent_result(raw)["agent_result"]
    outcome = await execute_task(RunFinalizeHandler(), payload, tmp_path)
    assert outcome.status == "failed"
    assert outcome.failure is not None
    assert outcome.failure.kind == "invalid_output"
    assert outcome.failure.retryable is True
    assert "outside the closed mapping" in outcome.failure.message


@pytest.mark.asyncio
async def test_finalize_rejects_receipt_failure_that_results_claim_passed(tmp_path: Path) -> None:
    payload, raw = await _prepared_finalize_payload(tmp_path)
    receipt = cast(dict[str, object], raw["receipt"])
    command = cast(list[dict[str, object]], receipt["commands"])[0]
    command.update({"passed": 0, "failed": 1})
    payload["agent_result"] = fake_agent_result(raw)["agent_result"]

    outcome = await execute_task(ExecuteFinalizeHandler(), payload, tmp_path)

    assert outcome.status == "failed"
    assert outcome.failure is not None
    assert outcome.failure.kind == "invalid_output"
    assert "receipt failure contradicts" in outcome.failure.message


@pytest.mark.asyncio
async def test_finalize_rejects_command_receipt_for_an_unselected_family(tmp_path: Path) -> None:
    payload, raw = await _prepared_finalize_payload(tmp_path)
    receipt = cast(dict[str, object], raw["receipt"])
    command = cast(list[dict[str, object]], receipt["commands"])[0]
    command["family"] = "e2e"
    payload["agent_result"] = fake_agent_result(raw)["agent_result"]

    outcome = await execute_task(ExecuteFinalizeHandler(), payload, tmp_path)

    assert outcome.status == "failed"
    assert outcome.failure is not None
    assert outcome.failure.kind == "invalid_output"
    assert "command receipts must exactly cover selected targets" in outcome.failure.message


@pytest.mark.asyncio
async def test_execute_finalize_accepts_typed_evidence(tmp_path: Path) -> None:
    payload, _ = await _prepared_finalize_payload(tmp_path)
    view_root = cast(str, payload["execution_view_root"])
    outcome = await execute_task(ExecuteFinalizeHandler(), payload, tmp_path)
    assert outcome.status == "succeeded"
    results = as_object(outcome.output)["results"]
    assert as_object(results[0])["test"] == "tests/api/test_generated.py::test_tc_a_001__ok"
    assert as_object(outcome.output)["executed_at"] == payload["executed_at"]
    assert not tmp_path.joinpath(*view_root.split("/")).exists()

    replay = await execute_task(ExecuteFinalizeHandler(), payload, tmp_path)
    assert replay.status == "succeeded"
    assert replay.output == outcome.output


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
        cast(JSONValue, ExecutionAgentResultV1.model_json_schema())
    )
    assert VALID_LEAFS


def test_execution_skills_keep_tool_environments_outside_candidate_and_use_family_runners() -> None:
    execute = (_RESOURCES / "skills/aa-execute/SKILL.md").read_text(encoding="utf-8")
    run = (_RESOURCES / "skills/aa-run/SKILL.md").read_text(encoding="utf-8")

    for skill in (execute, run):
        normalized = " ".join(skill.split())
        assert "Do not create or update `.venv`" in normalized
        assert "uv run --isolated pytest" in normalized
        assert "PYTHONDONTWRITEBYTECODE=1" in normalized
        assert "HYPOTHESIS_STORAGE_DIRECTORY=/tmp/aa-hypothesis-<batch_id>" in normalized
        assert "-p no:cacheprovider" in normalized
        assert "uv run --isolated pytest -p no:cacheprovider --rootdir <execution_view_root>" in normalized
        assert "--output=/tmp/aa-playwright-<batch_id>" in normalized
        assert (
            "uv run --isolated locust --locustfile <execution_view_root>/<mapped-locustfile> --headless"
        ) in normalized
        assert "Never" in normalized and "Locust file" in normalized and "pytest" in normalized
        assert (
            "For every family, the command receipt's passed, failed, and skipped counts must each be "
            "at least the corresponding counts in that family's result rows"
        ) in normalized
        assert (
            "A non-zero command exit with no failed test row still requires failed to be at least 1"
        ) in normalized


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
