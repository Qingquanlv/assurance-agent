from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from agent_runtime_contracts import AgentRunRequest, AgentRunResult
from agent_runtime_contracts.schema import canonical_digest
from graph_engine.canonical import JSONValue
from graph_engine.plugin_api import TaskFailure, TaskHandler, TaskOutcome
from tests.phase4.agent_harness import FakeAgentAdapter
from tests.phase4.conformance import execute_task

from assurance_intake.operations import (
    CaseDesignFinalizeHandler,
    CaseDesignPrepareHandler,
    CaseReviewFinalizeHandler,
    ExploreFinalizeHandler,
    IntakeFinalizeHandler,
    IntakePrepareHandler,
)
from assurance_intake.resource_loader import resource_text

_SHA = "a" * 64
_FIXTURES = Path(__file__).resolve().parent / "fixtures"
VALID_LEAFS = ("auth.session.create", "entities.item.create")
BINDING: dict[str, JSONValue] = {
    "execution": {
        "provider_model": "test-model",
        "worker_profile": "worker",
        "permission_profile_digest": _SHA,
        "limits": {"max_seconds": 5},
    },
    "request_policy_digest": _SHA,
    "request_config_digest": _SHA,
}
CASE_INPUT: dict[str, JSONValue] = {
    "change_id": "CH-DEMO-001",
    "capability_leafs": list(VALID_LEAFS),
    "artifact_paths": ["qa/changes/CH-DEMO-001/explore/advisory.json"],
}
INTAKE_INPUT: dict[str, JSONValue] = {
    "change_id": "RET-dept-management",
    "requirement": "Cover department CRUD and the department tree page.",
    "capability_leafs": [],
    "artifact_paths": ["qa/changes"],
}


async def run_prepare(
    handler: TaskHandler,
    payload: JSONValue,
    binding: JSONValue,
    workspace: Path,
) -> Any:
    return await execute_task(handler, payload, workspace, binding_data=binding)


async def run_finalize(handler: TaskHandler, result: AgentRunResult, workspace: Path) -> TaskOutcome:
    executed = await execute_task(
        handler,
        {
            "agent_result": result.model_dump(mode="json"),
            "capability_leafs": list(VALID_LEAFS),
            "artifact_paths": [],
        },
        workspace,
    )
    return executed.outcome


def fake_agent_result(structured_result: JSONValue) -> AgentRunResult:
    return AgentRunResult(
        structured_result=structured_result,
        result_digest=canonical_digest(structured_result),
        evidence_digest=FakeAgentAdapter.EVIDENCE_DIGEST,
        adapter_id="test.fake",
        adapter_version="1.0.0",
    )


def test_intake_skill_requires_direct_change_write() -> None:
    skill = resource_text("skills/aa-intake/SKILL.md")
    persona = resource_text("personas/intake-host.md")
    assert "qa/changes/<change-id>" in skill
    assert "must not ask" in skill.lower() or "do not ask" in skill.lower()
    assert "must not require" in skill.lower() or "do not require" in skill.lower()
    assert "initialize" in skill.lower()
    assert "requirement.md" in skill
    assert "interactive" not in persona.lower()
    assert "do not ask" in persona.lower()


@pytest.mark.asyncio
async def test_intake_prepare_rejects_missing_requirement(tmp_path: Path) -> None:
    payload = {key: value for key, value in INTAKE_INPUT.items() if key != "requirement"}
    prepared = await run_prepare(IntakePrepareHandler(), payload, BINDING, tmp_path)
    assert prepared.status == "failed"
    assert prepared.failure is not None
    assert prepared.failure.kind == "invalid_input"
    assert prepared.failure.retryable is False


@pytest.mark.asyncio
async def test_intake_prepare_embeds_locked_requirement_and_write_rules(tmp_path: Path) -> None:
    prepared = await run_prepare(IntakePrepareHandler(), INTAKE_INPUT, BINDING, tmp_path)
    request = AgentRunRequest.model_validate(prepared.output)
    skill, persona, business = request.instructions
    assert "Capability-owned intake" in (skill.text_content or "")
    assert "Do not ask" in (skill.text_content or "")
    assert "qa/changes/<change-id>" in (skill.text_content or "")
    assert "Do not ask" in (persona.text_content or "")
    payload = cast(Mapping[str, object], business.json_content)
    assert payload["change_id"] == "RET-dept-management"
    assert payload["requirement"] == "Cover department CRUD and the department tree page."


@pytest.mark.asyncio
async def test_case_design_prepare_is_canonical_and_provider_neutral(tmp_path: Path) -> None:
    first = await run_prepare(CaseDesignPrepareHandler(), CASE_INPUT, BINDING, tmp_path)
    second = await run_prepare(CaseDesignPrepareHandler(), CASE_INPUT, BINDING, tmp_path)
    assert AgentRunRequest.model_validate(first.output).canonical_bytes() == (
        AgentRunRequest.model_validate(second.output).canonical_bytes()
    )


@pytest.mark.asyncio
async def test_case_review_finalize_rejects_nonexistent_leaf(tmp_path: Path) -> None:
    result = fake_agent_result(_case_review_document(missing=["entities.fake"]))
    outcome = await run_finalize(CaseReviewFinalizeHandler(), result, tmp_path)
    assert outcome.failure == TaskFailure(
        kind="invalid_output",
        message="case review references unknown capability leaf: entities.fake",
        retryable=True,
    )


@pytest.mark.asyncio
async def test_prepare_instruction_order_is_skill_persona_business(tmp_path: Path) -> None:
    prepared = await run_prepare(CaseDesignPrepareHandler(), CASE_INPUT, BINDING, tmp_path)
    request = AgentRunRequest.model_validate(prepared.output)
    assert len(request.instructions) == 3
    skill, persona, business = request.instructions
    assert skill.media_type == "text/plain"
    assert persona.media_type == "text/plain"
    assert business.media_type == "application/json"
    assert "Capability-owned case-design skill" in (skill.text_content or "")
    assert "Document-author persona" in (persona.text_content or "")
    payload = cast(Mapping[str, object], business.json_content)
    leafs = payload["capability_leafs"]
    assert payload["change_id"] == "CH-DEMO-001"
    assert isinstance(leafs, list | tuple)
    assert tuple(leafs) == VALID_LEAFS
    encoded = request.canonical_bytes().decode("utf-8").lower()
    assert "opencode" not in encoded
    assert "cursor" not in encoded
    assert request.execution.provider_model == "test-model"
    assert request.result_contract.schema_document is not None
    assert request.result_contract.schema_id == "assurance.intake.result.case-design.v1"


@pytest.mark.asyncio
async def test_prepare_rejects_routing_marker_as_invalid_input(tmp_path: Path) -> None:
    binding = {
        **BINDING,
        "execution": {
            **BINDING["execution"],  # type: ignore[arg-type]
            "provider_model": "primary,fallback",
        },
    }
    prepared = await run_prepare(CaseDesignPrepareHandler(), CASE_INPUT, binding, tmp_path)
    assert prepared.status == "failed"
    assert prepared.failure is not None
    assert prepared.failure.kind == "invalid_input"
    assert prepared.failure.retryable is False


@pytest.mark.asyncio
async def test_finalize_rejects_malformed_input(tmp_path: Path) -> None:
    executed = await execute_task(CaseReviewFinalizeHandler(), {"agent_result": {}}, tmp_path)
    assert executed.status == "failed"
    assert executed.failure is not None
    assert executed.failure.kind == "invalid_input"
    assert executed.failure.retryable is False


def _case_review_document(*, missing: list[str]) -> JSONValue:
    skipped = len(missing)
    return cast(
        JSONValue,
        {
            "schema_version": "1.0",
            "review_type": "case",
            "change_id": "CH-DEMO-001",
            "decision": "pass",
            "findings": [],
            "auto_fix_plan": [],
            "next_action": "continue",
            "auto_fix_allowed": False,
            "human_review_required": False,
            "risk_level": "low",
            "minimum_coverage": {
                "total_required": 2,
                "covered": 2 - skipped,
                "skipped_by_scope": skipped,
                "missing": missing,
            },
            "source_verification": {
                "independent": True,
                "reviewed_source_files": ["src/app.py"],
                "verified_claims": [
                    {"claim": "create item persists a menu record", "evidence_files": ["src/app.py"]}
                ],
            },
        },
    )


async def _finalize_files(
    handler: TaskHandler,
    structured_result: JSONValue,
    workspace: Path,
    artifact_paths: list[str],
) -> Any:
    result = fake_agent_result(structured_result)
    executed = await execute_task(
        handler,
        cast(
            JSONValue,
            {
                "agent_result": result.model_dump(mode="json"),
                "capability_leafs": list(VALID_LEAFS),
                "artifact_paths": artifact_paths,
            },
        ),
        workspace,
    )
    return executed


@pytest.mark.asyncio
async def test_explore_finalize_returns_artifact_digests(tmp_path: Path) -> None:
    relative = "qa/changes/CH-DEMO-001/explore/advisory.json"
    path = tmp_path / relative
    path.parent.mkdir(parents=True)
    payload = b'{"ok":true}'
    path.write_bytes(payload)
    result = fake_agent_result({"output_files": [relative]})
    executed = await execute_task(
        ExploreFinalizeHandler(),
        {
            "agent_result": result.model_dump(mode="json"),
            "capability_leafs": list(VALID_LEAFS),
            "artifact_paths": [relative],
        },
        tmp_path,
    )
    assert executed.status == "succeeded"
    assert executed.output == {
        "artifacts": [{"path": relative, "digest": hashlib.sha256(payload).hexdigest()}]
    }


@pytest.mark.asyncio
async def test_intake_finalize_accepts_files_under_locked_prefix(tmp_path: Path) -> None:
    relative = "qa/changes/RET-dept-management/requirement.md"
    path = tmp_path / relative
    path.parent.mkdir(parents=True)
    payload = b"# RET-dept-management\n\nCover department CRUD.\n"
    path.write_bytes(payload)
    executed = await _finalize_files(
        IntakeFinalizeHandler(),
        {"output_files": [relative]},
        tmp_path,
        ["qa/archive", "qa/cases", "qa/changes"],
    )
    assert executed.status == "succeeded"
    assert executed.output == {
        "artifacts": [{"path": relative, "digest": hashlib.sha256(payload).hexdigest()}]
    }


@pytest.mark.asyncio
async def test_intake_finalize_rejects_file_outside_locked_prefix(tmp_path: Path) -> None:
    relative = "qa/notes/outside.md"
    path = tmp_path / relative
    path.parent.mkdir(parents=True)
    path.write_bytes(b"nope\n")
    executed = await _finalize_files(
        IntakeFinalizeHandler(),
        {"output_files": [relative]},
        tmp_path,
        ["qa/changes"],
    )
    assert executed.status == "failed"
    assert executed.failure is not None
    assert executed.failure.kind == "invalid_output"
    assert "undeclared" in executed.failure.message


@pytest.mark.asyncio
async def test_intake_finalize_returns_artifact_digests(tmp_path: Path) -> None:
    relative = "qa/changes/CH-DEMO-001/explore/advisory.json"
    path = tmp_path / relative
    path.parent.mkdir(parents=True)
    payload = b'{"ok":true}'
    path.write_bytes(payload)
    executed = await _finalize_files(
        IntakeFinalizeHandler(),
        {"output_files": [relative]},
        tmp_path,
        [relative],
    )
    assert executed.status == "succeeded"
    assert executed.output == {
        "artifacts": [{"path": relative, "digest": hashlib.sha256(payload).hexdigest()}]
    }


@pytest.mark.asyncio
async def test_case_design_finalize_accepts_typed_authoring(tmp_path: Path) -> None:
    structured = cast(
        JSONValue,
        yaml.safe_load((_FIXTURES / "case-authoring-valid.yaml").read_text(encoding="utf-8")),
    )
    executed = await _finalize_files(CaseDesignFinalizeHandler(), structured, tmp_path, [])
    assert executed.status == "succeeded"
    assert executed.output["added"][0]["trace"] == {"entities.item.create": {"covered": True}}


@pytest.mark.asyncio
async def test_case_review_finalize_accepts_typed_review(tmp_path: Path) -> None:
    executed = await _finalize_files(
        CaseReviewFinalizeHandler(),
        _case_review_document(missing=[]),
        tmp_path,
        [],
    )
    assert executed.status == "succeeded"
    assert executed.output["decision"] == "pass"
    assert executed.output["minimum_coverage"]["missing"] == []


@pytest.mark.asyncio
async def test_case_review_finalize_rejects_prefix_leaf(tmp_path: Path) -> None:
    result = fake_agent_result(_case_review_document(missing=["entities.item"]))
    outcome = await run_finalize(CaseReviewFinalizeHandler(), result, tmp_path)
    assert outcome.failure == TaskFailure(
        kind="invalid_output",
        message="case review references unknown capability leaf: entities.item",
        retryable=True,
    )


@pytest.mark.asyncio
async def test_explore_finalize_rejects_empty_artifact_paths(tmp_path: Path) -> None:
    relative = "qa/changes/CH-DEMO-001/explore/advisory.json"
    path = tmp_path / relative
    path.parent.mkdir(parents=True)
    path.write_bytes(b'{"ok":true}')
    executed = await _finalize_files(
        ExploreFinalizeHandler(),
        {"output_files": [relative]},
        tmp_path,
        [],
    )
    assert executed.status == "failed"
    assert executed.failure is not None
    assert executed.failure.kind == "invalid_input"
    assert executed.failure.retryable is False


@pytest.mark.asyncio
async def test_explore_finalize_rejects_path_escape(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (tmp_path / "secret.json").write_bytes(b'{"secret":true}')
    executed = await _finalize_files(
        ExploreFinalizeHandler(),
        {"output_files": ["../secret.json"]},
        workspace,
        ["qa/changes/CH-DEMO-001/explore/advisory.json"],
    )
    assert executed.status == "failed"
    assert executed.failure is not None
    assert executed.failure.kind == "invalid_output"
    assert executed.failure.retryable is True
    assert executed.output is None
